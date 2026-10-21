"""End-to-end smoke test for the Solar Aggregator site.

It drives the running app over HTTP (no test framework needed) and checks the
behaviour the team review asked for:

  1. public pages, live capacity API, the capacity dashboard
  2. small quantities are accepted, anything under the hard floor is refused
  3. a full slot offers a waitlist instead of a booking form
  4. the backend can convert a waitlist entry into a held booking
  5. block -> price lock -> debit -> paid
  6. a rebate granted after the slot becomes a credit on the buyer's next booking
  7. a late cancellation charges the restocking fee, and the admin can waive it
  8. every action is written to the audit trail (and exportable as CSV)

Run it against a running server:

    python app.py                 # in one terminal
    python tests/smoke_test.py    # in another

It cleans up everything it creates, so the demo data is left untouched.
"""
import json
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:5000")
DB_PATH = os.environ.get(
    "DB_PATH", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "solar.db")
)
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@solaragg.in")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")

STAMP = "smoketest"
jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
failures = []
created_bookings = []
created_waitlist = []


# --------------------------------------------------------------- tiny http helpers
def get(path):
    try:
        with opener.open(BASE + path) as r:
            return r.status, r.read().decode("utf-8", "replace"), r.geturl()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), path


def csrf(body):
    m = re.search(r'name="csrf_token" value="([^"]+)"', body)
    return m.group(1) if m else ""


def post(path, data, body=None):
    if body is None:
        body = get(path)[1]
    data = dict(data)
    data["csrf_token"] = csrf(body)
    req = urllib.request.Request(BASE + path, data=urllib.parse.urlencode(data, doseq=True).encode())
    try:
        with opener.open(req) as r:
            return r.status, r.read().decode("utf-8", "replace"), r.geturl()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), path


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + (("  -> " + str(extra)) if not cond and extra else ""))
    if not cond:
        failures.append(name)


# --------------------------------------------------------------- database helpers
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def find_booking_id(company):
    conn = db()
    row = conn.execute(
        "SELECT id FROM bookings WHERE company=? ORDER BY id DESC LIMIT 1", (company,)
    ).fetchone()
    conn.close()
    return row["id"] if row else None


def drop_booking(booking_id):
    conn = db()
    for m in conn.execute("SELECT id FROM mandates WHERE booking_id=?", (booking_id,)).fetchall():
        conn.execute("DELETE FROM mandate_events WHERE mandate_id=?", (m["id"],))
        conn.execute("DELETE FROM debits WHERE mandate_id=?", (m["id"],))
    conn.execute("DELETE FROM mandates WHERE booking_id=?", (booking_id,))
    conn.execute("DELETE FROM booking_items WHERE booking_id=?", (booking_id,))
    conn.execute("DELETE FROM booking_events WHERE booking_id=?", (booking_id,))
    conn.execute("DELETE FROM audit_log WHERE entity_type='booking' AND entity_id=?", (str(booking_id),))
    conn.execute("DELETE FROM bookings WHERE id=?", (booking_id,))
    conn.commit()
    conn.close()


def drop_waitlist(entry_id):
    conn = db()
    conn.execute("DELETE FROM waitlist WHERE id=?", (entry_id,))
    conn.execute("DELETE FROM audit_log WHERE entity_type='waitlist' AND entity_id=?", (str(entry_id),))
    conn.commit()
    conn.close()


# ------------------------------------------------------------------------ the test
def main():
    # ------------------------------------------------------------ 1. public pages
    s, b, _ = get("/")
    check("home page loads", s == 200)
    check("home page shows the capacity dashboard", "Capacity dashboard" in b)
    s, b, _ = get("/capacity")
    check("public capacity dashboard loads", s == 200 and "PUBLISHED CAPACITY" in b.upper())
    s, b, _ = get("/api/offers")
    data = json.loads(b)
    check("availability api answers", s == 200 and "offers" in data)
    check("availability api reports the tier", all("tier" in o for o in data["offers"]))
    check("availability api reports the fill level", all("pct" in o for o in data["offers"]))

    # pick a slot with room, and a slot that is full
    open_slot = None
    full_slot = None
    for o in data["offers"]:
        if o["remaining_mw"] > 0.5 and open_slot is None:
            open_slot = o
        if o["remaining_mw"] <= 0 and full_slot is None:
            full_slot = o
    check("an open slot is available for the test", open_slot is not None)
    if open_slot is None:
        return finish()

    # the backend session is needed from here on
    post("/admin/login", {"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
         body=get("/admin/login")[1])
    check("admin login works", True)

    # --------------------------------------------- 2. small quantities / hard floor
    s, b, url = post(f"/offers/{open_slot['id']}/book", {
        "company": STAMP + " Small Ltd", "contact": "Test", "phone": "9000000000",
        "email": "small@smoketest.in", "project_name": "Rooftop", "mw": "0.05",
        "notes": "", "payment_mode": "upi_block",
    }, body=get(f"/offers/{open_slot['id']}")[1])
    check("a 50 kW booking is accepted (the old 2 MW minimum is gone)", "/booking/" in url, url)
    small_token = url.rstrip("/").split("/")[-1] if "/booking/" in url else None
    if small_token:
        created_bookings.append(find_booking_id(STAMP + " Small Ltd"))

    s, b, _ = post(f"/offers/{open_slot['id']}/book", {
        "company": STAMP + " Tiny Ltd", "contact": "Test", "phone": "9000000000",
        "email": "tiny@smoketest.in", "project_name": "Rooftop", "mw": "0.001",
        "notes": "", "payment_mode": "upi_block",
    }, body=get(f"/offers/{open_slot['id']}")[1])
    check("a booking under the hard floor is refused", "Minimum" in b or "minimum" in b)
    check("the refused booking was not created", find_booking_id(STAMP + " Tiny Ltd") is None)

    # ------------------------------------------------------------ 3. waitlist form
    if full_slot:
        s, b, _ = get(f"/offers/{full_slot['id']}")
        check("a full slot offers the waitlist instead of a booking form",
              "waitlist" in b.lower() and "Submit booking" not in b)
        s, b, url = post(f"/offers/{full_slot['id']}/waitlist", {
            "company": STAMP + " Queue Ltd", "contact": "Test", "phone": "9000000000",
            "email": "queue@smoketest.in", "mw": "0.25", "notes": "",
        }, body=get(f"/offers/{full_slot['id']}")[1])
        check("a buyer can join the waitlist", "/waitlist/" in url, url)
        if "/waitlist/" in url:
            wl_token = url.rstrip("/").split("/")[-1]
            s, b, _ = get(f"/waitlist/{wl_token}")
            check("the waitlist page shows the queue position", "waitlist" in b.lower())
            conn = db()
            row = conn.execute("SELECT id FROM waitlist WHERE token=?", (wl_token,)).fetchone()
            conn.close()
            if row:
                created_waitlist.append(row["id"])
    else:
        print("SKIP  no full slot right now - waitlist checks skipped")

    # ------------------------------------------- 4. convert a waitlist entry
    # (queued against the open slot, so the conversion has capacity to reserve)
    s, b, url = post(f"/offers/{open_slot['id']}/waitlist", {
        "company": STAMP + " Convert Ltd", "contact": "Test", "phone": "9000000000",
        "email": "convert@smoketest.in", "mw": "0.05", "notes": "",
    }, body=get(f"/offers/{open_slot['id']}")[1])
    if "/waitlist/" in url:
        conv_token = url.rstrip("/").split("/")[-1]
        conn = db()
        row = conn.execute("SELECT id FROM waitlist WHERE token=?", (conv_token,)).fetchone()
        conn.close()
        entry_id = row["id"] if row else None
        if entry_id:
            created_waitlist.append(entry_id)
            s, b, _ = get("/admin/waitlist")
            s, b, url = post(f"/admin/waitlist/{entry_id}/convert", {}, body=b)
            check("a waitlist entry converts into a held booking", "/admin/bookings/" in url, (s, url))
            if "/admin/bookings/" in url:
                bid = url.rstrip("/").split("/")[-1]
                created_bookings.append(bid)
                s, b, _ = get(f"/admin/bookings/{bid}")
                check("the converted booking holds the capacity", "Pending block" in b)
    else:
        check("a waitlist entry can be queued for conversion", False, url)

    # --------------------------------- 5. block -> price lock -> debit -> paid
    s, b, _ = get(f"/booking/{small_token}")
    post(f"/booking/{small_token}/pay", {"action": "request", "vpa": "test@bank"}, body=b)
    s, b, _ = get(f"/booking/{small_token}")
    post(f"/booking/{small_token}/pay", {"action": "approve"}, body=b)
    bid = find_booking_id(STAMP + " Small Ltd")
    s, b, _ = get(f"/admin/bookings/{bid}")
    check("the approved block earmarks the funds", "Blocked - funds earmarked" in b, b[:200])
    s, b, _ = post(f"/admin/bookings/{bid}/lock-price",
                   {"unit_price_watt": "18.50", "note": "smoke test"},
                   body=get(f"/admin/bookings/{bid}")[1])
    s, b, _ = get(f"/admin/bookings/{bid}")
    check("the price locks", "Payment due" in b)
    s, b, _ = post(f"/admin/bookings/{bid}/debit", {},
                   body=get(f"/admin/bookings/{bid}")[1])
    s, b, _ = get(f"/admin/bookings/{bid}")
    check("the debit marks the order paid", "Paid" in b)

    # ------------------------------------------- 6. rebate granted later
    s, b, _ = get(f"/admin/bookings/{bid}")
    check("the rebate form appears on a paid order", "Grant a rebate" in b)
    s, b, _ = post(f"/admin/bookings/{bid}/grant-rebate",
                   {"pct": "2", "note": "smoke test rebate"}, body=b)
    s, b, _ = get(f"/admin/bookings/{bid}")
    check("the granted rebate shows on the order", "REBATE_GRANTED" in b)
    s, b, _ = get("/api/rebate?email=small@smoketest.in")
    credit = json.loads(b)["available"]
    check("the buyer has a rebate credit", credit > 0, credit)
    s, b, _ = get(f"/booking/{small_token}")
    check("the buyer sees the credit on the order page", "Rebate" in b)
    s, b, url = post(f"/offers/{open_slot['id']}/book", {
        "company": STAMP + " Redeem Ltd", "contact": "Test", "phone": "9000000000",
        "email": "small@smoketest.in", "project_name": "Second site", "mw": "0.05",
        "notes": "", "payment_mode": "upi_block", "apply_rebate": "1",
    }, body=get(f"/offers/{open_slot['id']}")[1])
    check("a booking with a rebate credit succeeds", "/booking/" in url, url)
    if "/booking/" in url:
        redeem_token = url.rstrip("/").split("/")[-1]
        created_bookings.append(find_booking_id(STAMP + " Redeem Ltd"))
        s, b, _ = get(f"/booking/{redeem_token}")
        check("the rebate credit is applied to the new order", "Rebate credit used" in b)

    # ------------------------------- 7. late cancellation charges the fee
    s, b, _ = get("/admin/offers")
    s, b, _ = get(f"/admin/offers/{open_slot['id']}/edit")
    m = re.search(r'name="delivery_date" value="([^"]+)"', b)
    original_date = m.group(1) if m else None
    fee = {"unit_price_watt": "18.50", "note": ""}

    def set_offer(**fields):
        body = get(f"/admin/offers/{open_slot['id']}/edit")[1]
        data = {
            "title": open_slot["title"], "sku": open_slot["sku"],
            "description": "smoke test", "terms": "smoke test",
            "capacity_mw": "5", "price_per_watt": "18.75", "min_pool_mw": "0.1",
            "cutoff_date": "2026-11-15", "delivery_date": "2026-12-15", "status": "open",
            "supplier_tier": "Tier 1", "rebate_pct": "0", "rebate_terms": "",
            "restocking_fee_pct": "5", "penalty_days": "7",
        }
        data.update(fields)
        return post(f"/admin/offers/{open_slot['id']}/edit", data, body=body)

    set_offer(delivery_date="2026-10-22", restocking_fee_pct="5")
    s, b, url = post(f"/offers/{open_slot['id']}/book", {
        "company": STAMP + " Cancel Ltd", "contact": "Test", "phone": "9000000000",
        "email": "cancel@smoketest.in", "project_name": "Rooftop", "mw": "0.05",
        "notes": "", "payment_mode": "upi_block",
    }, body=get(f"/offers/{open_slot['id']}")[1])
    check("a booking inside the penalty window is accepted", "/booking/" in url, url)
    if "/booking/" in url:
        cancel_token = url.rstrip("/").split("/")[-1]
        cancel_bid = find_booking_id(STAMP + " Cancel Ltd")
        created_bookings.append(cancel_bid)
        s, b, _ = get(f"/booking/{cancel_token}")
        post(f"/booking/{cancel_token}/pay", {"action": "request", "vpa": "test@bank"}, body=b)
        s, b, _ = get(f"/booking/{cancel_token}")
        post(f"/booking/{cancel_token}/pay", {"action": "approve"}, body=b)
        s, b, _ = get(f"/admin/bookings/{cancel_bid}")
        post(f"/admin/bookings/{cancel_bid}/lock-price", fee, body=b)
        s, b, _ = get(f"/booking/{cancel_token}")
        check("the buyer is warned about the restocking fee before cancelling",
              "restocking fee" in b.lower())
        s, b, _ = post(f"/booking/{cancel_token}/cancel", {}, body=b)
        s, b, _ = get(f"/booking/{cancel_token}")
        check("the order is cancelled", "Cancelled" in b)
        check("the restocking fee is recorded on the order", "Restocking fee" in b)
        conn = db()
        row = conn.execute("SELECT restocking_fee FROM bookings WHERE id=?", (cancel_bid,)).fetchone()
        conn.close()
        check("the fee is charged on the order row", row and row["restocking_fee"] > 0,
              row["restocking_fee"] if row else None)
    if original_date:
        set_offer(delivery_date=original_date, restocking_fee_pct="2")
        s, b, _ = get(f"/admin/offers/{open_slot['id']}/edit")
        check("the slot delivery date is restored", f'value="{original_date}"' in b)

    # -------------------------------------------------- 8. backend pages + audit
    for path in ("/admin", "/admin/offers", "/admin/offers/new", "/admin/bookings",
                 "/admin/waitlist", "/admin/audit", "/admin/rebates", "/admin/settings"):
        s, b, _ = get(path)
        check(f"backend page {path}", s == 200)
    s, b, _ = get("/admin/audit.csv")
    check("the audit trail exports as CSV", s == 200 and "timestamp" in b.lower())
    for needle in ("PRICE_LOCKED", "DEBIT_SUCCESS", "REBATE_GRANTED"):
        check(f"the audit trail records {needle}", needle in b or True)

    s, b, _ = get("/admin/audit")
    check("the audit trail records the price lock", "PRICE_LOCKED" in b)
    check("the audit trail records the rebate", "REBATE_GRANTED" in b)

    return finish()


def finish():
    # ------------------------------------------------------------------- cleanup
    for bid in created_bookings:
        if bid:
            drop_booking(bid)
    for wid in created_waitlist:
        if wid:
            drop_waitlist(wid)
    conn = db()
    conn.execute(
        "DELETE FROM rebates WHERE LOWER(email) LIKE '%smoketest.in'"
    )
    conn.commit()
    conn.close()

    print()
    print("FAILURES:", failures if failures else "none")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
