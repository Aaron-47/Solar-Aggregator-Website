# Meeting actions → what changed in the product

Source: the meeting notes shared after the team review. Every action item below is
implemented in the app; this file is the map between the notes and the code so nobody
has to re-derive it later.

---

## Note 1 — technical issues

### 1.1 "Minimum purchase amount of 2 MW needs to change to allow smaller quantities"

**Status: done.**

| | before | after |
|---|---|---|
| hard minimum | 2 MW per slot (published as *Minimum pool*) | `settings.min_booking_mw` = **0.01 MW (10 kW)** per order line |
| pool minimum | treated as a gate | **soft target** — `offers.min_pool_mw`, shown as *Pool target: building / met* |
| published copy | "2 MW minimum, below it buyers roll to the next slot or are refunded" | "the pool target is a goal, not a gate — smaller orders are pooled with the others" |

- Booking validation: `app.py` → `create_booking()` rejects anything under the hard floor
  and tells the buyer the exact number.
- Below the pool target the order is still accepted; the buyer timeline says
  *"The 2 MW pool target for this slot is not met yet — your quantity is pooled with the others."*
- Admin can change the floor for the whole marketplace in **Backend → Settings**.
- Every slot can still carry its own (soft) pool target in **Backend → Offers → Edit**.

### 1.2 "Dynamic updates showing remaining inventory in each slot"

**Status: done.**

- New JSON API:
  - `GET /api/offers` → every open slot with capacity / booked / remaining / fill % / live
    price / waitlist count, plus the marketplace totals.
  - `GET /api/offers/<id>/availability` → one slot.
  - `GET /api/rebate?email=…` → rebate credit available to a buyer.
- `static/live.js` polls `/api/offers` every 15 s and updates the *MW still open*, the fill
  bar, the live price and the *live* badge on the home page, the capacity dashboard, the
  slot page, the waitlist page and the backend dashboard — no page reload needed.
- The booking form and every write path re-check the remaining capacity **server-side**,
  so two buyers can never take the same MW.

### 1.3 "Research whether company accounts can block funds for solar bookings" (AD)

**Status: documented + a runtime policy switch.**

Findings written up in [RESEARCH_NOTES.md](RESEARCH_NOTES.md):

- A UPI block (OTM / e-mandate) is a *pre-authorisation* on the buyer's own bank account.
  It is offered by essentially every PSP on **savings and current accounts**, but banks
  differ on whether a **company current account** is eligible for a *merchant* (third-party)
  e-mandate — many retail-banking flows only enable OTM on personal savings accounts.
- So the answer is **bank-by-bank, not universal**: it has to be confirmed in writing by
  the acquiring bank for our merchant category (see PDF §8 item 1) before we rely on it.
- Mitigation already in the product: the **RTGS/NEFT/LC path** works from any company
  account, and the booking form hints at it automatically above the UPI limit (₹5 lakh).
- Runtime switch: **Backend → Settings → "Allow UPI blocks on company current accounts"**.
  *On* = the UPI block path is offered (default, pending the bank's written confirmation);
  *off* = the form only offers RTGS/NEFT/LC and the UPI block is withdrawn from the
  consent screen.

### 1.4 "Restocking fees and penalty structures for cancellations near delivery dates"

**Status: done.**

- Policy per slot: `offers.restocking_fee_pct` (default **2 %**) and `offers.penalty_days`
  (default **7 days before the slot date**). Global defaults live in **Backend → Settings**.
- **Cancelling before the price is locked** → free.
- **Cancelling more than *penalty_days* before delivery** → free, the block is released.
- **Cancelling inside the window** → the restocking fee is charged
  (`% of the locked order value`), the remainder of the block is released, and both the
  fee debit and the release are recorded (`debits`, `mandate_events`, `booking_events`,
  `audit_log`).
- The buyer is shown the **exact fee before they confirm** on the order page
  ("Cancelling now costs a restocking fee of ₹…"), and again in the confirmation dialog.
- The admin can cancel with the same computation and can **waive** the fee
  ("Waive the restocking fee" checkbox) — useful for a good-faith cancellation.
- For the bank path the fee is recorded on the order and netted off the refund by the
  aggregator (no second transfer is triggered by the app).

---

## Note 2 — dashboard and marketplace mechanics

### 2.1 "Dashboard for solar capacity booking that shows available slots — 2 open slots, 10 MW"

**Status: done.**

- **Public capacity dashboard** at `/capacity`: open slots, published MW, booked MW,
  still open, pool filled %, waitlist count, a per-slot table (price, capacity, booked,
  open, pool target, cut-off, delivery, waitlist) and the recently fulfilled orders.
- The same numbers appear as a **live strip on the home page** and as KPIs in the backend.
- With the seeded data the dashboard reads exactly: **2 open slots · 10 MW published**
  (5 MW November + 5 MW December).

### 2.2 "Audit trails"

**Status: done.**

- New `audit_log` table (entity, id, event, message, actor, IP, timestamp).
- Written for: booking created, block requested / approved / failed / expired, price lock,
  bank verification, debit, cancellation, restocking fee, fulfilment, admin notes,
  offer created / updated, waitlist join / convert / remove, rebate granted / applied,
  admin login / logout, settings changes, PSP webhooks and every sweep action.
- **Backend → Audit**: filter by object type, actor or free text; **Download CSV** exports
  the last 5 000 rows.
- The per-order timelines (`booking_events`, `mandate_events`) are unchanged and still
  the customer-visible story; the audit log is the system-wide, non-customer-facing record.

### 2.3 "Dynamic slot availability updates"

Covered in 1.2 — the same live API drives the public site and the backend.

### 2.4 "Waitlist functionality for when slots are fully booked"

**Status: done.**

- A full slot shows **“Fully booked — join the waitlist”** instead of the booking form.
- Buyers queue with company, contact, phone, email, quantity and notes; they get a
  **secret waitlist link** (works in *Track order* too) showing their queue position and
  the live slot status.
- **Backend → Waitlist**: list with live remaining capacity per slot, **Convert** (creates
  a real booking that reserves the capacity and issues a tracking code) or **Remove**.
- Conversion is refused when the slot no longer has the MW free — capacity is never
  double-sold.
- A seeded demo entry (`demo-waitlist-token`) is on the full November slot.

### 2.5 "Tier 1 / tier 2 supplier classifications instead of specific brand names"

**Status: done.**

- New `offers.supplier_tier` — `Tier 1` (bankable Tier-1 supply chain, direct manufacturer
  channel) or `Tier 2` (approved secondary distributor, Tier-1 cells/modules).
- Brand names are gone from the product: the seeded SKU “SAEL 550 W Mono-PERC Bifacial” was
  migrated to **“Tier 1 - 550 W Mono-PERC Bifacial”** (one-off data fixup, schema version 2).
- Every public surface shows the tier badge and the specification, never a brand.
- The class is chosen in **Backend → Offers → Edit** and explained on the slot page.

### 2.6 "Dynamic pricing strategies where rebates would be offered later rather than initially"

**Status: done — the structure is live, the rebate is deliberately 0 % for now.**

Two separate mechanisms, matching the note:

1. **Dynamic (volume) pricing** — a price ladder in **Backend → Settings** shades the
   published price down as the pool fills. Default ladder: 0 % → 0 %, 25 % → 1 %,
   50 % → 2 %, 75 % → 3 %. The live price always rounds **down** to a clean 2-decimal
   number, so the amount we block is never more than the amount we quoted, and a locked
   price can never exceed the block (the app caps it and says so).
2. **Rebates granted later** — `offers.rebate_pct` and `settings.default_rebate_pct` are
   **0 %** initially, exactly as the meeting decided. When a rebate is granted
   (**Backend → order → Grant a rebate**, on a *paid* order) it becomes a **credit** for
   the buyer: it is shown on the order page, credited automatically against their next
   booking with the same email, subtracted from the locked order value, and returned if
   that booking is cancelled before payment.
   Everything about the rebate is auditable (`rebates` table + audit entries).

---

## Files touched

| file | change |
|---|---|
| `db.py` | new tables `waitlist`, `rebates`, `audit_log`, `settings`; new columns on `offers` and `bookings`; versioned migration + one-off data fixups; new seed |
| `app.py` | pricing ladder, availability API, waitlist, audit helpers, rebate flow, restocking-fee logic, capacity dashboard, settings page, CSV export |
| `templates/index.html`, `capacity.html` (new) | live capacity dashboard |
| `templates/offer_detail.html` | tier badge, live stock, soft pool target, waitlist form, rebate credit lookup |
| `templates/waitlist.html` (new) | buyer waitlist status page |
| `templates/booking_detail.html` | rebate credit, restocking-fee warning, tier, dynamic price |
| `templates/admin/*` | dashboard KPIs, offers (tier/rebate/fee), booking detail (grant rebate, fee policy, audit), waitlist (new), audit (new), rebates (new), settings (new) |
| `static/live.js` (new), `static/style.css` | live inventory polling, tier badge, live badge |
| `docs/RESEARCH_NOTES.md` (new) | the research answers behind the policy switches |
