"""
FropTools Support Bot
---------------------
Telegram support bot: license-key purchase flow + live agent relay.
Runs as a Render Web Service (webhook mode).

Env vars required:
  BOT_TOKEN     - 8974038484:AAF1u0OSN66FyU7yfrRQEUzc_bgHQtpIQyE
  ADMIN_ID      - 7596633569
  WEBHOOK_URL   - your Render external URL, e.g. https://frop-support.onrender.com
Optional:
  PORT          - injected by Render automatically
  DB_PATH       - defaults to support.db
"""

import logging
import os
import secrets
import sqlite3
import string
from datetime import datetime, timezone

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").rstrip("/")
PORT = int(os.environ.get("PORT", "10000"))
DB_PATH = os.environ.get("DB_PATH", "support.db")

TOOLS = [
    "FropLog",
    "FropTrack",
    "Fropcasflaz",
    "FropbybFlaz",
    "Fropbiflaz",
    "Fropzflaz",
    "FropFlight",
]

DURATIONS = [30, 60, 90, 120, 360]

WELCOME = (
    "👋 <b>Welcome to Froptools Support Portal</b>\n\n"
    "How may we help you today?"
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("frop-support")

# ----------------------------------------------------------------------------
# Storage (sqlite — survives restarts if you attach a Render disk)
# ----------------------------------------------------------------------------


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS msg_map (
                admin_msg_id INTEGER PRIMARY KEY,
                user_id      INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tickets (
                ref        TEXT PRIMARY KEY,
                user_id    INTEGER NOT NULL,
                username   TEXT,
                tool       TEXT,
                days       INTEGER,
                status     TEXT DEFAULT 'awaiting_payment',
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS state (
                user_id    INTEGER PRIMARY KEY,
                mode       TEXT,           -- 'idle' | 'await_message' | 'live'
                tool       TEXT,
                last_ref   TEXT
            );
            """
        )


def get_state(user_id: int) -> dict:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM state WHERE user_id = ?", (user_id,)
        ).fetchone()
    return dict(row) if row else {"user_id": user_id, "mode": "idle", "tool": None, "last_ref": None}


def set_state(user_id: int, **kw):
    cur = get_state(user_id)
    cur.update(kw)
    with db() as conn:
        conn.execute(
            "INSERT INTO state (user_id, mode, tool, last_ref) VALUES (?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET mode=excluded.mode, "
            "tool=excluded.tool, last_ref=excluded.last_ref",
            (user_id, cur.get("mode"), cur.get("tool"), cur.get("last_ref")),
        )


def map_msg(admin_msg_id: int, user_id: int):
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO msg_map (admin_msg_id, user_id) VALUES (?,?)",
            (admin_msg_id, user_id),
        )


def lookup_msg(admin_msg_id: int):
    with db() as conn:
        row = conn.execute(
            "SELECT user_id FROM msg_map WHERE admin_msg_id = ?", (admin_msg_id,)
        ).fetchone()
    return row["user_id"] if row else None


def new_reference() -> str:
    alphabet = string.ascii_uppercase + string.digits
    while True:
        ref = "FT-" + "".join(secrets.choice(alphabet) for _ in range(4)) + "-" + \
              "".join(secrets.choice(alphabet) for _ in range(4))
        with db() as conn:
            hit = conn.execute("SELECT 1 FROM tickets WHERE ref = ?", (ref,)).fetchone()
        if not hit:
            return ref


def save_ticket(ref, user, tool, days):
    with db() as conn:
        conn.execute(
            "INSERT INTO tickets (ref, user_id, username, tool, days, status, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                ref,
                user.id,
                user.username or user.full_name,
                tool,
                days,
                "awaiting_payment",
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )


# ----------------------------------------------------------------------------
# Keyboards
# ----------------------------------------------------------------------------


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔑 Buy License Key", callback_data="menu:buy")],
            [InlineKeyboardButton("💬 Contact Live Agent", callback_data="menu:agent")],
        ]
    )


def tools_menu() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(name, callback_data=f"tool:{i}")]
        for i, name in enumerate(TOOLS)
    ]
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="menu:home")])
    return InlineKeyboardMarkup(rows)


def days_menu() -> InlineKeyboardMarkup:
    rows, buf = [], []
    for d in DURATIONS:
        buf.append(InlineKeyboardButton(f"{d} days", callback_data=f"days:{d}"))
        if len(buf) == 3:
            rows.append(buf)
            buf = []
    if buf:
        rows.append(buf)
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="menu:buy")])
    return InlineKeyboardMarkup(rows)


def end_chat_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔚 End chat / Main menu", callback_data="menu:home")]]
    )


def user_label(user) -> str:
    handle = f"@{user.username}" if user.username else "no username"
    return f"{user.full_name} ({handle}) — <code>{user.id}</code>"


# ----------------------------------------------------------------------------
# User handlers
# ----------------------------------------------------------------------------


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id == ADMIN_ID:
        return await admin_help(update, context)
    set_state(user.id, mode="idle", tool=None)
    await update.effective_message.reply_text(
        WELCOME, reply_markup=main_menu(), parse_mode=ParseMode.HTML
    )


async def on_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    action = q.data.split(":", 1)[1]
    user = q.from_user

    if action == "home":
        set_state(user.id, mode="idle", tool=None)
        await q.edit_message_text(
            WELCOME, reply_markup=main_menu(), parse_mode=ParseMode.HTML
        )

    elif action == "buy":
        set_state(user.id, mode="idle", tool=None)
        await q.edit_message_text(
            "🧰 <b>Which tool is the license for?</b>\n\nSelect from the list below:",
            reply_markup=tools_menu(),
            parse_mode=ParseMode.HTML,
        )

    elif action == "agent":
        set_state(user.id, mode="await_message")
        await q.edit_message_text(
            "💬 <b>Contact Live Agent</b>\n\n"
            "Please type the message you'd like to send to our support team.\n"
            "An agent will reply here shortly.",
            parse_mode=ParseMode.HTML,
        )


async def on_tool(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    idx = int(q.data.split(":", 1)[1])
    tool = TOOLS[idx]
    set_state(q.from_user.id, tool=tool)
    await q.edit_message_text(
        f"🧰 Tool: <b>{tool}</b>\n\n⏳ <b>How many days should the license key cover?</b>",
        reply_markup=days_menu(),
        parse_mode=ParseMode.HTML,
    )


async def on_days(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    days = int(q.data.split(":", 1)[1])
    user = q.from_user
    st = get_state(user.id)
    tool = st.get("tool")

    if not tool:
        return await q.edit_message_text(
            "Session expired. Please start again.", reply_markup=main_menu()
        )

    ref = new_reference()
    save_ticket(ref, user, tool, days)
    set_state(user.id, mode="live", last_ref=ref)

    await q.edit_message_text(
        "✅ <b>Order reference generated</b>\n\n"
        f"🔖 Reference: <code>{ref}</code>\n"
        f"🧰 Tool: <b>{tool}</b>\n"
        f"⏳ Duration: <b>{days} days</b>\n\n"
        "🔄 <i>Connecting you to a live agent to complete payment…</i>\n\n"
        "An agent will send you the payment details in this chat. "
        "You can reply here at any time.",
        parse_mode=ParseMode.HTML,
        reply_markup=end_chat_menu(),
    )

    sent = await context.bot.send_message(
        ADMIN_ID,
        "🧾 <b>NEW LICENSE REQUEST</b>\n\n"
        f"🔖 Ref: <code>{ref}</code>\n"
        f"🧰 Tool: <b>{tool}</b>\n"
        f"⏳ Duration: <b>{days} days</b>\n"
        f"👤 User: {user_label(user)}\n\n"
        "↩️ <i>Reply to this message to send payment details to the customer.</i>",
        parse_mode=ParseMode.HTML,
    )
    map_msg(sent.message_id, user.id)


async def on_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Any non-command message from a normal user."""
    msg = update.effective_message
    user = update.effective_user
    st = get_state(user.id)

    # Not in a conversation yet -> show the menu
    if st.get("mode") not in ("await_message", "live"):
        return await msg.reply_text(
            WELCOME, reply_markup=main_menu(), parse_mode=ParseMode.HTML
        )

    first_contact = st.get("mode") == "await_message"
    set_state(user.id, mode="live")

    header = await context.bot.send_message(
        ADMIN_ID,
        ("💬 <b>NEW SUPPORT MESSAGE</b>\n\n" if first_contact else "💬 <b>Message</b>\n\n")
        + f"👤 {user_label(user)}"
        + (f"\n🔖 Ref: <code>{st['last_ref']}</code>" if st.get("last_ref") else "")
        + "\n\n↩️ <i>Reply to this message to answer.</i>",
        parse_mode=ParseMode.HTML,
    )
    map_msg(header.message_id, user.id)

    copied = await context.bot.copy_message(
        chat_id=ADMIN_ID, from_chat_id=msg.chat_id, message_id=msg.message_id
    )
    map_msg(copied.message_id, user.id)

    if first_contact:
        await msg.reply_text(
            "✅ Your message has been sent to our support team.\n"
            "An agent will reply here shortly.",
            reply_markup=end_chat_menu(),
        )


# ----------------------------------------------------------------------------
# Admin handlers
# ----------------------------------------------------------------------------


async def admin_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "🛠 <b>Admin console</b>\n\n"
        "• Reply to any forwarded message to answer that customer.\n"
        "• <code>/reply &lt;user_id&gt; &lt;text&gt;</code> — answer without replying.\n"
        "• <code>/tickets</code> — last 10 license requests.\n"
        "• <code>/close &lt;user_id&gt;</code> — end that customer's live chat.",
        parse_mode=ParseMode.HTML,
    )


async def admin_relay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin replies to a forwarded message -> deliver to that customer."""
    msg = update.effective_message
    if not msg.reply_to_message:
        return await msg.reply_text(
            "↩️ Reply to a customer's message, or use /reply <user_id> <text>."
        )

    user_id = lookup_msg(msg.reply_to_message.message_id)
    if not user_id:
        return await msg.reply_text(
            "⚠️ I can't match that message to a customer. Use /reply <user_id> <text>."
        )

    try:
        if msg.text:
            await context.bot.send_message(
                user_id, f"👨‍💼 <b>Support Agent</b>\n\n{msg.text_html}",
                parse_mode=ParseMode.HTML,
            )
        else:
            await context.bot.copy_message(
                chat_id=user_id, from_chat_id=msg.chat_id, message_id=msg.message_id
            )
        set_state(user_id, mode="live")
        await msg.reply_text("✅ Delivered.")
    except Exception as e:  # user blocked the bot, etc.
        await msg.reply_text(f"❌ Could not deliver: {e}")


async def cmd_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if len(context.args) < 2:
        return await msg.reply_text("Usage: /reply <user_id> <message>")
    try:
        user_id = int(context.args[0])
    except ValueError:
        return await msg.reply_text("First argument must be a numeric user id.")
    text = " ".join(context.args[1:])
    try:
        await context.bot.send_message(
            user_id, f"👨‍💼 <b>Support Agent</b>\n\n{text}", parse_mode=ParseMode.HTML
        )
        set_state(user_id, mode="live")
        await msg.reply_text("✅ Delivered.")
    except Exception as e:
        await msg.reply_text(f"❌ Could not deliver: {e}")


async def cmd_tickets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM tickets ORDER BY created_at DESC LIMIT 10"
        ).fetchall()
    if not rows:
        return await update.effective_message.reply_text("No tickets yet.")
    out = ["🧾 <b>Recent license requests</b>\n"]
    for r in rows:
        out.append(
            f"<code>{r['ref']}</code> — {r['tool']} / {r['days']}d — "
            f"@{r['username']} (<code>{r['user_id']}</code>) — {r['status']}"
        )
    await update.effective_message.reply_text(
        "\n".join(out), parse_mode=ParseMode.HTML
    )


async def cmd_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not context.args:
        return await msg.reply_text("Usage: /close <user_id>")
    user_id = int(context.args[0])
    set_state(user_id, mode="idle", tool=None)
    try:
        await context.bot.send_message(
            user_id,
            "✅ This support session has been closed. Tap below if you need anything else.",
            reply_markup=main_menu(),
        )
    except Exception:
        pass
    await msg.reply_text("Closed.")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Error while handling update", exc_info=context.error)


# ----------------------------------------------------------------------------
# Wiring
# ----------------------------------------------------------------------------


def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    admin = filters.User(ADMIN_ID)

    # Admin commands first
    app.add_handler(CommandHandler("reply", cmd_reply, filters=admin))
    app.add_handler(CommandHandler("tickets", cmd_tickets, filters=admin))
    app.add_handler(CommandHandler("close", cmd_close, filters=admin))
    app.add_handler(CommandHandler("admin", admin_help, filters=admin))

    app.add_handler(CommandHandler(["start", "menu", "help"], start))

    app.add_handler(CallbackQueryHandler(on_menu, pattern=r"^menu:"))
    app.add_handler(CallbackQueryHandler(on_tool, pattern=r"^tool:"))
    app.add_handler(CallbackQueryHandler(on_days, pattern=r"^days:"))

    app.add_handler(MessageHandler(admin & ~filters.COMMAND, admin_relay))
    app.add_handler(MessageHandler(~filters.COMMAND, on_user_message))

    app.add_error_handler(on_error)

    if WEBHOOK_URL:
        log.info("Starting webhook on port %s", PORT)
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=BOT_TOKEN,
            webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}",
            drop_pending_updates=True,
        )
    else:
        log.info("WEBHOOK_URL not set — falling back to polling (local dev)")
        app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
