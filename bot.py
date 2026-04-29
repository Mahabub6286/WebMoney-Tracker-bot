import os
import re
import json
import time
import logging
from collections import defaultdict
from pathlib import Path

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("webmoney-bot")

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN environment variable is not set")

ADMIN_ID_RAW = os.environ.get("ADMIN_TELEGRAM_ID", "").strip()
try:
    ADMIN_ID = int(ADMIN_ID_RAW) if ADMIN_ID_RAW else None
except ValueError:
    ADMIN_ID = None

if ADMIN_ID is None:
    logger.warning("ADMIN_TELEGRAM_ID is not set or invalid — admin features disabled.")

BOT_DIR = Path(__file__).parent
DATA_FILE = BOT_DIR / "data.json"
USERS_FILE = BOT_DIR / "users.json"
EPHEMERAL_TTL = 120  # seconds
HISTORY_KEEP = 50

# ---- runtime state -------------------------------------------------------

balances: dict[str, float] = defaultdict(float)
known_users: set[str] = set()
history: dict[str, list[dict]] = defaultdict(list)
started_users: set[int] = set()

# users.json contents
owners_cfg: dict[str, dict] = {}

# chat_id -> message_id of the last bot ephemeral reply
last_bot_msg: dict[int, int] = {}


# ---- persistence ---------------------------------------------------------

def load_users_cfg() -> None:
    owners_cfg.clear()
    if not USERS_FILE.exists():
        logger.warning("users.json not found at %s", USERS_FILE)
        return
    try:
        raw = json.loads(USERS_FILE.read_text(encoding="utf-8"))
        for owner, info in (raw.get("owners") or {}).items():
            owners_cfg[owner] = {
                "tg_id": info.get("tg_id"),
                "payment_number": info.get("payment_number"),
                "payment_method": info.get("payment_method"),
                "ipweb_accounts": list(info.get("ipweb_accounts") or [owner]),
            }
    except Exception as e:
        logger.warning("Failed to load users.json: %s", e)


def load_data() -> None:
    if not DATA_FILE.exists():
        return
    try:
        raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        for u, v in (raw.get("balances") or {}).items():
            balances[u] = float(v)
        for u in raw.get("users") or []:
            known_users.add(u)
        for u, items in (raw.get("history") or {}).items():
            history[u] = list(items)[-HISTORY_KEEP:]
        for tid in raw.get("started_users") or []:
            try:
                started_users.add(int(tid))
            except (TypeError, ValueError):
                pass
    except Exception as e:
        logger.warning("Failed to load data.json: %s", e)


def save_data() -> None:
    try:
        DATA_FILE.write_text(
            json.dumps(
                {
                    "balances": dict(balances),
                    "users": sorted(known_users),
                    "history": {u: items for u, items in history.items() if items},
                    "started_users": sorted(started_users),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning("Failed to save data.json: %s", e)


def record_history(username: str, kind: str, amount: float) -> None:
    history[username].append({
        "ts": int(time.time()),
        "type": kind,
        "amount": round(amount, 4),
        "balance_after": round(balances.get(username, 0.0), 2),
    })
    if len(history[username]) > HISTORY_KEEP:
        history[username] = history[username][-HISTORY_KEEP:]


# ---- parsing -------------------------------------------------------------

AMOUNT_RE = re.compile(r"Received\s+([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)
ACCOUNT_RE = re.compile(r"с\s*аккаунта\s+([A-Za-z0-9_.\-]+)", re.IGNORECASE)
PIN_USER_RE = re.compile(r"📌\s*([^\n\r]+)")


def parse_message(text: str):
    if not text:
        return None, None
    amount_match = AMOUNT_RE.search(text)
    if not amount_match:
        return None, None
    try:
        amount = float(amount_match.group(1))
    except ValueError:
        return None, None
    username = None
    acc = ACCOUNT_RE.search(text)
    if acc:
        username = acc.group(1).strip().strip(".")
    else:
        pin = PIN_USER_RE.findall(text)
        if pin:
            username = pin[-1].strip().strip(".")
    if not username:
        return None, None
    return username, amount


def normalize_username(raw: str) -> str:
    return raw.strip().lstrip("@").strip().strip(".")


# ---- permission / mapping helpers ---------------------------------------

def is_admin(update: Update) -> bool:
    if ADMIN_ID is None:
        return False
    user = update.effective_user
    return bool(user and user.id == ADMIN_ID)


def owner_of_ipweb(ipweb: str) -> str | None:
    target = ipweb.lower()
    for owner, info in owners_cfg.items():
        for acc in info.get("ipweb_accounts", []):
            if acc.lower() == target:
                return owner
    return None


def owner_of_tg(tg_id: int | None) -> str | None:
    if tg_id is None:
        return None
    for owner, info in owners_cfg.items():
        if info.get("tg_id") == tg_id:
            return owner
    return None


def ipweb_accounts_for_owner(owner: str) -> list[str]:
    info = owners_cfg.get(owner) or {}
    return list(info.get("ipweb_accounts") or [owner])


def find_match_key(name: str) -> str | None:
    """Find the actual stored key (case-insensitive) for an Ipweb name."""
    target = name.lower()
    for k in list(balances.keys()) + list(known_users) + list(history.keys()):
        if k.lower() == target:
            return k
    return None


# ---- ephemeral message helpers ------------------------------------------

async def _safe_delete(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int):
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except TelegramError as e:
        logger.debug("delete_message failed (chat=%s msg=%s): %s", chat_id, message_id, e)


async def _delete_job(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data or {}
    chat_id = data.get("chat_id")
    message_id = data.get("message_id")
    if chat_id is None or message_id is None:
        return
    await _safe_delete(context, chat_id, message_id)
    if last_bot_msg.get(chat_id) == message_id:
        last_bot_msg.pop(chat_id, None)


async def send_ephemeral(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    *,
    parse_mode: str | None = None,
    delete_user_msg: bool = True,
    persistent: bool = False,
):
    chat = update.effective_chat
    if chat is None:
        return

    if persistent:
        await context.bot.send_message(
            chat_id=chat.id,
            text=text,
            parse_mode=parse_mode,
        )
        return

    if delete_user_msg and update.message is not None:
        await _safe_delete(context, chat.id, update.message.message_id)

    prev = last_bot_msg.pop(chat.id, None)
    if prev is not None:
        await _safe_delete(context, chat.id, prev)

    sent = await context.bot.send_message(
        chat_id=chat.id,
        text=text,
        parse_mode=parse_mode,
    )
    last_bot_msg[chat.id] = sent.message_id

    if context.job_queue is not None:
        context.job_queue.run_once(
            _delete_job,
            when=EPHEMERAL_TTL,
            data={"chat_id": chat.id, "message_id": sent.message_id},
            name=f"del-{chat.id}-{sent.message_id}",
        )


# ---- handlers ------------------------------------------------------------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Only admin's messages (including forwards) feed the tracker."""
    if not update.message or not update.message.text:
        return
    if not is_admin(update):
        return

    text = update.message.text
    username, amount = parse_message(text)
    if not username or amount is None:
        return

    balances[username] += amount
    known_users.add(username)
    total_user = balances[username]
    record_history(username, "auto", amount)
    save_data()

    await send_ephemeral(
        update,
        context,
        f"✅ Added\nUser: {username}\nAmount: {amount}$\nTotal: {total_user:.2f}$",
        delete_user_msg=False,  # keep the forwarded/pasted message
    )


def _format_balance_lines(accounts: list[str]) -> tuple[list[str], float]:
    """Return (lines, total) for the given Ipweb accounts (in order)."""
    lines: list[str] = []
    total = 0.0
    for acc in accounts:
        match = find_match_key(acc) or acc
        bal = balances.get(match, 0.0)
        lines.append(f"{match} → {bal:.2f}$")
        total += bal
    return lines, total


async def report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_admin(update):
        if not balances:
            await send_ephemeral(update, context, "📊 BALANCE REPORT\n\nNo data yet.")
            return
        lines = ["📊 BALANCE REPORT", ""]
        total = 0.0
        for user in sorted(balances.keys(), key=lambda u: u.lower()):
            bal = balances[user]
            lines.append(f"{user} → {bal:.2f}$")
            total += bal
        lines.append("")
        lines.append(f"🟢 TOTAL: {total:.2f}$")
        await send_ephemeral(update, context, "\n".join(lines))
        return

    tg_user = update.effective_user
    owner = owner_of_tg(tg_user.id if tg_user else None)
    if owner is None:
        await send_ephemeral(
            update,
            context,
            "🚫 You don't have access. Ask the admin to register your Telegram ID.",
            persistent=True,
        )
        return

    accounts = ipweb_accounts_for_owner(owner)
    lines, total = _format_balance_lines(accounts)
    msg = ["📊 Your balance", ""] + lines
    if len(accounts) > 1:
        msg += ["", f"🟢 TOTAL: {total:.2f}$"]
    await send_ephemeral(update, context, "\n".join(msg), persistent=True)


async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await send_ephemeral(update, context, "🚫 Admin only.")
        return

    args = context.args or []

    if not args:
        balances.clear()
        save_data()
        await send_ephemeral(
            update,
            context,
            "♻️ All balances reset.\nUser list kept intact.",
        )
        return

    cleared, not_found = [], []
    for raw in args:
        u = normalize_username(raw)
        if not u:
            continue
        match_key = find_match_key(u)
        if match_key:
            balances.pop(match_key, None)
            cleared.append(match_key)
        else:
            not_found.append(u)

    save_data()
    parts = []
    if cleared:
        parts.append("♻️ Balance reset for:\n" + "\n".join(f"• @{u}" for u in cleared))
    if not_found:
        parts.append("⚠️ Not found:\n" + "\n".join(f"• @{u}" for u in not_found))
    await send_ephemeral(update, context, "\n\n".join(parts) or "Nothing to reset.")


def _parse_amount_args(args: list[str]):
    if len(args) < 2:
        return None, None
    username = normalize_username(args[0])
    try:
        amount = float(args[1].replace(",", "."))
    except ValueError:
        return None, None
    if not username or amount <= 0:
        return None, None
    return username, amount


async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await send_ephemeral(update, context, "🚫 Admin only.")
        return

    username, amount = _parse_amount_args(context.args or [])
    if username is None:
        await send_ephemeral(
            update,
            context,
            "Usage: /add username amount\nExample: /add taleb12 1.5",
        )
        return

    match_key = find_match_key(username) or username
    balances[match_key] += amount
    known_users.add(match_key)
    record_history(match_key, "add", amount)
    save_data()

    await send_ephemeral(
        update,
        context,
        f"✅ Added {amount:.2f}$ to @{match_key}\nTotal: {balances[match_key]:.2f}$",
    )


async def less_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await send_ephemeral(update, context, "🚫 Admin only.")
        return

    username, amount = _parse_amount_args(context.args or [])
    if username is None:
        await send_ephemeral(
            update,
            context,
            "Usage: /less username amount\nExample: /less taleb12 0.5",
        )
        return

    match_key = find_match_key(username)
    if match_key is None:
        await send_ephemeral(update, context, f"⚠️ User @{username} not found.")
        return

    balances[match_key] -= amount
    record_history(match_key, "less", -amount)
    save_data()

    await send_ephemeral(
        update,
        context,
        f"➖ Subtracted {amount:.2f}$ from @{match_key}\nTotal: {balances[match_key]:.2f}$",
    )


def _format_history(username: str, items: list[dict], limit: int = 10) -> str:
    items = items[-limit:][::-1]
    if not items:
        return f"📜 No transactions for {username} yet."
    icons = {"auto": "🟢", "add": "➕", "less": "➖"}
    lines = [f"📜 History for {username} (last {len(items)}):", ""]
    for it in items:
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(it.get("ts", 0)))
        kind = it.get("type", "?")
        amt = float(it.get("amount", 0))
        bal = float(it.get("balance_after", 0))
        sign = "+" if amt >= 0 else "−"
        lines.append(f"{icons.get(kind, '•')} {ts}  {sign}{abs(amt):.2f}$  → {bal:.2f}$")
    return "\n".join(lines)


async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []

    if args and is_admin(update):
        target = normalize_username(args[0])
        match_key = find_match_key(target)
        if match_key is None or not history.get(match_key):
            await send_ephemeral(update, context, f"📜 No history for {target}.")
            return
        await send_ephemeral(update, context, _format_history(match_key, history[match_key], 10))
        return

    if is_admin(update):
        users_with_hist = sorted([u for u in history if history[u]], key=lambda u: u.lower())
        if not users_with_hist:
            await send_ephemeral(update, context, "📜 No transactions recorded yet.")
            return
        chunks = ["📜 Recent transactions (last 5 per user):", ""]
        for u in users_with_hist:
            chunks.append(_format_history(u, history[u], 5))
            chunks.append("")
        await send_ephemeral(update, context, "\n".join(chunks).rstrip())
        return

    tg_user = update.effective_user
    owner = owner_of_tg(tg_user.id if tg_user else None)
    if owner is None:
        await send_ephemeral(
            update,
            context,
            "🚫 You don't have access. Ask the admin to register your Telegram ID.",
        )
        return

    accounts = ipweb_accounts_for_owner(owner)
    chunks = []
    for acc in accounts:
        match_key = find_match_key(acc) or acc
        if history.get(match_key):
            chunks.append(_format_history(match_key, history[match_key], 10))
            chunks.append("")
    if not chunks:
        await send_ephemeral(update, context, "📜 No history yet.")
        return
    await send_ephemeral(update, context, "\n".join(chunks).rstrip())


async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show every Ipweb account grouped by permission owner, with payment.
    Each Ipweb @username and each payment number is wrapped in <code> for
    one-tap copy on Telegram mobile.
    """
    if not owners_cfg:
        await send_ephemeral(update, context, "📭 No users configured yet.")
        return

    owners_sorted = sorted(owners_cfg.items(), key=lambda kv: kv[0].lower())
    blocks: list[str] = []
    total_accounts = 0
    for owner, info in owners_sorted:
        accs = info.get("ipweb_accounts") or [owner]
        total_accounts += len(accs)
        acc_lines = "\n".join(f"<code>@{a}</code>" for a in accs)
        method = info.get("payment_method")
        number = info.get("payment_number")
        if number:
            payment_line = f"{method or 'Pay'}: <code>{number}</code>"
        else:
            payment_line = "Payment: <i>not set</i>"
        blocks.append(f"{acc_lines}\n💳 {payment_line}")

    header = (
        f"👥 Users ({len(owners_sorted)} owners, {total_accounts} accounts)\n"
        "Tap any @username or number to copy."
    )
    body = "\n\n".join(blocks)
    await send_ephemeral(update, context, f"{header}\n\n{body}", parse_mode=ParseMode.HTML)


async def reload_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: reload users.json after edits without restarting the bot."""
    if not is_admin(update):
        await send_ephemeral(update, context, "🚫 Admin only.")
        return
    load_users_cfg()
    await send_ephemeral(
        update,
        context,
        f"🔄 Reloaded users.json — {len(owners_cfg)} owners loaded.",
    )


WELCOME_MESSAGE = (
    "👋 Welcome to the WebMoney Tracker Bot!\n\n"
    "This bot helps you track your IPweb / WebMoney earnings.\n\n"
    "🔹 /report — see your current balance\n"
    "🔹 /history — see your recent transactions\n"
    "🔹 /list — see all users and payment numbers (tap any number or @username to copy)\n\n"
    "Your balance is updated automatically whenever the admin receives an IPweb payment for your account.\n\n"
    "If you don't have access yet, please share your Telegram numeric ID with the admin so it can be linked to your IPweb account."
)


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    admin = is_admin(update)
    if tg_user is not None and tg_user.id not in started_users:
        started_users.add(tg_user.id)
        save_data()
        await send_ephemeral(update, context, WELCOME_MESSAGE, persistent=not admin)
        return

    if admin:
        text = (
            "🤖 WebMoney Tracker Bot (Admin)\n\n"
            "Commands:\n"
            "/report — full balance report\n"
            "/list — users + payment methods (tap to copy)\n"
            "/add username amount — add to a balance\n"
            "/less username amount — subtract from a balance\n"
            "/reset — clear all balances (user list kept)\n"
            "/reset username [...] — clear specific users\n"
            "/history — recent transactions (all users)\n"
            "/history username — full history for one user\n"
            "/reload — reload users.json after editing it\n\n"
            "Forward or paste IPweb/WebMoney messages here to auto-track."
        )
    else:
        text = (
            "🤖 WebMoney Tracker Bot\n\n"
            "Commands:\n"
            "/report — show your balance\n"
            "/history — show your transaction history\n"
            "/list — show all users and payment methods"
        )
    await send_ephemeral(update, context, text, persistent=not admin)


def main():
    load_users_cfg()
    load_data()
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", start_cmd))
    app.add_handler(CommandHandler("report", report))
    app.add_handler(CommandHandler("list", list_users))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(CommandHandler("add", add_cmd))
    app.add_handler(CommandHandler("less", less_cmd))
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CommandHandler("reload", reload_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info(
        "Bot is running (polling)... admin_id=%s owners=%d",
        ADMIN_ID, len(owners_cfg),
    )
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
