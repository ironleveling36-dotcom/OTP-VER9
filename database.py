import sqlite3
import threading
import logging
import os

logger = logging.getLogger(__name__)

_lock = threading.RLock()

# DB path is configurable so it can live on a Railway persistent volume.
# Set DB_PATH=/data/bot.db (with a volume mounted at /data) in production.
# Defaults to a local file for development.
DB_FILE = os.getenv("DB_PATH", "bot.db")

# Ensure the parent directory exists (e.g. the volume mount path /data).
_db_dir = os.path.dirname(DB_FILE)
if _db_dir:
    try:
        os.makedirs(_db_dir, exist_ok=True)
    except Exception as _e:
        logger.warning("Could not create DB directory %s: %s", _db_dir, _e)


def _execute(query, params=(), fetch=None):
    """
    Thread-safe SQLite execution with proper transaction handling.

    Improvements (Stability requirement #5):
      - Explicit commit/rollback on writes (atomic transactions).
      - WAL journal + busy timeout to avoid 'database is locked' under load.
      - Detailed error logging instead of silent failures.
    """
    with _lock:
        conn = sqlite3.connect(DB_FILE, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            cursor = conn.cursor()
            cursor.execute(query, params)
            if fetch == "all":
                return [dict(r) for r in cursor.fetchall()]
            elif fetch == "one":
                row = cursor.fetchone()
                return dict(row) if row else None
            else:
                conn.commit()
                return cursor.lastrowid
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            logger.error("DB error on query=%r params=%r: %s", query[:120], params, exc)
            raise
        finally:
            conn.close()


def _execute_many(statements):
    """
    Run several (query, params) statements inside ONE atomic transaction.
    Either all succeed and commit, or all roll back. Returns True on success.
    Used for operations that must not leave the DB half-updated
    (e.g. changing a Service ID -> delete old + insert new).
    """
    with _lock:
        conn = sqlite3.connect(DB_FILE, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            cur = conn.cursor()
            for query, params in statements:
                cur.execute(query, params)
            conn.commit()
            return True
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            logger.error("DB transaction failed (%d stmts): %s", len(statements), exc)
            raise
        finally:
            conn.close()

def init_db():
    queries = [
        '''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            balance REAL DEFAULT 0.0,
            is_admin INTEGER DEFAULT 0
        )
        ''',
        '''
        CREATE TABLE IF NOT EXISTS services (
            service_id TEXT,
            country TEXT,
            service_name TEXT,
            service_price REAL,
            is_enabled INTEGER DEFAULT 1,
            is_top INTEGER DEFAULT 0,
            PRIMARY KEY (service_id, country)
        )
        ''',
        '''
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            type TEXT,
            amount REAL,
            description TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        ''',
        '''
        CREATE TABLE IF NOT EXISTS recharge_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount REAL,
            status TEXT DEFAULT 'pending',
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        ''',
        '''
        CREATE TABLE IF NOT EXISTS admin_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        ''',
        '''
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            category TEXT,
            message TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        ''',
        # ── Purchases / Sales ledger (Dashboard + Number History) ───────────
        '''
        CREATE TABLE IF NOT EXISTS purchases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            service_id TEXT,
            service_name TEXT,
            country TEXT,
            price REAL DEFAULT 0,
            phone TEXT,
            activation_id TEXT,
            status TEXT DEFAULT 'pending',   -- pending|active|completed|cancelled|expired|failed
            otp_count INTEGER DEFAULT 0,
            refunded INTEGER DEFAULT 0,
            is_sale INTEGER DEFAULT 0,        -- 1 once revenue is realised (OTP delivered, not refunded)
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        '''
    ]
    for q in queries:
        _execute(q)

    # Helpful indexes for dashboard queries
    for idx in [
        "CREATE INDEX IF NOT EXISTS idx_purchases_user ON purchases(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_purchases_status ON purchases(status)",
        "CREATE INDEX IF NOT EXISTS idx_purchases_created ON purchases(created_at)",
        "CREATE INDEX IF NOT EXISTS idx_purchases_act ON purchases(activation_id)",
    ]:
        try:
            _execute(idx)
        except Exception:
            pass

    # Initialize settings if not exists
    _execute("INSERT OR IGNORE INTO admin_settings (key, value) VALUES ('upi_id', 'notset@upi')")
    _execute("INSERT OR IGNORE INTO admin_settings (key, value) VALUES ('qr_file_id', '')")
    _execute("INSERT OR IGNORE INTO admin_settings (key, value) VALUES ('qr_text', 'Scan QR to pay')")
    _execute("INSERT OR IGNORE INTO admin_settings (key, value) VALUES ('swiggy_service_id', 'swiggy')")
    _execute("INSERT OR IGNORE INTO admin_settings (key, value) VALUES ('support_id', '@support')")
    _execute("INSERT OR IGNORE INTO admin_settings (key, value) VALUES ('support_text', 'Need help? Contact our support team.')")
    _execute("INSERT OR IGNORE INTO admin_settings (key, value) VALUES ('otp_api_key', '')")
    _execute("INSERT OR IGNORE INTO admin_settings (key, value) VALUES ('force_channel', '')")
    _execute("INSERT OR IGNORE INTO admin_settings (key, value) VALUES ('referral_reward', '1')")

    _run_migrations()


def _column_exists(table, column) -> bool:
    rows = _execute(f"PRAGMA table_info({table})", (), "all") or []
    return any(r["name"] == column for r in rows)


def _run_migrations():
    """Add new columns/tables to pre-existing databases (safe, idempotent)."""
    # users: referral tracking
    if not _column_exists("users", "referred_by"):
        try:
            _execute("ALTER TABLE users ADD COLUMN referred_by INTEGER")
        except Exception:
            pass
    if not _column_exists("users", "referral_earnings"):
        try:
            _execute("ALTER TABLE users ADD COLUMN referral_earnings REAL DEFAULT 0")
        except Exception:
            pass
    # recharge_requests: transaction / UTR id submitted by user
    if not _column_exists("recharge_requests", "txn_id"):
        try:
            _execute("ALTER TABLE recharge_requests ADD COLUMN txn_id TEXT")
        except Exception:
            pass
    # OTP messages received per purchase (Purchase History → view OTPs)
    _execute('''
        CREATE TABLE IF NOT EXISTS purchase_otps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            purchase_id INTEGER,
            text TEXT,
            received_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    try:
        _execute("CREATE INDEX IF NOT EXISTS idx_potps_pid ON purchase_otps(purchase_id)")
    except Exception:
        pass

# ── Logs ───────────────────────────────────────────────────────
def add_log(category, message, user_id=None):
    try:
        _execute("INSERT INTO logs (user_id, category, message) VALUES (?, ?, ?)",
                 (user_id, category, message))
    except Exception:
        pass

def get_logs(limit=100, category=None):
    if category:
        return _execute("SELECT * FROM logs WHERE category = ? ORDER BY timestamp DESC LIMIT ?",
                        (category, limit), "all")
    return _execute("SELECT * FROM logs ORDER BY timestamp DESC LIMIT ?", (limit,), "all")

def add_user(user_id, username, is_admin=0):
    _execute('''
        INSERT INTO users (user_id, username, is_admin)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            is_admin=CASE WHEN users.is_admin = 1 THEN 1 ELSE excluded.is_admin END
    ''', (user_id, username, is_admin))


def user_exists(user_id) -> bool:
    return _execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,), "one") is not None


def get_setting(key, default=None):
    row = _execute("SELECT value FROM admin_settings WHERE key = ?", (key,), "one")
    val = row["value"] if row else None
    return val if (val is not None and val != "") else default


# ── Referrals (Refer & Earn) ────────────────────────────────────────────────

def get_referred_by(user_id):
    row = _execute("SELECT referred_by FROM users WHERE user_id = ?", (user_id,), "one")
    return row["referred_by"] if row and row.get("referred_by") else None


def set_referred_by(user_id, referrer_id) -> bool:
    """Link a user to their referrer ONLY if not already set. Returns True if linked."""
    row = _execute("SELECT referred_by FROM users WHERE user_id = ?", (user_id,), "one")
    if not row or row.get("referred_by"):
        return False  # user missing or already referred (prevents duplicate credit)
    _execute("UPDATE users SET referred_by = ? WHERE user_id = ?", (referrer_id, user_id))
    return True


def add_referral_earning(user_id, amount):
    _execute("UPDATE users SET referral_earnings = COALESCE(referral_earnings,0) + ? WHERE user_id = ?",
             (amount, user_id))


def count_referrals(user_id) -> int:
    row = _execute("SELECT COUNT(*) AS c FROM users WHERE referred_by = ?", (user_id,), "one")
    return int(row["c"]) if row else 0


def get_referral_earnings(user_id) -> float:
    row = _execute("SELECT referral_earnings FROM users WHERE user_id = ?", (user_id,), "one")
    return float(row["referral_earnings"]) if row and row.get("referral_earnings") else 0.0

def get_user(user_id):
    return _execute("SELECT * FROM users WHERE user_id = ?", (user_id,), "one")

def get_user_balance(user_id) -> float:
    row = _execute("SELECT balance FROM users WHERE user_id = ?", (user_id,), "one")
    return float(row["balance"]) if row else 0.0

def credit_wallet(user_id, amount, description) -> float:
    _execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
    _execute("INSERT INTO transactions (user_id, type, amount, description) VALUES (?, 'credit', ?, ?)",
             (user_id, amount, description))
    return get_user_balance(user_id)

def debit_wallet(user_id, amount, description) -> float:
    _execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (amount, user_id))
    _execute("INSERT INTO transactions (user_id, type, amount, description) VALUES (?, 'debit', ?, ?)",
             (user_id, -amount, description))
    return get_user_balance(user_id)

def get_user_transactions(user_id):
    return _execute("SELECT * FROM transactions WHERE user_id = ? ORDER BY timestamp DESC LIMIT 50", (user_id,), "all")

def get_all_transactions():
    return _execute("SELECT t.*, u.username FROM transactions t LEFT JOIN users u ON t.user_id = u.user_id ORDER BY t.timestamp DESC LIMIT 100", (), "all")

def create_recharge_request(user_id, amount) -> int:
    return _execute("INSERT INTO recharge_requests (user_id, amount, status) VALUES (?, ?, 'pending')", (user_id, amount))

def get_recharge_request(request_id):
    return _execute("SELECT r.*, u.username FROM recharge_requests r JOIN users u ON r.user_id = u.user_id WHERE r.id = ?", (request_id,), "one")

def update_recharge_request(request_id, status):
    _execute("UPDATE recharge_requests SET status = ? WHERE id = ?", (status, request_id))


def set_recharge_txn(request_id, txn_id):
    _execute("UPDATE recharge_requests SET txn_id = ? WHERE id = ?", (txn_id, request_id))

def get_pending_recharge_requests():
    return _execute("SELECT r.*, u.username FROM recharge_requests r JOIN users u ON r.user_id = u.user_id WHERE r.status = 'pending' ORDER BY r.timestamp DESC", (), "all")

def get_admin_settings():
    rows = _execute("SELECT * FROM admin_settings", (), "all")
    return {r["key"]: r["value"] for r in rows}

def update_admin_setting(key, value):
    _execute("INSERT INTO admin_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

def is_admin(user_id) -> bool:
    row = _execute("SELECT is_admin FROM users WHERE user_id = ?", (user_id,), "one")
    return bool(row["is_admin"]) if row else False

def add_service(service_id, country, service_name, service_price, is_enabled=1, is_top=0):
    _execute('''
        INSERT INTO services (service_id, country, service_name, service_price, is_enabled, is_top)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(service_id, country) DO UPDATE SET
            service_name=excluded.service_name,
            service_price=excluded.service_price,
            is_enabled=excluded.is_enabled,
            is_top=excluded.is_top
    ''', (service_id, country, service_name, service_price, is_enabled, is_top))

def edit_service_price(service_id, country, price):
    _execute("UPDATE services SET service_price = ? WHERE service_id = ? AND country = ?", (price, service_id, country))


def change_service_id(old_service_id, new_service_id, country, new_price=None):
    """
    Update Service ID Bug Fix (requirement #2).

    Atomically migrates a service to a NEW Service ID:
      - Reads the existing record (name/price/flags).
      - DELETES the old Service ID record completely (no orphaned pricing).
      - Also deletes any pre-existing record on the new ID (prevents duplicates).
      - INSERTS one clean record under the new Service ID.

    Returns (True, message) on success, (False, reason) on failure.
    Price updates correctly reflect for both Service Name and Service ID.
    """
    old_service_id = str(old_service_id).strip()
    new_service_id = str(new_service_id).strip()
    country = str(country).strip()

    if not new_service_id:
        return False, "New Service ID cannot be empty."

    old = _execute(
        "SELECT * FROM services WHERE service_id = ? AND country = ?",
        (old_service_id, country), "one",
    )
    if not old:
        return False, "Original service record not found."

    if new_service_id == old_service_id and new_price is None:
        return False, "New Service ID is identical to the current one."

    price = float(new_price) if new_price is not None else float(old["service_price"])
    name = old["service_name"]
    is_enabled = old["is_enabled"]
    is_top = old["is_top"]

    statements = [
        # Remove the OLD id record entirely (kills duplicate/stale pricing)
        ("DELETE FROM services WHERE service_id = ? AND country = ?",
         (old_service_id, country)),
        # Remove any stale record already sitting on the NEW id (dedupe)
        ("DELETE FROM services WHERE service_id = ? AND country = ?",
         (new_service_id, country)),
        # Insert the single, clean, authoritative record
        ('''INSERT INTO services (service_id, country, service_name, service_price, is_enabled, is_top)
            VALUES (?, ?, ?, ?, ?, ?)''',
         (new_service_id, country, name, price, is_enabled, is_top)),
    ]
    _execute_many(statements)
    return True, f"Service ID changed {old_service_id} -> {new_service_id} (₹{price:.2f})."


def dedupe_services_by_name(country):
    """
    Safety helper: collapse duplicate rows that share the same service_name in a
    country down to a single (most-recently-priced) record. Prevents the
    'duplicate pricing records' symptom described in requirement #2.
    """
    rows = _execute("SELECT * FROM services WHERE country = ?", (country,), "all")
    seen = {}
    dupes = []
    for r in rows:
        key = r["service_name"].strip().lower()
        if key in seen:
            dupes.append(r)
        else:
            seen[key] = r
    for d in dupes:
        _execute("DELETE FROM services WHERE service_id = ? AND country = ?",
                 (d["service_id"], d["country"]))
    return len(dupes)

def delete_service(service_id, country):
    _execute("DELETE FROM services WHERE service_id = ? AND country = ?", (service_id, country))

def toggle_service_enabled(service_id, country, is_enabled):
    _execute("UPDATE services SET is_enabled = ? WHERE service_id = ? AND country = ?", (is_enabled, service_id, country))

def toggle_service_top(service_id, country, is_top):
    _execute("UPDATE services SET is_top = ? WHERE service_id = ? AND country = ?", (is_top, service_id, country))

def get_services(country) -> dict:
    rows = _execute("SELECT * FROM services WHERE country = ?", (country,), "all")
    return {r["service_id"]: {
        "service_name": r["service_name"],
        "service_price": r["service_price"],
        "is_enabled": r["is_enabled"],
        "is_top": r["is_top"]
    } for r in rows}

def get_top_services(country) -> list:
    return _execute("SELECT * FROM services WHERE country = ? AND is_top = 1 AND is_enabled = 1 ORDER BY service_name ASC", (country,), "all")

def get_all_services_list():
    return _execute("SELECT * FROM services ORDER BY is_top DESC, country ASC, service_name ASC", (), "all")

def get_all_top_services():
    return _execute("SELECT * FROM services WHERE is_top = 1 ORDER BY country ASC, service_name ASC", (), "all")

def get_all_users():
    return _execute("SELECT * FROM users ORDER BY username ASC", (), "all")


# ── Purchases / Sales ledger ────────────────────────────────────────────────

def record_purchase(user_id, username, service_id, service_name, country, price,
                    phone=None, activation_id=None, status="pending") -> int:
    """Create a purchase row when a buy is initiated. Returns purchase id."""
    return _execute(
        '''INSERT INTO purchases
           (user_id, username, service_id, service_name, country, price, phone,
            activation_id, status, otp_count, refunded, is_sale)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0)''',
        (user_id, username, service_id, service_name, country, float(price or 0),
         phone, activation_id, status),
    )


def update_purchase(purchase_id, **fields):
    """Update mutable purchase fields (status, phone, activation_id, otp_count, refunded, is_sale)."""
    if not purchase_id or not fields:
        return
    allowed = {"status", "phone", "activation_id", "otp_count", "refunded", "is_sale", "price"}
    sets, params = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k} = ?")
            params.append(v)
    if not sets:
        return
    sets.append("updated_at = CURRENT_TIMESTAMP")
    params.append(purchase_id)
    _execute(f"UPDATE purchases SET {', '.join(sets)} WHERE id = ?", tuple(params))


def get_user_purchases(user_id, limit=30):
    return _execute(
        "SELECT * FROM purchases WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
        (user_id, limit), "all",
    )


def get_all_purchases(limit=100):
    return _execute(
        "SELECT * FROM purchases ORDER BY created_at DESC LIMIT ?",
        (limit,), "all",
    )


def get_sales_history(limit=100):
    """Realised sales only (revenue actually earned)."""
    return _execute(
        "SELECT * FROM purchases WHERE is_sale = 1 ORDER BY created_at DESC LIMIT ?",
        (limit,), "all",
    )


def get_active_purchases():
    """Numbers currently active/waiting (running orders monitor)."""
    return _execute(
        "SELECT * FROM purchases WHERE status IN ('pending','active') ORDER BY created_at DESC",
        (), "all",
    )


def get_today_sales_stats() -> dict:
    row = _execute(
        '''SELECT COUNT(*) AS cnt, COALESCE(SUM(price), 0) AS revenue
           FROM purchases
           WHERE is_sale = 1 AND date(created_at) = date('now', 'localtime')''',
        (), "one",
    ) or {}
    orders = _execute(
        '''SELECT COUNT(*) AS cnt FROM purchases
           WHERE date(created_at) = date('now', 'localtime')''',
        (), "one",
    ) or {}
    return {
        "sales_count": int(row.get("cnt", 0) or 0),
        "revenue": float(row.get("revenue", 0) or 0),
        "orders_count": int(orders.get("cnt", 0) or 0),
    }


def get_total_revenue_stats() -> dict:
    rev = _execute(
        "SELECT COUNT(*) AS cnt, COALESCE(SUM(price),0) AS revenue FROM purchases WHERE is_sale = 1",
        (), "one",
    ) or {}
    refunds = _execute(
        "SELECT COUNT(*) AS cnt, COALESCE(SUM(price),0) AS amt FROM purchases WHERE refunded = 1",
        (), "one",
    ) or {}
    total_orders = _execute("SELECT COUNT(*) AS cnt FROM purchases", (), "one") or {}
    return {
        "total_sales": int(rev.get("cnt", 0) or 0),
        "total_revenue": float(rev.get("revenue", 0) or 0),
        "refund_count": int(refunds.get("cnt", 0) or 0),
        "refund_amount": float(refunds.get("amt", 0) or 0),
        "total_orders": int(total_orders.get("cnt", 0) or 0),
    }


def get_service_wise_sales(limit=25):
    return _execute(
        '''SELECT service_name,
                  COUNT(*) AS sales,
                  COALESCE(SUM(price),0) AS revenue
           FROM purchases
           WHERE is_sale = 1
           GROUP BY LOWER(service_name)
           ORDER BY revenue DESC
           LIMIT ?''',
        (limit,), "all",
    )


def get_purchase(purchase_id):
    return _execute("SELECT * FROM purchases WHERE id = ?", (purchase_id,), "one")


# ── OTPs received per purchase (Purchase History → view messages) ───────────

def add_purchase_otp(purchase_id, text):
    if not purchase_id:
        return
    _execute("INSERT INTO purchase_otps (purchase_id, text) VALUES (?, ?)",
             (purchase_id, text))


def get_purchase_otps(purchase_id):
    return _execute(
        "SELECT * FROM purchase_otps WHERE purchase_id = ? ORDER BY received_at ASC",
        (purchase_id,), "all",
    )