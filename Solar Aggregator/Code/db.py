"""Database layer: SQLite schema, seeding, and shared constants.

Schema mirrors the minimum data model from the project PDF (section 6):
bookings, mandates, mandate_events, debits - plus users, offers, items
and a booking timeline.

Meeting changes implemented in this layer (see docs/MEETING_ACTIONS.md):
  * smaller quantities allowed -> the pool minimum is now a soft target, the hard
    floor is the global setting `min_booking_mw` (default 10 kW).
  * tier classification        -> `offers.supplier_tier` (Tier 1 / Tier 2); brand
    names are no longer published.
  * dynamic pricing + later rebates -> `offers.rebate_pct` plus the `rebates`
    table; rebates are granted after the fact, never at booking time.
  * waitlist for full slots    -> `waitlist` table.
  * audit trail                -> `audit_log` table (system wide).
  * cancellation penalty       -> `bookings.restocking_fee`,
    `offers.restocking_fee_pct`, `offers.penalty_days`.
"""
import os
import sqlite3
import datetime as dt
from flask import g
from werkzeug.security import generate_password_hash

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "solar.db")

# Bumped when a data fixup has to run on an existing database.
SCHEMA_VERSION = 3

# ---------------------------------------------------------------- statuses
# Booking state machine (PDF section 5):
#   PENDING_BLOCK -> CONFIRMED_BLOCKED -> PAYMENT_DUE -> PAID
# Off-ramps: BLOCK_FAILED, CANCELLED, BLOCK_EXPIRED, PAYMENT_FAILED
# Additions: AWAITING_BANK_VERIFY (RTGS/NEFT/LC path for MW-scale orders),
#            FULFILLED (delivery tracking).
PENDING_BLOCK = "PENDING_BLOCK"
BLOCK_FAILED = "BLOCK_FAILED"
CONFIRMED_BLOCKED = "CONFIRMED_BLOCKED"
AWAITING_BANK = "AWAITING_BANK_VERIFY"
PAYMENT_DUE = "PAYMENT_DUE"
PAID = "PAID"
FULFILLED = "FULFILLED"
CANCELLED = "CANCELLED"
BLOCK_EXPIRED = "BLOCK_EXPIRED"
PAYMENT_FAILED = "PAYMENT_FAILED"

# Statuses that hold capacity on an offer.
HOLD_STATUSES = (PENDING_BLOCK, CONFIRMED_BLOCKED, AWAITING_BANK, PAYMENT_DUE, PAID, FULFILLED)

STATUS_LABELS = {
    PENDING_BLOCK: "Pending block",
    BLOCK_FAILED: "Block failed",
    CONFIRMED_BLOCKED: "Blocked - funds earmarked",
    AWAITING_BANK: "Awaiting bank verification",
    PAYMENT_DUE: "Payment due",
    PAID: "Paid",
    FULFILLED: "Fulfilled",
    CANCELLED: "Cancelled",
    BLOCK_EXPIRED: "Block expired",
    PAYMENT_FAILED: "Payment failed",
}

STATUS_TONE = {
    PAID: "ok", FULFILLED: "ok",
    CONFIRMED_BLOCKED: "info",
    PENDING_BLOCK: "warn", AWAITING_BANK: "warn", PAYMENT_DUE: "warn",
    BLOCK_FAILED: "bad", PAYMENT_FAILED: "bad",
    BLOCK_EXPIRED: "muted", CANCELLED: "muted",
}

# ---------------------------------------------------------------- catalogue
# Suppliers are classified, never named: the published SKU is a technical
# spec, the supplier behind it is Tier 1 or Tier 2.
SUPPLIER_TIERS = ("Tier 1", "Tier 2")

TIER_LABELS = {
    "Tier 1": "Tier 1 - bankable Tier-1 supply chain, direct manufacturer channel",
    "Tier 2": "Tier 2 - approved secondary distributor, Tier-1 cells/modules",
}

# Waitlist states
WL_WAITING = "WAITING"
WL_OFFERED = "OFFERED"
WL_CONVERTED = "CONVERTED"
WL_CANCELLED = "CANCELLED"

# Rebate states
REBATE_AVAILABLE = "AVAILABLE"
REBATE_APPLIED = "APPLIED"
REBATE_CANCELLED = "CANCELLED"

DEFAULT_SETTINGS = {
    # Bumped whenever a data fixup has to run on an existing database.
    "schema_version": "0",
    # Hard floor per booking line - the old 2 MW pool minimum is gone.
    "min_booking_mw": "0.01",
    # Restocking fee applied when a buyer cancels close to the slot date.
    "restocking_fee_pct": "2.0",
    # "Close to the slot date" = this many days before the delivery date.
    "penalty_window_days": "7",
    # Default rebate percentage granted later (dynamic pricing strategy).
    "default_rebate_pct": "0",
    # Research item: may a *company current account* be used to block funds?
    # 1 = allowed (bank/e-mandate capable accounts), 0 = savings accounts only.
    "allow_current_account_blocks": "1",
    # Volume (dynamic) price ladder: pick the highest step whose min_pct the
    # slot has already passed. Discount is applied to the published price.
    "pricing_ladder": (
        '[{"min_pct": 0, "discount_pct": 0.0},'
        ' {"min_pct": 25, "discount_pct": 1.0},'
        ' {"min_pct": 50, "discount_pct": 2.0},'
        ' {"min_pct": 75, "discount_pct": 3.0}]'
    ),
}

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  email         TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  company       TEXT NOT NULL,
  contact       TEXT NOT NULL,
  phone         TEXT DEFAULT '',
  role          TEXT NOT NULL DEFAULT 'admin',         -- single admin login; buyers do not register
  created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS offers (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  title           TEXT NOT NULL,
  sku             TEXT NOT NULL,
  description     TEXT DEFAULT '',
  terms           TEXT DEFAULT '',
  capacity_mw     REAL NOT NULL,
  price_per_watt  REAL NOT NULL,                     -- published INR/W, changes never touch existing bookings
  min_pool_mw     REAL DEFAULT 0,                    -- soft target - the pool a slot wants to reach
  delivery_date   TEXT NOT NULL,                     -- slot date
  cutoff_date     TEXT NOT NULL,                     -- orders close (end of day)
  status          TEXT NOT NULL DEFAULT 'draft',     -- draft | open | closed | fulfilled | cancelled
  supplier_tier   TEXT NOT NULL DEFAULT 'Tier 1',    -- Tier 1 | Tier 2 (no brand names)
  rebate_pct      REAL NOT NULL DEFAULT 0,           -- rebate offered LATER, not at booking
  rebate_terms    TEXT DEFAULT '',
  restocking_fee_pct REAL NOT NULL DEFAULT 0,        -- late-cancellation restocking fee
  penalty_days    INTEGER NOT NULL DEFAULT 7,        -- fee window: days before the slot date
  created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bookings (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  offer_id          INTEGER NOT NULL REFERENCES offers(id),
  company           TEXT NOT NULL,                    -- buyer company from the public form
  contact           TEXT NOT NULL,
  phone             TEXT DEFAULT '',
  email             TEXT DEFAULT '',
  token             TEXT NOT NULL UNIQUE,             -- secret link to track this order (no account)
  status            TEXT NOT NULL DEFAULT 'PENDING_BLOCK',
  total_mw          REAL NOT NULL,
  unit_price_watt   REAL,                            -- locked price (NULL until admin locks it)
  price_locked      INTEGER NOT NULL DEFAULT 0,
  amount            REAL,                            -- total INR once price is locked
  payment_mode      TEXT NOT NULL DEFAULT 'upi_block',  -- upi_block | bank
  payment_ref       TEXT DEFAULT '',                 -- RTGS/NEFT UTR or LC reference
  rebate_applied    REAL NOT NULL DEFAULT 0,         -- rebate credit used against this order
  rebate_source_id  INTEGER,                         -- rebates row consumed
  restocking_fee    REAL NOT NULL DEFAULT 0,         -- charged on a late cancellation
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL,
  admin_note        TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS booking_items (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  booking_id   INTEGER NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
  project_name TEXT NOT NULL,
  mw           REAL NOT NULL,
  notes        TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS mandates (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  booking_id      INTEGER NOT NULL REFERENCES bookings(id),
  psp_ref         TEXT NOT NULL,
  customer_vpa    TEXT DEFAULT '',
  blocked_amount  REAL NOT NULL,
  status          TEXT NOT NULL,                     -- PENDING_APPROVAL | ACTIVE | EXECUTED | RELEASED | FAILED | EXPIRED
  valid_from      TEXT NOT NULL,
  valid_to        TEXT NOT NULL,
  created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mandate_events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  mandate_id  INTEGER NOT NULL REFERENCES mandates(id),
  event_type  TEXT NOT NULL,
  payload     TEXT DEFAULT '',
  received_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS debits (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  mandate_id   INTEGER NOT NULL REFERENCES mandates(id),
  amount       REAL NOT NULL,
  psp_txn_ref  TEXT NOT NULL,
  status       TEXT NOT NULL,                        -- SUCCESS | FAILED
  attempted_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS booking_events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  booking_id INTEGER NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
  event      TEXT NOT NULL,
  message    TEXT NOT NULL,
  actor      TEXT NOT NULL,
  internal   INTEGER NOT NULL DEFAULT 0,             -- 1 = admin only, hidden from buyer
  created_at TEXT NOT NULL
);

-- Buyers waiting for a slot that is already full.
CREATE TABLE IF NOT EXISTS waitlist (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  offer_id        INTEGER NOT NULL REFERENCES offers(id),
  company         TEXT NOT NULL,
  contact         TEXT NOT NULL,
  phone           TEXT DEFAULT '',
  email           TEXT DEFAULT '',
  mw              REAL NOT NULL,
  notes           TEXT DEFAULT '',
  token           TEXT NOT NULL UNIQUE,              -- secret link to check the waitlist entry
  status          TEXT NOT NULL DEFAULT 'WAITING',   -- WAITING | OFFERED | CONVERTED | CANCELLED
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  converted_booking_id INTEGER
);

-- Rebates are granted AFTER the order is paid (dynamic pricing strategy).
CREATE TABLE IF NOT EXISTS rebates (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  booking_id      INTEGER NOT NULL REFERENCES bookings(id),
  company         TEXT NOT NULL,
  email           TEXT DEFAULT '',
  pct             REAL NOT NULL,
  amount          REAL NOT NULL,
  status          TEXT NOT NULL DEFAULT 'AVAILABLE', -- AVAILABLE | APPLIED | CANCELLED
  note            TEXT DEFAULT '',
  created_at      TEXT NOT NULL,
  applied_booking_id INTEGER
);

-- System wide audit trail (who did what, when, from where).
CREATE TABLE IF NOT EXISTS audit_log (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_type TEXT NOT NULL,                         -- booking | offer | waitlist | rebate | admin | setting
  entity_id   TEXT DEFAULT '',
  event       TEXT NOT NULL,
  message     TEXT DEFAULT '',
  actor       TEXT NOT NULL DEFAULT 'system',
  ip          TEXT DEFAULT '',
  created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


# ---------------------------------------------------------------- plumbing
def now_iso():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(_exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_app(app):
    app.teardown_appcontext(close_db)
    with app.app_context():
        conn = get_db()
        # Migration: v1 bookings pointed at a users row (buyer accounts).
        # v2 bookings carry the buyer's form details + a secret token.
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(bookings)")]
        if cols and "company" not in cols:
            conn.close()
            g.pop("db", None)
            os.replace(DB_PATH, DB_PATH + ".v1.bak")
            conn = get_db()
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()
        ensure_settings(conn)
        _upgrade_data(conn)
        _seed(conn)


# ---------------------------------------------------------------- migration
def _columns(conn, table):
    return [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]


def _add_column(conn, table, column, ddl):
    if column not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def _migrate(conn):
    """Bring an existing database up to the current schema."""
    _add_column(conn, "offers", "supplier_tier", "supplier_tier TEXT NOT NULL DEFAULT 'Tier 1'")
    _add_column(conn, "offers", "rebate_pct", "rebate_pct REAL NOT NULL DEFAULT 0")
    _add_column(conn, "offers", "rebate_terms", "rebate_terms TEXT DEFAULT ''")
    _add_column(conn, "offers", "restocking_fee_pct", "restocking_fee_pct REAL NOT NULL DEFAULT 0")
    _add_column(conn, "offers", "penalty_days", "penalty_days INTEGER NOT NULL DEFAULT 7")
    _add_column(conn, "bookings", "rebate_applied", "rebate_applied REAL NOT NULL DEFAULT 0")
    _add_column(conn, "bookings", "rebate_source_id", "rebate_source_id INTEGER")
    _add_column(conn, "bookings", "restocking_fee", "restocking_fee REAL NOT NULL DEFAULT 0")


def _upgrade_data(conn):
    """One-off data fixups. Each version block runs exactly once."""
    try:
        version = int(float(get_setting("schema_version", 0, conn)))
    except (TypeError, ValueError):
        version = 0
    if version >= SCHEMA_VERSION:
        return

    if version < 2:
        # v2 - brand names must not be published any more: swap them for the
        # Tier-1 specification string and classify the slot. The 2 MW pool
        # minimum becomes a soft target.
        for row in conn.execute("SELECT id, sku FROM offers").fetchall():
            sku = (row["sku"] or "")
            if "SAEL" in sku.upper():
                conn.execute(
                    "UPDATE offers SET sku=?, supplier_tier=? WHERE id=?",
                    ("Tier 1 - 550 W Mono-PERC Bifacial", "Tier 1", row["id"]),
                )
            elif not sku.lower().startswith("tier"):
                conn.execute(
                    "UPDATE offers SET sku=?, supplier_tier=? WHERE id=?",
                    (f"{sku} (Tier 1)".strip(), "Tier 1", row["id"]),
                )
        conn.execute("UPDATE offers SET min_pool_mw=0.1 WHERE min_pool_mw>0.1")

    if version < 3:
        # v3 - late cancellation policy (restocking fee) and the demo waitlist
        # entry on the full November slot.
        conn.execute("UPDATE offers SET restocking_fee_pct=? WHERE restocking_fee_pct=0",
                     (DEFAULT_SETTINGS["restocking_fee_pct"],))
        conn.execute("UPDATE offers SET penalty_days=? WHERE penalty_days=7",
                     (DEFAULT_SETTINGS["penalty_window_days"],))
        if not conn.execute("SELECT 1 FROM waitlist LIMIT 1").fetchone():
            offer = conn.execute("SELECT id FROM offers ORDER BY id LIMIT 1").fetchone()
            if offer:
                now = now_iso()
                conn.execute(
                    "INSERT INTO waitlist (offer_id, company, contact, phone, email, mw, notes,"
                    " token, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (offer["id"], "Helios Infra", "Neha Shah", "+91 90000 11111",
                     "neha@helios.in", 1.5, "Need 1.5 MW for a rooftop project",
                     "demo-waitlist-token", WL_WAITING, now, now),
                )
        # A second entry on an open slot, so the backend can try "convert to
        # booking" out of the box.  (On a brand new database the offers are
        # seeded afterwards, so there is nothing to do here yet.)
        if not conn.execute("SELECT 1 FROM waitlist WHERE token='demo-waitlist-token-2'").fetchone():
            first = conn.execute("SELECT id FROM offers ORDER BY id LIMIT 1").fetchone()
            second = None
            if first:
                second = conn.execute(
                    "SELECT id FROM offers WHERE id>? ORDER BY id LIMIT 1", (first["id"],)).fetchone()
            if second:
                now = now_iso()
                conn.execute(
                    "INSERT INTO waitlist (offer_id, company, contact, phone, email, mw, notes,"
                    " token, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (second["id"], "Vertex Power", "Anil Menon", "+91 90000 22222",
                     "anil@vertexpower.in", 0.5, "0.5 MW for a rooftop project - ready to book now",
                     "demo-waitlist-token-2", WL_WAITING, now, now),
                )

    conn.commit()
    set_setting("schema_version", SCHEMA_VERSION, conn)


# ---------------------------------------------------------------- settings
def ensure_settings(conn=None):
    conn = conn or get_db()
    for key, value in DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?,?)", (key, value))
    conn.commit()


def get_setting(key, default=None, conn=None):
    conn = conn or get_db()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return default if default is not None else DEFAULT_SETTINGS.get(key)
    return row["value"]


def set_setting(key, value, conn=None):
    conn = conn or get_db()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?,?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    conn.commit()


def all_settings(conn=None):
    conn = conn or get_db()
    ensure_settings(conn)
    out = dict(DEFAULT_SETTINGS)
    for row in conn.execute("SELECT key, value FROM settings"):
        out[row["key"]] = row["value"]
    return out


# ---------------------------------------------------------------- audit
def audit(entity_type, entity_id, event, message="", actor="system", ip="", conn=None):
    """Append one line to the system wide audit trail."""
    conn = conn or get_db()
    conn.execute(
        "INSERT INTO audit_log (entity_type, entity_id, event, message, actor, ip, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (str(entity_type), str(entity_id or ""), event, message or "", actor, ip or "",
         now_iso()),
    )
    conn.commit()


# ---------------------------------------------------------------- queries
def booked_mw(offer_id, conn=None):
    conn = conn or get_db()
    row = conn.execute(
        f"SELECT COALESCE(SUM(total_mw), 0) AS mw FROM bookings "
        f"WHERE offer_id = ? AND status IN ({','.join('?' * len(HOLD_STATUSES))})",
        (offer_id, *HOLD_STATUSES),
    ).fetchone()
    return float(row["mw"])


def waitlist_count(offer_id, conn=None):
    conn = conn or get_db()
    row = conn.execute(
        "SELECT COUNT(*) c FROM waitlist WHERE offer_id=? AND status IN (?,?)",
        (offer_id, WL_WAITING, WL_OFFERED),
    ).fetchone()
    return int(row["c"])


def available_rebate(email, conn=None):
    """Total rebate credit a buyer can still use, matched on email."""
    conn = conn or get_db()
    email = (email or "").strip().lower()
    if not email:
        return 0.0
    row = conn.execute(
        "SELECT COALESCE(SUM(amount),0) amt FROM rebates"
        " WHERE status=? AND LOWER(email)=?",
        (REBATE_AVAILABLE, email),
    ).fetchone()
    return float(row["amt"] or 0.0)


# ---------------------------------------------------------------- seed
def _seed(conn):
    """First-run data: one admin login, the November and December 2026
    slots from the strategy draft, and one example booking submitted
    through the public form (1 MW Company-A + 1 MW Company-B +
    3 MW Company-C) so the backend has something to show."""
    if conn.execute("SELECT 1 FROM users WHERE role='admin' LIMIT 1").fetchone():
        return

    now = now_iso()
    admin_email = os.environ.get("ADMIN_EMAIL", "admin@solaragg.in")
    admin_pass = os.environ.get("ADMIN_PASSWORD", "admin123")
    conn.execute(
        "INSERT INTO users (email, password_hash, company, contact, role, created_at) VALUES (?,?,?,?,?,?)",
        (admin_email, generate_password_hash(admin_pass), "Solar Aggregator", "Admin", "admin", now),
    )

    cur = conn.execute(
        "INSERT INTO offers (title, sku, description, terms, capacity_mw, price_per_watt, min_pool_mw,"
        " delivery_date, cutoff_date, status, supplier_tier, rebate_pct, rebate_terms,"
        " restocking_fee_pct, penalty_days, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "November 2026 Slot - 5 MW Module Pool",
            "Tier 1 - 550 W Mono-PERC Bifacial",
            "Pool of standard 550 W bifacial modules for the November delivery cycle. "
            "Orders close on the cut-off date; delivery on the slot date. "
            "Standard spec only - no custom specs inside a pool.",
            "Supplier invoices buyers directly. Aggregator earns commission. "
            "Any quantity from 10 kW upwards is accepted - the pool minimum is a target, "
            "not a gate. Volume pricing applies as the pool fills. "
            "Price is locked per booking at confirmation.",
            5.0, 18.50, 0.1, "2026-11-30", "2026-10-31", "open", "Tier 1", 0.0,
            "Rebates, if any, are granted after the slot date - not at booking.",
            2.0, 7, now,
        ),
    )
    offer_id = cur.lastrowid

    # Second open slot from the fixed calendar (deliveries on the 15th / last of month).
    cur2 = conn.execute(
        "INSERT INTO offers (title, sku, description, terms, capacity_mw, price_per_watt, min_pool_mw,"
        " delivery_date, cutoff_date, status, supplier_tier, rebate_pct, rebate_terms,"
        " restocking_fee_pct, penalty_days, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "December 2026 Slot - 5 MW Module Pool",
            "Tier 1 - 550 W Mono-PERC Bifacial",
            "Next cycle in the fixed calendar. Same standard spec, fresh published price for this slot.",
            "Supplier invoices buyers directly. Small quantities welcome - the pool minimum is a target.",
            5.0, 18.75, 0.1, "2026-12-15", "2026-11-15", "open", "Tier 1", 0.0,
            "Rebates, if any, are granted after the slot date - not at booking.",
            2.0, 7, now,
        ),
    )
    second_offer_id = cur2.lastrowid

    cur = conn.execute(
        "INSERT INTO bookings (offer_id, company, contact, phone, email, token, status, total_mw,"
        " payment_mode, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (offer_id, "Sunrise EPC", "Ravi Kumar", "+91 90000 00000", "demo@epc.in",
         "demo-tracking-token", PENDING_BLOCK, 5.0, "upi_block", now, now),
    )
    booking_id = cur.lastrowid
    for name, mw in (("Company-A", 1.0), ("Company-B", 1.0), ("Company-C", 3.0)):
        conn.execute(
            "INSERT INTO booking_items (booking_id, project_name, mw) VALUES (?,?,?)",
            (booking_id, name, mw),
        )
    conn.execute(
        "INSERT INTO booking_events (booking_id, event, message, actor, created_at) VALUES (?,?,?,?,?)",
        (booking_id, "BOOKING_CREATED",
         "Requested 5 MW - capacity on hold pending payment block.", "buyer", now),
    )

    # A demo waitlist entry for the (already full) November slot.
    if not conn.execute("SELECT 1 FROM waitlist LIMIT 1").fetchone():
        conn.execute(
            "INSERT INTO waitlist (offer_id, company, contact, phone, email, mw, notes, token,"
            " status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (offer_id, "Helios Infra", "Neha Shah", "+91 90000 11111", "neha@helios.in", 1.5,
             "Need 1.5 MW for a rooftop project", "demo-waitlist-token", WL_WAITING, now, now),
        )
        # A second entry on the open December slot, so the backend can try
        # "convert to booking" out of the box (the slot still has room).
        conn.execute(
            "INSERT INTO waitlist (offer_id, company, contact, phone, email, mw, notes, token,"
            " status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (second_offer_id, "Vertex Power", "Anil Menon", "+91 90000 22222",
             "anil@vertexpower.in", 0.5, "0.5 MW for a rooftop project - ready to book now",
             "demo-waitlist-token-2", WL_WAITING, now, now),
        )
    conn.commit()
