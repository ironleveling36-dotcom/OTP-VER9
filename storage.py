"""
storage.py — In-memory active-order state store.

Tracks:
  • recently_used  : deque of (service_id, service_name, price) per user
  • active_numbers : active activations per user  { activation_id: ActiveOrder }

All state is in-process (lost on restart). Wallet balances, services, and
transactions are persisted separately in database.py (SQLite).
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional


RECENTLY_USED_MAX = 5


@dataclass
class SmsMessage:
    """A single SMS received for an activation."""
    index: int
    text: str
    received_at: float = field(default_factory=time.time)


@dataclass
class ActiveOrder:
    """Represents a live activation being monitored."""
    activation_id: str
    phone: str
    service_id: str
    service_name: str
    price: str
    chat_id: int
    message_id: Optional[int]       = None
    sms_messages: list[SmsMessage]  = field(default_factory=list)
    is_cancelled: bool              = False
    is_expired: bool                = False
    cancel_requested: bool          = False
    created_at: float               = field(default_factory=time.time)
    status: str                     = "active"     # active|waiting|otp|cancelling|cancelled|expired
    is_swiggy: bool                 = False
    refunded: bool                  = False        # guard against double refunds

    def age_seconds(self) -> float:
        return time.time() - self.created_at


# ── Global state ───────────────────────────────────────────────────────────

# { user_id: deque[ (service_id, service_name, price) ] }
_recently_used: dict[int, deque] = {}

# { user_id: { activation_id: ActiveOrder } }
_active_orders: dict[int, dict[str, ActiveOrder]] = {}

# Lazy lock — created on first use inside running event loop
_lock: Optional[asyncio.Lock] = None


def _get_lock() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


# ── Recently Used ──────────────────────────────────────────────────────────

async def record_service_used(user_id: int, service_id: str, service_name: str, price: str) -> None:
    async with _get_lock():
        dq = _recently_used.setdefault(user_id, deque(maxlen=RECENTLY_USED_MAX))
        _recently_used[user_id] = deque(
            [e for e in dq if e[0] != service_id],
            maxlen=RECENTLY_USED_MAX,
        )
        _recently_used[user_id].appendleft((service_id, service_name, price))


async def get_recently_used(user_id: int) -> list[tuple[str, str, str]]:
    async with _get_lock():
        return list(_recently_used.get(user_id, []))


# ── Active Orders ──────────────────────────────────────────────────────────

async def add_active_order(user_id: int, order: ActiveOrder) -> None:
    async with _get_lock():
        _active_orders.setdefault(user_id, {})[order.activation_id] = order


async def get_active_order(user_id: int, activation_id: str) -> Optional[ActiveOrder]:
    async with _get_lock():
        return _active_orders.get(user_id, {}).get(activation_id)


async def remove_active_order(user_id: int, activation_id: str) -> None:
    async with _get_lock():
        _active_orders.get(user_id, {}).pop(activation_id, None)


async def get_all_active_orders(user_id: int) -> list[ActiveOrder]:
    async with _get_lock():
        return list(_active_orders.get(user_id, {}).values())


async def update_order(user_id: int, activation_id: str, **kwargs) -> None:
    async with _get_lock():
        order = _active_orders.get(user_id, {}).get(activation_id)
        if order:
            for k, v in kwargs.items():
                setattr(order, k, v)


# ── Admin global monitor ────────────────────────────────────────────────────

async def get_all_active_orders_global() -> list[tuple[int, "ActiveOrder"]]:
    """Return [(user_id, ActiveOrder), ...] across ALL users (admin monitor)."""
    async with _get_lock():
        out: list[tuple[int, ActiveOrder]] = []
        for uid, orders in _active_orders.items():
            for o in orders.values():
                out.append((uid, o))
        return out


# ── Duplicate-purchase guard (Stability #5) ─────────────────────────────────
# Tracks users currently in the middle of a purchase so the same person cannot
# fire two concurrent buys (double-debit / double provider call).
_purchasing: set[int] = set()

# Cooperative cancel flags for the purchase-search phase (before a number is
# secured). { user_id: True } means the user asked to abort the search.
_purchase_cancel: dict[int, bool] = {}


def try_begin_purchase(user_id: int) -> bool:
    """Atomically claim a purchase slot. Returns False if one is already running."""
    if user_id in _purchasing:
        return False
    _purchasing.add(user_id)
    _purchase_cancel.pop(user_id, None)
    return True


def end_purchase(user_id: int) -> None:
    _purchasing.discard(user_id)
    _purchase_cancel.pop(user_id, None)


def is_purchasing(user_id: int) -> bool:
    return user_id in _purchasing


def request_purchase_cancel(user_id: int) -> bool:
    """Signal the active purchase-search to stop. Returns True if one was running."""
    if user_id in _purchasing:
        _purchase_cancel[user_id] = True
        return True
    return False


def is_purchase_cancelled(user_id: int) -> bool:
    return _purchase_cancel.get(user_id, False)
