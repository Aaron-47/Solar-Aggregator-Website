# Research notes — the open questions from the team review

These are the "we need to find out" items from the meeting. They are written down here
because two of them ended up as **runtime policy switches** in the product, so the
decision can be flipped without a code change once the answer comes in.

---

## 1. Can a *company* account be used to block funds for a solar booking?

**Question (AD's action item).** Our whole UPI flow rests on a *block* (OTM / e-mandate /
ASBA-style pre-authorisation) on the buyer's bank account. Our buyers are companies, and
their money usually sits in a **current account**, not a savings account. Is a company
current account eligible for a third-party merchant block?

**What the flow actually needs.** A block is a pre-authorisation created by the *merchant
(us)* on the *buyer's own* account via the buyer's PSP/bank. It is not a transfer, and the
buyer can revoke it. Two things decide whether it works:

1. **Is the account type eligible for third-party OTM/e-mandate?** Retail UPI mandates
   (NPCI) are typically enabled on **personal savings accounts** and on **current
   accounts**, but the *merchant-side* (third-party) OTM/e-mandate flow is a **bank
   product** — every bank ships its own version, with its own account-type rules.
   Several retail banks enable third-party OTM on **personal savings and current
   accounts only**, and treat a **company current account** as a separate product that is
   eligible for a few merchant categories (utilities, tax, insurance) but not for
   arbitrary third-party blocks. Other banks do allow it.
   → **There is no single answer. It is bank-by-bank.**
2. **Is our merchant category eligible at all?** Even where the account type is fine, the
   acquiring bank/PSP has to approve the merchant category for OTM mandates, and to set
   the **per-mandate limit** and the **maximum validity** (this is already item 1 of the
   PDF §8 go-live checklist).

**Conclusion / what we do in the product**

- We do **not** block the buyer from using a company account. The UPI block path stays
  available by default, and the consent text on the booking page explicitly says a
  company current account with e-mandate/OTM support can be used.
- A switch exists for the day the bank says no: **Backend → Settings → "Allow UPI blocks
  on company current accounts"**. Turning it **off** removes the UPI block option from
  the public booking form and forces buyers onto **RTGS/NEFT/LC**, which works from any
  company account, any amount, and needs no mandate at all.
- The booking form already nudges buyers to the bank path when the order value goes past
  the UPI block limit (₹5 lakh in this build) — MW-scale orders are always above it, so
  the RTGS/NEFT/LC path is the normal route for big pools and the UPI path is the
  convenient route for smaller bookings.
- **To close this item:** get the acquiring bank's written confirmation of
  (a) third-party OTM on company current accounts, (b) our merchant category, (c) the
  per-mandate limit and (d) the maximum validity — then set the switch accordingly and
  record the answer here.

**Status:** open — needs the bank's written confirmation. The product is ready for
either answer.

---

## 2. Restocking fee and penalty structure for cancellations near the delivery date

**Question.** Buyers sitting on a cancellation-free window can hold capacity hostage
until close to the slot date, then cancel and free the MW for a buyer who cannot
re-deliver in time. What penalty is fair?

**Design decided (implemented)**

| when the buyer cancels | outcome |
|---|---|
| before the price is locked | free — nothing was committed |
| more than `penalty_days` before the slot date | free — the block is released in full |
| inside `penalty_days` of the slot date (default **7 days**) | **restocking fee** (default **2 %** of the locked order value) is charged out of the block, the rest of the block is released |
| after the slot date | the order is in fulfilment — handled outside the app, not a self-service cancel |

**Why these numbers**

- The fee is a *cost recovery*, not a profit centre: the aggregator has already sold the
  slot onward, so a late cancellation creates real cost. 2 % of a 1 MW order is roughly
  the gross margin on a pooled order — high enough to hurt a flipp-er, low enough that a
  genuine change of plan is not fatal.
- The 7-day window matches the time the supplier needs to re-allocate the modules
  (delivery slot + documentation), so it is the point where a cancellation really does
  cost money.
- The fee is **always shown before the buyer confirms** — no surprises, and the buyer can
  decide to keep the slot instead.
- The admin can **waive** the fee, which is what you want for a good-faith cancellation
  (site delay, force majeure) without giving the buyer a way to avoid the fee by asking.

**Open for the business to confirm:** the exact percentage and the window length per
slot, and the legal wording of the cancellation clause in the offer terms. Both are
configurable per slot and globally in **Backend → Settings**, so they can be tuned
without a release.

---

## 3. Why the rebate is not on the booking page

The meeting decided: **dynamic pricing now, rebates later** (not an upfront discount
game). That is implemented as two separate things — a volume price ladder that shades the
published price as the pool fills, and a **rebate credit granted after the slot** that is
applied against the buyer's *next* booking.

The rebate percentage is deliberately **0 %** today: the mechanism is live and testable
(grant a rebate on a paid order, see the credit on the buyer's order page, see it
subtracted from the next order value), but nothing is promised to buyers until the
commercial team decides the number.
