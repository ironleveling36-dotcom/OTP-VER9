from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

_MAX_SERVICES_PER_PAGE = 48
_CB_DATA_LIMIT = 64

def _safe_cb(data: str) -> str:
    return data.encode()[:_CB_DATA_LIMIT].decode(errors="ignore")

# ── Main menu ──────────────────────────────────────────────────

def main_menu_keyboard(top_services: list = None) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []

    # Pinned/highlighted Top Services if available
    if top_services:
        row: list[InlineKeyboardButton] = []
        for svc in top_services:
            name_label = f"🔥 {svc['service_name']} (₹{svc['service_price']})"
            cb_data = _safe_cb(f"service:{svc['service_id']}:{svc['service_name'][:25]}")
            row.append(InlineKeyboardButton(name_label, callback_data=cb_data))
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)

    buttons.extend([
        [InlineKeyboardButton("📱 Get OTP Number",  callback_data="menu:get_otp"),
         InlineKeyboardButton("🔍 Search",          callback_data="menu:search")],
        [InlineKeyboardButton("🍔 Swiggy New Numbers",  callback_data="menu:swiggy")],
        [InlineKeyboardButton("📋 Active Orders",   callback_data="menu:active"),
         InlineKeyboardButton("🕒 Recently Used",   callback_data="menu:recent")],
        [InlineKeyboardButton("🧾 Purchase History", callback_data="menu:history"),
         InlineKeyboardButton("💳 Wallet",          callback_data="menu:balance")],
        [InlineKeyboardButton("👥 Refer & Earn",    callback_data="menu:refer"),
         InlineKeyboardButton("🆘 Support",         callback_data="menu:support")],
    ])
    return InlineKeyboardMarkup(buttons)


def delivered_number_keyboard(activation_id: str, rebuy_sid: str = None,
                              rebuy_country: str = None, can_cancel: bool = False) -> InlineKeyboardMarkup:
    """Keyboard shown on a freshly delivered number — includes Rebuy Same Service."""
    cancel_label = "❌ Cancel Number" if can_cancel else "🔒 Cancel (after 3 min)"
    rows = [[
        InlineKeyboardButton("📩 Check SMS", callback_data=_safe_cb(f"refresh:{activation_id}")),
        InlineKeyboardButton(cancel_label,   callback_data=_safe_cb(f"cancel:{activation_id}")),
    ]]
    if rebuy_sid:
        rows.append([InlineKeyboardButton(
            "🔄 Rebuy Same Service",
            callback_data=_safe_cb(f"rebuy:{rebuy_sid}:{rebuy_country or 'in'}"))])
    rows.append([InlineKeyboardButton("🔙 Back to Menu", callback_data="back:main")])
    return InlineKeyboardMarkup(rows)


def force_join_keyboard(channel: str) -> InlineKeyboardMarkup:
    """Join + Verify gate. Accepts @username or a full t.me link."""
    ch = (channel or "").strip()
    if ch.startswith("https://t.me/") or ch.startswith("t.me/"):
        url = ch if ch.startswith("http") else f"https://{ch}"
    elif ch.startswith("@"):
        url = f"https://t.me/{ch.lstrip('@')}"
    else:
        url = f"https://t.me/{ch}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Join Channel", url=url)],
        [InlineKeyboardButton("✅ Verify Join", callback_data="verify_join")],
    ])


def purchase_history_keyboard(rows) -> InlineKeyboardMarkup:
    """Each recent purchase gets a button to view its received OTP messages."""
    buttons = []
    for r in rows:
        ph = str(r.get("phone") or "—")
        local = "".join(c for c in ph if c.isdigit())[-10:] if ph != "—" else "—"
        buttons.append([InlineKeyboardButton(
            f"📩 {r['service_name'].title()} • {local}",
            callback_data=_safe_cb(f"hist_otp:{r['id']}"))])
    buttons.append([InlineKeyboardButton("🔙 Back to Menu", callback_data="back:main")])
    return InlineKeyboardMarkup(buttons)


def support_keyboard(support_id: str = None) -> InlineKeyboardMarkup:
    """Support/contact section for users."""
    rows = []
    if support_id:
        handle = support_id.lstrip("@")
        # Only build a t.me deep link for plain @usernames (no spaces / urls)
        if handle and " " not in handle and "/" not in handle and "." not in handle:
            rows.append([InlineKeyboardButton(f"💬 Chat with {support_id}",
                                              url=f"https://t.me/{handle}")])
    rows.append([InlineKeyboardButton("🔙 Back to Menu", callback_data="back:main")])
    return InlineKeyboardMarkup(rows)


def purchase_searching_keyboard() -> InlineKeyboardMarkup:
    """Shown while a purchase is being secured — lets the user abort & get refunded."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛑 Cancel & Refund", callback_data="purchase:cancel")],
    ])

# ── Wallet / Recharge ───────────────────────────────────────────

def wallet_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 Recharge Wallet", callback_data="wallet:recharge")],
        [InlineKeyboardButton("📜 Transaction History", callback_data="wallet:tx_history")],
        [InlineKeyboardButton("🔙 Back to Menu", callback_data="back:main")]
    ])

def recharge_confirm_keyboard(amount: float, req_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ I Paid", callback_data=f"wallet:paid:{amount}:{req_id}")],
        [InlineKeyboardButton("❌ Cancel Request", callback_data=f"wallet:cancel_req:{req_id}")]
    ])

# ── Search ─────────────────────────────────────────────────────

def search_prompt_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Back to Menu", callback_data="back:main")],
    ])

# ── Recently Used ──────────────────────────────────────────────

def recently_used_keyboard(recent: list[tuple[str, str, str]]) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    for sid, sname, price in recent:
        label = f"🔄 {sname} ₹{price}"
        name_trunc = sname[:40]
        cb = _safe_cb(f"service:{sid}:{name_trunc}")
        buttons.append([InlineKeyboardButton(label, callback_data=cb)])

    buttons.append([InlineKeyboardButton("🔙 Back to Menu", callback_data="back:main")])
    return InlineKeyboardMarkup(buttons)

# ── Active numbers ─────────────────────────────────────────────

def active_numbers_keyboard(orders) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    for order in orders:
        label = f"📱 +{order.phone} — {order.service_name.title()}"
        cb = _safe_cb(f"view_active:{order.activation_id}")
        buttons.append([InlineKeyboardButton(label, callback_data=cb)])

    if not buttons:
        buttons.append([InlineKeyboardButton("(no active numbers)", callback_data="noop")])

    buttons.append([InlineKeyboardButton("🔙 Back to Menu", callback_data="back:main")])
    return InlineKeyboardMarkup(buttons)

def active_order_keyboard(activation_id: str, can_cancel: bool = True) -> InlineKeyboardMarkup:
    cancel_label = "❌ Cancel Number" if can_cancel else "🔒 Cancel (after 3 min)"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📩 Check SMS",        callback_data=_safe_cb(f"refresh:{activation_id}")),
            InlineKeyboardButton(cancel_label,           callback_data=_safe_cb(f"cancel:{activation_id}")),
        ],
        [InlineKeyboardButton("🔙 Back to Menu",         callback_data="back:main")],
    ])

# ── SMS list ────────────────────────────────────────────────────

def sms_list_keyboard(activation_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📩 Check SMS",     callback_data=_safe_cb(f"refresh:{activation_id}")),
            InlineKeyboardButton("❌ Cancel Number", callback_data=_safe_cb(f"cancel:{activation_id}")),
        ],
        [InlineKeyboardButton("🔙 Back to Menu", callback_data="back:main")],
    ])

# ── Countries ───────────────────────────────────────────────────

def countries_keyboard(countries: dict) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for code, name in countries.items():
        row.append(InlineKeyboardButton(
            f"🌍 {name}",
            callback_data=_safe_cb(f"country:{code}"),
        ))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(buttons)

# ── Services ────────────────────────────────────────────────────

def services_keyboard(
    services: dict,
    page: int = 0,
    search_query: str = "",
) -> InlineKeyboardMarkup:
    if search_query:
        q = search_query.lower()
        items = [
            (sid, info) for sid, info in services.items()
            if q in info.get("service_name", "").lower()
        ]
    else:
        items = list(services.items())

    # Sort items to display enabled first, and sorted alphabetically
    # Also filter out completely disabled local services
    filtered_items = []
    for sid, info in items:
        # If it's a local database format, check is_enabled
        if isinstance(info, dict) and info.get("is_enabled", 1) == 0:
            continue
        filtered_items.append((sid, info))

    total_pages = max(1, (len(filtered_items) + _MAX_SERVICES_PER_PAGE - 1) // _MAX_SERVICES_PER_PAGE)
    page = max(0, min(page, total_pages - 1))

    start = page * _MAX_SERVICES_PER_PAGE
    page_items = filtered_items[start: start + _MAX_SERVICES_PER_PAGE]

    buttons: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []

    for sid, info in page_items:
        name  = info["service_name"][:40]
        label = f"{info['service_name']} ₹{info['service_price']}"
        cb    = _safe_cb(f"service:{sid}:{name}")
        row.append(InlineKeyboardButton(label, callback_data=cb))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"svcpage:{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"svcpage:{page+1}"))
    if nav:
        buttons.append(nav)

    buttons.append([InlineKeyboardButton("🔙 Back", callback_data="back:countries")])
    return InlineKeyboardMarkup(buttons)

# ── Cancel Keyboard ──────────────────────────────────────────────

def cancel_keyboard(activation_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel Number", callback_data=_safe_cb(f"cancel:{activation_id}"))]
    ])

# ── Admin Panel Keyboards ────────────────────────────────────────

def admin_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Dashboard & Stats", callback_data="admin:dashboard")],
        [InlineKeyboardButton("⚙️ Services", callback_data="admin:services_menu"),
         InlineKeyboardButton("💼 User Wallets", callback_data="admin:users_menu")],
        [InlineKeyboardButton("🧾 Sales History", callback_data="admin:sales_history"),
         InlineKeyboardButton("📦 Purchases", callback_data="admin:purchase_history")],
        [InlineKeyboardButton("📡 Active Numbers Monitor", callback_data="admin:active_monitor")],
        [InlineKeyboardButton("📜 Transactions", callback_data="admin:tx_history"),
         InlineKeyboardButton("🪵 Logs", callback_data="admin:logs")],
        [InlineKeyboardButton("📢 Broadcast", callback_data="admin:notify_all"),
         InlineKeyboardButton("🆘 Support ID", callback_data="admin:support_cfg")],
        [InlineKeyboardButton("🔑 OTP API Key", callback_data="admin:api_cfg"),
         InlineKeyboardButton("👥 Referral Reward", callback_data="admin:referral_cfg")],
        [InlineKeyboardButton("📡 Force-Join Channel", callback_data="admin:channel_cfg")],
        [InlineKeyboardButton("💳 Update QR & UPI ID", callback_data="admin:payment_details")],
        [InlineKeyboardButton("🔙 Close Panel", callback_data="back:main")]
    ])


def admin_dashboard_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📈 Today's Sales", callback_data="admin:today_stats"),
         InlineKeyboardButton("💰 Total Revenue", callback_data="admin:revenue")],
        [InlineKeyboardButton("📊 Service-wise Report", callback_data="admin:service_sales")],
        [InlineKeyboardButton("🧾 Sales History", callback_data="admin:sales_history"),
         InlineKeyboardButton("📦 Purchases", callback_data="admin:purchase_history")],
        [InlineKeyboardButton("📡 Active Numbers Monitor", callback_data="admin:active_monitor")],
        [InlineKeyboardButton("🔄 Refresh Dashboard", callback_data="admin:dashboard")],
        [InlineKeyboardButton("🔙 Back to Admin", callback_data="admin:main")]
    ])

def admin_services_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Custom Service", callback_data="admin:service_add")],
        [InlineKeyboardButton("🔍 View/Manage Services List", callback_data="admin:service_list:0")],
        [InlineKeyboardButton("⭐ Manage Top Services", callback_data="admin:top_list:0")],
        [InlineKeyboardButton("🍔 Configure Swiggy Service ID", callback_data="admin:swiggy_cfg")],
        [InlineKeyboardButton("🔙 Back to Admin", callback_data="admin:main")]
    ])

def admin_edit_service_keyboard(sid: str, country: str, is_enabled: int, is_top: int) -> InlineKeyboardMarkup:
    en_label = "🟢 Enabled (Click to Disable)" if is_enabled else "🔴 Disabled (Click to Enable)"
    top_label = "⭐ Top Service (Remove)" if is_top else "☆ Mark as Top Service"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Change Price", callback_data=f"admin:svc_ep:{sid}:{country}"),
         InlineKeyboardButton("🆔 Change Service ID", callback_data=f"admin:svc_eid:{sid}:{country}")],
        [InlineKeyboardButton(en_label, callback_data=f"admin:svc_te:{sid}:{country}")],
        [InlineKeyboardButton(top_label, callback_data=f"admin:svc_tt:{sid}:{country}")],
        [InlineKeyboardButton("🗑️ Delete Service", callback_data=f"admin:svc_del:{sid}:{country}")],
        [InlineKeyboardButton("🔙 Back to List", callback_data="admin:service_list:0")]
    ])

def admin_users_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Search User (ID/Name)", callback_data="admin:user_search")],
        [InlineKeyboardButton("📋 View All Registered Users", callback_data="admin:user_list:0")],
        [InlineKeyboardButton("🔙 Back to Admin", callback_data="admin:main")]
    ])

def admin_user_detail_keyboard(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Balance (Credit)", callback_data=f"admin:user_cred:{uid}")],
        [InlineKeyboardButton("➖ Deduct Balance (Debit)", callback_data=f"admin:user_deb:{uid}")],
        [InlineKeyboardButton("📜 View Transaction Log", callback_data=f"admin:user_tx:{uid}")],
        [InlineKeyboardButton("🔙 Back to Users List", callback_data="admin:user_list:0")]
    ])

def admin_verify_keyboard(req_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Approve", callback_data=f"admin:approve:{req_id}")],
        [InlineKeyboardButton("❌ Reject", callback_data=f"admin:reject:{req_id}")]
    ])

def swiggy_checking_keyboard() -> InlineKeyboardMarkup:
    """Cancel button shown while the Swiggy Checker is hunting an unregistered number."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛑 Cancel Checking", callback_data="swiggy:cancel")],
    ])

def admin_top_services_keyboard(top_services: list) -> InlineKeyboardMarkup:
    """List current Top Services, each opens its detail (price/unpin)."""
    buttons: list[list[InlineKeyboardButton]] = []
    for s in top_services:
        buttons.append([InlineKeyboardButton(
            f"⭐ {s['service_name']} ({s['country']}) - ₹{s['service_price']}",
            callback_data=_safe_cb(f"admin:svc_dt:{s['service_id']}:{s['country']}")
        )])
    if not buttons:
        buttons.append([InlineKeyboardButton("(no top services yet)", callback_data="noop")])
    buttons.append([InlineKeyboardButton("➕ Pin from Services List", callback_data="admin:service_list:0")])
    buttons.append([InlineKeyboardButton("🔙 Back to Services Menu", callback_data="admin:services_menu")])
    return InlineKeyboardMarkup(buttons)
