"""
FropTools Support Bot — v3
--------------------------
Adds: broadcast, typing indicators, rate limiting, out-of-hours auto-reply,
ticket lifecycle (open/paid/delivered/closed/reopened) and admin inline
action buttons on every ticket.

Env vars:
  BOT_TOKEN        - from @BotFather                              (required)
  ADMIN_ID         - your numeric Telegram user id                (required)
  WEBHOOK_URL      - falls back to RENDER_EXTERNAL_URL            (optional)
  PREMIUM_EMOJI    - "1" (default) / "0" to force plain unicode   (optional)
  OPEN_HOUR        - support opens, WAT 24h clock (default 9)     (optional)
  CLOSE_HOUR       - support closes, WAT 24h clock (default 23)   (optional)
  RATE_MAX         - msgs allowed per window (default 6)          (optional)
  RATE_WINDOW      - window in seconds (default 20)               (optional)
  DB_PATH          - defaults to support.db                       (optional)
"""

import asyncio
import logging
import os
import re
import secrets
import sqlite3
import string
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, Forbidden
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

# Nigeria is UTC+1 year round (no DST), so a fixed offset is safer than
# depending on system tzdata being present in the container.
WAT = timezone(timedelta(hours=1), "WAT")
OPEN_HOUR = int(os.environ.get("OPEN_HOUR", "9"))
CLOSE_HOUR = int(os.environ.get("CLOSE_HOUR", "23"))

RATE_MAX = int(os.environ.get("RATE_MAX", "6"))
RATE_WINDOW = int(os.environ.get("RATE_WINDOW", "20"))

TOOLS = [
    "FropLogger",
    "FropTrack",
    "FropPaypalFlash",
    "FropCashappFlash",
    "FropBybitFlash",
    "FropBinanceFlash",
    "FropZelleFlash",
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

PREMIUM_ENABLED = os.environ.get("PREMIUM_EMOJI", "1") != "0"
_TG_EMOJI_RE = re.compile(r'<tg-emoji emoji-id="\d+">(.*?)</tg-emoji>', re.S)


def e(name: str) -> str:
    emoji_id, fallback = EMOJI[name]
    if not PREMIUM_ENABLED:
        return fallback
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


def plain(name: str) -> str:
    return EMOJI[name][1]


def degrade(text: str) -> str:
    return _TG_EMOJI_RE.sub(r"\1", text)


async def send_html(bot, chat_id: int, text: str, **kwargs):
    global PREMIUM_ENABLED
    try:
        return await bot.send_message(
            chat_id, text, parse_mode=ParseMode.HTML, **kwargs
        )
    except BadRequest as exc:
        if "custom emoji" not in str(exc).lower():
            raise
        PREMIUM_ENABLED = False
        log.warning("Custom emoji rejected — falling back to unicode")
        return await bot.send_message(
            chat_id, degrade(text), parse_mode=ParseMode.HTML, **kwargs
        )


async def edit_html(query, text: str, **kwargs):
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
        return await query.edit_message_text(
            degrade(text), parse_mode=ParseMode.HTML, **kwargs
        )


async def typing(bot, chat_id: int, seconds: float = 0.6):
    """Show a typing indicator so relayed replies feel live."""
    try:
        await bot.send_chat_action(chat_id, ChatAction.TYPING)
        await asyncio.sleep(seconds)
    except Exception:
        pass


# ----------------------------------------------------------------------------
# Business hours
# ----------------------------------------------------------------------------


def now_wat() -> datetime:
    return datetime.now(WAT)


def within_hours() -> bool:
    return OPEN_HOUR <= now_wat().hour < CLOSE_HOUR


def hours_label() -> str:
    def fmt(h):
        suffix = "AM" if h < 12 else "PM"
        display = h if 1 <= h <= 12 else abs(h - 12) or 12
        return f"{display}:00 {suffix}"
    return f"{fmt(OPEN_HOUR)} – {fmt(CLOSE_HOUR)} WAT"


def offline_notice() -> str:
    now = now_wat()
    if now.hour < OPEN_HOUR:
        when = f"today at {OPEN_HOUR}:00"
    else:
        when = f"tomorrow at {OPEN_HOUR}:00"
    return (
        f"{e('hourglass')} <b>Our agents are offline right now.</b>\n"
        f"{e('info')} Support hours: <b>{hours_label()}</b>\n\n"
        f"Your message is logged and an agent will reply {when} WAT."
    )


# ----------------------------------------------------------------------------
# Rate limiting (in-memory sliding window, per user)
# ----------------------------------------------------------------------------

_hits: dict[int, deque] = defaultdict(deque)
_warned: dict[int, float] = {}


def rate_ok(user_id: int) -> bool:
    now = time.time()
    q = _hits[user_id]
    while q and now - q[0] > RATE_WINDOW:
        q.popleft()
    if len(q) >= RATE_MAX:
        return False
    q.append(now)
    return True


def should_warn(user_id: int) -> bool:
    """Warn at most once per window so the limiter can't be used to spam back."""
    now = time.time()
    if now - _warned.get(user_id, 0) < RATE_WINDOW:
        return False
    _warned[user_id] = now
    return True


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
                status     TEXT DEFAULT 'open',
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS state (
                user_id    INTEGER PRIMARY KEY,
                mode       TEXT,
                tool       TEXT,
                last_ref   TEXT
            );
            CREATE TABLE IF NOT EXISTS users (
                user_id    INTEGER PRIMARY KEY,
                username   TEXT,
                full_name  TEXT,
                first_seen TEXT,
                active     INTEGER DEFAULT 1
            );
            """
        )
        # migrations for databases created by v1/v2
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(tickets)")}
        if "kind" not in cols:
            conn.execute(
                "ALTER TABLE tickets ADD COLUMN kind TEXT DEFAULT 'license'"
            )
        if "admin_msg_id" not in cols:
            conn.execute("ALTER TABLE tickets ADD COLUMN admin_msg_id INTEGER")


def remember_user(user):
    with db() as conn:
        conn.execute(
            "INSERT INTO users (user_id, username, full_name, first_seen, active) "
            "VALUES (?,?,?,?,1) ON CONFLICT(user_id) DO UPDATE SET "
            "username=excluded.username, full_name=excluded.full_name, active=1",
            (
                user.id,
                user.username,
                user.full_name,
                now_wat().isoformat(timespec="seconds"),
            ),
        )


def deactivate_user(user_id: int):
    with db() as conn:
        conn.execute("UPDATE users SET active = 0 WHERE user_id = ?", (user_id,))


def all_active_users() -> list[int]:
    with db() as conn:
        rows = conn.execute(
            "SELECT user_id FROM users WHERE active = 1 AND user_id != ?",
            (ADMIN_ID,),
        ).fetchall()
    return [r["user_id"] for r in rows]


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


def create_ticket(user, kind, tool=None, days=None) -> str:
    ref = new_reference()
    with db() as conn:
        conn.execute(
            "INSERT INTO tickets (ref, user_id, username, tool, days, status, "
            "created_at, kind) VALUES (?,?,?,?,?,?,?,?)",
            (
                ref,
                user.id,
                user.username or user.full_name,
                tool,
                days,
                "awaiting_payment" if kind == "license" else "open",
                now_wat().isoformat(timespec="seconds"),
                kind,
            ),
        )
    return ref


def get_ticket(ref: str):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM tickets WHERE ref = ?", (ref,)
        ).fetchone()


def set_ticket_status(ref: str, status: str):
    with db() as conn:
        conn.execute(
            "UPDATE tickets SET status = ? WHERE ref = ?", (status, ref)
        )


def set_ticket_admin_msg(ref: str, msg_id: int):
    with db() as conn:
        conn.execute(
            "UPDATE tickets SET admin_msg_id = ? WHERE ref = ?", (msg_id, ref)
        )


def open_ticket_for(user_id: int):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM tickets WHERE user_id = ? AND status != 'closed' "
            "ORDER BY created_at DESC LIMIT 1",
            (user_id,),
        ).fetchone()


# ----------------------------------------------------------------------------
# Copy & keyboards
# ----------------------------------------------------------------------------


def welcome_text() -> str:
    status = (
        f"{e('bolt')} <i>Agents online now</i>"
        if within_hours()
        else f"{e('hourglass')} <i>Agents offline · back at {OPEN_HOUR}:00 WAT</i>"
    )
    return (
        f"{e('sparkle')} <b>Welcome to Froptools Support Portal</b>\n"
        f"{e('globe')} <i>Premium tools · instant licensing · live support</i>\n"
        f"{status}\n\n"
        "How may we help you today?"
    )


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


STATUS_LABEL = {
    "open": f"{plain('bolt')} Open",
    "awaiting_payment": f"{plain('hourglass')} Awaiting payment",
    "paid": f"{plain('check')} Paid",
    "delivered": f"{plain('gem')} Delivered",
    "closed": f"{plain('cross')} Closed",
}


def ticket_keyboard(ref: str, status: str, kind: str) -> InlineKeyboardMarkup:
    """Admin actions available for a ticket in its current status."""
    rows = []
    if status == "closed":
        rows.append([InlineKeyboardButton(
            "↻  Reopen ticket", callback_data=f"tk:reopen:{ref}"
        )])
    else:
        if kind == "license":
            if status == "awaiting_payment":
                rows.append([
                    InlineKeyboardButton(
                        f"{plain('check')} Mark Paid", callback_data=f"tk:paid:{ref}"
                    ),
                    InlineKeyboardButton(
                        f"{plain('gem')} Delivered",
                        callback_data=f"tk:delivered:{ref}",
                    ),
                ])
            elif status == "paid":
                rows.append([InlineKeyboardButton(
                    f"{plain('gem')} Mark Delivered",
                    callback_data=f"tk:delivered:{ref}",
                )])
        rows.append([InlineKeyboardButton(
            f"{plain('cross')} Close ticket", callback_data=f"tk:close:{ref}"
        )])
    return InlineKeyboardMarkup(rows)


def ticket_admin_text(row) -> str:
    status = STATUS_LABEL.get(row["status"], row["status"])
    if row["kind"] == "license":
        head = f"{e('gem')} <b>LICENSE REQUEST</b>"
        body = (
            f"{e('rocket')} Tool: <b>{row['tool']}</b>\n"
            f"{e('hourglass')} Duration: <b>{row['days']} days</b>\n"
        )
    else:
        head = f"{e('sparkle')} <b>SUPPORT TICKET</b>"
        body = ""
    return (
        f"{head}\n\n"
        f"{e('star')} Ref: <code>{row['ref']}</code>\n"
        f"{body}"
        f"{e('user')} User: @{row['username']} — <code>{row['user_id']}</code>\n"
        f"{e('info')} Status: <b>{status}</b>\n\n"
        f"<i>Reply to this message to talk to the customer.</i>"
    )


# ----------------------------------------------------------------------------
# User handlers
# ----------------------------------------------------------------------------


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id == ADMIN_ID:
        return await admin_help(update, context)
    remember_user(user)
    set_state(user.id, mode="idle", tool=None)
    await send_html(context.bot, user.id, welcome_text(), reply_markup=main_menu())


async def on_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    action = q.data.split(":", 1)[1]
    user = q.from_user
    remember_user(user)

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
        text = (
            f"{e('user')} <b>Live Agent</b>\n\n"
            "Type the message you'd like to send to our support team.\n"
        )
        text += (
            f"{e('bolt')} An agent will reply right here."
            if within_hours()
            else f"\n{offline_notice()}"
        )
        await edit_html(q, text)


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

    ref = create_ticket(user, "license", tool, days)
    set_state(user.id, mode="live", last_ref=ref)

    text = (
        f"{e('check')} <b>Order Reference Generated</b>\n\n"
        f"{e('gem')} Reference: <code>{ref}</code>\n"
        f"{e('star')} Tool: <b>{tool}</b>\n"
        f"{e('hourglass')} Duration: <b>{days} days</b>\n\n"
    )
    if within_hours():
        text += (
            f"{e('bolt')} <i>Connecting you to a live agent to complete payment…</i>"
            f"\n\n{e('info')} Payment details will arrive in this chat."
        )
    else:
        text += offline_notice()

    await edit_html(q, text, reply_markup=end_chat_menu())

    await typing(context.bot, ADMIN_ID)
    row = get_ticket(ref)
    sent = await send_html(
        context.bot,
        ADMIN_ID,
        ticket_admin_text(row),
        reply_markup=ticket_keyboard(ref, row["status"], row["kind"]),
    )
    map_msg(sent.message_id, user.id)
    set_ticket_admin_msg(ref, sent.message_id)


async def on_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    remember_user(user)
    st = get_state(user.id)

    if st.get("mode") not in ("await_message", "live"):
        return await send_html(
            context.bot, user.id, welcome_text(), reply_markup=main_menu()
        )

    if not rate_ok(user.id):
        if should_warn(user.id):
            await send_html(
                context.bot,
                user.id,
                f"{e('hourglass')} You're sending messages too quickly.\n"
                f"{e('info')} Please wait about {RATE_WINDOW} seconds — "
                "your earlier messages have already reached our team.",
            )
        return

    first_contact = st.get("mode") == "await_message"
    ref = st.get("last_ref")

    if first_contact and not ref:
        ref = create_ticket(user, "support")
        set_state(user.id, mode="live", last_ref=ref)
    else:
        set_state(user.id, mode="live")

    await typing(context.bot, ADMIN_ID)

    if first_contact:
        row = get_ticket(ref)
        header = await send_html(
            context.bot,
            ADMIN_ID,
            ticket_admin_text(row),
            reply_markup=ticket_keyboard(ref, row["status"], row["kind"]),
        )
        set_ticket_admin_msg(ref, header.message_id)
    else:
        header = await send_html(
            context.bot,
            ADMIN_ID,
            f"{e('bolt')} <b>Message</b> · <code>{ref}</code>\n"
            f"{e('user')} {user_label(user)}",
        )
    map_msg(header.message_id, user.id)

    copied = await context.bot.copy_message(
        chat_id=ADMIN_ID, from_chat_id=msg.chat_id, message_id=msg.message_id
    )
    map_msg(copied.message_id, user.id)

    if first_contact:
        ack = (
            f"{e('check')} Your message has been delivered to our support team.\n"
            f"{e('gem')} Ticket: <code>{ref}</code>\n"
        )
        ack += (
            f"{e('hourglass')} An agent will reply here shortly."
            if within_hours()
            else f"\n{offline_notice()}"
        )
        await send_html(context.bot, user.id, ack, reply_markup=end_chat_menu())
    elif not within_hours():
        await send_html(context.bot, user.id, offline_notice())


# ----------------------------------------------------------------------------
# Ticket action buttons (admin)
# ----------------------------------------------------------------------------


async def on_ticket_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != ADMIN_ID:
        return await q.answer("Not authorised.", show_alert=True)

    _, action, ref = q.data.split(":", 2)
    row = get_ticket(ref)
    if not row:
        return await q.answer("Ticket not found.", show_alert=True)

    new_status = {
        "paid": "paid",
        "delivered": "delivered",
        "close": "closed",
        "reopen": "open" if row["kind"] == "support" else "awaiting_payment",
    }[action]

    set_ticket_status(ref, new_status)
    await q.answer(f"Ticket {ref} → {new_status}")

    updated = get_ticket(ref)
    await edit_html(
        q,
        ticket_admin_text(updated),
        reply_markup=ticket_keyboard(ref, new_status, updated["kind"]),
    )

    notices = {
        "paid": (
            f"{e('check')} <b>Payment confirmed</b>\n\n"
            f"{e('gem')} Ref: <code>{ref}</code>\n"
            f"{e('hourglass')} Your license is being prepared — hold tight."
        ),
        "delivered": (
            f"{e('gem')} <b>License delivered</b>\n\n"
            f"{e('star')} Ref: <code>{ref}</code>\n"
            f"{e('sparkle')} Thank you for choosing Froptools!"
        ),
        "closed": (
            f"{e('check')} <b>Ticket closed</b>\n\n"
            f"{e('info')} Ref <code>{ref}</code> has been resolved. "
            "Tap below if you need anything else."
        ),
        "open": (
            f"{e('bolt')} <b>Ticket reopened</b>\n\n"
            f"{e('info')} Ref <code>{ref}</code> is active again — "
            "an agent is with you."
        ),
        "awaiting_payment": (
            f"{e('bolt')} <b>Ticket reopened</b>\n\n"
            f"{e('info')} Ref <code>{ref}</code> is active again."
        ),
    }

    user_id = row["user_id"]
    if new_status == "closed":
        set_state(user_id, mode="idle", tool=None, last_ref=None)
        kb = main_menu()
    else:
        set_state(user_id, mode="live", last_ref=ref)
        kb = end_chat_menu()

    try:
        await typing(context.bot, user_id, 0.4)
        await send_html(context.bot, user_id, notices[new_status], reply_markup=kb)
    except Forbidden:
        deactivate_user(user_id)
    except Exception as exc:
        log.warning("Could not notify %s: %s", user_id, exc)


# ----------------------------------------------------------------------------
# Admin handlers
# ----------------------------------------------------------------------------


async def admin_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_html(
        context.bot,
        ADMIN_ID,
        f"{e('bolt')} <b>Admin Console</b>\n\n"
        f"{e('user')} Reply to any message to answer that customer\n"
        f"{e('star')} <code>/reply &lt;user_id&gt; &lt;text&gt;</code> — direct reply\n"
        f"{e('gem')} <code>/tickets</code> — recent tickets\n"
        f"{e('hourglass')} <code>/open</code> — tickets still open\n"
        f"{e('rocket')} <code>/broadcast &lt;text&gt;</code> — message all users\n"
        f"{e('cross')} <code>/close &lt;user_id&gt;</code> — close their ticket\n"
        f"{e('sparkle')} <code>/emoji</code> — premium emoji status\n"
        f"{e('globe')} <code>/status</code> — hours, users, live chats\n\n"
        f"{e('info')} Support hours: <b>{hours_label()}</b>",
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with db() as conn:
        users = conn.execute(
            "SELECT COUNT(*) c FROM users WHERE active = 1"
        ).fetchone()["c"]
        live = conn.execute(
            "SELECT COUNT(*) c FROM state WHERE mode = 'live'"
        ).fetchone()["c"]
        open_t = conn.execute(
            "SELECT COUNT(*) c FROM tickets WHERE status != 'closed'"
        ).fetchone()["c"]
    await send_html(
        context.bot,
        ADMIN_ID,
        f"{e('globe')} <b>Status</b>\n\n"
        f"{e('bolt')} Now: <b>{now_wat().strftime('%H:%M')} WAT</b> — "
        f"{'open' if within_hours() else 'closed'}\n"
        f"{e('info')} Hours: {hours_label()}\n"
        f"{e('user')} Reachable users: <b>{users}</b>\n"
        f"{e('sparkle')} Live chats: <b>{live}</b>\n"
        f"{e('hourglass')} Open tickets: <b>{open_t}</b>",
    )


async def cmd_emoji(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = "active" if PREMIUM_ENABLED else "disabled (unicode fallback)"
    sample = "  ".join(e(k) for k in EMOJI)
    await send_html(
        context.bot, ADMIN_ID,
        f"{e('info')} Premium emoji: <b>{state}</b>\n\n{sample}",
    )


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not context.args:
        return await msg.reply_text("Usage: /broadcast <message>")
    text = msg.text_html.split(" ", 1)[1]
    context.bot_data["broadcast"] = text
    recipients = len(all_active_users())
    await send_html(
        context.bot,
        ADMIN_ID,
        f"{e('rocket')} <b>Broadcast preview</b> → {recipients} users\n"
        f"{'─' * 24}\n\n{text}",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(
                f"{plain('check')} Send", callback_data="bc:send"
            ),
            InlineKeyboardButton(
                f"{plain('cross')} Cancel", callback_data="bc:cancel"
            ),
        ]]),
    )


async def on_broadcast_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != ADMIN_ID:
        return await q.answer("Not authorised.", show_alert=True)

    action = q.data.split(":", 1)[1]
    text = context.bot_data.get("broadcast")

    if action == "cancel" or not text:
        await q.answer("Cancelled.")
        return await edit_html(q, f"{e('cross')} Broadcast cancelled.")

    await q.answer("Sending…")
    await edit_html(q, f"{e('rocket')} <b>Broadcasting…</b>")

    sent = failed = blocked = 0
    for uid in all_active_users():
        try:
            await send_html(context.bot, uid, text)
            sent += 1
        except Forbidden:
            deactivate_user(uid)
            blocked += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.06)  # stay under Telegram's ~30 msg/s ceiling

    context.bot_data.pop("broadcast", None)
    await send_html(
        context.bot,
        ADMIN_ID,
        f"{e('check')} <b>Broadcast complete</b>\n\n"
        f"{e('rocket')} Delivered: <b>{sent}</b>\n"
        f"{e('cross')} Blocked the bot: <b>{blocked}</b>\n"
        f"{e('info')} Other failures: <b>{failed}</b>",
    )


async def admin_relay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg.reply_to_message:
        return await send_html(
            context.bot, ADMIN_ID,
            f"{e('info')} Reply to a customer's message, or use "
            "<code>/reply &lt;user_id&gt; &lt;text&gt;</code>.",
        )

    user_id = lookup_msg(msg.reply_to_message.message_id)
    if not user_id:
        return await send_html(
            context.bot, ADMIN_ID,
            f"{e('cross')} Can't match that message to a customer. "
            "Use <code>/reply &lt;user_id&gt; &lt;text&gt;</code>.",
        )

    try:
        await typing(context.bot, user_id)
        if msg.text:
            await send_html(
                context.bot, user_id,
                f"{e('user')} <b>Support Agent</b>\n\n{msg.text_html}",
            )
        else:
            await context.bot.copy_message(
                chat_id=user_id, from_chat_id=msg.chat_id, message_id=msg.message_id
            )
        row = open_ticket_for(user_id)
        set_state(user_id, mode="live", last_ref=row["ref"] if row else None)
        await msg.reply_text(f"{plain('check')} Delivered.")
    except Forbidden:
        deactivate_user(user_id)
        await msg.reply_text(f"{plain('cross')} User has blocked the bot.")
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
        await typing(context.bot, user_id)
        await send_html(
            context.bot, user_id, f"{e('user')} <b>Support Agent</b>\n\n{text}"
        )
        set_state(user_id, mode="live")
        await msg.reply_text(f"{plain('check')} Delivered.")
    except Forbidden:
        deactivate_user(user_id)
        await msg.reply_text(f"{plain('cross')} User has blocked the bot.")
    except Exception as exc:
        await msg.reply_text(f"{plain('cross')} Could not deliver: {exc}")


async def cmd_tickets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM tickets ORDER BY created_at DESC LIMIT 10"
        ).fetchall()
    await _render_tickets(context, rows, "Recent Tickets")


async def cmd_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM tickets WHERE status != 'closed' "
            "ORDER BY created_at DESC LIMIT 15"
        ).fetchall()
    await _render_tickets(context, rows, "Open Tickets")


async def _render_tickets(context, rows, title):
    if not rows:
        return await send_html(context.bot, ADMIN_ID, f"{e('info')} Nothing here yet.")
    out = [f"{e('gem')} <b>{title}</b>\n"]
    for r in rows:
        detail = f"{r['tool']} / {r['days']}d" if r["kind"] == "license" else "support"
        out.append(
            f"{e('star')} <code>{r['ref']}</code> — {detail}\n"
            f"     @{r['username']} (<code>{r['user_id']}</code>)\n"
            f"     {STATUS_LABEL.get(r['status'], r['status'])}"
        )
    await send_html(context.bot, ADMIN_ID, "\n".join(out))


async def cmd_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not context.args:
        return await msg.reply_text("Usage: /close <user_id>")
    user_id = int(context.args[0])
    row = open_ticket_for(user_id)
    if row:
        set_ticket_status(row["ref"], "closed")
    set_state(user_id, mode="idle", tool=None, last_ref=None)
    try:
        await send_html(
            context.bot, user_id,
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
    app.add_handler(CommandHandler("open", cmd_open, filters=admin))
    app.add_handler(CommandHandler("close", cmd_close, filters=admin))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast, filters=admin))
    app.add_handler(CommandHandler("emoji", cmd_emoji, filters=admin))
    app.add_handler(CommandHandler("status", cmd_status, filters=admin))
    app.add_handler(CommandHandler("admin", admin_help, filters=admin))

    app.add_handler(CommandHandler(["start", "menu", "help"], start))

    app.add_handler(CallbackQueryHandler(on_menu, pattern=r"^menu:"))
    app.add_handler(CallbackQueryHandler(on_tool, pattern=r"^tool:"))
    app.add_handler(CallbackQueryHandler(on_days, pattern=r"^days:"))
    app.add_handler(CallbackQueryHandler(on_ticket_action, pattern=r"^tk:"))
    app.add_handler(CallbackQueryHandler(on_broadcast_action, pattern=r"^bc:"))

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
