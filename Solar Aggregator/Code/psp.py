"""Sandbox adapter for the UPI block-and-debit flow (ASBA / UPI OTM) from the
project PDF. NO REAL MONEY MOVES HERE.

Every function has the same shape as a real PSP integration (PhonePe, Paytm,
Razorpay, bank PSP). To go live:

  1. Confirm with the PSP/acquiring bank that our merchant category is
     eligible for block (OTM) mandates and get the per-mandate limit and
     maximum validity (PDF section 8).
  2. Replace the bodies below with real HTTP calls; keep the signatures.
  3. Point the PSP's webhooks at /psp/webhook in app.py - signature
     verification and deduplication live there.

Remember the PDF's caveat: UPI per-mandate limits are small relative to
MW-scale order values. The UPI block path suits residential / small
commercial bookings; large bookings use the RTGS/NEFT/LC path
(payment_mode = "bank") with admin verification.
"""
import secrets
import datetime as dt

SANDBOX = True


def new_ref(prefix):
    return prefix + dt.datetime.now().strftime("%y%m%d") + secrets.token_hex(4).upper()


def create_block_request(amount, valid_to):
    """Backend -> PSP: create a block mandate. Returns the fields to persist."""
    return {
        "psp_ref": new_ref("SBXBLK"),
        "blocked_amount": round(float(amount), 2),
        "status": "PENDING_APPROVAL",
        "valid_to": valid_to,
    }


def approve_mandate(simulate_failure=False):
    """Customer entered their UPI PIN in their app.
    Returns (status, event_type, message)."""
    if simulate_failure:
        return ("FAILED", "BLOCK_FAILED",
                "Bank rejected the block - insufficient available balance in the account.")
    return ("ACTIVE", "BLOCK_APPROVED",
            "Block active - funds earmarked, available balance reduced.")


def execute_debit(blocked_amount, amount):
    """Debit instruction on the slot date. The debit can never exceed the
    blocked amount (PDF section 2). Returns (status, event_type, message)."""
    if amount > float(blocked_amount) + 1e-6:
        return ("REJECTED", "DEBIT_REJECTED",
                f"Debit of {amount:,.2f} exceeds the blocked amount {blocked_amount:,.2f}.")
    return ("SUCCESS", "DEBIT_SUCCESS", f"{amount:,.2f} debited against the block.")


def release_block():
    """Revoke / expiry: unused earmarked funds are freed.
    Returns (status, event_type, message)."""
    return ("RELEASED", "BLOCK_RELEASED", "Block released - earmarked funds freed.")
