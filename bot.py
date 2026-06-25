"""
OTPCart Telegram Bot — main entry point (Upgraded with Wallet, Admin Panel, and Auto-Retry).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest

import otp_api
import storage
import config
import database as db
import checkers

from keyboards import (
    countries_keyboard,
    services_keyboard,
    cancel_keyboard,
    main_menu_keyboard,
    search_prompt_keyboard,
    recently_used_keyboard,
    active_numbers_keyboard,
    active_order_keyboard,
    sms_list_keyboard,

    # Wallet & Admin Keyboards
    wallet_keyboard,
    recharge_confirm_keyboard,
    admin_main_keyboard,
    admin_dashboard_keyboard,
    admin_services_keyboard,
    admin_edit_service_keyboard,
    admin_users_keyboard,
    admin_user_detail_keyboard,
    admin_verify_keyboard,
    admin_top_services_keyboard,
    swiggy_checking_keyboard,
    support_keyboard,
    purchase_searching_keyboard,
    delivered_number_keyboard,
    force_join_keyboard,
    purchase_history_keyboard,
)
from config import (
    BOT_TOKEN, OTP_TIMEOUT, CANCEL_ALLOWED_AFTER,
    RETRY_INTERVAL, RETRY_MAX, RETRY_ERROR_KEYWORDS,
    SWIGGY_REGISTERED_CANCEL_DELAY, SWIGGY_RETRY_MAX, SWIGGY_CHECK_DELAY,
)


# ── Swiggy live search sessions: user_id -> session dict ──────────
# { user_id: {"cancelled": bool, "active_act_id": str|None, "msg": Message|None} }
_swiggy_sessions: dict[int, dict] = {}


# ── Helpers: number formatting (display vs copy) ──────────────────
def _local_number(phone: str) -> str:
    """Return only the local mobile number (last 10 digits, no country code)."""
    digits = "".join(ch for ch in str(phone) if ch.isdigit())
    return digits[-10:] if len(digits) > 10 else digits

def _country_code(phone: str) -> str:
    digits = "".join(ch for ch in str(phone) if ch.isdigit())
    return digits[:-10] if len(digits) > 10 else ""

def _fmt_number(phone: str) -> str:
    """Display: '+91 9876543210' with the local number in a tap-to-copy code span."""
    cc = _country_code(phone)
    local = _local_number(phone)
    prefix = f"+{cc} " if cc else ""
    return f"{prefix}`{local}`"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── State keys stored in ctx.user_data ────────────────────────────
_K_COUNTRY  = "country"
_K_SERVICES = "services"
_K_AWAITING_SEARCH = "awaiting_search"   # bool: next text message is a search query

# Helper to load and sync services with database
async def get_and_sync_services(country_code: str) -> dict:
    try:
        api_services = await otp_api.get_services(country_code)
    except Exception:
        api_services = {}

    local_services = db.get_services(country_code)

    # Merge API services into DB if not present
    for sid, info in api_services.items():
        if sid not in local_services:
            db.add_service(
                service_id=sid,
                country=country_code,
                service_name=info.get("service_name", "Unknown"),
                service_price=float(info.get("service_price", 0.0)),
                is_enabled=1,
                is_top=0
            )

    # Fetch final updated list of enabled services from local DB
    all_local = db.get_services(country_code)
    return {
        sid: {
            "service_name": info["service_name"],
            "service_price": info["service_price"]
        } for sid, info in all_local.items() if info["is_enabled"]
    }

async def _bg_sync_services(country_code: str) -> None:
    """Background task: sync services from API without blocking the user."""
    try:
        await get_and_sync_services(country_code)
    except Exception:
        pass

# ── Commands ──────────────────────────────────────────────────────

async def _is_member(bot, channel: str, user_id: int) -> bool:
    """
    True if the user is a member of the required channel.
    Fails OPEN on lookup errors (misconfig / bot not admin) but an explicit
    'left'/'kicked' status correctly blocks access.
    """
    if not channel:
        return True
    ch = channel.strip()
    if ch.startswith("https://t.me/") or ch.startswith("t.me/"):
        ch = "@" + ch.rsplit("/", 1)[-1]
    if not ch.startswith("@") and not ch.lstrip("-").isdigit():
        ch = "@" + ch
    try:
        member = await bot.get_chat_member(chat_id=ch, user_id=user_id)
        return member.status in ("member", "administrator", "creator", "owner")
    except Exception as e:
        logger.warning("Force-join check failed for channel=%s user=%s: %s", ch, user_id, e)
        return True  # fail open


async def _force_join_blocked(bot, user_id: int) -> bool:
    """Returns True (caller should stop) if the user must still join the channel."""
    if user_id in config.ADMIN_IDS or db.is_admin(user_id):
        return False
    channel = db.get_setting("force_channel", None)
    if not channel:
        return False
    return not await _is_member(bot, channel, user_id)


async def _send_join_gate(target, bot) -> None:
    channel = db.get_setting("force_channel", "")
    text = (
        "🔒 *Access Locked*\n\n"
        f"To use this bot you must first join our channel:\n*{channel}*\n\n"
        "1️⃣ Tap *📢 Join Channel*\n"
        "2️⃣ Then tap *✅ Verify Join*"
    )
    kb = force_join_keyboard(channel)
    try:
        if hasattr(target, "edit_message_text"):
            await target.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        else:
            await target.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    except Exception:
        pass


async def _process_referral(bot, user_id: int, username: str, ctx) -> None:
    """Credit the referrer once when a brand-new user starts via a ref link."""
    args = getattr(ctx, "args", None) or []
    if not args:
        return
    payload = str(args[0])
    if not payload.startswith("ref_"):
        return
    try:
        referrer_id = int(payload[4:])
    except ValueError:
        return
    if referrer_id == user_id or not db.user_exists(referrer_id):
        return
    if not db.set_referred_by(user_id, referrer_id):
        return  # already referred → no duplicate credit
    try:
        reward = float(db.get_setting("referral_reward", "1") or 1)
    except ValueError:
        reward = 1.0
    if reward <= 0:
        return
    new_bal = db.credit_wallet(referrer_id, reward, f"Referral bonus: {username}")
    db.add_referral_earning(referrer_id, reward)
    db.add_log("referral", f"Referral credit ₹{reward:.2f} to {referrer_id} for new user {user_id}", referrer_id)
    try:
        await bot.send_message(
            chat_id=referrer_id,
            text=(f"🎉 *Referral Reward!*\n\n"
                  f"A new user joined using your link.\n"
                  f"💰 `₹{reward:.2f}` added to your wallet.\n"
                  f"💳 Balance: `₹{new_bal:.2f}`"),
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception:
        pass


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name or f"User_{user_id}"
    is_admin = 1 if user_id in config.ADMIN_IDS else 0

    # Referral must run BEFORE the user row exists (detects brand-new users)
    is_new_user = not db.user_exists(user_id)
    db.add_user(user_id, username, is_admin)
    if is_new_user:
        try:
            await _process_referral(ctx.bot, user_id, username, ctx)
        except Exception as e:
            logger.warning("Referral processing failed: %s", e)

    # Force-Join gate
    if await _force_join_blocked(ctx.bot, user_id):
        await _send_join_gate(update.message, ctx.bot)
        return

    ctx.user_data[_K_AWAITING_SEARCH] = False

    country_code = ctx.user_data.get(_K_COUNTRY, "in")

    # Fire-and-forget: sync services in background so the menu appears instantly.
    # Users won't wait for the OTPDoctor API call on every /start.
    asyncio.create_task(_bg_sync_services(country_code))

    top_svcs = db.get_top_services(country_code)

    await update.message.reply_text(
        f"👋 *Welcome to OTPCart, {update.effective_user.first_name}!*\n\n"
        f"Recharge your wallet and get instant phone numbers for OTP verification. "
        f"Select an option below to get started.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_keyboard(top_services=top_svcs),
    )

async def balance_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name or f"User_{user_id}"
    is_admin = 1 if user_id in config.ADMIN_IDS else 0
    db.add_user(user_id, username, is_admin)

    balance = db.get_user_balance(user_id)
    text = f"💳 *Your Wallet Balance:* `₹{balance:.2f}`\n\nChoose an option below to manage your funds."

    if db.is_admin(user_id):
        try:
            api_bal = await otp_api.get_balance()
            text += f"\n\n⚙️ *Admin Note:* Provider API Balance: `₹{api_bal}`"
        except Exception:
            pass

    await update.message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=wallet_keyboard()
    )

async def admin_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not db.is_admin(user_id):
        return
    await update.message.reply_text(
        "⚙️ *Admin Control Panel*\n\nWelcome back, Admin! Select an option below to manage the system.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=admin_main_keyboard()
    )

# ── Text Message Handler (Search + Wallet + Admin States) ──────────

async def text_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    text = update.message.text.strip()

    # 0. User: Submitting Transaction ID for a recharge (Admin Approval system)
    if ctx.user_data.get("awaiting_txn_id"):
        ctx.user_data["awaiting_txn_id"] = False
        amount = float(ctx.user_data.pop("txn_amount", 0) or 0)
        req_id = ctx.user_data.pop("txn_req_id", None)
        txn_id = text.strip()
        if not req_id:
            await update.message.reply_text("⚠️ Session expired. Please start the recharge again.",
                                            reply_markup=main_menu_keyboard())
            return
        if len(txn_id) < 4:
            ctx.user_data["awaiting_txn_id"] = True
            ctx.user_data["txn_amount"] = amount
            ctx.user_data["txn_req_id"] = req_id
            await update.message.reply_text("⚠️ That doesn't look like a valid Transaction ID. Please re-enter it.")
            return

        db.set_recharge_txn(req_id, txn_id)
        db.update_recharge_request(req_id, "pending")
        db.add_log("recharge", f"User submitted txn {txn_id} for req #{req_id} (₹{amount:.2f})", user_id)

        await update.message.reply_text(
            f"⏳ *Payment Submitted!*\n\n"
            f"💰 Amount: `₹{amount:.2f}`\n"
            f"🧾 Transaction ID: `{txn_id}`\n"
            f"🆔 Request ID: `{req_id}`\n\n"
            f"Your payment has been sent to the admin for verification. "
            f"Your wallet will be credited *only after approval*.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_menu_keyboard()
        )

        uname = update.effective_user.username or update.effective_user.first_name
        admin_alert = (
            f"🔔 *New Payment Alert!*\n\n"
            f"👤 *User:* {uname} (ID: `{user_id}`)\n"
            f"💰 *Amount:* `₹{amount:.2f}`\n"
            f"🧾 *Transaction ID:* `{txn_id}`\n"
            f"🆔 *Request ID:* `{req_id}`\n\n"
            f"Verify the payment, then Approve or Reject below."
        )
        for admin_id in config.ADMIN_IDS:
            try:
                await ctx.bot.send_message(
                    chat_id=admin_id, text=admin_alert,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=admin_verify_keyboard(req_id),
                )
            except Exception:
                pass
        return

    # 1. User: Awaiting Recharge Amount
    if ctx.user_data.get("awaiting_recharge_amount"):
        ctx.user_data["awaiting_recharge_amount"] = False
        try:
            amount = float(text)
            if amount < 10 or amount > 10000:
                raise ValueError()
        except ValueError:
            ctx.user_data["awaiting_recharge_amount"] = True
            await update.message.reply_text("⚠️ Please enter a valid number between 10 and 10000.")
            return

        req_id = db.create_recharge_request(user_id, amount)
        settings = db.get_admin_settings()
        upi_id = settings.get("upi_id", "notset@upi")
        qr_file_id = settings.get("qr_file_id", "")

        instructions = (
            f"📥 *Wallet Recharge Request Created*\n\n"
            f"💰 *Amount to Pay:* `₹{amount:.2f}`\n"
            f"💳 *UPI ID:* `{upi_id}`\n\n"
            f"📌 *Instructions:*\n"
            f"1. Open your preferred payment app (GPay, PhonePe, Paytm, etc.).\n"
            f"2. Pay exactly `₹{amount:.2f}` to the UPI ID above or scan the QR code.\n"
            f"3. Once successful, tap the **✅ I Paid** button below.\n\n"
            f"⏳ _Admin will verify and credit your wallet shortly after payment submission._"
        )

        kb = recharge_confirm_keyboard(amount, req_id)
        if qr_file_id:
            try:
                await update.message.reply_photo(
                    photo=qr_file_id,
                    caption=instructions,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=kb
                )
            except Exception:
                await update.message.reply_text(
                    instructions,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=kb
                )
        else:
            await update.message.reply_text(
                instructions,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb
            )
        return

    # 2. Admin: Awaiting Broadcast Notification
    if ctx.user_data.get("awaiting_admin_noti"):
        ctx.user_data["awaiting_admin_noti"] = False
        if not db.is_admin(user_id): return

        users = db.get_all_users()
        sent_count = 0
        for u in users:
            try:
                await ctx.bot.send_message(
                    chat_id=u["user_id"],
                    text=f"📢 *Notification from Admin:*\n\n{text}",
                    parse_mode=ParseMode.MARKDOWN
                )
                sent_count += 1
            except Exception:
                pass
        await update.message.reply_text(
            f"📢 *Broadcast Complete!*\n\nSuccessfully sent to `{sent_count}` out of `{len(users)}` users.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=admin_main_keyboard()
        )
        return

    # 3. Admin: Awaiting UPI Update
    if ctx.user_data.get("awaiting_admin_upi"):
        ctx.user_data["awaiting_admin_upi"] = False
        if not db.is_admin(user_id): return

        db.update_admin_setting("upi_id", text)
        db.add_log("admin", f"UPI updated to {text}", user_id)
        await update.message.reply_text(
            f"✅ *UPI ID Updated successfully!*\n\nNew UPI ID: `{text}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=admin_main_keyboard()
        )
        return

    # 3b. Admin: Awaiting Swiggy Service ID
    if ctx.user_data.get("awaiting_admin_swiggy_id"):
        ctx.user_data["awaiting_admin_swiggy_id"] = False
        if not db.is_admin(user_id): return

        db.update_admin_setting("swiggy_service_id", text.strip())
        db.add_log("admin", f"Swiggy service id set to {text.strip()}", user_id)
        await update.message.reply_text(
            f"✅ *Swiggy Checker Service ID updated!*\n\nNow using provider Service ID: `{text.strip()}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=admin_services_keyboard()
        )
        return

    # 3c. Admin: Awaiting Customer Support ID
    if ctx.user_data.get("awaiting_admin_support_id"):
        ctx.user_data["awaiting_admin_support_id"] = False
        if not db.is_admin(user_id): return
        db.update_admin_setting("support_id", text.strip())
        db.add_log("admin", f"Support ID set to {text.strip()}", user_id)
        await update.message.reply_text(
            f"✅ *Customer Support ID updated!*\n\nUsers will now see: `{text.strip()}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=admin_main_keyboard()
        )
        return

    # 3e. Admin: Awaiting OTPDoctor API Key
    if ctx.user_data.get("awaiting_admin_api_key"):
        ctx.user_data["awaiting_admin_api_key"] = False
        if not db.is_admin(user_id): return
        new_key = text.strip()
        if len(new_key) < 6:
            await update.message.reply_text("⚠️ That key looks too short. Update cancelled.",
                                            reply_markup=admin_main_keyboard())
            return
        db.update_admin_setting("otp_api_key", new_key)
        db.add_log("admin", "OTPDoctor API key updated", user_id)
        masked = new_key[:4] + "•••" + new_key[-4:]
        # Live validation: try a balance call with the new key
        verify = ""
        try:
            bal = await otp_api.get_balance()
            verify = f"\n\n✅ Verified — provider balance: `₹{bal}`"
        except Exception as e:
            verify = f"\n\n⚠️ Saved, but a test call failed: `{str(e)[:60]}`"
        await update.message.reply_text(
            f"✅ *OTPDoctor API Key updated!*\n\nNew key: `{masked}`{verify}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=admin_main_keyboard()
        )
        return

    # 3f. Admin: Awaiting Force-Join Channel
    if ctx.user_data.get("awaiting_admin_channel"):
        ctx.user_data["awaiting_admin_channel"] = False
        if not db.is_admin(user_id): return
        val = text.strip()
        if val.lower() in ("off", "none", "disable", "-"):
            db.update_admin_setting("force_channel", "")
            db.add_log("admin", "Force-join channel disabled", user_id)
            await update.message.reply_text("✅ *Force-Join disabled.* Users can access the bot freely.",
                                            parse_mode=ParseMode.MARKDOWN, reply_markup=admin_main_keyboard())
            return
        db.update_admin_setting("force_channel", val)
        db.add_log("admin", f"Force-join channel set to {val}", user_id)
        await update.message.reply_text(
            f"✅ *Force-Join channel set!*\n\nUsers must now join: `{val}`\n\n"
            f"⚠️ _Make sure this bot is an *admin* of that channel so it can verify membership._",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=admin_main_keyboard()
        )
        return

    # 3g. Admin: Awaiting Referral Reward amount
    if ctx.user_data.get("awaiting_admin_referral_reward"):
        ctx.user_data["awaiting_admin_referral_reward"] = False
        if not db.is_admin(user_id): return
        try:
            reward = float(text)
            if reward < 0: raise ValueError()
        except ValueError:
            await update.message.reply_text("❌ Invalid amount. Update cancelled.",
                                            reply_markup=admin_main_keyboard())
            return
        db.update_admin_setting("referral_reward", str(reward))
        db.add_log("admin", f"Referral reward set to ₹{reward:.2f}", user_id)
        await update.message.reply_text(
            f"✅ *Referral reward updated!*\n\nEach successful referral now credits `₹{reward:.2f}`.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=admin_main_keyboard()
        )
        return

    # 3d. Admin: Awaiting NEW Service ID (Service ID change — requirement #2)
    if ctx.user_data.get("awaiting_admin_svc_newid"):
        ctx.user_data["awaiting_admin_svc_newid"] = False
        if not db.is_admin(user_id): return
        old = ctx.user_data.pop("awaiting_admin_svc_newid_old", (None, None))
        old_sid, country = old
        if not old_sid:
            return
        new_sid = text.strip().lower()
        try:
            ok, msg = db.change_service_id(old_sid, new_sid, country)
        except Exception as e:
            ok, msg = False, f"Database error: {e}"
        if ok:
            db.add_log("admin", f"Service ID changed {old_sid}->{new_sid} ({country})", user_id)
            await update.message.reply_text(
                f"✅ *Service ID changed successfully!*\n\n"
                f"🆔 Old ID `{old_sid}` removed completely.\n"
                f"🆕 New ID: `{new_sid}` ({country})\n\n"
                f"_{msg}_",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=admin_services_keyboard()
            )
        else:
            await update.message.reply_text(
                f"❌ *Could not change Service ID.*\n\n{msg}",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=admin_services_keyboard()
            )
        return

    # 4. Admin: Awaiting User Search
    if ctx.user_data.get("awaiting_admin_user_search"):
        ctx.user_data["awaiting_admin_user_search"] = False
        if not db.is_admin(user_id): return

        users = db.get_all_users()
        matches = [u for u in users if text.lower() in str(u["user_id"]) or text.lower() in str(u["username"]).lower()]

        if not matches:
            await update.message.reply_text(
                "❌ No matching users found.",
                reply_markup=admin_users_keyboard()
            )
            return

        text_out = "🔍 *Search Results:*\n\n"
        buttons = []
        for u in matches[:20]:
            text_out += f"👤 *{u['username']}* | ID: `{u['user_id']}` | Bal: `₹{u['balance']:.2f}`\n"
            buttons.append([InlineKeyboardButton(f"👤 {u['username']} (₹{u['balance']:.2f})", callback_data=f"admin:user_dt:{u['user_id']}")])

        buttons.append([InlineKeyboardButton("🔙 Back to Users Menu", callback_data="admin:users_menu")])
        await update.message.reply_text(
            text_out,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    # 5. Admin: Awaiting Add Balance Amount
    if ctx.user_data.get("awaiting_admin_add_balance_amt"):
        ctx.user_data["awaiting_admin_add_balance_amt"] = False
        if not db.is_admin(user_id): return

        target_uid = ctx.user_data.pop("awaiting_admin_add_balance_uid", None)
        if not target_uid: return

        try:
            amount = float(text)
            if amount <= 0: raise ValueError()
        except ValueError:
            await update.message.reply_text("❌ Invalid amount. Transaction cancelled.", reply_markup=admin_users_keyboard())
            return

        new_bal = db.credit_wallet(target_uid, amount, "Credited by Admin")
        try:
            await ctx.bot.send_message(
                chat_id=target_uid,
                text=f"🎁 *Admin Credit!*\n\nThe admin has credited your wallet with `₹{amount:.2f}`!\n💰 *Current Balance:* `₹{new_bal:.2f}`",
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception:
            pass

        await update.message.reply_text(
            f"✅ Successfully credited `₹{amount:.2f}` to user `{target_uid}`. New balance: `₹{new_bal:.2f}`.",
            reply_markup=admin_users_keyboard()
        )
        return

    # 6. Admin: Awaiting Deduct Balance Amount
    if ctx.user_data.get("awaiting_admin_sub_balance_amt"):
        ctx.user_data["awaiting_admin_sub_balance_amt"] = False
        if not db.is_admin(user_id): return

        target_uid = ctx.user_data.pop("awaiting_admin_sub_balance_uid", None)
        if not target_uid: return

        try:
            amount = float(text)
            if amount <= 0: raise ValueError()
        except ValueError:
            await update.message.reply_text("❌ Invalid amount. Transaction cancelled.", reply_markup=admin_users_keyboard())
            return

        new_bal = db.debit_wallet(target_uid, amount, "Debited by Admin")
        try:
            await ctx.bot.send_message(
                chat_id=target_uid,
                text=f"📉 *Admin Debit!*\n\nThe admin has deducted `₹{amount:.2f}` from your wallet.\n💰 *Current Balance:* `₹{new_bal:.2f}`",
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception:
            pass

        await update.message.reply_text(
            f"✅ Successfully debited `₹{amount:.2f}` from user `{target_uid}`. New balance: `₹{new_bal:.2f}`.",
            reply_markup=admin_users_keyboard()
        )
        return

    # 7. Admin: Awaiting Price Change Value
    if ctx.user_data.get("awaiting_admin_svc_price_val"):
        ctx.user_data["awaiting_admin_svc_price_val"] = False
        if not db.is_admin(user_id): return

        sid, country = ctx.user_data.pop("awaiting_admin_svc_price_sid_country", (None, None))
        if not sid: return

        try:
            price = float(text)
            if price < 0: raise ValueError()
        except ValueError:
            await update.message.reply_text("❌ Invalid price. Modification cancelled.", reply_markup=admin_services_keyboard())
            return

        db.edit_service_price(sid, country, price)
        await update.message.reply_text(
            f"✅ Price for service `{sid}` ({country}) has been updated to `₹{price:.2f}`.",
            reply_markup=admin_services_keyboard()
        )
        return

    # 8. Admin: Adding service ID
    if ctx.user_data.get("awaiting_admin_add_svc_id"):
        ctx.user_data["awaiting_admin_add_svc_id"] = False
        if not db.is_admin(user_id): return
        ctx.user_data["add_svc_id"] = text.lower()
        ctx.user_data["awaiting_admin_add_svc_country"] = True
        await update.message.reply_text("🌍 Enter Country Code (e.g., `in`):")
        return

    # 9. Admin: Adding country
    if ctx.user_data.get("awaiting_admin_add_svc_country"):
        ctx.user_data["awaiting_admin_add_svc_country"] = False
        if not db.is_admin(user_id): return
        ctx.user_data["add_svc_country"] = text.lower()
        ctx.user_data["awaiting_admin_add_svc_name"] = True
        await update.message.reply_text("✏️ Enter Service Name (e.g., `Google`):")
        return

    # 10. Admin: Adding name
    if ctx.user_data.get("awaiting_admin_add_svc_name"):
        ctx.user_data["awaiting_admin_add_svc_name"] = False
        if not db.is_admin(user_id): return
        ctx.user_data["add_svc_name"] = text
        ctx.user_data["awaiting_admin_add_svc_price"] = True
        await update.message.reply_text("💰 Enter Price (in ₹):")
        return

    # 11. Admin: Adding price
    if ctx.user_data.get("awaiting_admin_add_svc_price"):
        ctx.user_data["awaiting_admin_add_svc_price"] = False
        if not db.is_admin(user_id): return

        try:
            price = float(text)
            if price < 0: raise ValueError()
        except ValueError:
            await update.message.reply_text("❌ Invalid price. Cancelled.", reply_markup=admin_services_keyboard())
            return

        sid = ctx.user_data.pop("add_svc_id")
        country = ctx.user_data.pop("add_svc_country")
        name = ctx.user_data.pop("add_svc_name")

        db.add_service(sid, country, name, price, is_enabled=1, is_top=0)
        await update.message.reply_text(
            f"✅ Custom Service added successfully!\n\n"
            f"🆔 ID: `{sid}`\n"
            f"🌍 Country: `{country}`\n"
            f"✏️ Name: `*{name}*`\n"
            f"💰 Price: `₹{price:.2f}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=admin_services_keyboard()
        )
        return

    # Fallback to search query
    if ctx.user_data.get(_K_AWAITING_SEARCH):
        ctx.user_data[_K_AWAITING_SEARCH] = False
        query_text = text
        country_code = ctx.user_data.get(_K_COUNTRY, "in")

        if not query_text:
            await update.message.reply_text("⚠️ Empty search query. Try again.")
            return

        try:
            services = await get_and_sync_services(country_code)
            ctx.user_data[_K_SERVICES] = services
        except Exception as e:
            await update.message.reply_text(f"❌ Could not load services: {e}")
            return

        kb = services_keyboard(services, page=0, search_query=query_text)
        await update.message.reply_text(
            f"🔍 *Search results for* `{query_text}`:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb,
        )
        return

    # Default fallback
    await update.message.reply_text(
        "Use /start to open the menu.",
        reply_markup=main_menu_keyboard(),
    )

# ── General Photo Handler (for QR Photo Updates) ───────────────────

async def photo_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not db.is_admin(user_id): return

    if ctx.user_data.get("awaiting_admin_qr"):
        ctx.user_data["awaiting_admin_qr"] = False
        file_id = update.message.photo[-1].file_id
        db.update_admin_setting("qr_file_id", file_id)
        await update.message.reply_text(
            "✅ *QR Code Photo Updated successfully!*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=admin_main_keyboard()
        )

# ── Callback Router ────────────────────────────────────────────────

async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    data = query.data
    user_id = query.from_user.id
    chat_id = query.message.chat_id

    # Register/Verify user
    username = query.from_user.username or query.from_user.first_name or f"User_{user_id}"
    is_admin = 1 if user_id in config.ADMIN_IDS else 0
    db.add_user(user_id, username, is_admin)

    # ── Main menu callbacks ─────────────────────────────────────
    # Force-Join gate
    if data == "verify_join":
        if await _force_join_blocked(ctx.bot, user_id):
            await query.answer("❌ You haven't joined yet. Please join the channel first.", show_alert=True)
            await _send_join_gate(query, ctx.bot)
        else:
            top_svcs = db.get_top_services(ctx.user_data.get(_K_COUNTRY, "in"))
            await query.answer("✅ Verified! Welcome aboard.", show_alert=False)
            try:
                await query.edit_message_text(
                    f"👋 *Welcome to OTPCart, {query.from_user.first_name}!*\n\n"
                    f"You're verified. Select an option below to get started.",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=main_menu_keyboard(top_services=top_svcs),
                )
            except BadRequest:
                pass
        return

    if await _force_join_blocked(ctx.bot, user_id):
        await _send_join_gate(query, ctx.bot)
        return

    if data == "menu:get_otp":
        ctx.user_data[_K_AWAITING_SEARCH] = False
        try:
            countries = await otp_api.get_countries()
        except Exception as e:
            await query.edit_message_text(f"❌ Could not load countries: {e}")
            return
        await query.edit_message_text(
            "🌍 *Select Country*\n\nChoose the country for the phone number:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=countries_keyboard(countries),
        )

    elif data == "menu:swiggy":
        await start_swiggy_flow(query, ctx)

    elif data == "swiggy:cancel":
        sess = _swiggy_sessions.get(user_id)
        if sess and not sess.get("done"):
            sess["cancelled"] = True
            await query.answer("🛑 Stopping the Swiggy check… no charge.", show_alert=False)
            try:
                await query.edit_message_text(
                    "🛑 *Cancelling Swiggy check…* stopping retries and releasing any number.",
                    parse_mode=ParseMode.MARKDOWN,
                )
            except BadRequest:
                pass
        else:
            await query.answer("No active Swiggy check to cancel.", show_alert=True)

    elif data == "purchase:cancel":
        # Abort an in-progress purchase search → flow refunds and notifies.
        if storage.request_purchase_cancel(user_id):
            await query.answer("🛑 Cancelling… you'll be refunded.", show_alert=False)
            try:
                await query.edit_message_text(
                    "🛑 *Cancelling purchase…* releasing the request and refunding your balance.",
                    parse_mode=ParseMode.MARKDOWN,
                )
            except BadRequest:
                pass
        else:
            await query.answer("No active purchase to cancel.", show_alert=True)

    elif data == "menu:balance":
        balance = db.get_user_balance(user_id)
        text = f"💳 *Your Wallet Balance:* `₹{balance:.2f}`\n\nChoose an option below to manage your funds."
        if db.is_admin(user_id):
            try:
                api_bal = await otp_api.get_balance()
                text += f"\n\n⚙️ *Admin Note:* Provider API Balance: `₹{api_bal}`"
            except Exception:
                pass
        await query.edit_message_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=wallet_keyboard()
        )

    elif data == "menu:search":
        ctx.user_data[_K_AWAITING_SEARCH] = True
        country_code = ctx.user_data.get(_K_COUNTRY, "in")

        # Prefetch services in background
        if _K_SERVICES not in ctx.user_data:
            try:
                services = await get_and_sync_services(country_code)
                ctx.user_data[_K_SERVICES] = services
            except Exception:
                pass

        await query.edit_message_text(
            "🔍 *Search Service*\n\nPlease type the name of the service you want to search (e.g. `Google`, `Telegram`, `WhatsApp`):",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=search_prompt_keyboard(),
        )

    elif data == "menu:recent":
        recent = await storage.get_recently_used(user_id)
        if not recent:
            await query.edit_message_text(
                "🕒 *Recently Used Services*\n\nYou haven't purchased any numbers yet.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=main_menu_keyboard(),
            )
            return
        await query.edit_message_text(
            "🕒 *Recently Used Services*\n\nClick any option to quickly reorder:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=recently_used_keyboard(recent),
        )

    elif data == "menu:active":
        orders = await storage.get_all_active_orders(user_id)
        live_orders = [o for o in orders if not o.is_cancelled and not o.is_expired]
        await query.edit_message_text(
            f"📋 *Active Numbers ({len(live_orders)})*\n\nSelect an active number to view its status:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=active_numbers_keyboard(live_orders),
        )

    elif data == "menu:history":
        rows = db.get_user_purchases(user_id, limit=5)
        if not rows:
            await query.edit_message_text(
                "\U0001f9fe *Purchase History*\n\nYou haven't purchased any numbers yet.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=main_menu_keyboard(),
            )
            return
        _icons = {"completed": "\u2705", "active": "\u23f3", "pending": "\u23f3",
                  "cancelled": "\U0001f6d1", "expired": "\U0001f7e1", "failed": "\u274c"}
        out = "\U0001f9fe *Your Last 5 Purchases*\n\n"
        for r in rows:
            ic = _icons.get(r["status"], "\u2022")
            ph = _fmt_number(r["phone"]) if r["phone"] else "\u2014"
            refund_tag = " _(refunded)_" if r["refunded"] else ""
            out += (f"{ic} *{r['service_name'].title()}* \u2014 {ph}\n"
                    f"   `\u20b9{r['price']:.2f}` \u00b7 {r['status']}{refund_tag} \u00b7 {r['otp_count']} OTP \u00b7 `{r['created_at']}`\n")
        out += "\n_Tap a number below to view its received OTP messages._"
        await query.edit_message_text(
            out[:4000],
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=purchase_history_keyboard(rows),
        )

    elif data == "menu:support":
        settings = db.get_admin_settings()
        support_id = settings.get("support_id", "@support")
        support_text = settings.get("support_text", "Need help? Contact our support team.")
        await query.edit_message_text(
            f"🆘 *Support & Help*\n\n"
            f"{support_text}\n\n"
            f"💬 *Contact:* `{support_id}`\n\n"
            f"_Please include your User ID (`{user_id}`) when reaching out so we can help faster._",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=support_keyboard(support_id),
        )

    elif data.startswith("hist_otp:"):
        try:
            pid = int(data.split(":", 1)[1])
        except ValueError:
            await query.answer("Invalid selection.", show_alert=True)
            return
        p = db.get_purchase(pid)
        if not p or p["user_id"] != user_id:
            await query.answer("Purchase not found.", show_alert=True)
            return
        otps = db.get_purchase_otps(pid)
        ph = _fmt_number(p["phone"]) if p["phone"] else "\u2014"
        out = (f"\U0001f4e9 *OTP / Messages*\n\n"
               f"\U0001f4e6 Service: *{p['service_name'].title()}*\n"
               f"\U0001f4f1 Number: {ph}\n"
               f"\U0001f4c5 `{p['created_at']}`\n\n")
        if not otps:
            out += "_No OTP messages were received for this number._"
        else:
            for i2, o in enumerate(otps, 1):
                out += f"*#{i2}* \u2014 `{o['text']}`  _({o['received_at']})_\n"
        await query.edit_message_text(
            out[:4000],
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("\U0001f519 Back to History", callback_data="menu:history")],
                [InlineKeyboardButton("\U0001f3e0 Main Menu", callback_data="back:main")],
            ]),
        )

    elif data == "menu:refer":
        try:
            me = await ctx.bot.get_me()
            bot_username = me.username
        except Exception:
            bot_username = None
        reward = db.get_setting("referral_reward", "1")
        count = db.count_referrals(user_id)
        earned = db.get_referral_earnings(user_id)
        link = f"https://t.me/{bot_username}?start=ref_{user_id}" if bot_username else f"(payload ref_{user_id})"
        await query.edit_message_text(
            f"\U0001f465 *Refer & Earn*\n\n"
            f"Share your personal link. When a new user starts the bot through it, "
            f"you earn *\u20b9{reward}* in wallet credit!\n\n"
            f"\U0001f517 *Your link:*\n`{link}`\n\n"
            f"\U0001f4ca *Your Referrals:* `{count}`\n"
            f"\U0001f4b0 *Total Earned:* `\u20b9{earned:.2f}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("\U0001f519 Back to Menu", callback_data="back:main")],
            ]),
        )

    elif data.startswith("rebuy:"):
        parts = data.split(":")
        if len(parts) < 3:
            await query.answer("Invalid rebuy request.", show_alert=True)
            return
        rb_sid, rb_country = parts[1], parts[2]
        svcs = db.get_services(rb_country)
        if rb_sid not in svcs:
            await query.answer("This service is no longer available.", show_alert=True)
            return
        info = svcs[rb_sid]
        if not info.get("is_enabled", 1):
            await query.answer("This service is currently disabled.", show_alert=True)
            return
        ctx.user_data["service_id"] = rb_sid
        ctx.user_data["service_name"] = info["service_name"].lower()
        ctx.user_data[_K_COUNTRY] = rb_country
        await handle_service(query, ctx, is_rebuy=True)


    elif data.startswith("view_active:"):
        activation_id = data.split(":", 1)[1]
        order = await storage.get_active_order(user_id, activation_id)
        if not order:
            await query.answer("Order not found or expired.", show_alert=True)
            return
        can_cancel = order.age_seconds() >= CANCEL_ALLOWED_AFTER
        await query.edit_message_text(
            _format_order_status(order),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=active_order_keyboard(activation_id, can_cancel=can_cancel),
        )

    elif data.startswith("refresh:"):
        activation_id = data.split(":", 1)[1]
        order = await storage.get_active_order(user_id, activation_id)
        if not order:
            await query.answer("Order not found or expired.", show_alert=True)
            return

        try:
            status_resp = await otp_api.check_status(activation_id)
        except Exception as e:
            await query.answer(f"Error checking status: {e}", show_alert=True)
            return

        new_texts = otp_api.parse_sms_from_status(status_resp)
        changed = False
        for text in new_texts:
            if not any(m.text == text for m in order.sms_messages):
                idx = len(order.sms_messages) + 1
                order.sms_messages.append(storage.SmsMessage(index=idx, text=text))
                changed = True
                db.add_log("otp", f"OTP received +{order.phone} {order.service_name}: {text}", user_id)

        if changed and order.status not in ("cancelled", "expired"):
            await storage.update_order(user_id, activation_id, status="otp")
            order.status = "otp"

        can_cancel = order.age_seconds() >= CANCEL_ALLOWED_AFTER
        try:
            await query.edit_message_text(
                _format_order_status(order),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=active_order_keyboard(activation_id, can_cancel=can_cancel),
            )
        except BadRequest:
            pass

        if changed:
            await query.answer("📩 New message received!", show_alert=True)
        else:
            await query.answer("🔄 Refreshed. No new messages yet.")

    elif data.startswith("country:"):
        country_code = data.split(":", 1)[1]
        ctx.user_data[_K_COUNTRY] = country_code
        await show_services(query, ctx, country_code)

    elif data.startswith("service:"):
        parts = data.split(":", 2)
        if len(parts) < 3:
            await query.edit_message_text("❌ Invalid service selection.")
            return
        _, service_id, service_name = parts
        ctx.user_data["service_id"]   = service_id
        ctx.user_data["service_name"] = service_name.lower()
        await handle_service(query, ctx)

    elif data.startswith("svcpage:"):
        try:
            page = int(data.split(":", 1)[1])
        except ValueError:
            page = 0
        country_code = ctx.user_data.get(_K_COUNTRY, "in")
        await show_services(query, ctx, country_code, page=page)

    elif data.startswith("cancel:"):
        activation_id = data.split(":", 1)[1]
        order = await storage.get_active_order(user_id, activation_id)
        if not order:
            await query.answer("Order not found or already closed.", show_alert=True)
            return

        # Enforce: users may cancel only after 3 minutes
        age = order.age_seconds()
        if age < CANCEL_ALLOWED_AFTER:
            remaining = int(CANCEL_ALLOWED_AFTER - age)
            await query.answer(
                f"⏳ You can cancel this number only after 3 minutes.\n"
                f"Please wait {remaining} more second(s).",
                show_alert=True,
            )
            return

        # Mark as cancelling — keep the number VISIBLE with a live status
        await storage.update_order(user_id, activation_id, cancel_requested=True, status="cancelling")
        order.cancel_requested = True
        order.status = "cancelling"
        db.add_log("cancel", f"Cancel requested +{order.phone} {order.service_name}", user_id)

        try:
            await query.edit_message_text(
                _format_order_status(order),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=active_order_keyboard(activation_id, can_cancel=False),
            )
        except BadRequest:
            pass

        ok = await otp_api.cancel_number(activation_id)
        if ok:
            await storage.update_order(user_id, activation_id, is_cancelled=True, status="cancelled")
            order.is_cancelled = True
            order.status = "cancelled"

            # Refund only if NO messages received yet, and not already refunded
            refund_note = ""
            if len(order.sms_messages) == 0 and not order.refunded:
                try:
                    refund_price = float(order.price)
                except Exception:
                    refund_price = 0.0
                db.credit_wallet(user_id, refund_price, f"Refund: Cancelled {order.service_name}")
                await storage.update_order(user_id, activation_id, refunded=True)
                order.refunded = True
                db.add_log("refund", f"Refund ₹{refund_price:.2f} (cancel) +{order.phone}", user_id)
                refund_note = f"\n\n💰 `₹{refund_price:.2f}` has been refunded to your wallet."

            try:
                await query.edit_message_text(
                    f"✅ *Number cancelled successfully.*\n\n"
                    f"📱 Number: {_fmt_number(order.phone)}\n"
                    f"📊 Status: 🔴 Cancelled{refund_note}",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=main_menu_keyboard(),
                )
            except BadRequest:
                pass
        else:
            # Provider hasn't confirmed — keep number visible, status stays 'cancelling'
            await query.answer(
                "⚠️ Cancellation is being processed by the provider.\n"
                "The number will stay visible until it is fully cancelled.",
                show_alert=True,
            )

    # ── Wallet callbacks ────────────────────────────────────────
    elif data == "wallet:recharge":
        ctx.user_data["awaiting_recharge_amount"] = True
        await query.edit_message_text(
            "📥 *Recharge Wallet*\n\n"
            "Please type the amount (in ₹) you want to add to your wallet.\n"
            "_(Minimum: ₹10, Maximum: ₹10,000)_",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="menu:balance")]])
        )

    elif data == "wallet:tx_history":
        txs = db.get_user_transactions(user_id)
        if not txs:
            await query.answer("📜 No transaction history found.", show_alert=True)
            return

        out = "📜 *Your Transaction History (Last 50):*\n\n"
        for t in txs:
            sign = "+" if t["type"] == "credit" else "-"
            out += f"• `{t['timestamp']}` | *{t['description']}* | `{sign}₹{abs(t['amount']):.2f}`\n"

        await query.edit_message_text(
            out,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="menu:balance")]])
        )

    elif data.startswith("wallet:paid:"):
        parts = data.split(":")
        amount = float(parts[2])
        req_id = int(parts[3])

        # Ask the user for their payment / transaction (UTR) ID before notifying admin.
        ctx.user_data["awaiting_txn_id"] = True
        ctx.user_data["txn_amount"] = amount
        ctx.user_data["txn_req_id"] = req_id

        # NOTE: The recharge message may be a PHOTO (QR code) — edit_message_text
        # cannot convert a photo message to text and will silently fail/get stuck.
        # Always send a fresh reply so it works whether QR is set or not.
        await query.answer()  # dismiss the loading spinner on the button
        await query.message.reply_text(
            f"🧾 *Submit Payment Proof*\n\n"
            f"You're adding `₹{amount:.2f}` (Request ID: `{req_id}`).\n\n"
            f"Please type your *Transaction ID / UTR / Reference number* from the payment.\n"
            f"_The admin will verify it and approve your recharge._",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel Request", callback_data=f"wallet:cancel_req:{req_id}")]])
        )

    elif data.startswith("wallet:cancel_req:"):
        req_id = int(data.split(":")[2])
        db.update_recharge_request(req_id, "cancelled")
        await query.edit_message_text(
            "❌ *Recharge request cancelled.*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_menu_keyboard()
        )

    # ── Admin Main callbacks ────────────────────────────────────
    elif data == "admin:main":
        if not db.is_admin(user_id): return
        await query.edit_message_text(
            "🔧 *Admin Control Panel*\n\nSelect an option below to manage the system.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=admin_main_keyboard()
        )

    # ── Admin Dashboard ──────────────────────────────────────────────────
    elif data == "admin:dashboard":
        if not db.is_admin(user_id): return
        today = db.get_today_sales_stats()
        totals = db.get_total_revenue_stats()
        active = db.get_active_purchases()
        await query.edit_message_text(
            f"📊 *Admin Dashboard*\n\n"
            f"📈 *Today*\n"
            f"   • Orders: `{today['orders_count']}`\n"
            f"   • Sales: `{today['sales_count']}`\n"
            f"   • Revenue: `₹{today['revenue']:.2f}`\n\n"
            f"💰 *All-Time*\n"
            f"   • Total Sales: `{totals['total_sales']}`\n"
            f"   • Total Revenue: `₹{totals['total_revenue']:.2f}`\n"
            f"   • Total Orders: `{totals['total_orders']}`\n"
            f"   • Refunds: `{totals['refund_count']}` (`₹{totals['refund_amount']:.2f}`)\n\n"
            f"📡 *Live Active Numbers:* `{len(active)}`\n\n"
            f"_Select a report below._",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=admin_dashboard_keyboard(),
        )

    elif data == "admin:today_stats":
        if not db.is_admin(user_id): return
        s = db.get_today_sales_stats()
        await query.edit_message_text(
            f"📈 *Today's Sales Statistics*\n\n"
            f"🛒 Total Orders Today: `{s['orders_count']}`\n"
            f"✅ Completed Sales: `{s['sales_count']}`\n"
            f"💰 Revenue Today: `₹{s['revenue']:.2f}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Dashboard", callback_data="admin:dashboard")]]),
        )

    elif data == "admin:revenue":
        if not db.is_admin(user_id): return
        t = db.get_total_revenue_stats()
        net = t["total_revenue"] - t["refund_amount"]
        await query.edit_message_text(
            f"💰 *Total Revenue Statistics*\n\n"
            f"✅ Total Sales: `{t['total_sales']}`\n"
            f"💵 Gross Revenue: `₹{t['total_revenue']:.2f}`\n"
            f"↩️ Refunds: `{t['refund_count']}` orders (`₹{t['refund_amount']:.2f}`)\n"
            f"📦 Total Orders: `{t['total_orders']}`\n"
            f"━━━━━━━━━━━━━━\n"
            f"📊 *Net Revenue:* `₹{net:.2f}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Dashboard", callback_data="admin:dashboard")]]),
        )

    elif data == "admin:service_sales":
        if not db.is_admin(user_id): return
        rows = db.get_service_wise_sales(limit=25)
        if not rows:
            await query.answer("No sales recorded yet.", show_alert=True)
            return
        out = "📊 *Service-wise Sales Report*\n\n"
        for r in rows:
            out += f"• *{r['service_name'].title()}* — `{r['sales']}` sales · `₹{r['revenue']:.2f}`\n"
        await query.edit_message_text(
            out[:4000],
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Dashboard", callback_data="admin:dashboard")]]),
        )

    elif data == "admin:sales_history":
        if not db.is_admin(user_id): return
        rows = db.get_sales_history(limit=30)
        if not rows:
            await query.answer("No completed sales yet.", show_alert=True)
            return
        out = "🧾 *Sales History (last 30 completed)*\n\n"
        for r in rows:
            ph = _fmt_number(r["phone"]) if r["phone"] else "—"
            out += (f"✅ *{r['service_name'].title()}* — {ph}\n"
                    f"   {r['username']} (`{r['user_id']}`) · `₹{r['price']:.2f}` · `{r['created_at']}`\n")
        await query.edit_message_text(
            out[:4000],
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Dashboard", callback_data="admin:dashboard")]]),
        )

    elif data == "admin:purchase_history":
        if not db.is_admin(user_id): return
        rows = db.get_all_purchases(limit=30)
        if not rows:
            await query.answer("No purchases recorded yet.", show_alert=True)
            return
        _icons = {"completed": "✅", "active": "⏳", "pending": "⏳",
                  "cancelled": "🛑", "expired": "🟡", "failed": "❌"}
        out = "📦 *Number Purchase History (last 30)*\n\n"
        for r in rows:
            ic = _icons.get(r["status"], "•")
            ph = _fmt_number(r["phone"]) if r["phone"] else "—"
            rt = " _(refunded)_" if r["refunded"] else ""
            out += (f"{ic} *{r['service_name'].title()}* — {ph}\n"
                    f"   {r['username']} (`{r['user_id']}`) · `₹{r['price']:.2f}` · {r['status']}{rt}\n")
        await query.edit_message_text(
            out[:4000],
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Dashboard", callback_data="admin:dashboard")]]),
        )

    elif data == "admin:active_monitor":
        if not db.is_admin(user_id): return
        live = await storage.get_all_active_orders_global()
        live = [(uid, o) for uid, o in live if not o.is_cancelled and not o.is_expired]
        if not live:
            await query.edit_message_text(
                "📡 *Active / Running Numbers Monitor*\n\nNo numbers are currently active.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Refresh", callback_data="admin:active_monitor")],
                    [InlineKeyboardButton("🔙 Back to Dashboard", callback_data="admin:dashboard")],
                ]),
            )
            return
        out = f"📡 *Active / Running Numbers ({len(live)})*\n\n"
        for uid, o in live[:40]:
            age = int(o.age_seconds())
            out += (f"📱 {_fmt_number(o.phone)} — *{o.service_name.title()}*\n"
                    f"   user `{uid}` · `₹{o.price}` · {o.status} · {age}s · "
                    f"{len(o.sms_messages)} SMS\n")
        await query.edit_message_text(
            out[:4000],
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh", callback_data="admin:active_monitor")],
                [InlineKeyboardButton("🔙 Back to Dashboard", callback_data="admin:dashboard")],
            ]),
        )

    elif data == "admin:support_cfg":
        if not db.is_admin(user_id): return
        settings = db.get_admin_settings()
        ctx.user_data["awaiting_admin_support_id"] = True
        await query.edit_message_text(
            f"🆘 *Customer Support ID Management*\n\n"
            f"Current Support Contact: `{settings.get('support_id', '@support')}`\n\n"
            f"Type the new support contact (e.g. `@YourSupport` or a phone/email) "
            f"that users will see in the Support section:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="admin:main")]]),
        )

    elif data == "admin:api_cfg":
        if not db.is_admin(user_id): return
        cur = db.get_setting("otp_api_key", None)
        masked = (cur[:4] + "•••" + cur[-4:]) if cur and len(cur) >= 8 else ("set (env default)" if not cur else "set")
        ctx.user_data["awaiting_admin_api_key"] = True
        await query.edit_message_text(
            f"🔑 *OTPDoctor API Management*\n\n"
            f"Current key: `{masked}`\n\n"
            f"Type the *new OTPDoctor API key*. It will be validated with a live "
            f"balance check and used immediately — no code edit needed.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="admin:main")]]),
        )

    elif data == "admin:channel_cfg":
        if not db.is_admin(user_id): return
        cur = db.get_setting("force_channel", "") or "(disabled)"
        ctx.user_data["awaiting_admin_channel"] = True
        await query.edit_message_text(
            f"📡 *Force-Join Channel*\n\n"
            f"Current: `{cur}`\n\n"
            f"Type the channel users must join (e.g. `@YourChannel` or a `t.me/...` link).\n"
            f"Send `off` to disable the requirement.\n\n"
            f"⚠️ _The bot must be an admin of that channel to verify membership._",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="admin:main")]]),
        )

    elif data == "admin:referral_cfg":
        if not db.is_admin(user_id): return
        cur = db.get_setting("referral_reward", "1")
        ctx.user_data["awaiting_admin_referral_reward"] = True
        await query.edit_message_text(
            f"👥 *Referral Reward*\n\n"
            f"Current reward per successful referral: `₹{cur}`\n\n"
            f"Type the new reward amount (in ₹) credited to a user for each new "
            f"person who joins through their referral link:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="admin:main")]]),
        )

    elif data == "admin:services_menu":
        if not db.is_admin(user_id): return
        await query.edit_message_text(
            "⚙️ *Services Management*\n\nAdd, edit, enable/disable, change prices and Service IDs.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=admin_services_keyboard()
        )

    elif data == "admin:users_menu":
        if not db.is_admin(user_id): return
        await query.edit_message_text(
            "💼 *User Wallets Management*\n\nManage user balances, view logs, credit or debit user wallets.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=admin_users_keyboard()
        )

    elif data == "admin:tx_history":
        if not db.is_admin(user_id): return
        txs = db.get_all_transactions()
        if not txs:
            await query.answer("No transactions found in the system.", show_alert=True)
            return

        out = "📜 *System Transactions (Last 100):*\n\n"
        for t in txs[:30]: # Limit text size
            sign = "+" if t["type"] == "credit" else "-"
            out += f"• `{t['timestamp']}` | {t['username']} (`{t['user_id']}`) | *{t['description']}* | `{sign}₹{abs(t['amount']):.2f}`\n"

        await query.edit_message_text(
            out[:4000],
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Admin", callback_data="admin:main")]])
        )

    elif data == "admin:logs":
        if not db.is_admin(user_id): return
        logs = db.get_logs(limit=40)
        if not logs:
            await query.answer("No logs recorded yet.", show_alert=True)
            return
        out = "🧾 *System Logs (latest 40):*\n\n"
        for lg in logs:
            uid = lg.get("user_id")
            who = f" u{uid}" if uid else ""
            out += f"• `{lg['timestamp']}` [{lg['category']}]{who} {lg['message']}\n"
        await query.edit_message_text(
            out[:4000],
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Admin", callback_data="admin:main")]])
        )

    elif data == "admin:notify_all":
        if not db.is_admin(user_id): return
        ctx.user_data["awaiting_admin_noti"] = True
        await query.edit_message_text(
            "📢 *Broadcast Notification*\n\nType the message you want to send to ALL users in the system:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="admin:main")]])
        )

    elif data == "admin:payment_details":
        if not db.is_admin(user_id): return
        settings = db.get_admin_settings()
        await query.edit_message_text(
            f"💳 *Payment and QR Code Details*\n\n"
            f"💳 *Current UPI ID:* `{settings.get('upi_id', 'notset@upi')}`\n"
            f"🖼️ *QR Code File ID:* `{settings.get('qr_file_id', 'Not Set')}`\n\n"
            f"Select what you'd like to update:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️ Update UPI ID", callback_data="admin:update_upi")],
                [InlineKeyboardButton("📸 Upload New QR Photo", callback_data="admin:update_qr")],
                [InlineKeyboardButton("🔙 Back to Admin", callback_data="admin:main")]
            ])
        )

    elif data == "admin:update_upi":
        if not db.is_admin(user_id): return
        ctx.user_data["awaiting_admin_upi"] = True
        await query.edit_message_text(
            "✏️ *Update UPI ID*\n\nPlease type the new UPI ID to show users for recharge payments:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="admin:payment_details")]])
        )

    elif data == "admin:update_qr":
        if not db.is_admin(user_id): return
        ctx.user_data["awaiting_admin_qr"] = True
        await query.edit_message_text(
            "📸 *Upload QR Photo*\n\nPlease upload/send a QR Code photo for payment recharges.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="admin:payment_details")]])
        )

    # ── Admin Services callbacks ────────────────────────────────
    elif data.startswith("admin:top_list:"):
        if not db.is_admin(user_id): return
        tops = db.get_all_top_services()
        await query.edit_message_text(
            f"⭐ *Top Services ({len(tops)})*\n\n"
            f"These appear pinned at the top of the user menu.\n"
            f"Tap one to edit its price or unpin it, or pin new ones from the services list.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=admin_top_services_keyboard(tops),
        )

    elif data == "admin:swiggy_cfg":
        if not db.is_admin(user_id): return
        settings = db.get_admin_settings()
        ctx.user_data["awaiting_admin_swiggy_id"] = True
        await query.edit_message_text(
            f"🍔 *Configure Swiggy Checker*\n\n"
            f"Current Service ID: `{settings.get('swiggy_service_id', 'swiggy')}`\n\n"
            f"Type the new provider *Service ID* to use when purchasing numbers for the Swiggy Checker:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="admin:services_menu")]])
        )

    elif data == "admin:service_add":
        if not db.is_admin(user_id): return
        ctx.user_data["awaiting_admin_add_svc_id"] = True
        await query.edit_message_text(
            "➕ *Add Custom Service*\n\nEnter the service ID (e.g., `go` for Google):",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="admin:services_menu")]])
        )

    elif data.startswith("admin:service_list:"):
        if not db.is_admin(user_id): return
        page = int(data.split(":")[2])
        svcs = db.get_all_services_list()

        if not svcs:
            await query.edit_message_text(
                "❌ No services currently stored in local database.\nTry opening any service catalog as a normal user first to auto-sync services from the API.",
                reply_markup=admin_services_keyboard()
            )
            return

        svcs_per_page = 15
        total_pages = max(1, (len(svcs) + svcs_per_page - 1) // svcs_per_page)
        page = max(0, min(page, total_pages - 1))

        start_idx = page * svcs_per_page
        page_svcs = svcs[start_idx : start_idx + svcs_per_page]

        buttons = []
        out = f"📋 *Local Services (Page {page+1}/{total_pages}):*\n\nSelect a service to edit its price, toggle status, delete, or mark as Top:\n"
        for s in page_svcs:
            top_badge = "⭐" if s["is_top"] else ""
            status_badge = "🟢" if s["is_enabled"] else "🔴"
            out += f"• {status_badge}{top_badge} *{s['service_name']}* ({s['country']}) | ID: `{s['service_id']}` | Price: `₹{s['service_price']}`\n"
            buttons.append([InlineKeyboardButton(f"{top_badge}{s['service_name']} ({s['country']}) - ₹{s['service_price']}", callback_data=f"admin:svc_dt:{s['service_id']}:{s['country']}")])

        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"admin:service_list:{page-1}"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"admin:service_list:{page+1}"))
        if nav:
            buttons.append(nav)

        buttons.append([InlineKeyboardButton("🔙 Back to Services Menu", callback_data="admin:services_menu")])
        await query.edit_message_text(
            out,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(buttons)
        )

    elif data.startswith("admin:svc_dt:"):
        if not db.is_admin(user_id): return
        parts = data.split(":")
        sid, country = parts[2], parts[3]

        services = db.get_services(country)
        if sid not in services:
            await query.answer("Service not found.", show_alert=True)
            return

        svc = services[sid]
        out = (
            f"⚙️ *Service Details*\n\n"
            f"🆔 *Service ID:* `{sid}`\n"
            f"🌍 *Country Code:* `{country}`\n"
            f"✏️ *Service Name:* `*{svc['service_name']}*`\n"
            f"💰 *Service Price:* `₹{svc['service_price']:.2f}`\n"
            f"🟢 *Status:* {'Enabled' if svc['is_enabled'] else 'Disabled'}\n"
            f"⭐ *Top Service:* {'Yes' if svc['is_top'] else 'No'}\n\n"
            f"What would you like to edit?"
        )
        await query.edit_message_text(
            out,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=admin_edit_service_keyboard(sid, country, svc['is_enabled'], svc['is_top'])
        )

    elif data.startswith("admin:svc_ep:"):
        if not db.is_admin(user_id): return
        parts = data.split(":")
        sid, country = parts[2], parts[3]
        ctx.user_data["awaiting_admin_svc_price_sid_country"] = (sid, country)
        ctx.user_data["awaiting_admin_svc_price_val"] = True
        await query.edit_message_text(
            f"✏️ *Change Service Price*\n\nEnter new price (in ₹) for `{sid}` in country `{country}`:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data=f"admin:svc_dt:{sid}:{country}")]])
        )

    elif data.startswith("admin:svc_eid:"):
        if not db.is_admin(user_id): return
        parts = data.split(":")
        sid, country = parts[2], parts[3]
        ctx.user_data["awaiting_admin_svc_newid_old"] = (sid, country)
        ctx.user_data["awaiting_admin_svc_newid"] = True
        await query.edit_message_text(
            f"🆔 *Change Service ID*\n\n"
            f"Current Service ID: `{sid}` (country `{country}`)\n\n"
            f"Type the *new* provider Service ID. The old ID's pricing record will be "
            f"removed completely and replaced with the new one (no duplicates).",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data=f"admin:svc_dt:{sid}:{country}")]])
        )

    elif data.startswith("admin:svc_te:"):
        if not db.is_admin(user_id): return
        parts = data.split(":")
        sid, country = parts[2], parts[3]

        services = db.get_services(country)
        if sid in services:
            new_status = 0 if services[sid]["is_enabled"] else 1
            db.toggle_service_enabled(sid, country, new_status)
            await query.answer(f"Service status updated to {'Enabled' if new_status else 'Disabled'}.", show_alert=True)

            services = db.get_services(country)
            svc = services[sid]
            out = (
                f"⚙️ *Service Details*\n\n"
                f"🆔 *Service ID:* `{sid}`\n"
                f"🌍 *Country Code:* `{country}`\n"
                f"✏️ *Service Name:* `*{svc['service_name']}*`\n"
                f"💰 *Service Price:* `₹{svc['service_price']:.2f}`\n"
                f"🟢 *Status:* {'Enabled' if svc['is_enabled'] else 'Disabled'}\n"
                f"⭐ *Top Service:* {'Yes' if svc['is_top'] else 'No'}\n\n"
                f"What would you like to edit?"
            )
            await query.edit_message_text(
                out,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=admin_edit_service_keyboard(sid, country, svc['is_enabled'], svc['is_top'])
            )

    elif data.startswith("admin:svc_tt:"):
        if not db.is_admin(user_id): return
        parts = data.split(":")
        sid, country = parts[2], parts[3]

        services = db.get_services(country)
        if sid in services:
            new_status = 0 if services[sid]["is_top"] else 1
            db.toggle_service_top(sid, country, new_status)
            await query.answer(f"Top Service status updated to {'Yes' if new_status else 'No'}.", show_alert=True)

            services = db.get_services(country)
            svc = services[sid]
            out = (
                f"⚙️ *Service Details*\n\n"
                f"🆔 *Service ID:* `{sid}`\n"
                f"🌍 *Country Code:* `{country}`\n"
                f"✏️ *Service Name:* `*{svc['service_name']}*`\n"
                f"💰 *Service Price:* `₹{svc['service_price']:.2f}`\n"
                f"🟢 *Status:* {'Enabled' if svc['is_enabled'] else 'Disabled'}\n"
                f"⭐ *Top Service:* {'Yes' if svc['is_top'] else 'No'}\n\n"
                f"What would you like to edit?"
            )
            await query.edit_message_text(
                out,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=admin_edit_service_keyboard(sid, country, svc['is_enabled'], svc['is_top'])
            )

    elif data.startswith("admin:svc_del:"):
        if not db.is_admin(user_id): return
        parts = data.split(":")
        sid, country = parts[2], parts[3]
        db.delete_service(sid, country)
        await query.answer("Service deleted.", show_alert=True)
        await query.edit_message_text(
            f"🗑️ Service `{sid}` ({country}) has been deleted successfully.",
            reply_markup=admin_services_keyboard()
        )

    # ── Admin Users callbacks ───────────────────────────────────
    elif data == "admin:user_search":
        if not db.is_admin(user_id): return
        ctx.user_data["awaiting_admin_user_search"] = True
        await query.edit_message_text(
            "🔍 *Search User*\n\nType the User ID or Username of the user you want to find:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="admin:users_menu")]])
        )

    elif data.startswith("admin:user_list:"):
        if not db.is_admin(user_id): return
        page = int(data.split(":")[2])
        users = db.get_all_users()

        if not users:
            await query.edit_message_text(
                "❌ No users registered yet.",
                reply_markup=admin_main_keyboard()
            )
            return

        users_per_page = 15
        total_pages = max(1, (len(users) + users_per_page - 1) // users_per_page)
        page = max(0, min(page, total_pages - 1))

        start_idx = page * users_per_page
        page_users = users[start_idx : start_idx + users_per_page]

        buttons = []
        out = f"📋 *Registered Users (Page {page+1}/{total_pages}):*\n\nSelect a user to credit/debit or view history:\n"
        for u in page_users:
            out += f"• *{u['username']}* | ID: `{u['user_id']}` | Wallet: `₹{u['balance']:.2f}`\n"
            buttons.append([InlineKeyboardButton(f"{u['username']} (₹{u['balance']:.2f})", callback_data=f"admin:user_dt:{u['user_id']}")])

        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"admin:user_list:{page-1}"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"admin:user_list:{page+1}"))
        if nav:
            buttons.append(nav)

        buttons.append([InlineKeyboardButton("🔙 Back to Users Menu", callback_data="admin:users_menu")])
        await query.edit_message_text(
            out,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(buttons)
        )

    elif data.startswith("admin:user_dt:"):
        if not db.is_admin(user_id): return
        uid = int(data.split(":")[2])
        u = db.get_user(uid)

        if not u:
            await query.answer("User not found.", show_alert=True)
            return

        out = (
            f"👤 *User Account Details*\n\n"
            f"👤 *Username:* `@{u['username']}`\n"
            f"🆔 *User ID:* `{u['user_id']}`\n"
            f"💰 *Wallet Balance:* `₹{u['balance']:.2f}`\n"
            f"⚙ *Role:* {'Admin' if u['is_admin'] else 'Regular User'}\n\n"
            f"Choose an administrative action below:"
        )
        await query.edit_message_text(
            out,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=admin_user_detail_keyboard(uid)
        )

    elif data.startswith("admin:user_cred:"):
        if not db.is_admin(user_id): return
        uid = int(data.split(":")[2])
        ctx.user_data["awaiting_admin_add_balance_uid"] = uid
        ctx.user_data["awaiting_admin_add_balance_amt"] = True
        await query.edit_message_text(
            f"➕ *Credit User Wallet*\n\nEnter amount (in ₹) to ADD to user `{uid}`:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data=f"admin:user_dt:{uid}")]])
        )

    elif data.startswith("admin:user_deb:"):
        if not db.is_admin(user_id): return
        uid = int(data.split(":")[2])
        ctx.user_data["awaiting_admin_sub_balance_uid"] = uid
        ctx.user_data["awaiting_admin_sub_balance_amt"] = True
        await query.edit_message_text(
            f"➖ *Debit User Wallet*\n\nEnter amount (in ₹) to DEDUCT from user `{uid}`:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data=f"admin:user_dt:{uid}")]])
        )

    elif data.startswith("admin:user_tx:"):
        if not db.is_admin(user_id): return
        uid = int(data.split(":")[2])
        txs = db.get_user_transactions(uid)

        if not txs:
            await query.answer("No transactions found for this user.", show_alert=True)
            return

        out = f"📜 *Transactions for User {uid}:*\n\n"
        for t in txs[:30]:
            sign = "+" if t["type"] == "credit" else "-"
            out += f"• `{t['timestamp']}` | {t['description']} | `{sign}₹{abs(t['amount']):.2f}`\n"

        await query.edit_message_text(
            out,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=f"admin:user_dt:{uid}")]])
        )

    # ── Admin recharge approvals/rejections ─────────────────────
    elif data.startswith("admin:approve:"):
        if not db.is_admin(user_id): return
        req_id = int(data.split(":")[2])
        req = db.get_recharge_request(req_id)

        if not req:
            await query.answer("Recharge request not found.", show_alert=True)
            return

        if req["status"] != "pending":
            await query.answer(f"This request has already been {req['status']}.", show_alert=True)
            return

        db.update_recharge_request(req_id, "approved")
        new_bal = db.credit_wallet(req["user_id"], req["amount"], "Wallet Recharge Approved")
        db.add_log("admin", f"Approved recharge #{req_id}: +₹{req['amount']:.2f} to user {req['user_id']}", user_id)

        await query.edit_message_text(
            f"✅ *Recharge Request Approved!*\n\n"
            f"Request ID: `{req_id}`\n"
            f"🧾 Txn ID: `{req.get('txn_id') or 'N/A'}`\n"
            f"👤 User: `{req['username']}` (ID: `{req['user_id']}`)\n"
            f"💰 Amount: `₹{req['amount']:.2f}` has been successfully credited.\n"
            f"📈 User's new balance: `₹{new_bal:.2f}`",
            parse_mode=ParseMode.MARKDOWN
        )

        try:
            await ctx.bot.send_message(
                chat_id=req["user_id"],
                text=f"🎉 *Wallet Credited!*\n\n"
                     f"Your recharge of `₹{req['amount']:.2f}` has been verified and *Approved* by the admin!\n"
                     f"💰 *Current Balance:* `₹{new_bal:.2f}`",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=main_menu_keyboard()
            )
        except Exception:
            pass

    elif data.startswith("admin:reject:"):
        if not db.is_admin(user_id): return
        req_id = int(data.split(":")[2])
        req = db.get_recharge_request(req_id)

        if not req:
            await query.answer("Recharge request not found.", show_alert=True)
            return

        if req["status"] != "pending":
            await query.answer(f"This request has already been {req['status']}.", show_alert=True)
            return

        db.update_recharge_request(req_id, "rejected")
        db.add_log("admin", f"Rejected recharge #{req_id} for user {req['user_id']} (₹{req['amount']:.2f})", user_id)

        await query.edit_message_text(
            f"❌ *Recharge Request Rejected.*\n\n"
            f"Request ID: `{req_id}`\n"
            f"👤 User: `{req['username']}` (ID: `{req['user_id']}`)\n"
            f"💰 Amount: `₹{req['amount']:.2f}`",
            parse_mode=ParseMode.MARKDOWN
        )

        try:
            await ctx.bot.send_message(
                chat_id=req["user_id"],
                text=f"❌ *Recharge Rejected!*\n\n"
                     f"Your recharge request of `₹{req['amount']:.2f}` was *Rejected* by the admin.\n"
                     f"Please make sure your payment was successful or contact support.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=main_menu_keyboard()
            )
        except Exception:
            pass

    # ── Noop & Back ─────────────────────────────────────────────
    elif data == "noop":
        pass

    elif data == "back:countries":
        ctx.user_data[_K_AWAITING_SEARCH] = False
        try:
            countries = await asyncio.wait_for(otp_api.get_countries(), timeout=8)
        except asyncio.TimeoutError:
            await query.edit_message_text(
                "⚠️ *Country list is taking too long to load.*\n\nPlease try again in a moment.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Retry", callback_data="back:countries"),
                                                    InlineKeyboardButton("🔙 Menu", callback_data="back:main")]]),
            )
            return
        except Exception as e:
            await query.edit_message_text(f"❌ Could not load countries: {e}")
            return
        await query.edit_message_text(
            "🌍 *Select Country*\n\nChoose the country for the phone number:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=countries_keyboard(countries),
        )

    elif data == "back:main":
        ctx.user_data[_K_AWAITING_SEARCH] = False
        country_code = ctx.user_data.get(_K_COUNTRY, "in")
        top_svcs = db.get_top_services(country_code)
        await query.edit_message_text(
            f"👋 *Welcome to OTPCart, {query.from_user.first_name}!*\n\n"
            f"Recharge your wallet and get instant phone numbers for OTP verification. "
            f"Select an option below to get started.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_menu_keyboard(top_services=top_svcs),
        )

# ── Country Service catalog visualizer ─────────────────────────────

async def show_countries(query, ctx) -> None:
    try:
        countries = await asyncio.wait_for(otp_api.get_countries(), timeout=8)
    except asyncio.TimeoutError:
        await query.edit_message_text(
            "⚠️ *Country list is taking too long to load.*\n\nPlease try again in a moment.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Retry", callback_data="back:countries"),
                                                InlineKeyboardButton("🔙 Menu", callback_data="back:main")]]),
        )
        return
    except Exception as e:
        await query.edit_message_text(f"❌ Could not load countries: {e}")
        return
    await query.edit_message_text(
        "🌍 *Select Country*\n\nChoose the country for the phone number:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=countries_keyboard(countries),
    )

async def show_services(query, ctx, country_code: str, page: int = 0) -> None:
    try:
        services = await asyncio.wait_for(get_and_sync_services(country_code), timeout=8)
        ctx.user_data[_K_SERVICES] = services
        ctx.user_data[_K_COUNTRY]  = country_code
    except asyncio.TimeoutError:
        # Timeout: fall back to DB-only services so the user isn't blocked
        services = {
            sid: {"service_name": info["service_name"], "service_price": info["service_price"]}
            for sid, info in db.get_services(country_code).items() if info.get("is_enabled")
        }
        ctx.user_data[_K_SERVICES] = services
        ctx.user_data[_K_COUNTRY]  = country_code
        if not services:
            await query.answer("⚠️ Services unavailable right now. Please try again.", show_alert=True)
            return
    except Exception as e:
        await query.answer(f"Error fetching services: {e}", show_alert=True)
        return

    kb = services_keyboard(services, page=page)
    await query.edit_message_text(
        "📱 *Select Service*\n\nChoose the service you want an OTP for:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
    )

def _is_retryable_error(err_str: str) -> bool:
    s = err_str.upper()
    if "ACCESS_NUMBER" in s:
        return False
    return any(k in s for k in RETRY_ERROR_KEYWORDS) or "ACCESS" not in s


async def _purchase_with_retry(bot, chat_id, service_id, user_id):
    """
    Number Purchase Error Handling (requirement #1).

    Auto-retries the SAME service every RETRY_INTERVAL (2s), up to RETRY_MAX (10)
    attempts, whenever no number is received or the provider returns an error.

    Returns:
      • (act_id, phone)  → a number was secured (continue normal OTP flow)
      • "CANCELLED"      → the user pressed Cancel during the search
      • None             → all attempts failed / no number available

    A live "Cancel & Refund" button is shown the whole time so the user is never
    stuck in an infinite purchasing loop.
    """
    retry_msg = None

    async def _render(text):
        nonlocal retry_msg
        if retry_msg is None:
            retry_msg = await bot.send_message(
                chat_id, text, parse_mode=ParseMode.MARKDOWN,
                reply_markup=purchase_searching_keyboard(),
            )
        else:
            try:
                await retry_msg.edit_text(
                    text, parse_mode=ParseMode.MARKDOWN,
                    reply_markup=purchase_searching_keyboard(),
                )
            except Exception:
                pass

    async def _cleanup():
        if retry_msg:
            try:
                await retry_msg.delete()
            except Exception:
                pass

    for attempt in range(1, RETRY_MAX + 1):
        # User asked to abort the search → stop immediately (caller refunds)
        if storage.is_purchase_cancelled(user_id):
            await _cleanup()
            db.add_log("purchase", f"Search cancelled by user on attempt {attempt} ({service_id})", user_id)
            return "CANCELLED"

        try:
            act_id, phone = await otp_api.purchase_number(service_id)
            await _cleanup()
            db.add_log("purchase", f"Number +{phone} obtained for service {service_id} (try {attempt})", user_id)
            return act_id, phone
        except Exception as e:
            err = str(e)
            db.add_log("purchase", f"Attempt {attempt}/{RETRY_MAX} failed for {service_id}: {err}", user_id)

            # Non-retryable hard error → stop now (caller refunds)
            if not _is_retryable_error(err):
                await _cleanup()
                return None

            # Exhausted all attempts
            if attempt >= RETRY_MAX:
                await _cleanup()
                return None

            await _render(
                f"⏳ *Searching for a number…*\n\n"
                f"⚠️ Provider busy / no number yet.\n"
                f"🔁 Retrying automatically every {RETRY_INTERVAL}s.\n"
                f"📊 Attempt `{attempt}/{RETRY_MAX}`\n\n"
                f"_You can cancel anytime — your balance will be refunded._"
            )

            # Sleep in short slices so a Cancel press is honoured fast
            slept = 0.0
            while slept < RETRY_INTERVAL:
                if storage.is_purchase_cancelled(user_id):
                    await _cleanup()
                    db.add_log("purchase", f"Search cancelled by user during wait ({service_id})", user_id)
                    return "CANCELLED"
                await asyncio.sleep(0.25)
                slept += 0.25

    await _cleanup()
    return None


async def handle_service(query, ctx, is_rebuy: bool = False) -> None:
    service_id   = ctx.user_data["service_id"]
    service_name = ctx.user_data["service_name"]
    chat_id      = query.message.chat_id
    user_id      = query.from_user.id
    country_code = ctx.user_data.get(_K_COUNTRY, "in")
    bot          = ctx.bot

    # Load correct price from DB
    services = db.get_services(country_code)
    price_val = 0.0
    if service_id in services:
        price_val = float(services[service_id]["service_price"])
    else:
        cached_services = ctx.user_data.get(_K_SERVICES, {})
        if service_id in cached_services:
            try:
                price_val = float(cached_services[service_id].get("service_price", 0.0))
            except Exception:
                price_val = 0.0

    # DUPLICATE PURCHASE GUARD (Stability #5)
    if not storage.try_begin_purchase(user_id):
        await query.answer(
            "⚠️ A purchase is already in progress.\n"
            "Please wait for it to finish or cancel it first.",
            show_alert=True,
        )
        return

    # WALLET CHECK — Issue #3: always send a persistent message for insufficient balance
    user_bal = db.get_user_balance(user_id)
    if user_bal < price_val:
        storage.end_purchase(user_id)   # release guard — nothing was charged
        await bot.send_message(
            chat_id,
            f"❌ *Insufficient wallet balance.*\n\n"
            f"💳 *Your balance:* `₹{user_bal:.2f}`\n"
            f"💰 *Required:* `₹{price_val:.2f}`\n\n"
            f"Please recharge your wallet to continue using this service.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=wallet_keyboard(),
        )
        return

    # Deduct from wallet balance
    new_bal = db.debit_wallet(user_id, price_val, f"Purchase {service_name} ({country_code})")
    db.add_log("wallet", f"Debited ₹{price_val:.2f} for {service_name}; bal ₹{new_bal:.2f}", user_id)

    # Record the purchase in the sales ledger (Dashboard + Number History)
    username = query.from_user.username or query.from_user.first_name or f"User_{user_id}"
    purchase_id = db.record_purchase(
        user_id, username, service_id, service_name, country_code,
        price_val, status="pending",
    )

    # Issue #1 & #2: For rebuy, always send a fresh message so previous active
    # number messages stay visible. For first buy, edit the existing message.
    searching_text = (
        f"📱 *Securing a number for* *{service_name.title()}*…\n\n"
        f"⏳ Please wait — this is automatic.\n"
        f"💰 Wallet Balance: `₹{new_bal:.2f}`"
    )
    if is_rebuy:
        await bot.send_message(
            chat_id,
            searching_text,
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await query.edit_message_text(
            searching_text,
            parse_mode=ParseMode.MARKDOWN,
        )

    asyncio.create_task(
        _otp_flow(bot, service_id, service_name, str(price_val), chat_id, user_id, purchase_id, country_code)
    )


# ── Swiggy Checker flow ───────────────────────────────────────────

async def start_swiggy_flow(query, ctx) -> None:
    user_id = query.from_user.id
    chat_id = query.message.chat_id

    # Prevent duplicate concurrent sessions for the same user
    existing = _swiggy_sessions.get(user_id)
    if existing and not existing.get("cancelled") and not existing.get("done"):
        await query.answer("⚠️ A Swiggy check is already running. Cancel it first.", show_alert=True)
        return

    settings = db.get_admin_settings()
    swiggy_sid = settings.get("swiggy_service_id", "swiggy")

    price_val = 0.0
    for row in db.get_all_services_list():
        if row["service_id"] == swiggy_sid:
            price_val = float(row["service_price"])
            break

    # Verify funds but DO NOT deduct yet — charge only on a successful unregistered number
    user_bal = db.get_user_balance(user_id)
    if user_bal < price_val:
        await ctx.bot.send_message(
            chat_id,
            f"❌ *Insufficient wallet balance.*\n\n"
            f"💳 *Your balance:* `₹{user_bal:.2f}`\n"
            f"💰 *Required:* `₹{price_val:.2f}`\n\n"
            f"Please recharge your wallet to continue using this service.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=wallet_keyboard(),
        )
        return

    msg = await query.edit_message_text(
        f"🍔 *Swiggy Checker*\n\n"
        f"🔎 Searching for an *unregistered* number…\n"
        f"💰 You will be charged `₹{price_val:.2f}` *only* when an unregistered number is found.\n\n"
        f"_Attempt 0/{SWIGGY_RETRY_MAX}_",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=swiggy_checking_keyboard(),
    )
    _swiggy_sessions[user_id] = {"cancelled": False, "active_act_id": None, "msg": msg, "done": False}

    asyncio.create_task(
        _swiggy_search(ctx.bot, swiggy_sid, price_val, chat_id, user_id)
    )


async def _swiggy_cleanup(act_id):
    """Best-effort release of an OTPDoctor order."""
    if act_id:
        try:
            await otp_api.cancel_number(act_id)
        except Exception:
            pass


async def _swiggy_search(bot, service_id, price_val, chat_id, user_id) -> None:
    """
    Hunt for an UNREGISTERED Swiggy number.
      • Up to SWIGGY_RETRY_MAX purchase+check cycles.
      • API/purchase errors → skip & request a new number (no charge).
      • Registered → cancel number, request a new one (no charge).
      • Unregistered → charge wallet, deliver number, start OTP monitor.
      • User can cancel anytime → stop immediately, release order, no charge.
    """
    session = _swiggy_sessions.get(user_id, {})

    def cancelled() -> bool:
        return _swiggy_sessions.get(user_id, {}).get("cancelled", False)

    last_edit = ""
    for attempt in range(1, SWIGGY_RETRY_MAX + 1):
        if cancelled():
            break

        # live progress (avoid redundant edits)
        progress = (f"🍔 *Swiggy Checker*\n\n🔎 Searching for an *unregistered* number…\n"
                    f"💰 Charged `₹{price_val:.2f}` *only* on success.\n\n"
                    f"_Attempt {attempt}/{SWIGGY_RETRY_MAX}_")
        if progress != last_edit and session.get("msg"):
            try:
                await session["msg"].edit_text(progress, parse_mode=ParseMode.MARKDOWN,
                                               reply_markup=swiggy_checking_keyboard())
                last_edit = progress
            except Exception:
                pass

        # 1) purchase a number (skip on any error)
        try:
            act_id, phone = await otp_api.purchase_number(service_id)
        except Exception as e:
            db.add_log("swiggy", f"Attempt {attempt}: purchase error {str(e)[:40]} (skip)", user_id)
            await asyncio.sleep(SWIGGY_CHECK_DELAY)
            continue

        if cancelled():
            await _swiggy_cleanup(act_id)
            break

        _swiggy_sessions.get(user_id, {})["active_act_id"] = act_id

        # 2) check registration
        status = await checkers.check_swiggy(phone)
        db.add_log("swiggy", f"Attempt {attempt}: +{phone} -> {status}", user_id)

        if cancelled():
            await _swiggy_cleanup(act_id)
            _swiggy_sessions.get(user_id, {})["active_act_id"] = None
            break

        if status not in ("registered", "error"):
            # 3) charge now (re-verify funds), deliver, monitor
            bal = db.get_user_balance(user_id)
            if bal < price_val:
                await _swiggy_cleanup(act_id)
                _swiggy_sessions.get(user_id, {})["done"] = True
                if session.get("msg"):
                    try:
                        await session["msg"].edit_text(
                            f"❌ Found an unregistered number but your balance "
                            f"(`₹{bal:.2f}`) is below the price (`₹{price_val:.2f}`). Not charged.",
                            parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu_keyboard())
                    except Exception:
                        pass
                return

            new_bal = db.debit_wallet(user_id, price_val, "Swiggy Checker (unregistered)")
            db.add_log("wallet", f"Charged ₹{price_val:.2f} for unregistered +{phone}; bal ₹{new_bal:.2f}", user_id)

            # Record in the sales ledger (Dashboard + Number History)
            _sw_user = db.get_user(user_id) or {}
            swiggy_pid = db.record_purchase(
                user_id, _sw_user.get("username", f"User_{user_id}"),
                service_id, "swiggy checker", "in", price_val,
                phone=phone, activation_id=act_id, status="active",
            )

            order = storage.ActiveOrder(
                activation_id=act_id, phone=phone, service_id=service_id,
                service_name="swiggy checker", price=str(price_val), chat_id=chat_id, is_swiggy=True,
            )
            await storage.add_active_order(user_id, order)
            _swiggy_sessions.get(user_id, {})["done"] = True
            _swiggy_sessions.get(user_id, {})["active_act_id"] = None

            try:
                await session["msg"].edit_text(
                    f"🍔 *Swiggy Checker — unregistered number found!* ✅\n\n"
                    f"📱 *Your number:* {_fmt_number(phone)}\n"
                    f"💰 Charged: `₹{price_val:.2f}`  |  Balance: `₹{new_bal:.2f}`\n"
                    f"⏳ Waiting for OTP… (auto-cancel & refund in {OTP_TIMEOUT // 60} min if none received)",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=active_order_keyboard(act_id, can_cancel=False),
                )
                await storage.update_order(user_id, act_id,
                                           message_id=session["msg"].message_id, status="waiting")
            except Exception:
                sm = await bot.send_message(
                    chat_id,
                    f"🍔 *Swiggy Checker — unregistered number found!* ✅\n\n"
                    f"📱 *Your number:* {_fmt_number(phone)}\n"
                    f"⏳ Waiting for OTP…",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=active_order_keyboard(act_id, can_cancel=False),
                )
                await storage.update_order(user_id, act_id, message_id=sm.message_id, status="waiting")

            await _monitor_order(bot, act_id, phone, "swiggy checker", price_val, chat_id, user_id, swiggy_pid)
            return

        # registered OR check-error → release and try a new number (NO charge)
        await _swiggy_cleanup(act_id)
        _swiggy_sessions.get(user_id, {})["active_act_id"] = None
        await asyncio.sleep(SWIGGY_CHECK_DELAY)

    # Loop ended — cancelled or exhausted (never charged)
    sess = _swiggy_sessions.get(user_id, {})
    sess["done"] = True
    if cancelled():
        await _swiggy_cleanup(sess.get("active_act_id"))
        sess["active_act_id"] = None
        db.add_log("swiggy", "Search cancelled by user (no charge)", user_id)
        text = "🛑 *Swiggy check cancelled.* No amount was deducted."
    else:
        text = (f"😕 *Swiggy Checker:* no unregistered number found after "
                f"{SWIGGY_RETRY_MAX} attempts. *No charge was made.*")
        db.add_log("swiggy", f"No unregistered number after {SWIGGY_RETRY_MAX} attempts (no charge)", user_id)
    if sess.get("msg"):
        try:
            await sess["msg"].edit_text(text, parse_mode=ParseMode.MARKDOWN,
                                        reply_markup=main_menu_keyboard())
        except Exception:
            await bot.send_message(chat_id, text, parse_mode=ParseMode.MARKDOWN,
                                   reply_markup=main_menu_keyboard())


# ── Unified OTP flow (with Auto-Retry, Polling, and Auto-Refund) ──

async def _otp_flow(
    bot,
    service_id: str,
    service_name: str,
    price: str,
    chat_id: int,
    user_id: int,
    purchase_id: int = None,
    country: str = "in",
) -> None:
    try:
        price_val = float(price)
    except Exception:
        price_val = 0.0

    try:
        # Auto-Retry purchase (requirement #1)
        res = await _purchase_with_retry(bot, chat_id, service_id, user_id)

        # User cancelled during the search -> refund and stop cleanly
        if res == "CANCELLED":
            if not _already_refunded(purchase_id):
                db.credit_wallet(user_id, price_val, f"Refund: Cancelled search {service_name}")
                db.update_purchase(purchase_id, status="cancelled", refunded=1)
                db.add_log("refund", f"Refund \u20b9{price_val:.2f} (user cancelled search) {service_name}", user_id)
            bal = db.get_user_balance(user_id)
            await bot.send_message(
                chat_id,
                f"\U0001f6d1 *Purchase cancelled.*\n\n"
                f"\U0001f4b0 `\u20b9{price_val:.2f}` has been refunded.\n"
                f"\U0001f4b3 Wallet Balance: `\u20b9{bal:.2f}`",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=main_menu_keyboard(),
            )
            return

        # All attempts failed / no number available -> refund and stop
        if not res:
            if not _already_refunded(purchase_id):
                db.credit_wallet(user_id, price_val, f"Refund: No number {service_name}")
                db.update_purchase(purchase_id, status="failed", refunded=1)
                db.add_log("refund", f"Refund \u20b9{price_val:.2f} (no number after {RETRY_MAX} tries) {service_name}", user_id)
            bal = db.get_user_balance(user_id)
            await bot.send_message(
                chat_id,
                f"\u274c *No Number Available / Provider Error*\n\n"
                f"We tried *{RETRY_MAX} times* but couldn't secure a number for "
                f"*{service_name.title()}* right now.\n\n"
                f"\U0001f4b0 `\u20b9{price_val:.2f}` has been automatically refunded.\n"
                f"\U0001f4b3 Wallet Balance: `\u20b9{bal:.2f}`\n\n"
                f"Please try again in a little while.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=main_menu_keyboard(),
            )
            return

        act_id, phone = res
        db.update_purchase(purchase_id, status="active", phone=phone, activation_id=act_id)

        # Register active order
        order = storage.ActiveOrder(
            activation_id=act_id,
            phone=phone,
            service_id=service_id,
            service_name=service_name,
            price=str(price_val),
            chat_id=chat_id,
        )
        await storage.add_active_order(user_id, order)
        await storage.record_service_used(user_id, service_id, service_name, str(price_val))

        # Announce number (display +CC, copy = local only)
        status_msg = await bot.send_message(
            chat_id,
            f"\u2705 *Your number is ready!*\n\n"
            f"\U0001f4f1 *Number:* {_fmt_number(phone)}\n"
            f"\U0001f4e6 *Service:* *{service_name.title()}*\n\n"
            f"\u23f3 Waiting for OTP\u2026 _(auto-cancel & refund in {OTP_TIMEOUT // 60} min if none received)_",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=delivered_number_keyboard(act_id, rebuy_sid=service_id, rebuy_country=country, can_cancel=False),
        )
        await storage.update_order(user_id, act_id, message_id=status_msg.message_id, status="waiting")
    finally:
        # Purchase phase finished -> release the duplicate guard.
        storage.end_purchase(user_id)

    await _monitor_order(bot, act_id, phone, service_name, price_val, chat_id, user_id, purchase_id)


def _already_refunded(purchase_id) -> bool:
    """True if this purchase row is already marked refunded (guards double refunds)."""
    if not purchase_id:
        return False
    try:
        for r in db.get_all_purchases(limit=1000):
            if r["id"] == purchase_id:
                return bool(r["refunded"])
    except Exception:
        pass
    return False


async def _monitor_order(bot, act_id, phone, service_name, price_val, chat_id, user_id, purchase_id=None) -> None:
    """Shared SMS monitor loop: multi-SMS, auto-cancel + refund on timeout/expiry."""
    sms_count = 0
    async for msg in otp_api.monitor_sms(act_id, first_timeout=OTP_TIMEOUT):
        order = await storage.get_active_order(user_id, act_id)
        if order is None:
            return  # removed elsewhere

        if msg is None:
            # timeout with no SMS -> auto-cancel & refund
            await otp_api.cancel_number(act_id)
            await storage.update_order(user_id, act_id, is_expired=True, status="expired")
            if not order.refunded:
                db.credit_wallet(user_id, price_val, f"Refund: Timeout {service_name}")
                await storage.update_order(user_id, act_id, refunded=True)
                db.update_purchase(purchase_id, status="expired", refunded=1, otp_count=sms_count)
                db.add_log("refund", f"Refund \u20b9{price_val:.2f} (timeout) +{phone}", user_id)
            await bot.send_message(
                chat_id,
                f"\u23f3 *No OTP received.*\n"
                f"Number {_fmt_number(phone)} automatically cancelled after {OTP_TIMEOUT // 60} minutes.\n\n"
                f"\U0001f4b0 `\u20b9{price_val:.2f}` has been refunded to your wallet.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=main_menu_keyboard(),
            )
            await storage.remove_active_order(user_id, act_id)
            return

        sms_count += 1
        order.sms_messages.append(msg)
        await storage.update_order(user_id, act_id, status="otp")
        # Persist the received OTP for Purchase History
        db.add_purchase_otp(purchase_id, msg.text)
        # First OTP realises the sale (revenue earned)
        if sms_count == 1:
            db.update_purchase(purchase_id, status="completed", is_sale=1, otp_count=sms_count)
        else:
            db.update_purchase(purchase_id, otp_count=sms_count)
        db.add_log("otp", f"OTP #{sms_count} +{phone} {service_name}: {msg.text}", user_id)
        sms_block = _format_sms_block(order.sms_messages)

        note = ""
        if order.cancel_requested and sms_count == 1:
            note = "\u26a0\ufe0f _Cancellation was requested but OTP arrived before it completed:_\n\n"

        await bot.send_message(
            chat_id,
            f"{note}"
            f"\U0001f4e9 *SMS #{sms_count} received!*\n\n"
            f"\U0001f4f1 Number: {_fmt_number(phone)}\n"
            f"\U0001f4e6 Service: *{service_name.title()}*\n\n"
            f"{sms_block}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=sms_list_keyboard(act_id),
        )

        if order.is_cancelled:
            await storage.remove_active_order(user_id, act_id)
            return

    # Loop ended (number expired / closed)
    order = await storage.get_active_order(user_id, act_id)
    if sms_count > 0:
        db.update_purchase(purchase_id, status="completed", is_sale=1, otp_count=sms_count)
        await bot.send_message(
            chat_id,
            f"\u2705 *Session complete.*\n"
            f"\U0001f4f1 {_fmt_number(phone)} received *{sms_count} message(s)* in total.\n"
            f"The number is now expired/closed.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_menu_keyboard(),
        )
    else:
        if order is not None and not order.refunded:
            db.credit_wallet(user_id, price_val, f"Refund: Expired {service_name}")
            await storage.update_order(user_id, act_id, refunded=True)
            db.update_purchase(purchase_id, status="expired", refunded=1)
            db.add_log("refund", f"Refund \u20b9{price_val:.2f} (expired) +{phone}", user_id)
        await bot.send_message(
            chat_id,
            f"\u23f3 Number {_fmt_number(phone)} expired with no messages received.\n\n"
            f"\U0001f4b0 `\u20b9{price_val:.2f}` has been automatically refunded to your wallet.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_menu_keyboard(),
        )

    await storage.remove_active_order(user_id, act_id)

# ── Formatting Helpers ─────────────────────────────────────────────

def _format_sms_block(messages: list) -> str:
    if not messages:
        return "_No messages yet._"
    import time as _t
    lines = []
    for m in messages:
        ts = getattr(m, "received_at", None)
        when = _t.strftime("%H:%M:%S", _t.localtime(ts)) if ts else ""
        suffix = f"  _({when})_" if when else ""
        lines.append(f"*#{m.index}* — `{m.text}`{suffix}")
    return "\n".join(lines)

def _format_order_status(order: storage.ActiveOrder) -> str:
    # Live status mapping
    if order.is_cancelled or order.status == "cancelled":
        status_emoji, status_text = "🔴", "Cancelled"
    elif order.is_expired or order.status == "expired":
        status_emoji, status_text = "🟡", "Expired / Closed"
    elif order.status == "cancelling" or order.cancel_requested:
        status_emoji, status_text = "🟠", "Cancelling…"
    elif order.sms_messages or order.status == "otp":
        status_emoji, status_text = "📩", "OTP Received"
    elif order.status == "waiting":
        status_emoji, status_text = "⏳", "Waiting OTP"
    else:
        status_emoji, status_text = "🟢", "Active"

    sms_block = _format_sms_block(order.sms_messages)
    age = int(order.age_seconds())
    can_cancel = age >= CANCEL_ALLOWED_AFTER
    cancel_hint = "" if can_cancel else f"\n🔒 _Cancel unlocks in {max(0, CANCEL_ALLOWED_AFTER - age)}s_"

    return (
        f"📱 *Active Order Status*\n\n"
        f"📱 *Number:* {_fmt_number(order.phone)}\n"
        f"📦 *Service:* *{order.service_name.title()}* (₹{order.price})\n"
        f"ℹ️ *Status:* {status_emoji} {status_text}{cancel_hint}\n\n"
        f"📩 *Received Messages:*\n"
        f"{sms_block}"
    )

# ── Error handler ──────────────────────────────────────────────────

async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling update %s:", update, exc_info=ctx.error)

# ── Main Entrypoint ────────────────────────────────────────────────

def main() -> None:
    # Initialize Persistent DB
    db.init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start",   start))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("admin",   admin_cmd))

    # Callbacks
    app.add_handler(CallbackQueryHandler(callback_handler))

    # General Message Handlers
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    # Error log
    app.add_error_handler(error_handler)

    logger.info("OTPCart Bot started successfully...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()