"""
checkers.py — Swiggy Checker integration.

Calls the OTPCart Swiggy checker API to determine whether a mobile number is
already registered on Swiggy.

API:
    POST https://checker.otpcart.xyz/api/check-swiggy
    body: {"mobile": "XXXXXXXXXX"}   (10-digit local number, no country code)
    resp: {"status": "registered" | "unregistered", "mobile": "XXXXXXXXXX"}
"""

from __future__ import annotations

import logging
import httpx

from config import CHECKER_API_URL

logger = logging.getLogger(__name__)


def _local_number(phone: str) -> str:
    """Strip country code / non-digits → return the last 10 digits."""
    digits = "".join(ch for ch in str(phone) if ch.isdigit())
    if len(digits) > 10:
        digits = digits[-10:]
    return digits


async def check_swiggy(phone: str) -> str:
    """
    Returns one of:
      • "registered"   → number IS registered on Swiggy  → must be rejected
      • "unregistered" → number is NOT registered        → deliver to user
      • "error"        → the check could not be performed → caller should retry

    Rule: ONLY an explicit "registered" status is treated as registered.
    Any other valid 200 payload is treated as unregistered (deliverable).
    Network / HTTP / parse failures return "error" so the caller retries with a
    fresh number (this never rejects a genuinely unregistered number).
    """
    mobile = _local_number(phone)
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                CHECKER_API_URL,
                json={"mobile": mobile},
                headers={"Content-Type": "application/json"},
            )
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        logger.error("Swiggy check failed for %s: %s", mobile, e)
        return "error"

    status = str(data.get("status", "")).strip().lower()
    if status == "registered":
        logger.info("Swiggy check %s -> registered (reject)", mobile)
        return "registered"

    # Anything that is NOT explicitly "registered" is deliverable.
    logger.info("Swiggy check %s -> %s (deliver as unregistered)", mobile, status or "empty")
    return "unregistered"


async def is_swiggy_unregistered(phone: str) -> bool:
    """True unless the API explicitly says the number is 'registered'."""
    return (await check_swiggy(phone)) != "registered"


async def is_myntra_unregistered(phone: str) -> bool:
    raise NotImplementedError("Myntra Checker is not available.")
