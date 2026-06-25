import os

# ── Telegram ──────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "123456789").split(",") if x.strip()]

# ── OTPDoctor ─────────────────────────────────────────────────
OTP_API_KEY  = os.getenv("OTP_API_KEY", "q6v6ef7r50mm4wkbkmq8a1ntxs8qx3wl")
OTP_BASE_URL = "https://www.otpdoctor.in/stubs/handler_api.php"

# ── Swiggy Checker ────────────────────────────────────────────
CHECKER_API_URL = os.getenv("CHECKER_API_URL", "https://checker.otpcart.xyz/api/check-swiggy")
# Default service id used by the Swiggy Checker (admin can change at runtime via panel)
SWIGGY_SERVICE_ID = os.getenv("SWIGGY_SERVICE_ID", "swiggy")

# ── Timing ────────────────────────────────────────────────────
OTP_POLL_INTERVAL  = 2     # seconds between status checks (real-time monitoring)
OTP_TIMEOUT        = 180   # wait up to 3 minutes (180 s) before auto-cancel & refund
MULTI_SMS_TIMEOUT  = 1200  # seconds to keep number alive after first OTP (20 min)

CANCEL_ALLOWED_AFTER = 180          # users may cancel only after 3 minutes (180 s)
SWIGGY_REGISTERED_CANCEL_DELAY = 300  # auto-cancel a registered Swiggy number after 5 min

# Swiggy Checker search engine
SWIGGY_RETRY_MAX   = 30   # max purchase+check cycles while hunting an unregistered number
SWIGGY_CHECK_DELAY = 1    # seconds between Swiggy search cycles (kept low for speed)

# ── Auto-Retry ────────────────────────────────────────────────
RETRY_INTERVAL = 2    # seconds between purchase retries
RETRY_MAX      = 10   # maximum retry attempts on provider errors (spec: max 10)
# Provider error keywords that trigger an auto-retry
RETRY_ERROR_KEYWORDS = [
    "NO_NUMBERS", "NO_NUMBER", "NO NUMBER", "NO STOCK", "NO_STOCK",
    "TRY_AGAIN", "TRY AGAIN", "TEMPORARY", "TEMP_ERROR",
    "PROVIDER", "LIMIT", "WAIT", "AVAILABLE", "BUSY",
]

# ── UX / Limits ───────────────────────────────────────────────
RECENTLY_USED_MAX  = 5    # max recently-used entries stored per user
