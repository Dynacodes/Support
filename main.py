"""
FropTools Support Bot — v2 (premium custom emoji)
-------------------------------------------------
Telegram support bot: license-key purchase flow + live agent relay.
Runs as a Render Web Service (webhook mode).

Env vars:
  BOT_TOKEN        - from @BotFather                              (required)
  ADMIN_ID         - your numeric Telegram user id                (required)
  WEBHOOK_URL      - falls back to RENDER_EXTERNAL_URL            (optional)
  PREMIUM_EMOJI    - "1" (default) / "0" to force plain unicode   (optional)
  DB_PATH          - defaults to support.db                       (optional)
"""

import asyncio
import logging
import os
import re
import secrets
import sqlite3
import string
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
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
WEBHOOK_URL = os.environ.get(
    "WEBHOOK_URL", os.environ.get("RENDER_EXTERNAL_URL", "")
).rstrip("/")
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

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("frop-support")

# ----------------------------------------------------------------------------
# Premium (custom) emoji layer
# ----------------------------------------------------------------------------
#
# Telegram renders <tg-emoji emoji-id="..."> only under HTML parse mode.
# Every entry carries a plain-unicode fallback, used when:
#   * PREMIUM_EMOJI=0
#   * the target surface can't render entities (inline buttons, etc.)
#   * Telegram rejects the entity at send time (auto-degrade, see send_html)

EMOJI = {
    "gem":       ("5226656353744862682", "\U0001F48E"),    # 💎
    "star":      ("5980995951160987855", "\u2B50"),        # ⭐
    "check":     ("5278622189556354905", "\u2705"),        # ✅
    "bolt":      ("5084974483685507801", "\u26A1"),        # ⚡
    "cross":     ("5042112436648281096", "\u274C"),        # ❌
    "globe":     ("5134452506935427991", "\U0001F30D"),    # 🌍
    "user":      ("5321304384838057247", "\U0001F464"),    # 👤
    "rocket":    ("5195033767969839232", "\U0001F680"),    # 🚀
    "sparkle":   ("5172739056592749710", "\u2728"),        # ✨
    "hourglass": ("5215327832040811010", "\u23F3"),        # ⏳
    "info":      ("5303138782004924588", "\u2139\uFE0F"),  # ℹ️
}

# Flipped to False automatically if Telegram refuses custom emoji entities.
PREMIUM_ENABLED = os.environ.get("PREMIUM_EMOJI", "1") != "0"

_TG_EMOJI_RE = re.compile(r'<tg-emoji emoji-id="\d+">(.*?)</tg-emoji>', re.S)


def e(name: str) -> str:
    """Premium emoji markup (HTML parse mode) with unicode fallback."""
    emoji_id, fallback = EMOJI[name]
    if not PREMIUM_ENABLED:
        return fallback
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


def plain(name: str) -> str:
    """Plain unicode only — for inline button labels, which strip entities."""
    return EMOJI[name][1]


def degrade(text: str) -> str:
    """Replace every <tg-emoji> wrapper with its inner unicode character."""
    return _TG_EMOJI_RE.sub(r"\1", text)


async def send_html(bot, chat_id: int, text: str, **kwargs):
    """
    send_message in HTML mode, degrading premium emoji if Telegram rejects them.

    Custom emoji entities are only permitted for bots that bought a username on
    Fragment. Rather than fail, we strip the tags once and remember the result
    for the rest of the process lifetime.
    """
    global PREMIUM_ENABLED
    try:
        return await bot.send_message(
            chat_id, text, parse_mode=ParseMode.HTML, **kwargs
        )
    except BadRequest as exc:
        if "custom emoji" not in str(exc).lower():
            raise
        PREMIUM_ENABLED = False
        log.warning("Custom emoji rejected by Telegram — falling back to unicode")
        return await bot.send_message(
            chat_id, degrade(text), parse_mode=ParseMode.HTML, **kwargs
        )


async def edit_html(query, text: str, **kwargs):
    """Same degrade-on-failure behaviour for callback query edits."""
    global PREMIUM_ENABLED
    try:
        return await query.edit_message_text(
            text, parse_mode=ParseMode.HTML, **kwargs
        )
    except BadRequest as exc:
        msg = str(exc).lower()
        if "not modified" in msg:
            return
        if "custom emoji" not in msg:
            raise
        PREMIUM_ENABLED = False
        log.warning("Custom emoji rejected by Telegram — falling back to unicode")
        return await query.edit_message_text(
            degrade(text), parse_mode=ParseMode.HTML, **kwargs
        )


# ----------------------------------------------------------------------------
# Copy
# ----------------------------------------------------------------------------


def welcome_text() -> str:
    return (
        f"{e('sparkle')} <b>Welcome to Froptools Support Portal</b>\n"
        f"{e('globe')} <i>Premium tools · instant licensing · live support</i>\n\n"
        "How may we help you today?"
    )


# ----------------------------------------------------------------------------
# Storage
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
                mode       TEXT,
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
    return dict(row) if row else {
        "user_id": user_id, "mode": "idle", "tool": None, "last_ref": None
    }


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
        ref = (
            "FT-"
            + "".join(secrets.choice(alphabet) for _ in range(4))
            + "-"
            + "".join(secrets.choice(alphabet) for _ in range(4))
        )
        with db() as conn:
            if not conn.execute(
                "SELECT 1 FROM tickets WHERE ref = ?", (ref,)
            ).fetchone():
                return ref


def save_ticket(ref, user, tool, days):
    with db() as conn:
        conn.execute(
            "INSERT INTO tickets (ref, user_id, username, tool, days, status, "
            "created_at) VALUES (?,?,?,?,?,?,?)",
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
# Keyboards  (button labels use plain unicode — Telegram strips entities here)
# ----------------------------------------------------------------------------


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(
                f"{plain('gem')}  Buy License Key", callback_data="menu:buy"
            )],
            [InlineKeyboardButton(
                f"{plain('user')}  Contact Live Agent", callback_data="menu:agent"
            )],
        ]
    )


def tools_menu() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"{plain('star')}  {name}", callback_data=f"tool:{i}")]
        for i, name in enumerate(TOOLS)
    ]
    rows.append([InlineKeyboardButton("‹  Back", callback_data="menu:home")])
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
    rows.append([InlineKeyboardButton("‹  Back", callback_data="menu:buy")])
    return InlineKeyboardMarkup(rows)


def end_chat_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("‹  End chat / Main menu", callback_data="menu:home")]]
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
    await send_html(
        context.bot, user.id, welcome_text(), reply_markup=main_menu()
    )


async def on_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    action = q.data.split(":", 1)[1]
    user = q.from_user

    if action == "home":
        set_state(user.id, mode="idle", tool=None)
        await edit_html(q, welcome_text(), reply_markup=main_menu())

    elif action == "buy":
        set_state(user.id, mode="idle", tool=None)
        await edit_html(
            q,
            f"{e('rocket')} <b>Select Your Tool</b>\n\n"
            f"{e('info')} Which product is this license for?",
            reply_markup=tools_menu(),
        )

    elif action == "agent":
        set_state(user.id, mode="await_message")
        await edit_html(
            q,
            f"{e('user')} <b>Live Agent</b>\n\n"
            "Type the message you'd like to send to our support team.\n"
            f"{e('bolt')} An agent will reply right here.",
        )


async def on_tool(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    tool = TOOLS[int(q.data.split(":", 1)[1])]
    set_state(q.from_user.id, tool=tool)
    await edit_html(
        q,
        f"{e('star')} Tool: <b>{tool}</b>\n\n"
        f"{e('hourglass')} <b>Choose your license duration</b>",
        reply_markup=days_menu(),
    )


async def on_days(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    days = int(q.data.split(":", 1)[1])
    user = q.from_user
    tool = get_state(user.id).get("tool")

    if not tool:
        return await edit_html(
            q,
            f"{e('cross')} Session expired. Please start again.",
            reply_markup=main_menu(),
        )

    ref = new_reference()
    save_ticket(ref, user, tool, days)
    set_state(user.id, mode="live", last_ref=ref)

    await edit_html(
        q,
        f"{e('check')} <b>Order Reference Generated</b>\n\n"
        f"{e('gem')} Reference: <code>{ref}</code>\n"
        f"{e('star')} Tool: <b>{tool}</b>\n"
        f"{e('hourglass')} Duration: <b>{days} days</b>\n\n"
        f"{e('bolt')} <i>Connecting you to a live agent to complete payment…</i>\n\n"
        f"{e('info')} Payment details will arrive in this chat. "
        "You can reply here at any time.",
        reply_markup=end_chat_menu(),
    )

    sent = await send_html(
        context.bot,
        ADMIN_ID,
        f"{e('gem')} <b>NEW LICENSE REQUEST</b>\n\n"
        f"{e('star')} Ref: <code>{ref}</code>\n"
        f"{e('rocket')} Tool: <b>{tool}</b>\n"
        f"{e('hourglass')} Duration: <b>{days} days</b>\n"
        f"{e('user')} User: {user_label(user)}\n\n"
        f"{e('info')} <i>Reply to this message to send payment details.</i>",
    )
    map_msg(sent.message_id, user.id)


async def on_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    st = get_state(user.id)

    if st.get("mode") not in ("await_message", "live"):
        return await send_html(
            context.bot, user.id, welcome_text(), reply_markup=main_menu()
        )

    first_contact = st.get("mode") == "await_message"
    set_state(user.id, mode="live")

    header = (
        f"{e('sparkle')} <b>NEW SUPPORT MESSAGE</b>\n\n"
        if first_contact
        else f"{e('bolt')} <b>Message</b>\n\n"
    )
    header += f"{e('user')} {user_label(user)}"
    if st.get("last_ref"):
        header += f"\n{e('gem')} Ref: <code>{st['last_ref']}</code>"
    header += f"\n\n{e('info')} <i>Reply to this message to answer.</i>"

    sent = await send_html(context.bot, ADMIN_ID, header)
    map_msg(sent.message_id, user.id)

    copied = await context.bot.copy_message(
        chat_id=ADMIN_ID, from_chat_id=msg.chat_id, message_id=msg.message_id
    )
    map_msg(copied.message_id, user.id)

    if first_contact:
        await send_html(
            context.bot,
            user.id,
            f"{e('check')} Your message has been delivered to our support team.\n"
            f"{e('hourglass')} An agent will reply here shortly.",
            reply_markup=end_chat_menu(),
        )


# ----------------------------------------------------------------------------
# Admin handlers
# ----------------------------------------------------------------------------


async def admin_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_html(
        context.bot,
        ADMIN_ID,
        f"{e('bolt')} <b>Admin Console</b>\n\n"
        f"{e('user')} Reply to any forwarded message to answer that customer.\n"
        f"{e('star')} <code>/reply &lt;user_id&gt; &lt;text&gt;</code> — answer directly\n"
        f"{e('gem')} <code>/tickets</code> — last 10 license requests\n"
        f"{e('cross')} <code>/close &lt;user_id&gt;</code> — end a live chat\n"
        f"{e('sparkle')} <code>/emoji</code> — premium emoji status",
    )


async def cmd_emoji(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = "active" if PREMIUM_ENABLED else "disabled (unicode fallback)"
    sample = "  ".join(e(k) for k in EMOJI)
    await send_html(
        context.bot,
        ADMIN_ID,
        f"{e('info')} Premium emoji: <b>{state}</b>\n\n{sample}",
    )


async def admin_relay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg.reply_to_message:
        return await send_html(
            context.bot,
            ADMIN_ID,
            f"{e('info')} Reply to a customer's message, or use "
            "<code>/reply &lt;user_id&gt; &lt;text&gt;</code>.",
        )

    user_id = lookup_msg(msg.reply_to_message.message_id)
    if not user_id:
        return await send_html(
            context.bot,
            ADMIN_ID,
            f"{e('cross')} Can't match that message to a customer. "
            "Use <code>/reply &lt;user_id&gt; &lt;text&gt;</code>.",
        )

    try:
        if msg.text:
            await send_html(
                context.bot,
                user_id,
                f"{e('user')} <b>Support Agent</b>\n\n{msg.text_html}",
            )
        else:
            await context.bot.copy_message(
                chat_id=user_id, from_chat_id=msg.chat_id, message_id=msg.message_id
            )
        set_state(user_id, mode="live")
        await msg.reply_text(f"{plain('check')} Delivered.")
    except Exception as exc:
        await msg.reply_text(f"{plain('cross')} Could not deliver: {exc}")


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
        await send_html(
            context.bot, user_id, f"{e('user')} <b>Support Agent</b>\n\n{text}"
        )
        set_state(user_id, mode="live")
        await msg.reply_text(f"{plain('check')} Delivered.")
    except Exception as exc:
        await msg.reply_text(f"{plain('cross')} Could not deliver: {exc}")


async def cmd_tickets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM tickets ORDER BY created_at DESC LIMIT 10"
        ).fetchall()
    if not rows:
        return await send_html(
            context.bot, ADMIN_ID, f"{e('info')} No tickets yet."
        )
    out = [f"{e('gem')} <b>Recent License Requests</b>\n"]
    for r in rows:
        out.append(
            f"{e('star')} <code>{r['ref']}</code> — {r['tool']} / {r['days']}d\n"
            f"     @{r['username']} (<code>{r['user_id']}</code>) · {r['status']}"
        )
    await send_html(context.bot, ADMIN_ID, "\n".join(out))


async def cmd_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not context.args:
        return await msg.reply_text("Usage: /close <user_id>")
    user_id = int(context.args[0])
    set_state(user_id, mode="idle", tool=None)
    try:
        await send_html(
            context.bot,
            user_id,
            f"{e('check')} This support session has been closed.\n"
            f"{e('sparkle')} Tap below if you need anything else.",
            reply_markup=main_menu(),
        )
    except Exception:
        pass
    await msg.reply_text(f"{plain('check')} Closed.")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Error while handling update", exc_info=context.error)


# ----------------------------------------------------------------------------
# Wiring
# ----------------------------------------------------------------------------


def main():
    init_db()
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    app = Application.builder().token(BOT_TOKEN).build()
    admin = filters.User(ADMIN_ID)

    app.add_handler(CommandHandler("reply", cmd_reply, filters=admin))
    app.add_handler(CommandHandler("tickets", cmd_tickets, filters=admin))
    app.add_handler(CommandHandler("close", cmd_close, filters=admin))
    app.add_handler(CommandHandler("emoji", cmd_emoji, filters=admin))
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
        log.info("No webhook URL — polling mode (local dev)")
        app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
