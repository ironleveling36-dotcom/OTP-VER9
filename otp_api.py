"""
OTPDoctor API wrapper — async (httpx).

Changes in this version:
- OTP_POLL_INTERVAL reduced to 2 s for real-time SMS monitoring (Feature 8)
- get_all_sms() added: returns ALL SMS received so far for an activation (Feature 3)
- monitor_sms() async generator: yields new SmsMessage objects as they arrive,
  used by the multi-SMS flow to stream updates to the user in real time (Feature 3)
- Auto-cancel after OTP_TIMEOUT if no SMS received (Feature 5)
- cancel_number() signature unchanged; callers use it for manual & auto-cancel
"""

from __future__ import annotations

import asyncio
import logging
import httpx

from config import OTP_API_KEY, OTP_BASE_URL, OTP_POLL_INTERVAL, OTP_TIMEOUT
from storage import SmsMessage

logger = logging.getLogger(__name__)

_MAX_RETRY = 3   # retries on TRY_AGAIN or transient network errors


def _api_key() -> str:
    """
    Resolve the active OTPDoctor API key at call time.
    Priority: admin-configured key (admin_settings.otp_api_key) -> env/config default.
    Lets the admin change the key from the panel without editing code.
    """
    try:
        import database as _db
        k = _db.get_setting("otp_api_key", None)
        if k:
            return k
    except Exception:
        pass
    return OTP_API_KEY


# ── Shared helpers ─────────────────────────────────────────────────────────────

async def _get(params: dict) -> str:
    """GET → plain-text response."""
    p = dict(params)
    p["api_key"] = _api_key()
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(OTP_BASE_URL, params=p)
        r.raise_for_status()
        return r.text.strip()


async def _get_json(params: dict) -> dict:
    """GET → JSON response."""
    p = dict(params)
    p["api_key"] = _api_key()
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(OTP_BASE_URL, params=p)
        r.raise_for_status()
        return r.json()


# ── Balance ────────────────────────────────────────────────────────────────────

async def get_balance() -> str:
    """Returns balance as a string, or raises ValueError."""
    resp = await _get({"action": "getBalance"})
    if resp.startswith("ACCESS_BALANCE:"):
        return resp.split(":")[1]
    raise ValueError(resp)


# ── Countries ──────────────────────────────────────────────────────────────────

async def get_countries() -> dict:
    """Returns {code: name, ...}"""
    return await _get_json({"action": "getCountries"})


# ── Services ───────────────────────────────────────────────────────────────────

async def get_services(country: str) -> dict:
    """Returns {service_id: {service_name, service_price, server_name}, ...}"""
    return await _get_json({"action": "getServices", "country": country})


# ── Purchase Number ────────────────────────────────────────────────────────────

async def purchase_number(service_id: str, max_price: float = None) -> tuple[str, str]:
    """
    Returns (activation_id, phone_number).
    Raises ValueError on unrecoverable API errors.
    Retries up to _MAX_RETRY times on TRY_AGAIN.
    """
    params = {"action": "getNumber", "service": service_id}
    if max_price is not None:
        params["maxPrice"] = str(max_price)

    for _ in range(_MAX_RETRY):
        resp = await _get(params)
        if resp.startswith("ACCESS_NUMBER:"):
            parts = resp.split(":")
            return parts[1], parts[2]
        if resp == "TRY_AGAIN":
            await asyncio.sleep(3)
            continue
        raise ValueError(resp)

    raise ValueError("TRY_AGAIN — server temporarily unavailable, please retry later.")


# ── Cancel Number ──────────────────────────────────────────────────────────────

async def cancel_number(activation_id: str) -> bool:
    """Cancel an activation. Returns True if successfully cancelled."""
    try:
        resp = await _get({"action": "setStatus", "id": activation_id, "status": "8"})
        return "STATUS_CANCEL" in resp
    except Exception:
        return False


# ── Raw status check ───────────────────────────────────────────────────────────

async def check_status(activation_id: str) -> str:
    """
    Raw status string from the API. Callers interpret it.
    Returns e.g. "STATUS_WAIT_CODE", "STATUS_OK:123456", "STATUS_CANCEL",
    "STATUS_OK:1:123456 \n 789012" (multi-SMS).
    """
    try:
        return await _get({"action": "getStatus", "id": activation_id})
    except Exception as exc:
        logger.warning("check_status error for %s: %s", activation_id, exc)
        return "STATUS_WAIT_CODE"


# ── Parse SMS messages from a status response ──────────────────────────────────

def parse_sms_from_status(resp: str) -> list[str]:
    """
    OTPDoctor encodes multiple OTPs as:
      STATUS_OK:1:otp1 \n otp2 \n otp3
    or simply:
      STATUS_OK:otp1

    Returns list of individual OTP/SMS strings (stripped).
    Returns [] if no OTP present (STATUS_WAIT_CODE, STATUS_CANCEL, etc.).
    """
    if not resp.startswith("STATUS_OK:"):
        return []
    payload = resp[len("STATUS_OK:"):]
    # Strip leading index like "1:" if present
    if payload and payload[0].isdigit() and ":" in payload:
        payload = payload.split(":", 1)[1]
    # Split multi-SMS by literal "\n" separator (some APIs use actual newlines)
    parts = [p.strip() for p in payload.replace("\\n", "\n").split("\n") if p.strip()]
    return parts if parts else [payload.strip()]


# ── Multi-SMS monitor (async generator) ────────────────────────────────────────

async def monitor_sms(
    activation_id: str,
    first_timeout: int = OTP_TIMEOUT,
    total_timeout: int = None,
) -> "AsyncGenerator[SmsMessage | None, None]":
    """
    Async generator that yields:
      • SmsMessage(index, text)  — each time a NEW SMS arrives
      • None                     — when the activation expires/cancels with
                                   no first SMS (timeout signal)

    Args:
        activation_id : the activation to monitor
        first_timeout : seconds to wait for the VERY FIRST SMS before auto-cancel
        total_timeout : max seconds to keep monitoring after first SMS
                        (defaults to config.MULTI_SMS_TIMEOUT)

    Usage:
        async for msg in monitor_sms(act_id):
            if msg is None:
                # timed out with no OTP
                break
            # new SMS received
            await bot.send_message(chat_id, msg.text)
    """
    from config import MULTI_SMS_TIMEOUT
    if total_timeout is None:
        total_timeout = MULTI_SMS_TIMEOUT

    seen_sms: list[str] = []
    elapsed = 0
    got_first = False
    deadline_after_first = None

    while True:
        resp = await check_status(activation_id)

        if resp == "STATUS_CANCEL":
            # externally cancelled — stop but let caller decide what to show
            return

        new_texts = parse_sms_from_status(resp)

        for text in new_texts:
            if text not in seen_sms:
                seen_sms.append(text)
                idx = len(seen_sms)
                if not got_first:
                    got_first = True
                    deadline_after_first = asyncio.get_running_loop().time() + total_timeout
                yield SmsMessage(index=idx, text=text)

        # Auto-cancel: no first SMS within first_timeout
        if not got_first and elapsed >= first_timeout:
            yield None  # signal: timed out
            return

        # After first SMS: keep alive until total_timeout or cancellation
        if got_first and deadline_after_first is not None:
            if asyncio.get_running_loop().time() >= deadline_after_first:
                return  # number expired naturally

        await asyncio.sleep(OTP_POLL_INTERVAL)
        elapsed += OTP_POLL_INTERVAL


# ── Legacy single-OTP helper (kept for compatibility) ─────────────────────────

async def get_otp(activation_id: str) -> str | None:
    """
    Poll until OTP arrives or OTP_TIMEOUT is reached.
    Returns the first OTP string, or None on timeout / cancellation.
    """
    async for msg in monitor_sms(activation_id, first_timeout=OTP_TIMEOUT):
        if msg is None:
            return None
        return msg.text  # return first OTP only
    return None
