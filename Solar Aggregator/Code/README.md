# Solar Aggregator — Demand Pooling Platform

A simple, direct web app for the "Groupon of Solar" model (see `Solar_Demand_Aggregation_Strategy_DRAFT.md`):

- **Open website** — anyone can see the published slot offers (capacity, published ₹/W price, cut-off date, delivery date) without logging in.
- **Public booking form (no buyer accounts)** — a buyer fills an online form right on the slot page:
  their **company name, contact person, phone, email**, and each line of **required wattage**
  (e.g. *1 MW for Company-A, 1 MW for Company-B, 3 MW for Company-C*). Submitting holds the capacity
  and issues a **secret tracking code/link** — that page shows the order, the payment step and the
  full status timeline. Only someone with the code can see that order.
- **One admin login** — the full backend: every offer and every order **with the buyer's company name**,
  price locking, payment verification, debits, cancellation, fulfilment and internal notes.
- **Small quantities welcome** — the old 2 MW pool minimum is gone. The hard floor is
  **10 kW** per order line; the slot's *pool target* is a soft goal, not a gate, so a
  0.25 MW order is pooled with the others instead of being turned away.
- **Live inventory** — every page shows the **MW still open** per slot and refreshes it
  from `/api/offers` every 15 s, so buyers never book into a slot that just filled up.
- **Capacity dashboard** — `/capacity` (public) and the backend KPIs: open slots,
  published MW, booked MW, still open, pool filled %, waitlist count.
- **Waitlist** — a full slot offers a waitlist instead of a booking form; the backend can
  convert a queue entry into a real booking that reserves the capacity.
- **Audit trail** — every state change, payment instruction and admin action is recorded
  in `audit_log`, browsable and exportable at `/admin/audit`.
- **Tier 1 / Tier 2 supplier classes** — suppliers are classified, never named. Brand
  names are not published anywhere.
- **Dynamic pricing + later rebates** — a volume ladder shades the published price as the
  pool fills; rebates are granted **after** the slot and credited against the buyer's
  next booking (never as an upfront discount).
- **Late-cancellation restocking fee** — cancelling inside the window before the slot
  date costs a % restocking fee, shown to the buyer before they confirm; the admin can
  waive it.
- **Money commitment** — implements the attached PDF (*UPI Block-and-Debit for Solar Slot Booking*):
  a UPI ASBA/OTM-style **block** earmarks the buyer's money at booking (nothing leaves the account),
  the **debit runs on the slot date**, and the block is **released on cancellation/expiry**.
  Because MW-scale orders exceed UPI mandate limits (as the PDF notes), a second path —
  **RTGS/NEFT/LC with admin verification** — is built in for large orders.

## Run it

```bash
python app.py
# → http://127.0.0.1:5000
```

Requirements: Python 3.10+ and Flask (`pip install flask`). The SQLite database (`solar.db`)
is created and seeded automatically on first run.

## Sharing the site with remote testers (no hosting needed)

`start_public.ps1` (double-click) starts the app **and** a free **Cloudflare Quick Tunnel**,
then prints a public `https://…trycloudflare.com` URL. Anyone in India (or anywhere) can open
that link and use the site while your PC is on and the window is open. Notes:

- The tunnel URL is **temporary** — it changes every time you restart `start_public.ps1`.
  Share the fresh link with the testing team each time.
- The site only runs while your device is on and both windows (Python + tunnel) are open.
- For a permanent URL/hosting later, deploy `app.py` + `db.py` + `psp.py` + `templates/` + `static/`
  to any Python host (Render, Railway, PythonAnywhere, or a VPS) and put HTTPS in front.

### Logins

There is exactly **one login**: the admin backend (`ADMIN_EMAIL` / `ADMIN_PASSWORD` env vars
override the seeded `admin@solaragg.in` / `admin123` on first run). Buyers never log in —
they use the public booking form and their secret tracking code.

### Seeded demo data

- **November 2026 slot** — 5 MW, ₹18.50/W, cut-off 31 Oct, delivery 30 Nov.
  Seeded order #1 = your example (Sunrise EPC: 1 MW A + 1 MW B + 3 MW C).
  Its tracking code is `demo-tracking-token` (try it on *Track order*).
- **December 2026 slot** — 5 MW, ₹18.75/W, cut-off 15 Nov, delivery 15 Dec, empty — use it to try booking.
- A demo **waitlist** entry sits on the full November slot — its code is `demo-waitlist-token`.

> The public capacity dashboard (`/capacity`) reads **2 open slots · 10 MW published** with
> this seed, which is what the meeting asked the dashboard to show.

## The features added after the team review

Everything below is explained in [docs/MEETING_ACTIONS.md](docs/MEETING_ACTIONS.md)
(note → code map) and [docs/RESEARCH_NOTES.md](docs/RESEARCH_NOTES.md)
(the two open research questions and how they were answered).

### Smaller quantities

The 2 MW minimum is gone. A booking is accepted from **10 kW** (`settings.min_booking_mw`)
upwards. The slot's *pool target* (`offers.min_pool_mw`) is shown to buyers as a goal the
whole pool is working towards, not as a gate. Change the floor in **Backend → Settings**.

### Live inventory and the capacity dashboard

- `GET /api/offers` — every open slot with capacity, booked, remaining, fill %, live price,
  waitlist count + marketplace totals.
- `GET /api/offers/<id>/availability` — one slot.
- `GET /api/rebate?email=…` — rebate credit available to a buyer.
- `static/live.js` polls every 15 s and refreshes the numbers on the home page, the
  capacity dashboard, the slot pages and the backend dashboard — no reload.
- `/capacity` — the public dashboard (open slots, published/booked/open MW, fill %,
  waitlist count, per-slot table).

### Dynamic pricing (volume ladder)

`settings.pricing_ladder` shades the published price down as the pool fills —
default `0 % → 0 %, 25 % → 1 %, 50 % → 2 %, 75 % → 3 %`. The live price is always rounded
**down** to 2 decimals so we never block more than we quoted, and a locked price is capped
at the blocked amount (a UPI debit can never exceed the block).

### Rebates offered later, not upfront

`offers.rebate_pct` and `settings.default_rebate_pct` are **0 %** by default. An admin
grants a rebate on a **paid** order (**Backend → order → Grant a rebate**); it becomes a
**credit** that is shown on the order page, applied automatically against the buyer's next
booking with the same email, subtracted from the locked order value, and returned if that
booking is cancelled before payment. Every step is audited.

### Waitlist

A full slot shows *Fully booked — join the waitlist*. The buyer gets a secret waitlist link
(queue position + live slot status, also reachable from *Track order*). **Backend → Waitlist**
lists the queue and can **Convert** an entry into a real booking (capacity reserved,
tracking code issued) or remove it. Conversion is refused when the slot no longer has the
MW free — capacity is never double-sold.

### Tier 1 / Tier 2 supplier classes

`offers.supplier_tier` replaces brand names everywhere (the seeded "SAEL 550 W" SKU was
migrated to "Tier 1 - 550 W Mono-PERC Bifacial"). Tier is set per offer in
**Backend → Offers → Edit**.

### Cancellation penalty (restocking fee)

Per slot: `restocking_fee_pct` (default 2 %) and `penalty_days` (default 7 days before
delivery), both also configurable globally in **Backend → Settings**.

| when the buyer cancels | outcome |
|---|---|
| before the price is locked | free |
| more than `penalty_days` before the slot date | free, the block is released |
| inside the window | **restocking fee** charged out of the block, the rest released |

The buyer sees the exact fee before confirming; the admin can cancel with the same
computation and can **waive** the fee.

### Audit trail

`audit_log` records every state change, payment instruction, admin action, waitlist and
rebate event, settings change and PSP webhook. **Backend → Audit** filters by object type,
actor or free text and exports **CSV** (last 5 000 rows).

### Company current accounts (research item)

**Backend → Settings → "Allow UPI blocks on company current accounts"** decides whether
buyers can use the UPI block path from a company current account, or are pushed onto
RTGS/NEFT/LC. See [docs/RESEARCH_NOTES.md](docs/RESEARCH_NOTES.md) §1 for why the answer
is bank-by-bank and what to get in writing.

## Walkthrough (full money flow)

1. **Buyer** opens an open slot → fills the booking form (company, contact, email,
   e.g. 1 MW Company-A + 1 MW Company-B + 3 MW Company-C) → *Submit booking* →
   lands on their secret order page → consent screen (amount, what is blocked, debit date,
   how to cancel) → enter a VPA → **Send block request** → **Approve block** (sandbox UPI PIN).
   Status: *Blocked – funds earmarked*. The order also appears in the backend with the buyer's name.
2. **Admin** logs in at `/admin/login` → *Awaiting price lock* → open the order →
   **Lock price** (defaults to the published price; can be adjusted). Status: *Payment due*.
   The lock is refused if it exceeds the blocked amount (a UPI debit can never exceed the block).
3. **Admin** → **Execute debit now** (in production the scheduler runs it on the slot date).
   Status: *Paid* → **Mark fulfilled**. The buyer's timeline shows every step.
4. **Bank path** — a buyer booking with *RTGS/NEFT/LC* submits a UTR/LC reference;
   the admin verifies it against the bank statement → *Paid*.

## Booking state machine (from the PDF, §5)

```
PENDING_BLOCK → CONFIRMED_BLOCKED → PAYMENT_DUE → PAID → FULFILLED
off-ramps:  BLOCK_FAILED · BLOCK_EXPIRED · PAYMENT_FAILED · CANCELLED
bank path:  PENDING_BLOCK → AWAITING_BANK_VERIFY → PAID
```

Capacity is held by `PENDING_BLOCK, CONFIRMED_BLOCKED, AWAITING_BANK_VERIFY, PAYMENT_DUE, PAID, FULFILLED`
and released on cancel/expiry. Capacity is re-checked server-side on every booking.

## Project structure

```
app.py       routes, admin auth, CSRF, public booking form, token order tracking,
             sweep scheduler, PSP webhook endpoint, live availability API,
             pricing ladder, waitlist, rebate credit, audit trail
db.py        SQLite schema + versioned migration + seed (users[admin], offers,
             bookings + buyer form details + tracking token, items, mandates,
             mandate_events, debits, booking_events, waitlist, rebates,
             audit_log, settings)
psp.py       sandbox PSP adapter — create/approve block, execute debit, release
templates/   public site (home, slots, capacity dashboard, booking form, track,
             waitlist status, order page) + admin backend (dashboard, offers,
             bookings, waitlist, rebates, audit, settings)
static/      stylesheet + live.js (15 s inventory poller)
docs/        MEETING_ACTIONS.md (note → implementation) and RESEARCH_NOTES.md
```

Every PSP interaction is recorded: `mandate_events` is the audit trail the PDF requires,
`debits` records each attempt with its transaction reference, and `booking_events`
is the customer-visible tracking timeline (plus internal-only admin events).
The system-wide `audit_log` sits on top of that for the admin.

## Settings (Backend → Settings)

| setting | default | meaning |
|---|---|---|
| `min_booking_mw` | 0.01 | hard floor per order line (10 kW) |
| `pricing_ladder` | 0/25/50/75 % → 0/1/2/3 % | volume discount as the pool fills |
| `default_rebate_pct` | 0 | rebate granted later (0 % = not offered yet) |
| `restocking_fee_pct` | 2 | late-cancellation fee |
| `penalty_days` | 7 | window before the slot date in which the fee applies |
| `allow_current_account_blocks` | on | whether company current accounts may use the UPI block path |

Settings changes are versioned, audited and applied without a restart.

## Going live with a real PSP (PDF §8 checklist)

1. Confirm with the acquiring bank/PSP that our merchant category is eligible for
   block (OTM) mandates; get the **per-mandate limit and maximum validity**.
2. Replace the bodies in `psp.py` with real HTTP calls (keep the signatures) —
   idempotent calls keyed on booking id.
3. Point the PSP webhook at `POST /psp/webhook` — add signature verification and
   deduplication by event id before production.
4. Put the expiry/reminder/debit sweep on a real scheduler
   (`POST /admin/run-sweep` does the same thing manually):
   `* * * * * curl -X POST -b admin_session .../admin/run-sweep` or a small cron script.
5. Legal: consent text, cancellation policy, refund/dispute process, DPDP notice, GST invoice at debit time.
6. For orders above UPI limits (the normal case at 5 MW) keep using the **RTGS/NEFT/LC** path,
   or add escrow/bank guarantee later.

## Security notes

- Single admin account with role checks on every backend route.
- Buyers have no accounts and see nothing they didn't submit: each order lives behind a
  **secret random tracking token** (`/booking/<token>`); guessing one returns 404, and the
  backend shows every order only to the logged-in admin.
- Passwords hashed (Werkzeug), CSRF token on every POST, parameterised SQL.
- Session secret stored in `.secret_key` (auto-generated, keep it out of version control).
- This is a development server — deploy behind a proper WSGI server + HTTPS before going live.
