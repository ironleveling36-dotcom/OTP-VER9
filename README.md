# OTPCart Telegram Bot — v8 (Referrals · Force-Join · Rebuy · API Mgmt)

A Telegram OTP-number bot powered by OTPDoctor, with a full **Wallet System**,
**Admin Control Panel**, **Top Services**, **Swiggy Checker**, **Multi-SMS**,
an improved **cancellation system**, and a robust **Auto-Retry** engine.

## 🆕 What's new in v8 (this release)

1. **Insufficient-balance message** — clicking a service without funds shows:
   *"Insufficient balance for this service. Please recharge your wallet."*
2. **🔄 Rebuy Same Service** — a button on every delivered number instantly
   repurchases the same service (same country) without navigating back.
3. **Purchase History** — users see their **last 5** purchases and can tap any
   one to view the **OTP/messages received** for that number (persisted in DB).
4. **Admin Recharge Approval w/ Transaction ID** — after paying, the user
   submits their **Txn/UTR ID**, which is sent to the admin. Wallet is credited
   **only after Approve** (Reject notifies the user). Status-guarded against
   double credits.
5. **OTPDoctor API Management** — admin can change the API key from the panel
   (**🔑 OTP API Key**); it's validated with a live balance check and used
   immediately, no code edit / redeploy.
6. **Force-Join Channel** — admin sets a required channel (**📡 Force-Join
   Channel**). Non-members get a *Join + ✅ Verify Join* gate before any access.
   Admins bypass; misconfig fails open so the bot is never bricked.
7. **Refer & Earn** — every user has a referral link (`?start=ref_<id>`). A new
   user joining through it credits the referrer (default **₹1**, admin-editable
   via **👥 Referral Reward**). One credit per referred user (no duplicates).
8. All features integrated with the wallet + admin panel.
9. Hardened: no duplicate wallet credits, no duplicate purchases, atomic DB
   transactions, validation everywhere, and admin-action logging.

## 🗄️ v7 — Persistence, Dashboard & Purchase hardening

### 1. Number-purchase error handling (critical fix)
- After a failed/empty provider response, the bot **auto-retries the same
  service every 2s, up to 10 attempts** (`RETRY_INTERVAL` / `RETRY_MAX`).
- A live **🛑 Cancel & Refund** button is shown the entire time — the user is
  never stuck in an infinite purchasing loop and can abort anytime.
- If a number arrives during retries → normal OTP flow continues.
- If all 10 attempts fail → a clear *"No Number Available / Provider Error"*
  message is shown and the **debited amount is automatically refunded**.
- Refunds are guarded against double-crediting (`refunded` flag per purchase).

### 2. Service Price & Service ID update bug
- New **🆔 Change Service ID** action in the service editor.
- Changing a Service ID now **deletes the old ID record entirely** and stores a
  single clean record under the new ID — no duplicate / stale pricing.
- `dedupe_services_by_name()` collapses accidental name duplicates.
- Done inside one atomic DB transaction (`_execute_many`).

### 3. Admin dashboard
- **📊 Dashboard & Stats**: Today's sales, total revenue (gross / net /
  refunds), service-wise sales report, sales history, number-purchase history,
  and a **live Active/Running Numbers monitor** across all users.
- **🆘 Customer Support ID management**.

### 4. User panel
- Cleaner two-column menu, clearer purchase/loading status messages,
  **🧾 Number History**, **📋 Active Orders**, and a **🆘 Support** section.

### 5. General stability
- Duplicate-purchase guard (one active purchase per user at a time).
- Atomic SQLite transactions with commit/rollback, WAL mode + busy timeout,
  and detailed DB error logging.
- Every purchase is tracked in a `purchases` ledger for history & reporting.

## 🆕 What's new in v4

### 📞 Number display & copy format
- Numbers display with the country code (e.g. `+91 9876543210`).
- The number is shown in a tap-to-copy code span containing **only the local
  10-digit number** (no country code) — tapping copies `9876543210`.

### 🍔 Swiggy Checker
- Dedicated **🍔 Swiggy Checker** button on the dashboard.
- Admin configures the provider **Service ID** used by the checker
  (Admin → Services Management → *Configure Swiggy Service ID*).
- **Charge-on-success search engine:**
  - Buys a number → `POST https://checker.otpcart.xyz/api/check-swiggy` with `{"mobile":"XXXXXXXXXX"}`.
  - **Registered** → number auto-cancelled, a fresh number requested instantly — **no charge**.
  - API error / failed / unknown → number released and skipped automatically — **no charge**.
  - **Unregistered** → wallet charged, number shown, normal OTP flow begins.
  - Retries up to **30 attempts**, stopping the instant an unregistered number is found.
- **Live Cancel button** during checking: stops all retries immediately, releases the
  active OTPDoctor order, and never deducts the wallet.
- The wallet is charged **only** for a successful unregistered number — never for
  registered numbers, API errors, failed checks, or cancellations.
- Duplicate-session protection prevents two concurrent checks per user.

### 📩 Multi-SMS & Check SMS
- Supports multiple OTP/SMS messages per activation.
- **📩 Check SMS** button to manually refresh and view incoming messages.
- All messages are listed with timestamps.

### ⏱️ Improved cancellation
- Users can cancel **only after 3 minutes** (a live countdown shows when it unlocks).
- On cancel, the number stays visible until the provider confirms — with live
  status: Active → Waiting OTP → OTP Received → Cancelling → Cancelled / Expired.

### 🔁 Auto-Retry (expanded)
- Retries on `No Number Available`, `Try Again`, `No Stock`, `Temporary Error`,
  `Provider Error`, and similar — every **2s, up to 20 attempts**.
- Stops immediately when a valid number is received.
- On total failure → cancel + automatic wallet refund + user notification.

### 🧾 Logging & stability
- Detailed logs for purchases, OTP receipts, cancellations, refunds, wallet
  transactions, admin actions, and Swiggy checks (Admin → *View System Logs*).
- Refund guards prevent double-refunds / balance drift.

## ✨ Features

### 💳 Wallet System
- Users recharge their wallet via **QR code + UPI** (managed by admin).
- User enters an amount → sees QR/UPI → taps **✅ I Paid**.
- Admin gets an instant notification with **Approve / Reject** buttons.
- On approval, the balance is credited automatically and the user is notified.
- Services are purchased directly from wallet balance — the bot verifies funds
  and deducts the cost before activation.

### 💸 Automatic Refunds
- If a number is **cancelled** before any OTP arrives → amount refunded.
- If **no OTP arrives within 3 minutes** → number auto-cancelled + refunded.
- If a number **expires** with no messages → refunded.
- If all auto-retry attempts fail → refunded.

### 🔁 Auto-Retry System
- On provider errors (`No Number Available`, `Try Again`, etc.), the bot
  automatically retries the **same service every 2 seconds, up to 20 attempts**.
- Stops as soon as a number is received.
- If all 20 attempts fail → service cancelled + wallet refunded.

### ⭐ Top Services
- Admin can mark any service as a **Top Service**.
- Top services appear as **pinned/highlighted 🔥 buttons** at the top of the main menu.

### 🔧 Admin Panel (`/admin`)
- **Services:** add / edit / delete, enable / disable, change prices, mark as Top.
- **User Wallets:** search users, view balances, credit / debit, view tx logs.
- **Transactions:** view full system transaction history.
- **Notifications:** broadcast a message to all users.
- **Payment Details:** update UPI ID and QR-code image at any time.

## 🚀 Setup

### Environment variables
| Variable      | Required | Description                                              |
|---------------|----------|----------------------------------------------------------|
| `BOT_TOKEN`   | ✅       | Telegram bot token from @BotFather.                      |
| `ADMIN_IDS`   | ✅       | Comma-separated Telegram user IDs of admins, e.g. `12345,67890`. |
| `OTP_API_KEY` | optional | OTPDoctor API key (defaults to the bundled key).         |

> Get your numeric Telegram ID from [@userinfobot](https://t.me/userinfobot).

### Install & run
```bash
pip install -r requirements.txt
export BOT_TOKEN="your_token_here"
export ADMIN_IDS="123456789"        # your Telegram ID
python bot.py
```

## 🗂️ Files
| File           | Purpose                                                        |
|----------------|---------------------------------------------------------------|
| `bot.py`       | Main bot: handlers, wallet flow, admin panel, OTP + auto-retry |
| `database.py`  | SQLite persistence (users, wallets, services, tx, settings)   |
| `keyboards.py` | All inline keyboards (user + admin + wallet)                  |
| `otp_api.py`   | OTPDoctor API wrapper                                          |
| `storage.py`   | In-memory active-order tracking                               |
| `config.py`    | Config + timing (3-min OTP timeout)                          |

## 📝 Notes
- Wallet balances, services, transactions and payment settings persist in a local
  `bot.db` SQLite file (created automatically on first run).
- The first time a country's service catalog is opened, services are auto-synced
  from the provider into the local DB so the admin can manage/price them.
