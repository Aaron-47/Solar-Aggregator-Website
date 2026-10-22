"""Solar Aggregator - demand-pooling marketplace.

An open (public) website where pooled solar-module slot offers are published
with a cut-off date. Buyers do not register: they fill a public booking form
(company details + required wattage, line by line) on the slot page. Every
booking gets a secret tracking link. A single admin account runs the full
backend: buyer names, price locking, payment blocks (UPI block-and-debit
sandbox + RTGS/NEFT/LC verification), and order tracking.

Run:  python app.py   ->  http://127.0.0.1:5000

Meeting changes implemented here (see docs/MEETING_ACTIONS.md):
  1. smaller quantities allowed - the 2 MW pool minimum is gone; the pool
     target is a soft goal and the hard floor is `min_booking_mw` (10 kW).
  2. dynamic inventory - /api/offers + /api/offers/<id>/availability feed the
     live "MW still open" counters on every page.
  3. capacity dashboard - /capacity, plus live KPIs in the backend.
  4. audit trail - every meaningful action lands in the audit_log table and is
     browsable at /admin/audit (CSV export included).
  5. waitlist - buyers queue for a full slot at /offers/<id>/waitlist, the
     backend converts a queue entry into a held booking.
  6. tier classification - suppliers are published as Tier 1 / Tier 2, never
     by brand name.
  7. dynamic pricing + later rebates - a volume ladder sets the live price and
     rebates are granted after the slot, never at booking time.
  8. cancellation penalty - restocking fee inside the window before delivery.
"""
import os
import sqlite3
import json
import math
import secrets
import itertools
import datetime as dt

from flask import (
    Flask, g, session, request, redirect, url_for, render_template, flash,
    abort, jsonify
)
from werkzeug.security import check_password_hash

import db as D
import psp

APP_DIR = os.path.dirname(os.path.abspath(__file__))
UPI_DEMO_LIMIT = 500_000  # Rs 5L - above this the PDF says use RTGS/NEFT/LC, not a UPI block


# ------------------------------------------------------------------ app
def _load_secret():
    path = os.path.join(APP_DIR, ".secret_key")
    if os.path.exists(path):
        with open(path, "rb") as fh:
            return fh.read()
    key = secrets.token_hex(32).encode()
    with open(path, "wb") as fh:
        fh.write(key)
    return key


app = Flask(__name__)
app.config["SECRET_KEY"] = _load_secret()
app.config["BLOCK_TTL_HOURS"] = 24  # buyer must approve the block within this window
D.init_app(app)


# ------------------------------------------------------------------ helpers
def now():
    return dt.datetime.now()


def today():
    return now().date()


def client_ip():
    return (request.headers.get("X-Forwarded-For", request.remote_addr) or "")[:64]


def audit(entity_type, entity_id, event, message="", actor="system"):
    """Append to the system wide audit trail."""
    D.audit(entity_type, entity_id, event, message, actor, client_ip())


def log_event(booking_id, event, message, actor, internal=0):
    D.get_db().execute(
        "INSERT INTO booking_events (booking_id, event, message, actor, internal, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (booking_id, event, message, actor, internal, D.now_iso()),
    )
    D.get_db().commit()


def touch(booking_id, status=None):
    conn = D.get_db()
    if status:
        conn.execute(
            "UPDATE bookings SET status=?, updated_at=? WHERE id=?",
            (status, D.now_iso(), booking_id),
        )
    else:
        conn.execute("UPDATE bookings SET updated_at=? WHERE id=?", (D.now_iso(), booking_id))
    conn.commit()


def offer_metrics(offer_id):
    booked = D.booked_mw(offer_id)
    row = D.get_db().execute("SELECT capacity_mw FROM offers WHERE id=?", (offer_id,)).fetchone()
    cap = float(row["capacity_mw"]) if row else 0.0
    pct = min(100.0, (booked / cap * 100.0)) if cap else 0.0
    return booked, cap, round(booked, 3), round(cap, 3), round(pct, 1)


def fmt_inr(value, decimals=2):
    if value is None:
        return "-"
    value = float(value)
    neg = value < 0
    intpart, _, frac = f"{abs(value):.{decimals}f}".partition(".")
    if len(intpart) > 3:
        tail = intpart[-3:]
        head = intpart[:-3]
        while len(head) > 2:
            tail = head[-2:] + "," + tail
            head = head[:-2]
        if head:
            tail = head + "," + tail
        intpart = tail
    out = "₹" + intpart + (("." + frac) if frac else "")
    return ("-" if neg else "") + out


def fmt_dt(value, with_time=True):
    if not value:
        return "-"
    try:
        d = dt.datetime.strptime(value[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            d = dt.datetime.strptime(value[:10], "%Y-%m-%d")
            return d.strftime("%d %b %Y")
        except ValueError:
            return value
    return d.strftime("%d %b %Y, %H:%M") if with_time else d.strftime("%d %b %Y")


def days_left(date_str):
    try:
        d = dt.datetime.strptime(date_str[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    return (d - today()).days


# ------------------------------------------------------------------ settings
def setting_float(key, default=0.0):
    try:
        return float(D.get_setting(key) or default)
    except (TypeError, ValueError):
        return default


def setting_int(key, default=0):
    try:
        return int(float(D.get_setting(key) or default))
    except (TypeError, ValueError):
        return default


def min_booking_mw():
    """Hard floor for a single booking (the old 2 MW minimum is gone)."""
    return max(0.001, setting_float("min_booking_mw", 0.01))


def pricing_ladder():
    """Volume ladder: [(min fill %, discount %)] sorted ascending."""
    raw = D.get_setting("pricing_ladder") or "[]"
    try:
        steps = json.loads(raw)
    except (TypeError, ValueError):
        steps = []
    out = []
    for step in steps or []:
        try:
            out.append((float(step["min_pct"]), float(step["discount_pct"])))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(out, key=lambda x: x[0])


def volume_discount(fill_pct):
    """Dynamic pricing: the better the pool is filled, the lower the price."""
    best = 0.0
    for min_pct, discount in pricing_ladder():
        if fill_pct >= min_pct:
            best = max(best, discount)
    return best


def effective_price(offer, booked=None):
    """(live price per watt, volume discount %, fill %) for an offer.

    The volume discount always rounds DOWN to a clean 2-decimal price, so the
    amount we block is never more than the amount the buyer is quoted.
    """
    if booked is None:
        booked = D.booked_mw(offer["id"])
    base = float(offer["price_per_watt"])
    cap = float(offer["capacity_mw"] or 0)
    fill = (booked / cap * 100.0) if cap else 0.0
    discount = volume_discount(fill)
    price = math.floor(base * (1.0 - discount / 100.0) * 100.0) / 100.0
    return round(price, 2), discount, round(fill, 1)


def offer_state(offer):
    """Everything a page needs to know about one slot (JSON friendly)."""
    booked, cap, booked_r, cap_r, pct = offer_metrics(offer["id"])
    price, discount, fill = effective_price(offer, booked)
    pool_target = float(offer["min_pool_mw"] or 0)
    return {
        "id": offer["id"],
        "title": offer["title"],
        "tier": offer["supplier_tier"],
        "sku": offer["sku"],
        "price": price,
        "base_price": float(offer["price_per_watt"]),
        "discount_pct": discount,
        "rebate_pct": float(offer["rebate_pct"] or 0),
        "capacity_mw": cap_r,
        "booked_mw": booked_r,
        "remaining_mw": round(max(0.0, cap_r - booked_r), 3),
        "pct": pct,
        "min_pool_mw": pool_target,
        "pool_met": booked_r >= pool_target,
        "waitlist": D.waitlist_count(offer["id"]),
        "cutoff_date": offer["cutoff_date"],
        "delivery_date": offer["delivery_date"],
        "cutoff_in_days": days_left(offer["cutoff_date"]),
        "full": booked_r >= cap_r,
        "status": offer["status"],
    }


# ------------------------------------------------------------------ policy
def cancellation_penalty(offer, b):
    """Restocking fee that applies when a buyer cancels close to delivery.

    Returns a dict so the buyer can see the fee BEFORE confirming a cancel.
    """
    fee_pct = float(offer["restocking_fee_pct"] or 0)
    window = int(offer["penalty_days"] or 0)
    delivery_in = days_left(offer["delivery_date"])
    base = float(b["amount"] or 0)
    info = {
        "fee": 0.0, "pct": fee_pct, "window": window, "days": delivery_in,
        "applies": False, "reason": "", "base": base,
    }
    if not b["price_locked"] or not base:
        info["reason"] = "no locked price yet - cancelling is free"
        return info
    if fee_pct <= 0:
        info["reason"] = "no restocking fee configured for this slot"
        return info
    if delivery_in is None:
        return info
    if delivery_in < 0:
        info["reason"] = "the slot date has passed - please contact the aggregator"
        return info
    if delivery_in <= window:
        info["fee"] = round(base * fee_pct / 100.0, 2)
        info["applies"] = True
        info["reason"] = f"delivery is in {delivery_in} day(s) - inside the {window} day window"
    else:
        info["reason"] = f"delivery is in {delivery_in} days - outside the {window} day window"
    return info


def rebate_credit(email, conn=None):
    return D.available_rebate(email, conn)


@app.context_processor
def inject_globals():
    def status_label(s):
        return D.STATUS_LABELS.get(s, s)

    def status_tone(s):
        return D.STATUS_TONE.get(s, "muted")

    def tier_label(t):
        return D.TIER_LABELS.get(t, t)

    return dict(
        fmt_inr=fmt_inr, fmt_dt=fmt_dt, days_left=days_left,
        status_label=status_label, status_tone=status_tone, tier_label=tier_label,
        UPI_DEMO_LIMIT=UPI_DEMO_LIMIT, psp=psp,
        current_user=g.get("user"),
        csrf_token=lambda: session.setdefault("csrf", secrets.token_hex(16)),
        min_booking_mw=min_booking_mw(),
        current_accounts_ok=setting_int("allow_current_account_blocks", 1) == 1,
        pricing_ladder=pricing_ladder(),
    )


@app.before_request
def csrf_protect():
    if request.method == "POST":
        token = request.form.get("csrf_token") or ""
        if not token or token != session.get("csrf"):
            abort(400, description="Invalid or missing CSRF token.")


@app.before_request
def load_user():
    g.user = None
    uid = session.get("user_id")
    if uid:
        g.user = D.get_db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if g.user is None:
            session.clear()


def admin_required(view):
    def wrapped(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("admin_login"))
        if g.user["role"] != "admin":
            abort(403)
        return view(*args, **kwargs)
    wrapped.__name__ = view.__name__
    return wrapped


def booking_by_token_or_404(token):
    row = D.get_db().execute("SELECT * FROM bookings WHERE token=?", (token,)).fetchone()
    if row is None:
        abort(404)  # the secret token is the only way to see a booking
    return row


# ------------------------------------------------------------------ scheduler sweep
def sweep():
    """Idempotent maintenance pass (PDF section 6: scheduler / job queue).
    Wire it to cron or a real scheduler in production; the admin dashboard
    also runs it on load."""
    conn = D.get_db()
    done = []

    # 1. Blocks never approved inside the TTL -> expire, release capacity.
    ttl = (now() - dt.timedelta(hours=app.config["BLOCK_TTL_HOURS"])).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        "SELECT m.* FROM mandates m JOIN bookings b ON b.id=m.booking_id"
        " WHERE m.status='PENDING_APPROVAL' AND m.created_at < ?"
        f" AND b.status IN ({','.join('?' * 2)})",
        (ttl, D.PENDING_BLOCK, D.BLOCK_FAILED),
    ).fetchall()
    for m in rows:
        conn.execute("UPDATE mandates SET status='EXPIRED' WHERE id=?", (m["id"],))
        _mandate_event(m["id"], "BLOCK_EXPIRED", "Block request expired unapproved.")
        conn.execute("UPDATE bookings SET status=?, updated_at=? WHERE id=?",
                     (D.BLOCK_EXPIRED, D.now_iso(), m["booking_id"]))
        log_event(m["booking_id"], "BLOCK_EXPIRED",
                  "Payment block was not approved in time - capacity released. "
                  "Re-book or join the waitlist for this slot.", "system")
        audit("booking", m["booking_id"], "BLOCK_EXPIRED",
              "Unapproved block expired - capacity released.", "system")
    if rows:
        done.append(f"{len(rows)} unapproved block(s) expired")

    # 2. Active blocks past validity -> expired (debit window closed).
    now_s = D.now_iso()
    rows = conn.execute(
        "SELECT m.* FROM mandates m JOIN bookings b ON b.id=m.booking_id"
        " WHERE m.status='ACTIVE' AND m.valid_to < ?"
        f" AND b.status IN ({','.join('?' * 3)})",
        (now_s, D.CONFIRMED_BLOCKED, D.PAYMENT_DUE, D.CANCELLED),
    ).fetchall()
    for m in rows:
        conn.execute("UPDATE mandates SET status='EXPIRED' WHERE id=?", (m["id"],))
        _mandate_event(m["id"], "BLOCK_EXPIRED", "Block validity ended before debit.")
        conn.execute("UPDATE bookings SET status=?, updated_at=? WHERE id=?",
                     (D.BLOCK_EXPIRED, D.now_iso(), m["booking_id"]))
        log_event(m["booking_id"], "BLOCK_EXPIRED",
                  "Block validity ended - please contact us to re-confirm this order.", "system")
        audit("booking", m["booking_id"], "BLOCK_EXPIRED", "Block validity ended.", "system")
    if rows:
        done.append(f"{len(rows)} block(s) ran past validity")

    # 3. Debits due on/after the slot date -> execute (PDF step 7).
    rows = conn.execute(
        "SELECT b.id FROM bookings b"
        " JOIN mandates m ON m.booking_id=b.id"
        " JOIN offers o ON o.id=b.offer_id"
        " WHERE b.status=? AND m.status='ACTIVE' AND b.payment_mode='upi_block'"
        " AND o.delivery_date <= ?",
        (D.PAYMENT_DUE, today().isoformat()),
    ).fetchall()
    for b in rows:
        _run_debit(b["id"])
    if rows:
        done.append(f"{len(rows)} due debit(s) executed")

    return done


def _mandate_event(mandate_id, event_type, payload=""):
    D.get_db().execute(
        "INSERT INTO mandate_events (mandate_id, event_type, payload, received_at) VALUES (?,?,?,?)",
        (mandate_id, event_type, payload, D.now_iso()),
    )
    D.get_db().commit()


def _run_debit(booking_id):
    """Execute the debit against an active block (PDF step 7)."""
    conn = D.get_db()
    b = conn.execute(
        "SELECT b.*, m.id AS mandate_id, m.blocked_amount, m.psp_ref FROM bookings b"
        " JOIN mandates m ON m.booking_id=b.id WHERE b.id=?",
        (booking_id,),
    ).fetchone()
    if b is None or b["status"] != D.PAYMENT_DUE or not b["amount"]:
        return "Booking is not due for debit."
    status, event, msg = psp.execute_debit(b["blocked_amount"], b["amount"])
    txn = psp.new_ref("SBXTXN")
    conn.execute(
        "INSERT INTO debits (mandate_id, amount, psp_txn_ref, status, attempted_at) VALUES (?,?,?,?,?)",
        (b["mandate_id"], b["amount"], txn, status, D.now_iso()),
    )
    _mandate_event(b["mandate_id"], event, msg)
    if status == "SUCCESS":
        conn.execute("UPDATE mandates SET status='EXECUTED' WHERE id=?", (b["mandate_id"],))
        conn.execute("UPDATE bookings SET status=?, updated_at=? WHERE id=?",
                     (D.PAID, D.now_iso(), booking_id))
        conn.commit()
        log_event(booking_id, "DEBIT_SUCCESS",
                  f"Payment of {fmt_inr(b['amount'])} debited against the block (ref {txn}).", "system")
        audit("booking", booking_id, "DEBIT_SUCCESS",
              f"Debited {fmt_inr(b['amount'])} (ref {txn}).", "system")
        return f"Debited {fmt_inr(b['amount'])}."
    conn.commit()
    log_event(booking_id, "PAYMENT_FAILED", msg, "system")
    audit("booking", booking_id, "PAYMENT_FAILED", msg, "system")
    return msg


def _charge_restocking_fee(booking_id, fee):
    """Take the late-cancellation restocking fee out of the buyer's block.

    The fee can never exceed what is actually blocked; the remainder of the
    block is released. For bank transfers the fee is simply recorded - it is
    netted off the refund by the aggregator.
    """
    conn = D.get_db()
    b = conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
    if b is None or fee <= 0:
        return 0.0
    mandate = conn.execute(
        "SELECT * FROM mandates WHERE booking_id=? AND status='ACTIVE' ORDER BY id DESC LIMIT 1",
        (booking_id,),
    ).fetchone()
    charged = fee
    if mandate:
        charged = min(fee, float(mandate["blocked_amount"]))
        txn = psp.new_ref("SBXFEE")
        conn.execute(
            "INSERT INTO debits (mandate_id, amount, psp_txn_ref, status, attempted_at)"
            " VALUES (?,?,?,?,?)",
            (mandate["id"], charged, txn, "SUCCESS", D.now_iso()),
        )
        _mandate_event(mandate["id"], "RESTOCKING_FEE",
                       f"Restocking fee {fmt_inr(charged)} charged on late cancellation (ref {txn}).")
        status, event, msg = psp.release_block()
        conn.execute("UPDATE mandates SET status=? WHERE id=?", (status, mandate["id"]))
        _mandate_event(mandate["id"], event, msg)
    conn.execute("UPDATE bookings SET restocking_fee=?, updated_at=? WHERE id=?",
                 (charged, D.now_iso(), booking_id))
    conn.commit()
    audit("booking", booking_id, "RESTOCKING_FEE",
          f"Restocking fee {fmt_inr(charged)} charged on late cancellation.", "system")
    return charged


def _return_rebate_credit(booking_id):
    """Give an unused rebate credit back when the order is cancelled."""
    conn = D.get_db()
    b = conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
    if b is None or not b["rebate_source_id"]:
        return
    conn.execute(
        "UPDATE rebates SET status=?, applied_booking_id=NULL WHERE id=? AND status=?",
        (D.REBATE_AVAILABLE, b["rebate_source_id"], D.REBATE_APPLIED),
    )
    conn.execute("UPDATE bookings SET rebate_applied=0, rebate_source_id=NULL WHERE id=?",
                 (booking_id,))
    conn.commit()


# ------------------------------------------------------------------ public site
@app.route("/")
def index():
    conn = D.get_db()
    offers = conn.execute(
        "SELECT * FROM offers WHERE status='open' ORDER BY cutoff_date ASC"
    ).fetchall()
    past = conn.execute(
        "SELECT * FROM offers WHERE status IN ('closed','fulfilled','cancelled')"
        " ORDER BY delivery_date DESC LIMIT 6"
    ).fetchall()
    states = {o["id"]: offer_state(o) for o in offers}
    capacity = _capacity_summary(offers)
    return render_template("index.html", offers=offers, past=past,
                           metrics={o["id"]: offer_metrics(o["id"]) for o in offers},
                           states=states, capacity=capacity)


def _capacity_summary(offers):
    """Headline capacity numbers for the dashboard."""
    cap = booked = 0.0
    waiting = 0
    for o in offers:
        b, c, _, cap_r, _ = offer_metrics(o["id"])
        cap += cap_r
        booked += b
        waiting += D.waitlist_count(o["id"])
    return {
        "slots": len(offers),
        "capacity_mw": round(cap, 3),
        "booked_mw": round(booked, 3),
        "remaining_mw": round(max(0.0, cap - booked), 3),
        "pct": round((booked / cap * 100.0) if cap else 0.0, 1),
        "waitlist": waiting,
    }


@app.route("/capacity")
def capacity_dashboard():
    """Public capacity dashboard: open slots, published MW, live remaining."""
    conn = D.get_db()
    offers = conn.execute(
        "SELECT * FROM offers WHERE status='open' ORDER BY cutoff_date ASC"
    ).fetchall()
    states = {o["id"]: offer_state(o) for o in offers}
    capacity = _capacity_summary(offers)
    history = conn.execute(
        "SELECT b.created_at, b.total_mw, b.company, o.title FROM bookings b"
        " JOIN offers o ON o.id=b.offer_id"
        " WHERE b.status IN ('PAID','FULFILLED') ORDER BY b.created_at DESC LIMIT 8"
    ).fetchall()
    return render_template("capacity.html", offers=offers, states=states,
                           capacity=capacity, history=history)


@app.route("/offers/<int:offer_id>")
def offer_detail(offer_id):
    conn = D.get_db()
    offer = conn.execute("SELECT * FROM offers WHERE id=?", (offer_id,)).fetchone()
    if offer is None:
        abort(404)
    state = offer_state(offer)
    dl = days_left(offer["cutoff_date"])
    open_now = offer["status"] == "open" and dl is not None and dl >= 0
    can_book = open_now and not state["full"]
    return render_template("offer_detail.html", offer=offer, state=state,
                           booked=state["booked_mw"], cap=state["capacity_mw"],
                           pct=state["pct"], can_book=can_book, dl=dl,
                           open_now=open_now)


# ------------------------------------------------------------------ live inventory API
@app.route("/api/offers")
def api_offers():
    """Live slot inventory - polled by the public pages."""
    conn = D.get_db()
    offers = conn.execute(
        "SELECT * FROM offers WHERE status='open' ORDER BY cutoff_date ASC"
    ).fetchall()
    return jsonify({
        "capacity": _capacity_summary(offers),
        "offers": [offer_state(o) for o in offers],
        "generated_at": D.now_iso(),
    })


@app.route("/api/offers/<int:offer_id>/availability")
def api_offer_availability(offer_id):
    offer = D.get_db().execute("SELECT * FROM offers WHERE id=?", (offer_id,)).fetchone()
    if offer is None:
        abort(404)
    return jsonify(offer_state(offer))


@app.route("/api/rebate")
def api_rebate():
    """Rebate credit available to a buyer (matched on email)."""
    email = (request.args.get("email") or "").strip()
    return jsonify({"email": email, "available": round(rebate_credit(email), 2)})


# ------------------------------------------------------------------ track (no login)
@app.route("/track", methods=["GET", "POST"])
def track():
    """Buyers have no accounts - they keep their secret tracking link.
    This page lets them re-open it by pasting the code."""
    if request.method == "POST":
        code = (request.form.get("code") or "").strip()
        conn = D.get_db()
        row = conn.execute("SELECT id FROM bookings WHERE token=?", (code,)).fetchone()
        if row is not None:
            return redirect(url_for("booking_detail", token=code))
        wl = conn.execute("SELECT id FROM waitlist WHERE token=?", (code,)).fetchone()
        if wl is not None:
            return redirect(url_for("waitlist_status", token=code))
        flash("No order found for that code - check the link/email you were sent.", "bad")
    return render_template("track.html")


# ------------------------------------------------------------------ waitlist
@app.route("/offers/<int:offer_id>/waitlist", methods=["POST"])
def join_waitlist(offer_id):
    """Queue for a slot that is (or is about to be) full."""
    conn = D.get_db()
    offer = conn.execute("SELECT * FROM offers WHERE id=?", (offer_id,)).fetchone()
    if offer is None:
        abort(404)
    company = (request.form.get("company") or "").strip()
    contact = (request.form.get("contact") or "").strip()
    phone = (request.form.get("phone") or "").strip()
    email = (request.form.get("email") or "").strip()
    try:
        mw = float(request.form.get("mw") or 0)
    except (TypeError, ValueError):
        mw = 0.0
    if not company or not contact:
        flash("Company name and contact person are required.", "bad")
        return redirect(url_for("offer_detail", offer_id=offer_id))
    if not email or "@" not in email:
        flash("Enter a valid email - we notify you when capacity frees up.", "bad")
        return redirect(url_for("offer_detail", offer_id=offer_id))
    if mw < min_booking_mw():
        flash(f"Enter the quantity you need (minimum {min_booking_mw():g} MW).", "bad")
        return redirect(url_for("offer_detail", offer_id=offer_id))

    existing = conn.execute(
        "SELECT id FROM waitlist WHERE offer_id=? AND LOWER(email)=? AND status IN (?,?)",
        (offer_id, email.lower(), D.WL_WAITING, D.WL_OFFERED),
    ).fetchone()
    if existing:
        flash("You are already on the waitlist for this slot - we will contact you.", "info")
        return redirect(url_for("waitlist_status", token=conn.execute(
            "SELECT token FROM waitlist WHERE id=?", (existing["id"],)).fetchone()["token"]))

    ts = D.now_iso()
    token = secrets.token_urlsafe(16)
    cur = conn.execute(
        "INSERT INTO waitlist (offer_id, company, contact, phone, email, mw, notes, token,"
        " status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (offer_id, company, contact, phone, email, mw,
         (request.form.get("notes") or "").strip(), token, D.WL_WAITING, ts, ts),
    )
    conn.commit()
    audit("waitlist", cur.lastrowid, "WAITLIST_JOINED",
          f"{company} joined the waitlist for {offer['title']} ({mw:g} MW).", "buyer")
    flash("You are on the waitlist - we will contact you the moment capacity frees up.", "ok")
    return redirect(url_for("waitlist_status", token=token))


@app.route("/waitlist/<token>")
def waitlist_status(token):
    conn = D.get_db()
    row = conn.execute("SELECT * FROM waitlist WHERE token=?", (token,)).fetchone()
    if row is None:
        abort(404)
    offer = conn.execute("SELECT * FROM offers WHERE id=?", (row["offer_id"],)).fetchone()
    state = offer_state(offer)
    ahead = conn.execute(
        "SELECT COUNT(*) c FROM waitlist WHERE offer_id=? AND status IN (?,?)"
        " AND created_at <= ?",
        (row["offer_id"], D.WL_WAITING, D.WL_OFFERED, row["created_at"]),
    ).fetchone()["c"]
    return render_template("waitlist.html", entry=row, offer=offer, state=state,
                           position=ahead)


# ------------------------------------------------------------------ public booking form + tracking
@app.route("/offers/<int:offer_id>/book", methods=["POST"])
def create_booking(offer_id):
    conn = D.get_db()
    offer = conn.execute("SELECT * FROM offers WHERE id=?", (offer_id,)).fetchone()
    if offer is None:
        abort(404)
    if offer["status"] != "open" or (days_left(offer["cutoff_date"]) or -1) < 0:
        flash("This slot is not accepting orders.", "bad")
        return redirect(url_for("offer_detail", offer_id=offer_id))

    company = (request.form.get("company") or "").strip()
    contact = (request.form.get("contact") or "").strip()
    phone = (request.form.get("phone") or "").strip()
    email = (request.form.get("email") or "").strip()
    if not company or not contact:
        flash("Company name and contact person are required.", "bad")
        return redirect(url_for("offer_detail", offer_id=offer_id))
    if not email or "@" not in email:
        flash("Enter a valid email - your tracking link is shown on the next page.", "bad")
        return redirect(url_for("offer_detail", offer_id=offer_id))

    names = request.form.getlist("project_name")
    mws = request.form.getlist("mw")
    notes = request.form.getlist("notes")
    payment_mode = request.form.get("payment_mode") or "upi_block"
    floor = min_booking_mw()
    items = []
    total = 0.0
    # zip_longest: the optional notes column may be missing entirely.
    for name, mw_s, note in itertools.zip_longest(names, mws, notes, fillvalue=""):
        name = (name or "").strip()
        if not name:
            continue
        try:
            mw = float(mw_s)
        except (TypeError, ValueError):
            flash(f"Quantity for \"{name}\" is not a number.", "bad")
            return redirect(url_for("offer_detail", offer_id=offer_id))
        if mw < floor - 1e-9:
            flash(f"Quantity for \"{name}\" must be at least {floor:g} MW.", "bad")
            return redirect(url_for("offer_detail", offer_id=offer_id))
        items.append((name, mw, (note or "").strip()))
        total += mw
    if not items:
        flash("Add at least one requirement line (e.g. \"0.5 MW for Company-A\").", "bad")
        return redirect(url_for("offer_detail", offer_id=offer_id))
    booked = D.booked_mw(offer_id, conn)
    if total > offer["capacity_mw"] - booked + 1e-9:
        flash(f"Only {offer['capacity_mw'] - booked:g} MW is still available in this slot - "
              "join the waitlist and we will call you when capacity frees up.", "bad")
        return redirect(url_for("offer_detail", offer_id=offer_id))

    # Live (volume) price at the moment of booking.
    price, discount, fill = effective_price(offer, booked)
    estimate = total * 1_000_000 * price

    # Rebate credit earned earlier (granted after a previous slot) is used here.
    credit = 0.0
    rebate_id = None
    if request.form.get("apply_rebate") == "1":
        available = rebate_credit(email, conn)
        if available > 0:
            credit = min(available, estimate)
            row = conn.execute(
                "SELECT id FROM rebates WHERE status=? AND LOWER(email)=?"
                " ORDER BY created_at ASC LIMIT 1",
                (D.REBATE_AVAILABLE, email.lower()),
            ).fetchone()
            rebate_id = row["id"] if row else None
    estimate = round(max(0.0, estimate - credit), 2)

    ts = D.now_iso()
    token = secrets.token_urlsafe(16)
    try:
        cur = conn.execute(
            "INSERT INTO bookings (offer_id, company, contact, phone, email, token,"
            " status, total_mw, payment_mode, rebate_applied, rebate_source_id,"
            " created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (offer_id, company, contact, phone, email, token,
             D.PENDING_BLOCK, round(total, 3), payment_mode, round(credit, 2), rebate_id,
             ts, ts),
        )
    except sqlite3.OperationalError as exc:
        flash(f"Could not save the booking: {exc}", "bad")
        return redirect(url_for("offer_detail", offer_id=offer_id))
    booking_id = cur.lastrowid
    for name, mw, note in items:
        conn.execute(
            "INSERT INTO booking_items (booking_id, project_name, mw, notes) VALUES (?,?,?,?)",
            (booking_id, name, mw, note),
        )
    if rebate_id:
        conn.execute(
            "UPDATE rebates SET status=?, applied_booking_id=? WHERE id=?",
            (D.REBATE_APPLIED, booking_id, rebate_id),
        )
    conn.commit()

    pool_target = float(offer["min_pool_mw"] or 0)
    pool_note = ""
    if pool_target and (booked + total) < pool_target:
        pool_note = (f" The {pool_target:g} MW pool target for this slot is not met yet - "
                     "your quantity is pooled with the others.")
    log_event(booking_id, "BOOKING_CREATED",
              f"{company} requested {total:g} MW - capacity on hold pending payment block "
              f"(estimate {fmt_inr(estimate)} at {price:g} ₹/W).{pool_note}", "buyer")
    audit("booking", booking_id, "BOOKING_CREATED",
          f"{company} booked {total:g} MW of {offer['title']} (estimate {fmt_inr(estimate)}).",
          "buyer")

    if payment_mode == "upi_block":
        blk = psp.create_block_request(
            estimate, valid_to=offer["delivery_date"] + " 23:59:59")
        conn.execute(
            "INSERT INTO mandates (booking_id, psp_ref, blocked_amount, status, valid_from, valid_to, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (booking_id, blk["psp_ref"], blk["blocked_amount"], blk["status"],
             ts, blk["valid_to"], ts),
        )
        conn.commit()
        mid = conn.execute("SELECT id FROM mandates WHERE booking_id=?", (booking_id,)).fetchone()["id"]
        _mandate_event(mid, "BLOCK_REQUEST_CREATED",
                       f"Sandbox block request {blk['psp_ref']} for {fmt_inr(blk['blocked_amount'])}.")
        audit("booking", booking_id, "BLOCK_REQUEST_CREATED",
              f"Block request {blk['psp_ref']} for {fmt_inr(blk['blocked_amount'])}.", "buyer")
    flash(f"Booking #{booking_id} created - {total:g} MW held for {company}. "
          "Complete the payment step to confirm.", "ok")
    return redirect(url_for("booking_detail", token=token))


@app.route("/booking/<token>")
def booking_detail(token):
    b = booking_by_token_or_404(token)
    conn = D.get_db()
    offer = conn.execute("SELECT * FROM offers WHERE id=?", (b["offer_id"],)).fetchone()
    items = conn.execute(
        "SELECT * FROM booking_items WHERE booking_id=? ORDER BY id", (b["id"],)).fetchall()
    events = conn.execute(
        "SELECT * FROM booking_events WHERE booking_id=? AND internal=0 ORDER BY created_at DESC, id DESC",
        (b["id"],),
    ).fetchall()
    mandate = conn.execute(
        "SELECT * FROM mandates WHERE booking_id=? ORDER BY id DESC LIMIT 1", (b["id"],)
    ).fetchone()
    debits = []
    if mandate:
        debits = conn.execute(
            "SELECT * FROM debits WHERE mandate_id=? ORDER BY attempted_at DESC", (mandate["id"],)
        ).fetchall()
    # The volume discount follows the SLOT fill level, not the size of this
    # single order.
    price, discount, _ = effective_price(offer)
    estimate = b["total_mw"] * 1_000_000 * price
    if b["price_locked"] and b["amount"]:
        estimate = b["amount"]
    estimate = max(0.0, estimate - float(b["rebate_applied"] or 0))

    penalty = cancellation_penalty(offer, b)
    can_cancel = (
        b["status"] in (D.PENDING_BLOCK, D.CONFIRMED_BLOCKED, D.AWAITING_BANK,
                        D.BLOCK_FAILED, D.PAYMENT_DUE)
        and (days_left(offer["delivery_date"]) is None or days_left(offer["delivery_date"]) >= 0)
    )
    my_rebates = conn.execute(
        "SELECT * FROM rebates WHERE status=? AND LOWER(email)=? ORDER BY created_at DESC",
        (D.REBATE_AVAILABLE, (b["email"] or "").lower()),
    ).fetchall()
    return render_template("booking_detail.html", b=b, offer=offer, items=items,
                           events=events, mandate=mandate, debits=debits,
                           can_cancel=can_cancel, estimate=estimate, penalty=penalty,
                           price=price, discount=discount, my_rebates=my_rebates)


@app.route("/booking/<token>/pay", methods=["POST"])
def pay_booking(token):
    b = booking_by_token_or_404(token)
    conn = D.get_db()
    offer = conn.execute("SELECT * FROM offers WHERE id=?", (b["offer_id"],)).fetchone()
    mandate = conn.execute(
        "SELECT * FROM mandates WHERE booking_id=? ORDER BY id DESC LIMIT 1", (b["id"],)
    ).fetchone()

    if b["status"] in (D.CANCELLED, D.BLOCK_EXPIRED, D.PAID, D.FULFILLED):
        return redirect(url_for("booking_detail", token=token))

    action = request.form.get("action")
    if b["payment_mode"] == "upi_block":
        if action == "request":
            if mandate is None:  # recreate a block request (retry path)
                price, _, _ = effective_price(offer)
                estimate = max(0.0, b["total_mw"] * 1_000_000 * price
                               - float(b["rebate_applied"] or 0))
                blk = psp.create_block_request(estimate, offer["delivery_date"] + " 23:59:59")
                ts = D.now_iso()
                conn.execute(
                    "INSERT INTO mandates (booking_id, psp_ref, blocked_amount, status, valid_from, valid_to, created_at)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (b["id"], blk["psp_ref"], blk["blocked_amount"], blk["status"], ts, blk["valid_to"], ts),
                )
                conn.commit()
                mandate = conn.execute(
                    "SELECT * FROM mandates WHERE booking_id=? ORDER BY id DESC LIMIT 1",
                    (b["id"],)).fetchone()
            conn.execute("UPDATE mandates SET customer_vpa=? WHERE id=?",
                         ((request.form.get("vpa") or "").strip(), mandate["id"]))
            conn.commit()
            log_event(b["id"], "BLOCK_REQUESTED",
                      f"Block request {mandate['psp_ref']} sent to {(request.form.get('vpa') or '').strip()} "
                      "- approve it in the UPI app.", "buyer")
            audit("booking", b["id"], "BLOCK_REQUESTED",
                  f"Block request {mandate['psp_ref']}.", "buyer")
            flash("Block request sent. Approve it below (sandbox UPI PIN).", "ok")
            return redirect(url_for("booking_detail", token=token))
        elif action == "approve":
            fail = request.form.get("simulate_fail") == "1"
            status, event, msg = psp.approve_mandate(simulate_failure=fail)
            conn.execute("UPDATE mandates SET status=? WHERE id=?", (status, mandate["id"]))
            _mandate_event(mandate["id"], event, msg)
            if status == "ACTIVE":
                conn.execute("UPDATE bookings SET status=?, updated_at=? WHERE id=?",
                             (D.CONFIRMED_BLOCKED, D.now_iso(), b["id"]))
                conn.commit()
                log_event(b["id"], "BLOCK_CONFIRMED",
                          f"Funds earmarked ({fmt_inr(mandate['blocked_amount'])}). "
                          "The aggregator will now lock your price - no money has left your account yet.", "system")
                audit("booking", b["id"], "BLOCK_CONFIRMED",
                      f"Funds earmarked ({fmt_inr(mandate['blocked_amount'])}).", "buyer")
                flash("Block approved - funds earmarked. Await price lock from the aggregator.", "ok")
            else:
                conn.execute("UPDATE bookings SET status=?, updated_at=? WHERE id=?",
                             (D.BLOCK_FAILED, D.now_iso(), b["id"]))
                conn.commit()
                log_event(b["id"], "BLOCK_FAILED", msg, "system")
                audit("booking", b["id"], "BLOCK_FAILED", msg, "buyer")
                flash(msg, "bad")
            return redirect(url_for("booking_detail", token=token))
    else:  # RTGS / NEFT / LC path
        ref = (request.form.get("payment_ref") or "").strip()
        if action == "submit_ref" and ref:
            conn.execute("UPDATE bookings SET payment_ref=?, status=?, updated_at=? WHERE id=?",
                         (ref, D.AWAITING_BANK, D.now_iso(), b["id"]))
            conn.commit()
            log_event(b["id"], "BANK_REF_SUBMITTED",
                      f"Bank reference submitted ({ref}) - awaiting verification by the aggregator.", "buyer")
            audit("booking", b["id"], "BANK_REF_SUBMITTED",
                  f"Bank reference {ref} submitted.", "buyer")
            flash("Reference submitted - we will verify and confirm.", "ok")
    return redirect(url_for("booking_detail", token=token))


@app.route("/booking/<token>/cancel", methods=["POST"])
def cancel_booking(token):
    b = booking_by_token_or_404(token)
    offer = D.get_db().execute("SELECT * FROM offers WHERE id=?", (b["offer_id"],)).fetchone()
    allowed = (
        b["status"] in (D.PENDING_BLOCK, D.CONFIRMED_BLOCKED, D.AWAITING_BANK,
                        D.BLOCK_FAILED, D.PAYMENT_DUE)
        and (days_left(offer["delivery_date"]) is None
             or days_left(offer["delivery_date"]) >= 0)
    )
    if not allowed:
        flash("This order can no longer be cancelled - contact the aggregator.", "bad")
        return redirect(url_for("booking_detail", token=token))

    conn = D.get_db()
    penalty = cancellation_penalty(offer, b)
    waive = request.form.get("waive_fee") == "1"
    fee = 0.0 if waive else penalty["fee"]

    mandate = conn.execute(
        "SELECT * FROM mandates WHERE booking_id=? AND status IN ('PENDING_APPROVAL','ACTIVE')"
        " ORDER BY id DESC LIMIT 1", (b["id"],)).fetchone()
    if mandate and not fee:
        status, event, msg = psp.release_block()
        conn.execute("UPDATE mandates SET status=? WHERE id=?", (status, mandate["id"]))
        _mandate_event(mandate["id"], event, msg)
    charged = _charge_restocking_fee(b["id"], fee) if fee else 0.0
    _return_rebate_credit(b["id"])
    conn.execute("UPDATE bookings SET status=?, updated_at=? WHERE id=?",
                 (D.CANCELLED, D.now_iso(), b["id"]))
    conn.commit()
    if charged:
        log_event(b["id"], "CANCELLED",
                  f"Order cancelled inside the {penalty['window']} day window before delivery - "
                  f"a restocking fee of {fmt_inr(charged)} was charged and the rest of the block released.",
                  "buyer")
    else:
        log_event(b["id"], "CANCELLED", "Order cancelled - any block released.", "buyer")
    audit("booking", b["id"], "CANCELLED",
          f"Order cancelled by the buyer (restocking fee {fmt_inr(charged)}).", "buyer")
    flash("Booking cancelled" + (f" and a restocking fee of {fmt_inr(charged)} charged."
                                  if charged else " and any block released."), "ok")
    return redirect(url_for("booking_detail", token=token))


# ------------------------------------------------------------------ admin auth
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        user = D.get_db().execute(
            "SELECT * FROM users WHERE email=? AND role='admin'", (email,)).fetchone()
        if user is None or not check_password_hash(user["password_hash"], password):
            flash("Wrong email or password.", "bad")
            audit("admin", email or "?", "LOGIN_FAILED", "Wrong email or password.", "anonymous")
        else:
            session["user_id"] = user["id"]
            flash("Signed in to the backend.", "ok")
            audit("admin", user["email"], "LOGIN", "Signed in to the backend.", "admin")
            return redirect(url_for("admin_dashboard"))
    return render_template("admin/login.html")


@app.route("/logout")
def logout():
    if g.get("user"):
        audit("admin", g.user["email"], "LOGOUT", "Signed out.", "admin")
    session.clear()
    flash("Logged out.", "ok")
    return redirect(url_for("index"))


# ------------------------------------------------------------------ admin backend
@app.route("/admin")
@admin_required
def admin_dashboard():
    sweep()
    conn = D.get_db()
    stats = {
        "open_offers": conn.execute("SELECT COUNT(*) c FROM offers WHERE status='open'").fetchone()["c"],
        "total_capacity": conn.execute(
            "SELECT COALESCE(SUM(capacity_mw),0) c FROM offers WHERE status='open'").fetchone()["c"],
        "booked_mw": conn.execute(
            f"SELECT COALESCE(SUM(total_mw),0) c FROM bookings WHERE status IN ({','.join('?' * len(D.HOLD_STATUSES))})",
            D.HOLD_STATUSES).fetchone()["c"],
        "paid": conn.execute(
            "SELECT COALESCE(SUM(amount),0) c FROM bookings WHERE status IN ('PAID','FULFILLED')").fetchone()["c"],
        "buyers": conn.execute("SELECT COUNT(DISTINCT company) c FROM bookings").fetchone()["c"],
        "bookings": conn.execute("SELECT COUNT(*) c FROM bookings").fetchone()["c"],
        "waitlist": conn.execute(
            "SELECT COUNT(*) c FROM waitlist WHERE status IN (?,?)",
            (D.WL_WAITING, D.WL_OFFERED)).fetchone()["c"],
        "rebates": conn.execute(
            "SELECT COALESCE(SUM(amount),0) c FROM rebates WHERE status=?",
            (D.REBATE_AVAILABLE,)).fetchone()["c"],
    }
    stats["remaining_mw"] = round(max(0.0, stats["total_capacity"] - stats["booked_mw"]), 3)
    to_lock = conn.execute(
        "SELECT b.id, o.title, b.company, b.total_mw FROM bookings b"
        " JOIN offers o ON o.id=b.offer_id"
        " WHERE b.price_locked=0 AND b.status IN ('CONFIRMED_BLOCKED','PENDING_BLOCK','AWAITING_BANK_VERIFY')"
        " ORDER BY b.created_at").fetchall()
    bank_verify = conn.execute(
        "SELECT b.id, o.title, b.company, b.payment_ref, b.total_mw FROM bookings b"
        " JOIN offers o ON o.id=b.offer_id"
        " WHERE b.status='AWAITING_BANK_VERIFY' ORDER BY b.created_at").fetchall()
    due = conn.execute(
        "SELECT b.id, o.title, b.company, b.amount, o.delivery_date FROM bookings b"
        " JOIN offers o ON o.id=b.offer_id"
        " WHERE b.status='PAYMENT_DUE' AND b.payment_mode='upi_block' ORDER BY o.delivery_date").fetchall()
    recent = conn.execute(
        "SELECT b.*, o.title FROM bookings b"
        " JOIN offers o ON o.id=b.offer_id"
        " ORDER BY b.created_at DESC LIMIT 8").fetchall()
    offers = conn.execute("SELECT * FROM offers ORDER BY cutoff_date DESC").fetchall()
    waitlist = conn.execute(
        "SELECT w.*, o.title FROM waitlist w JOIN offers o ON o.id=w.offer_id"
        " WHERE w.status IN (?,?) ORDER BY w.created_at", (D.WL_WAITING, D.WL_OFFERED)).fetchall()
    return render_template("admin/dashboard.html", stats=stats, to_lock=to_lock,
                           bank_verify=bank_verify, due=due, recent=recent, offers=offers,
                           metrics={o["id"]: offer_metrics(o["id"]) for o in offers},
                           states={o["id"]: offer_state(o) for o in offers},
                           waitlist=waitlist)


@app.route("/admin/offers")
@admin_required
def admin_offers():
    offers = D.get_db().execute("SELECT * FROM offers ORDER BY cutoff_date DESC").fetchall()
    return render_template("admin/offers.html", offers=offers,
                           metrics={o["id"]: offer_metrics(o["id"]) for o in offers},
                           states={o["id"]: offer_state(o) for o in offers})


OFFER_FORM_FIELDS = ("title", "sku", "description", "terms", "capacity_mw",
                     "price_per_watt", "min_pool_mw", "delivery_date", "cutoff_date",
                     "status", "supplier_tier", "rebate_pct", "rebate_terms",
                     "restocking_fee_pct", "penalty_days")


def _offer_payload(offer=None):
    """Read + validate the offer form. Returns (values, error)."""
    source = request.form
    try:
        capacity = float(source.get("capacity_mw") or 0)
        price = float(source.get("price_per_watt") or 0)
        min_pool = float(source.get("min_pool_mw") or 0)
        rebate_pct = float(source.get("rebate_pct") or 0)
        restocking_pct = float(source.get("restocking_fee_pct") or 0)
        penalty_days = int(float(source.get("penalty_days") or 0))
    except (TypeError, ValueError):
        return None, "Capacity, minimum pool, price and fees must be numbers."
    if capacity <= 0 or price <= 0:
        return None, "Capacity and price must be positive."
    if min_pool < 0 or rebate_pct < 0 or restocking_pct < 0 or penalty_days < 0:
        return None, "Pool target, rebate and fees cannot be negative."
    tier = (source.get("supplier_tier") or "Tier 1").strip()
    if tier not in D.SUPPLIER_TIERS:
        tier = "Tier 1"
    return {
        "title": (source.get("title") or "Untitled slot").strip(),
        "sku": (source.get("sku") or "").strip(),
        "description": source.get("description") or "",
        "terms": source.get("terms") or "",
        "capacity_mw": capacity,
        "price_per_watt": price,
        "min_pool_mw": min_pool,
        "delivery_date": source.get("delivery_date") or (offer["delivery_date"] if offer else today().isoformat()),
        "cutoff_date": source.get("cutoff_date") or (offer["cutoff_date"] if offer else today().isoformat()),
        "status": source.get("status") or "draft",
        "supplier_tier": tier,
        "rebate_pct": rebate_pct,
        "rebate_terms": source.get("rebate_terms") or "",
        "restocking_fee_pct": restocking_pct,
        "penalty_days": penalty_days,
    }, None


@app.route("/admin/offers/new", methods=["GET", "POST"])
@admin_required
def admin_offer_new():
    if request.method == "POST":
        values, error = _offer_payload()
        if error:
            flash(error, "bad")
            return render_template("admin/offer_form.html", offer=request.form, creating=True)
        conn = D.get_db()
        cur = conn.execute(
            "INSERT INTO offers (title, sku, description, terms, capacity_mw, price_per_watt,"
            " min_pool_mw, delivery_date, cutoff_date, status, supplier_tier, rebate_pct,"
            " rebate_terms, restocking_fee_pct, penalty_days, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (values["title"], values["sku"], values["description"], values["terms"],
             values["capacity_mw"], values["price_per_watt"], values["min_pool_mw"],
             values["delivery_date"], values["cutoff_date"], values["status"],
             values["supplier_tier"], values["rebate_pct"], values["rebate_terms"],
             values["restocking_fee_pct"], values["penalty_days"], D.now_iso()),
        )
        conn.commit()
        audit("offer", cur.lastrowid, "OFFER_CREATED",
              f"{values['title']} created ({values['capacity_mw']:g} MW, {values['supplier_tier']}).",
              "admin")
        flash(f"Offer #{cur.lastrowid} created.", "ok")
        return redirect(url_for("admin_offers"))
    return render_template("admin/offer_form.html", offer=None, creating=True)


@app.route("/admin/offers/<int:offer_id>/edit", methods=["GET", "POST"])
@admin_required
def admin_offer_edit(offer_id):
    conn = D.get_db()
    offer = conn.execute("SELECT * FROM offers WHERE id=?", (offer_id,)).fetchone()
    if offer is None:
        abort(404)
    if request.method == "POST":
        values, error = _offer_payload(offer)
        if error:
            flash(error, "bad")
            return render_template("admin/offer_form.html", offer=offer, creating=False)
        booked = D.booked_mw(offer_id, conn)
        if values["capacity_mw"] < booked:
            flash(f"Cannot set capacity below {booked:g} MW already blocked by buyers.", "bad")
            return render_template("admin/offer_form.html", offer=offer, creating=False)
        conn.execute(
            "UPDATE offers SET title=?, sku=?, description=?, terms=?, capacity_mw=?, price_per_watt=?,"
            " min_pool_mw=?, delivery_date=?, cutoff_date=?, status=?, supplier_tier=?,"
            " rebate_pct=?, rebate_terms=?, restocking_fee_pct=?, penalty_days=? WHERE id=?",
            (values["title"], values["sku"], values["description"], values["terms"],
             values["capacity_mw"], values["price_per_watt"], values["min_pool_mw"],
             values["delivery_date"], values["cutoff_date"], values["status"],
             values["supplier_tier"], values["rebate_pct"], values["rebate_terms"],
             values["restocking_fee_pct"], values["penalty_days"], offer_id),
        )
        conn.commit()
        audit("offer", offer_id, "OFFER_UPDATED",
              f"{values['title']} updated - price {values['price_per_watt']:g} ₹/W, "
              f"{values['supplier_tier']}, rebate {values['rebate_pct']:g}%.", "admin")
        flash("Offer updated. Existing bookings keep their locked price.", "ok")
        return redirect(url_for("admin_offers"))
    return render_template("admin/offer_form.html", offer=offer, creating=False)


@app.route("/admin/bookings")
@admin_required
def admin_bookings():
    conn = D.get_db()
    status = request.args.get("status", "")
    offer_id = request.args.get("offer", "")
    q = (request.args.get("q") or "").strip()
    sql = ("SELECT b.*, o.title FROM bookings b"
           " JOIN offers o ON o.id=b.offer_id WHERE 1=1")
    args = []
    if status:
        sql += " AND b.status=?"
        args.append(status)
    if offer_id:
        sql += " AND b.offer_id=?"
        args.append(offer_id)
    if q:
        sql += " AND (b.company LIKE ? OR b.contact LIKE ? OR b.email LIKE ? OR o.title LIKE ? OR CAST(b.id AS TEXT)=?)"
        args += [f"%{q}%"] * 4 + [q]
    sql += " ORDER BY b.created_at DESC"
    rows = conn.execute(sql, args).fetchall()
    offers = conn.execute("SELECT id, title FROM offers ORDER BY id DESC").fetchall()
    return render_template("admin/bookings.html", rows=rows, offers=offers,
                           f_status=status, f_offer=offer_id, q=q)


@app.route("/admin/bookings/<int:booking_id>")
@admin_required
def admin_booking_detail(booking_id):
    conn = D.get_db()
    b = conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
    if b is None:
        abort(404)
    offer = conn.execute("SELECT * FROM offers WHERE id=?", (b["offer_id"],)).fetchone()
    items = conn.execute(
        "SELECT * FROM booking_items WHERE booking_id=? ORDER BY id", (booking_id,)).fetchall()
    events = conn.execute(
        "SELECT * FROM booking_events WHERE booking_id=? ORDER BY created_at DESC, id DESC",
        (booking_id,)).fetchall()
    mandate = conn.execute(
        "SELECT * FROM mandates WHERE booking_id=? ORDER BY id DESC LIMIT 1", (booking_id,)).fetchone()
    m_events = debits = []
    if mandate:
        m_events = conn.execute(
            "SELECT * FROM mandate_events WHERE mandate_id=? ORDER BY received_at DESC, id DESC",
            (mandate["id"],)).fetchall()
        debits = conn.execute(
            "SELECT * FROM debits WHERE mandate_id=? ORDER BY attempted_at DESC",
            (mandate["id"],)).fetchall()
    price, discount, _ = effective_price(offer)
    penalty = cancellation_penalty(offer, b)
    rebates = conn.execute(
        "SELECT * FROM rebates WHERE booking_id=? ORDER BY created_at DESC", (booking_id,)).fetchall()
    audit_rows = conn.execute(
        "SELECT * FROM audit_log WHERE entity_type='booking' AND entity_id=?"
        " ORDER BY created_at DESC, id DESC LIMIT 50", (str(booking_id),)).fetchall()
    return render_template("admin/booking_detail.html", b=b, offer=offer,
                           items=items, events=events, mandate=mandate,
                           m_events=m_events, debits=debits, price=price,
                           discount=discount, penalty=penalty, rebates=rebates,
                           audit_rows=audit_rows)


@app.route("/admin/bookings/<int:booking_id>/lock-price", methods=["POST"])
@admin_required
def admin_lock_price(booking_id):
    conn = D.get_db()
    b = conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
    if b is None:
        abort(404)
    if b["price_locked"] or b["status"] in (D.CANCELLED, D.PAID, D.FULFILLED, D.BLOCK_EXPIRED):
        flash("Price can no longer be locked on this booking.", "bad")
        return redirect(url_for("admin_booking_detail", booking_id=booking_id))
    try:
        price = float(request.form.get("unit_price_watt"))
    except (TypeError, ValueError):
        flash("Enter a valid price (INR per watt).", "bad")
        return redirect(url_for("admin_booking_detail", booking_id=booking_id))
    if price <= 0:
        flash("Price must be positive.", "bad")
        return redirect(url_for("admin_booking_detail", booking_id=booking_id))
    gross = round(b["total_mw"] * 1_000_000 * price, 2)
    credit = float(b["rebate_applied"] or 0)
    amount = round(max(0.0, gross - credit), 2)

    mandate = conn.execute(
        "SELECT * FROM mandates WHERE booking_id=? ORDER BY id DESC LIMIT 1", (booking_id,)).fetchone()
    capped = False
    if b["payment_mode"] == "upi_block" and mandate and mandate["status"] == "ACTIVE":
        # PDF edge case: the debit can never exceed the blocked amount. Instead of
        # refusing the lock we cap the price at the maximum the block allows.
        blocked = float(mandate["blocked_amount"])
        if amount > blocked + 1e-6:
            price = round(blocked / (b["total_mw"] * 1_000_000), 4) if b["total_mw"] else price
            amount = round(max(0.0, min(blocked, b["total_mw"] * 1_000_000 * price - credit)), 2)
            capped = True
        new_status = D.PAYMENT_DUE
    elif b["payment_mode"] == "bank":
        # Bank path: the buyer still has to submit / we still have to verify the transfer.
        new_status = D.AWAITING_BANK
    else:
        new_status = D.PAYMENT_DUE if b["status"] == D.CONFIRMED_BLOCKED else b["status"]

    conn.execute(
        "UPDATE bookings SET unit_price_watt=?, amount=?, price_locked=1, status=?, updated_at=? WHERE id=?",
        (price, amount, new_status, D.now_iso(), booking_id),
    )
    conn.commit()
    note = (request.form.get("note") or "").strip()
    if note:
        conn.execute("UPDATE bookings SET admin_note=? WHERE id=?", (note, booking_id))
        conn.commit()
    credit_note = f" (less {fmt_inr(credit)} rebate credit)" if credit else ""
    log_event(booking_id, "PRICE_LOCKED",
              f"Price locked at ₹{price:g}/W - order value {fmt_inr(amount)}{credit_note}.", "admin", internal=1)
    audit("booking", booking_id, "PRICE_LOCKED",
          f"Price locked at ₹{price:g}/W - order value {fmt_inr(amount)}{credit_note}.", "admin")
    if capped:
        flash(f"Price capped at ₹{price:g}/W ({fmt_inr(amount)}) - a debit can never exceed "
              "the blocked amount.", "info")
    else:
        flash(f"Price locked at ₹{price:g}/W ({fmt_inr(amount)}).", "ok")
    return redirect(url_for("admin_booking_detail", booking_id=booking_id))


@app.route("/admin/bookings/<int:booking_id>/grant-rebate", methods=["POST"])
@admin_required
def admin_grant_rebate(booking_id):
    """Dynamic pricing strategy: the rebate is offered LATER, not at booking."""
    conn = D.get_db()
    b = conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
    if b is None:
        abort(404)
    if b["status"] not in (D.PAID, D.FULFILLED):
        flash("Rebates can only be granted on a paid or fulfilled order.", "bad")
        return redirect(url_for("admin_booking_detail", booking_id=booking_id))
    try:
        pct = float(request.form.get("pct") or 0)
    except (TypeError, ValueError):
        pct = 0.0
    if pct <= 0:
        flash("Enter a rebate percentage above zero.", "bad")
        return redirect(url_for("admin_booking_detail", booking_id=booking_id))
    base = float(b["amount"] or 0)
    amount = round(base * pct / 100.0, 2)
    if amount <= 0:
        flash("The rebate amount comes to zero - check the order value.", "bad")
        return redirect(url_for("admin_booking_detail", booking_id=booking_id))
    note = (request.form.get("note") or "").strip()
    cur = conn.execute(
        "INSERT INTO rebates (booking_id, company, email, pct, amount, status, note, created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (booking_id, b["company"], (b["email"] or "").lower(), pct, amount,
         D.REBATE_AVAILABLE, note, D.now_iso()),
    )
    conn.commit()
    log_event(booking_id, "REBATE_GRANTED",
              f"A {pct:g}% rebate of {fmt_inr(amount)} was granted - it is credited against "
              "your next booking with the same email address.", "admin")
    audit("booking", booking_id, "REBATE_GRANTED",
          f"Rebate {pct:g}% ({fmt_inr(amount)}) granted to {b['company']}.", "admin")
    flash(f"Rebate of {fmt_inr(amount)} ({pct:g}%) granted to {b['company']}.", "ok")
    return redirect(url_for("admin_booking_detail", booking_id=booking_id))


@app.route("/admin/rebates")
@admin_required
def admin_rebates():
    conn = D.get_db()
    rows = conn.execute(
        "SELECT r.*, b.status AS booking_status FROM rebates r"
        " JOIN bookings b ON b.id=r.booking_id ORDER BY r.created_at DESC"
    ).fetchall()
    return render_template("admin/rebates.html", rows=rows)


@app.route("/admin/bookings/<int:booking_id>/verify-bank", methods=["POST"])
@admin_required
def admin_verify_bank(booking_id):
    conn = D.get_db()
    b = conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
    if b is None or b["status"] != D.AWAITING_BANK:
        flash("This booking is not awaiting bank verification.", "bad")
        return redirect(url_for("admin_booking_detail", booking_id=booking_id))
    conn.execute("UPDATE bookings SET status=?, updated_at=? WHERE id=?",
                 (D.PAID, D.now_iso(), booking_id))
    conn.commit()
    log_event(booking_id, "BANK_TRANSFER_VERIFIED",
              f"Bank transfer {b['payment_ref'] or 'confirmed'} verified - payment recorded.", "admin", internal=1)
    audit("booking", booking_id, "BANK_TRANSFER_VERIFIED",
          f"Bank transfer {b['payment_ref'] or 'confirmed'} verified.", "admin")
    flash("Bank transfer verified - booking marked PAID.", "ok")
    return redirect(url_for("admin_booking_detail", booking_id=booking_id))


@app.route("/admin/bookings/<int:booking_id>/debit", methods=["POST"])
@admin_required
def admin_debit(booking_id):
    msg = _run_debit(booking_id)
    flash(msg, "ok" if "Debited" in msg else "bad")
    return redirect(url_for("admin_booking_detail", booking_id=booking_id))


@app.route("/admin/bookings/<int:booking_id>/cancel", methods=["POST"])
@admin_required
def admin_cancel(booking_id):
    conn = D.get_db()
    b = conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
    if b is None:
        abort(404)
    if b["status"] in (D.PAID, D.FULFILLED, D.CANCELLED):
        flash("Paid orders cannot be cancelled here - handle refunds outside the app.", "bad")
        return redirect(url_for("admin_booking_detail", booking_id=booking_id))
    offer = conn.execute("SELECT * FROM offers WHERE id=?", (b["offer_id"],)).fetchone()
    penalty = cancellation_penalty(offer, b)
    waive = request.form.get("waive_fee") == "1"
    reason = (request.form.get("reason") or "Cancelled by admin.").strip()
    fee = 0.0 if waive else penalty["fee"]
    mandate = conn.execute(
        "SELECT * FROM mandates WHERE booking_id=? AND status IN ('PENDING_APPROVAL','ACTIVE')"
        " ORDER BY id DESC LIMIT 1", (booking_id,)).fetchone()
    if mandate and not fee:
        status, event, msg = psp.release_block()
        conn.execute("UPDATE mandates SET status=? WHERE id=?", (status, mandate["id"]))
        _mandate_event(mandate["id"], event, msg)
    charged = _charge_restocking_fee(booking_id, fee) if fee else 0.0
    _return_rebate_credit(booking_id)
    conn.execute("UPDATE bookings SET status=?, updated_at=? WHERE id=?",
                 (D.CANCELLED, D.now_iso(), booking_id))
    conn.commit()
    log_event(booking_id, "CANCELLED", reason, "admin")
    audit("booking", booking_id, "CANCELLED",
          f"Order cancelled by the admin (restocking fee {fmt_inr(charged)}).", "admin")
    flash("Booking cancelled, capacity released."
           + (f" Restocking fee {fmt_inr(charged)} charged." if charged else ""), "ok")
    return redirect(url_for("admin_booking_detail", booking_id=booking_id))


@app.route("/admin/bookings/<int:booking_id>/fulfill", methods=["POST"])
@admin_required
def admin_fulfill(booking_id):
    conn = D.get_db()
    b = conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
    if b is None or b["status"] != D.PAID:
        flash("Only paid bookings can be marked fulfilled.", "bad")
        return redirect(url_for("admin_booking_detail", booking_id=booking_id))
    conn.execute("UPDATE bookings SET status=?, updated_at=? WHERE id=?",
                 (D.FULFILLED, D.now_iso(), booking_id))
    conn.commit()
    log_event(booking_id, "FULFILLED", "Delivery confirmed - order fulfilled.", "admin", internal=1)
    audit("booking", booking_id, "FULFILLED", "Delivery confirmed - order fulfilled.", "admin")
    flash("Order marked fulfilled.", "ok")
    return redirect(url_for("admin_booking_detail", booking_id=booking_id))


@app.route("/admin/bookings/<int:booking_id>/note", methods=["POST"])
@admin_required
def admin_note(booking_id):
    note = (request.form.get("note") or "").strip()
    if note:
        conn = D.get_db()
        conn.execute("UPDATE bookings SET admin_note=? WHERE id=?", (note, booking_id))
        conn.commit()
        log_event(booking_id, "ADMIN_NOTE", note, "admin", internal=1)
        audit("booking", booking_id, "ADMIN_NOTE", note, "admin")
        flash("Note saved (internal only).", "ok")
    return redirect(url_for("admin_booking_detail", booking_id=booking_id))


# ------------------------------------------------------------------ waitlist backend
@app.route("/admin/waitlist")
@admin_required
def admin_waitlist():
    conn = D.get_db()
    rows = conn.execute(
        "SELECT w.*, o.title FROM waitlist w JOIN offers o ON o.id=w.offer_id"
        " ORDER BY (w.status IN (?,?)) DESC, w.created_at", (D.WL_WAITING, D.WL_OFFERED)
    ).fetchall()
    offers = conn.execute("SELECT * FROM offers ORDER BY cutoff_date DESC").fetchall()
    return render_template("admin/waitlist.html", rows=rows, offers=offers,
                           states={o["id"]: offer_state(o) for o in offers})


@app.route("/admin/waitlist/<int:entry_id>/convert", methods=["POST"])
@admin_required
def admin_waitlist_convert(entry_id):
    """Turn a waitlist entry into a held booking (capacity is reserved)."""
    conn = D.get_db()
    w = conn.execute("SELECT * FROM waitlist WHERE id=?", (entry_id,)).fetchone()
    if w is None:
        abort(404)
    if w["status"] in (D.WL_CONVERTED, D.WL_CANCELLED):
        flash("This waitlist entry is already closed.", "bad")
        return redirect(url_for("admin_waitlist"))
    offer = conn.execute("SELECT * FROM offers WHERE id=?", (w["offer_id"],)).fetchone()
    booked = D.booked_mw(offer["id"], conn)
    if w["mw"] > offer["capacity_mw"] - booked + 1e-9:
        flash(f"Not enough capacity left in {offer['title']} - only "
              f"{offer['capacity_mw'] - booked:g} MW is free.", "bad")
        return redirect(url_for("admin_waitlist"))
    price, _, _ = effective_price(offer, booked)
    estimate = round(w["mw"] * 1_000_000 * price, 2)
    ts = D.now_iso()
    token = secrets.token_urlsafe(16)
    cur = conn.execute(
        "INSERT INTO bookings (offer_id, company, contact, phone, email, token, status,"
        " total_mw, payment_mode, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (w["offer_id"], w["company"], w["contact"], w["phone"], w["email"], token,
         D.PENDING_BLOCK, w["mw"], "upi_block", ts, ts),
    )
    booking_id = cur.lastrowid
    conn.execute(
        "INSERT INTO booking_items (booking_id, project_name, mw, notes) VALUES (?,?,?,?)",
        (booking_id, w["company"], w["mw"], w["notes"]),
    )
    conn.execute(
        "UPDATE waitlist SET status=?, updated_at=?, converted_booking_id=? WHERE id=?",
        (D.WL_CONVERTED, ts, booking_id, entry_id),
    )
    blk = psp.create_block_request(estimate, valid_to=offer["delivery_date"] + " 23:59:59")
    conn.execute(
        "INSERT INTO mandates (booking_id, psp_ref, blocked_amount, status, valid_from, valid_to, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (booking_id, blk["psp_ref"], blk["blocked_amount"], blk["status"], ts, blk["valid_to"], ts),
    )
    conn.commit()
    log_event(booking_id, "WAITLIST_CONVERTED",
              f"Created from the waitlist - {w['mw']:g} MW held for {w['company']} "
              f"(estimate {fmt_inr(estimate)}).", "admin")
    audit("waitlist", entry_id, "CONVERTED_TO_BOOKING",
          f"{w['company']} moved from the waitlist to booking #{booking_id}.", "admin")
    flash(f"Waitlist entry converted to booking #{booking_id} - tracking code {token}.", "ok")
    return redirect(url_for("admin_booking_detail", booking_id=booking_id))


@app.route("/admin/waitlist/<int:entry_id>/remove", methods=["POST"])
@admin_required
def admin_waitlist_remove(entry_id):
    conn = D.get_db()
    w = conn.execute("SELECT * FROM waitlist WHERE id=?", (entry_id,)).fetchone()
    if w is None:
        abort(404)
    conn.execute("UPDATE waitlist SET status=?, updated_at=? WHERE id=?",
                 (D.WL_CANCELLED, D.now_iso(), entry_id))
    conn.commit()
    audit("waitlist", entry_id, "REMOVED", f"{w['company']} removed from the waitlist.", "admin")
    flash("Waitlist entry removed.", "ok")
    return redirect(url_for("admin_waitlist"))


# ------------------------------------------------------------------ audit trail
@app.route("/admin/audit")
@admin_required
def admin_audit():
    conn = D.get_db()
    entity = request.args.get("entity", "")
    actor = request.args.get("actor", "")
    q = (request.args.get("q") or "").strip()
    sql = "SELECT * FROM audit_log WHERE 1=1"
    args = []
    if entity:
        sql += " AND entity_type=?"
        args.append(entity)
    if actor:
        sql += " AND actor=?"
        args.append(actor)
    if q:
        sql += " AND (message LIKE ? OR event LIKE ? OR entity_id LIKE ?)"
        args += [f"%{q}%"] * 3
    sql += " ORDER BY created_at DESC, id DESC LIMIT 500"
    rows = conn.execute(sql, args).fetchall()
    entities = [r["entity_type"] for r in conn.execute(
        "SELECT DISTINCT entity_type FROM audit_log ORDER BY entity_type").fetchall()]
    return render_template("admin/audit.html", rows=rows, entities=entities,
                           f_entity=entity, f_actor=actor, q=q)


@app.route("/admin/audit.csv")
@admin_required
def admin_audit_csv():
    conn = D.get_db()
    rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 5000").fetchall()
    lines = ["timestamp,entity_type,entity_id,event,actor,ip,message"]
    for r in rows:
        lines.append(",".join([
            f'"{r["created_at"]}"', f'"{r["entity_type"]}"', f'"{r["entity_id"]}"',
            f'"{r["event"]}"', f'"{r["actor"]}"', f'"{r["ip"]}"', f'"{r["message"]}"',
        ]))
    body = "\n".join(lines)
    return app.response_class(
        body, mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=solar-audit-trail.csv"})


# ------------------------------------------------------------------ settings
@app.route("/admin/settings", methods=["GET", "POST"])
@admin_required
def admin_settings():
    conn = D.get_db()
    if request.method == "POST":
        D.ensure_settings(conn)
        for key in ("min_booking_mw", "restocking_fee_pct", "penalty_window_days",
                    "default_rebate_pct"):
            value = (request.form.get(key) or "").strip()
            if value:
                D.set_setting(key, value, conn)
        ladder = (request.form.get("pricing_ladder") or "").strip()
        if ladder:
            try:
                json.loads(ladder)
            except (TypeError, ValueError):
                flash("The price ladder must be valid JSON.", "bad")
                return render_template("admin/settings.html", settings=D.all_settings(conn))
            D.set_setting("pricing_ladder", ladder, conn)
        D.set_setting("allow_current_account_blocks",
                      "1" if request.form.get("allow_current_account_blocks") == "1" else "0",
                      conn)
        conn.commit()
        audit("setting", "pricing", "SETTINGS_UPDATED", "Marketplace settings updated.", "admin")
        flash("Settings saved.", "ok")
        return redirect(url_for("admin_settings"))
    settings = D.all_settings(conn)
    return render_template("admin/settings.html", settings=settings)


@app.route("/admin/run-sweep", methods=["POST"])
@admin_required
def admin_sweep():
    done = sweep()
    flash("Maintenance run: " + ("; ".join(done) if done else "nothing due."), "ok")
    return redirect(url_for("admin_dashboard"))


# ------------------------------------------------------------------ PSP webhook
@app.route("/psp/webhook", methods=["POST"])
def psp_webhook():
    """In production the PSP posts signed events here. The handler must
    verify the signature and deduplicate by event id (PDF section 6).
    Sandbox: accepts JSON {mandate_ref, event_type, payload}."""
    import json as _json
    data = request.get_json(silent=True) or {}
    ref, event = data.get("mandate_ref"), data.get("event_type")
    if not ref or not event:
        return {"ok": False, "error": "mandate_ref and event_type required"}, 400
    conn = D.get_db()
    mandate = conn.execute("SELECT * FROM mandates WHERE psp_ref=?", (ref,)).fetchone()
    if mandate is None:
        return {"ok": False, "error": "unknown mandate"}, 404
    _mandate_event(mandate["id"], event, _json.dumps(data.get("payload", {}))[:2000])
    conn.commit()
    audit("booking", mandate["booking_id"], f"PSP_{event}",
          f"PSP webhook {event} for mandate {ref}.", "psp")
    return {"ok": True}


# ------------------------------------------------------------------ run
@app.template_filter("mw")
def mw_fmt(v):
    try:
        return f"{float(v):g} MW"
    except (TypeError, ValueError):
        return str(v)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
