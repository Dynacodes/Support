"""
FropTools Support Bot — v5
--------------------------
Adds on top of v4: unsend (delete a reply from the customer's chat at any
time), automatic edit-sync (editing your message rewrites theirs), and a
/sent browser listing recent replies with a tap-to-unsend button.

Env vars:
  BOT_TOKEN        - from @BotFather                              (required)
  ADMIN_ID         - your numeric Telegram user id                (required)
  WEBHOOK_URL      - falls back to RENDER_EXTERNAL_URL            (optional)
  PREMIUM_EMOJI    - "1" (default) / "0" to force plain unicode   (optional)
  OPEN_HOUR        - support opens, WAT 24h clock (default 9)     (optional)
  CLOSE_HOUR       - support closes, WAT 24h clock (default 23)   (optional)
  RATE_MAX         - msgs allowed per window (default 6)          (optional)
  RATE_WINDOW      - window in seconds (default 20)               (optional)
  IDLE_HOURS       - auto-close idle tickets after N h (default 24)
  PING_MINUTES     - keep-alive interval (default 10, 0 disables)
  RELAY_KEEP_DAYS  - how long to keep sent-reply records (default 30)
  DB_PATH          - defaults to support.db                       (optional)

Note: Telegram only permits a bot to delete a message within 48 hours of it
being sent, and never permits it to delete the admin's own messages. Unsend
therefore removes the customer's copy only, and reports when it is too old.

Requires: python-telegram-bot[webhooks,job-queue]==21.6
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

import httpx
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

WAT = timezone(timedelta(hours=1), "WAT")
OPEN_HOUR = int(os.environ.get("OPEN_HOUR", "9"))
CLOSE_HOUR = int(os.environ.get("CLOSE_HOUR", "23"))

RATE_MAX = int(os.environ.get("RATE_MAX", "6"))
RATE_WINDOW = int(os.environ.get("RATE_WINDOW", "20"))

IDLE_HOURS = int(os.environ.get("IDLE_HOURS", "24"))
PING_MINUTES = int(os.environ.get("PING_MINUTES", "10"))
RELAY_KEEP_DAYS = int(os.environ.get("RELAY_KEEP_DAYS", "30"))

DEFAULT_TOOLS = [
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


async def edit_html_msg(bot, chat_id: int, message_id: int, text: str, **kwargs):
    """Edit a message we sent earlier (used by the auto-close job)."""
    global PREMIUM_ENABLED
    try:
        return await bot.edit_message_text(
            text, chat_id=chat_id, message_id=message_id,
            parse_mode=ParseMode.HTML, **kwargs
        )
    except BadRequest as exc:
        msg = str(exc).lower()
        if "not modified" in msg or "message to edit not found" in msg:
            return
        if "custom emoji" not in msg:
            raise
        PREMIUM_ENABLED = False
        return await bot.edit_message_text(
            degrade(text), chat_id=chat_id, message_id=message_id,
            parse_mode=ParseMode.HTML, **kwargs
        )


async def typing(bot, chat_id: int, seconds: float = 0.6):
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
    when = f"today at {OPEN_HOUR}:00" if now_wat().hour < OPEN_HOUR \
        else f"tomorrow at {OPEN_HOUR}:00"
    return (
        f"{e('hourglass')} <b>Our agents are offline right now.</b>\n"
        f"{e('info')} Support hours: <b>{hours_label()}</b>\n\n"
        f"Your message is logged and an agent will reply {when} WAT."
    )


# ----------------------------------------------------------------------------
# Rate limiting
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
            CREATE TABLE IF NOT EXISTS relay_map (
                admin_msg_id INTEGER PRIMARY KEY,
                user_id      INTEGER NOT NULL,
                user_msg_id  INTEGER NOT NULL,
                sent_at      TEXT,
                preview      TEXT
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
            CREATE TABLE IF NOT EXISTS tools (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                name     TEXT UNIQUE NOT NULL,
                active   INTEGER DEFAULT 1,
                position INTEGER DEFAULT 0
            );
            """
        )
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(tickets)")}
        if "kind" not in cols:
            conn.execute("ALTER TABLE tickets ADD COLUMN kind TEXT DEFAULT 'license'")
        if "admin_msg_id" not in cols:
            conn.execute("ALTER TABLE tickets ADD COLUMN admin_msg_id INTEGER")
        if "last_activity" not in cols:
            conn.execute("ALTER TABLE tickets ADD COLUMN last_activity TEXT")
            conn.execute(
                "UPDATE tickets SET last_activity = created_at "
                "WHERE last_activity IS NULL"
            )
        # relay_map gained a preview column in v5
        rcols = {r["name"] for r in conn.execute("PRAGMA table_info(relay_map)")}
        if "preview" not in rcols:
            conn.execute("ALTER TABLE relay_map ADD COLUMN preview TEXT")
        # seed the tool list on first run
        if not conn.execute("SELECT 1 FROM tools LIMIT 1").fetchone():
            conn.executemany(
                "INSERT INTO tools (name, position) VALUES (?,?)",
                [(n, i) for i, n in enumerate(DEFAULT_TOOLS)],
            )


# --- tools -----------------------------------------------------------------


def list_tools(active_only=True):
    q = "SELECT * FROM tools"
    if active_only:
        q += " WHERE active = 1"
    q += " ORDER BY position, id"
    with db() as conn:
        return conn.execute(q).fetchall()


def tool_by_id(tool_id: int):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM tools WHERE id = ?", (tool_id,)
        ).fetchone()


def add_tool(name: str) -> bool:
    with db() as conn:
        row = conn.execute(
            "SELECT id, active FROM tools WHERE name = ?", (name,)
        ).fetchone()
        if row:
            if row["active"]:
                return False
            conn.execute("UPDATE tools SET active = 1 WHERE id = ?", (row["id"],))
            return True
        pos = conn.execute(
            "SELECT COALESCE(MAX(position), 0) + 1 p FROM tools"
        ).fetchone()["p"]
        conn.execute(
            "INSERT INTO tools (name, position) VALUES (?,?)", (name, pos)
        )
    return True


def remove_tool(tool_id: int):
    """Soft delete, so historic tickets keep a readable tool name."""
    with db() as conn:
        conn.execute("UPDATE tools SET active = 0 WHERE id = ?", (tool_id,))


# --- users -----------------------------------------------------------------


def remember_user(user):
    with db() as conn:
        conn.execute(
            "INSERT INTO users (user_id, username, full_name, first_seen, active) "
            "VALUES (?,?,?,?,1) ON CONFLICT(user_id) DO UPDATE SET "
            "username=excluded.username, full_name=excluded.full_name, active=1",
            (user.id, user.username, user.full_name,
             now_wat().isoformat(timespec="seconds")),
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


# --- state -----------------------------------------------------------------


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


# --- relay map (admin message -> the copy the customer received) ------------


def map_relay(admin_msg_id: int, user_id: int, user_msg_id: int, preview: str = ""):
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO relay_map "
            "(admin_msg_id, user_id, user_msg_id, sent_at, preview) "
            "VALUES (?,?,?,?,?)",
            (admin_msg_id, user_id, user_msg_id,
             now_wat().isoformat(timespec="seconds"), preview),
        )


def lookup_relay(admin_msg_id: int):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM relay_map WHERE admin_msg_id = ?", (admin_msg_id,)
        ).fetchone()


def drop_relay(admin_msg_id: int):
    with db() as conn:
        conn.execute("DELETE FROM relay_map WHERE admin_msg_id = ?", (admin_msg_id,))


def purge_relays(days: int = RELAY_KEEP_DAYS):
    """Housekeeping only — rows are kept well past Telegram's 48h delete window."""
    cutoff = (now_wat() - timedelta(days=days)).isoformat(timespec="seconds")
    with db() as conn:
        conn.execute("DELETE FROM relay_map WHERE sent_at < ?", (cutoff,))


def recent_relays(user_id: int | None = None, limit: int = 12):
    q, params = "SELECT * FROM relay_map", []
    if user_id:
        q += " WHERE user_id = ?"
        params.append(user_id)
    q += " ORDER BY admin_msg_id DESC LIMIT ?"
    params.append(limit)
    with db() as conn:
        return conn.execute(q, params).fetchall()


def relay_preview(msg) -> str:
    """A short label so /sent is readable without opening anything."""
    if msg.text:
        return msg.text.strip().replace("\n", " ")[:38]
    if msg.caption:
        return f"[media] {msg.caption.strip()[:30]}"
    for kind in ("photo", "video", "document", "voice",
                 "audio", "sticker", "animation"):
        if getattr(msg, kind, None):
            return f"[{kind}]"
    return "[message]"


# --- tickets ---------------------------------------------------------------


def new_reference() -> str:
    alphabet = string.ascii_uppercase + string.digits
    while True:
        ref = ("FT-" + "".join(secrets.choice(alphabet) for _ in range(4))
               + "-" + "".join(secrets.choice(alphabet) for _ in range(4)))
        with db() as conn:
            if not conn.execute(
                "SELECT 1 FROM tickets WHERE ref = ?", (ref,)
            ).fetchone():
                return ref


def create_ticket(user, kind, tool=None, days=None) -> str:
    ref = new_reference()
    stamp = now_wat().isoformat(timespec="seconds")
    with db() as conn:
        conn.execute(
            "INSERT INTO tickets (ref, user_id, username, tool, days, status, "
            "created_at, kind, last_activity) VALUES (?,?,?,?,?,?,?,?,?)",
            (ref, user.id, user.username or user.full_name, tool, days,
             "awaiting_payment" if kind == "license" else "open",
             stamp, kind, stamp),
        )
    return ref


def get_ticket(ref: str):
    with db() as conn:
        return conn.execute("SELECT * FROM tickets WHERE ref = ?", (ref,)).fetchone()


def set_ticket_status(ref: str, status: str):
    with db() as conn:
        conn.execute("UPDATE tickets SET status = ? WHERE ref = ?", (status, ref))


def set_ticket_admin_msg(ref: str, msg_id: int):
    with db() as conn:
        conn.execute(
            "UPDATE tickets SET admin_msg_id = ? WHERE ref = ?", (msg_id, ref)
        )


def touch_ticket(ref: str):
    """Reset the idle clock — called on every message either way."""
    if not ref:
        return
    with db() as conn:
        conn.execute(
            "UPDATE tickets SET last_activity = ? WHERE ref = ?",
            (now_wat().isoformat(timespec="seconds"), ref),
        )


def open_ticket_for(user_id: int):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM tickets WHERE user_id = ? AND status != 'closed' "
            "ORDER BY created_at DESC LIMIT 1",
            (user_id,),
        ).fetchone()


def stale_tickets(hours: int):
    cutoff = (now_wat() - timedelta(hours=hours)).isoformat(timespec="seconds")
    with db() as conn:
        return conn.execute(
            "SELECT * FROM tickets WHERE status != 'closed' AND last_activity < ?",
            (cutoff,),
        ).fetchall()


# ----------------------------------------------------------------------------
# Copy & keyboards
# ----------------------------------------------------------------------------


def welcome_text() -> str:
    status = (
        f"{e('bolt')} <i>Agents online now</i>" if within_hours()
        else f"{e('hourglass')} <i>Agents offline · back at {OPEN_HOUR}:00 WAT</i>"
    )
    return (
        f"{e('sparkle')} <b>Welcome to Froptools Support Portal</b>\n"
        f"{e('globe')} <i>Premium tools · instant licensing · live support</i>\n"
        f"{status}\n\n"
        "How may we help you today?"
    )


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"{plain('gem')}  Buy License Key", callback_data="menu:buy"
        )],
        [InlineKeyboardButton(
            f"{plain('user')}  Contact Live Agent", callback_data="menu:agent"
        )],
    ])


def tools_menu() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            f"{plain('star')}  {t['name']}", callback_data=f"tool:{t['id']}"
        )]
        for t in list_tools()
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


def duplicate_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"{plain('bolt')}  Continue existing ticket", callback_data="menu:resume"
        )],
        [InlineKeyboardButton(
            f"{plain('cross')}  Close it & start over", callback_data="menu:reset"
        )],
    ])


def unsend_button(admin_msg_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(
        f"{plain('cross')}  Unsend for customer",
        callback_data=f"rm:{admin_msg_id}",
    )]])


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
        body = (f"{e('rocket')} Tool: <b>{row['tool']}</b>\n"
                f"{e('hourglass')} Duration: <b>{row['days']} days</b>\n")
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


def duplicate_notice(row) -> str:
    detail = (f"{row['tool']} · {row['days']} days"
              if row["kind"] == "license" else "support request")
    return (
        f"{e('info')} <b>You already have an open ticket</b>\n\n"
        f"{e('gem')} Ref: <code>{row['ref']}</code>\n"
        f"{e('star')} {detail}\n"
        f"{e('hourglass')} Status: <b>"
        f"{STATUS_LABEL.get(row['status'], row['status'])}</b>\n\n"
        "Please finish this one before opening another — "
        "it keeps your conversation with our agent in one place."
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
        return await edit_html(q, welcome_text(), reply_markup=main_menu())

    if action == "resume":
        row = open_ticket_for(user.id)
        if not row:
            set_state(user.id, mode="idle", tool=None, last_ref=None)
            return await edit_html(q, welcome_text(), reply_markup=main_menu())
        set_state(user.id, mode="live", last_ref=row["ref"])
        touch_ticket(row["ref"])
        return await edit_html(
            q,
            f"{e('bolt')} <b>You're back on ticket "
            f"<code>{row['ref']}</code></b>\n\n"
            "Send your message and our agent will see it.",
            reply_markup=end_chat_menu(),
        )

    if action == "reset":
        row = open_ticket_for(user.id)
        if row:
            set_ticket_status(row["ref"], "closed")
            await _sync_admin_ticket(context.bot, row["ref"])
            await send_html(
                context.bot, ADMIN_ID,
                f"{e('info')} <code>{row['ref']}</code> was closed by the customer.",
            )
        set_state(user.id, mode="idle", tool=None, last_ref=None)
        return await edit_html(q, welcome_text(), reply_markup=main_menu())

    # --- duplicate guard: one live ticket per user --------------------------
    existing = open_ticket_for(user.id)
    if existing and action in ("buy", "agent"):
        return await edit_html(
            q, duplicate_notice(existing), reply_markup=duplicate_menu()
        )

    if action == "buy":
        set_state(user.id, mode="idle", tool=None)
        if not list_tools():
            return await edit_html(
                q,
                f"{e('cross')} No tools are listed right now. "
                "Please contact a live agent.",
                reply_markup=main_menu(),
            )
        await edit_html(
            q,
            f"{e('rocket')} <b>Select Your Tool</b>\n\n"
            f"{e('info')} Which product is this license for?",
            reply_markup=tools_menu(),
        )

    elif action == "agent":
        set_state(user.id, mode="await_message")
        text = (f"{e('user')} <b>Live Agent</b>\n\n"
                "Type the message you'd like to send to our support team.\n")
        text += (f"{e('bolt')} An agent will reply right here."
                 if within_hours() else f"\n{offline_notice()}")
        await edit_html(q, text)


async def on_tool(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    row = tool_by_id(int(q.data.split(":", 1)[1]))
    if not row:
        return await edit_html(
            q,
            f"{e('cross')} That tool is no longer available.",
            reply_markup=tools_menu(),
        )
    set_state(q.from_user.id, tool=row["name"])
    await edit_html(
        q,
        f"{e('star')} Tool: <b>{row['name']}</b>\n\n"
        f"{e('hourglass')} <b>Choose your license duration</b>",
        reply_markup=days_menu(),
    )


async def on_days(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    days = int(q.data.split(":", 1)[1])
    user = q.from_user

    existing = open_ticket_for(user.id)
    if existing:
        return await edit_html(
            q, duplicate_notice(existing), reply_markup=duplicate_menu()
        )

    tool = get_state(user.id).get("tool")
    if not tool:
        return await edit_html(
            q, f"{e('cross')} Session expired. Please start again.",
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
    text += (
        f"{e('bolt')} <i>Connecting you to a live agent to complete payment…</i>"
        f"\n\n{e('info')} Payment details will arrive in this chat."
        if within_hours() else offline_notice()
    )
    await edit_html(q, text, reply_markup=end_chat_menu())

    await typing(context.bot, ADMIN_ID)
    row = get_ticket(ref)
    sent = await send_html(
        context.bot, ADMIN_ID, ticket_admin_text(row),
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
                context.bot, user.id,
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
    touch_ticket(ref)

    await typing(context.bot, ADMIN_ID)

    if first_contact:
        row = get_ticket(ref)
        header = await send_html(
            context.bot, ADMIN_ID, ticket_admin_text(row),
            reply_markup=ticket_keyboard(ref, row["status"], row["kind"]),
        )
        set_ticket_admin_msg(ref, header.message_id)
    else:
        header = await send_html(
            context.bot, ADMIN_ID,
            f"{e('bolt')} <b>Message</b> · <code>{ref}</code>\n"
            f"{e('user')} {user_label(user)}",
        )
    map_msg(header.message_id, user.id)

    copied = await context.bot.copy_message(
        chat_id=ADMIN_ID, from_chat_id=msg.chat_id, message_id=msg.message_id
    )
    map_msg(copied.message_id, user.id)

    if first_contact:
        ack = (f"{e('check')} Your message has been delivered to our support team.\n"
               f"{e('gem')} Ticket: <code>{ref}</code>\n")
        ack += (f"{e('hourglass')} An agent will reply here shortly."
                if within_hours() else f"\n{offline_notice()}")
        await send_html(context.bot, user.id, ack, reply_markup=end_chat_menu())
    elif not within_hours():
        await send_html(context.bot, user.id, offline_notice())


# ----------------------------------------------------------------------------
# Ticket actions
# ----------------------------------------------------------------------------


async def _sync_admin_ticket(bot, ref: str):
    """Refresh the admin-side ticket card after a status change."""
    row = get_ticket(ref)
    if not row or not row["admin_msg_id"]:
        return
    try:
        await edit_html_msg(
            bot, ADMIN_ID, row["admin_msg_id"], ticket_admin_text(row),
            reply_markup=ticket_keyboard(ref, row["status"], row["kind"]),
        )
    except Exception as exc:
        log.debug("Could not sync ticket card %s: %s", ref, exc)


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
    touch_ticket(ref)
    await q.answer(f"Ticket {ref} → {new_status}")

    updated = get_ticket(ref)
    await edit_html(
        q, ticket_admin_text(updated),
        reply_markup=ticket_keyboard(ref, new_status, updated["kind"]),
    )

    notices = {
        "paid": (f"{e('check')} <b>Payment confirmed</b>\n\n"
                 f"{e('gem')} Ref: <code>{ref}</code>\n"
                 f"{e('hourglass')} Your license is being prepared — hold tight."),
        "delivered": (f"{e('gem')} <b>License delivered</b>\n\n"
                      f"{e('star')} Ref: <code>{ref}</code>\n"
                      f"{e('sparkle')} Thank you for choosing Froptools!"),
        "closed": (f"{e('check')} <b>Ticket closed</b>\n\n"
                   f"{e('info')} Ref <code>{ref}</code> has been resolved. "
                   "Tap below if you need anything else."),
        "open": (f"{e('bolt')} <b>Ticket reopened</b>\n\n"
                 f"{e('info')} Ref <code>{ref}</code> is active again."),
        "awaiting_payment": (f"{e('bolt')} <b>Ticket reopened</b>\n\n"
                             f"{e('info')} Ref <code>{ref}</code> is active again."),
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
# Unsend & edit-sync
# ----------------------------------------------------------------------------

_UNSEND_LABEL = {
    "ok": "Removed from the customer's chat.",
    "gone": "Already gone on their side.",
    "too_old": "Too old — Telegram only allows deletion within 48h.",
    "blocked": "That user has blocked the bot.",
    "notfound": "No delivered copy tracked for that message.",
}


async def _unsend(bot, admin_msg_id: int) -> str:
    rel = lookup_relay(admin_msg_id)
    if not rel:
        return "notfound"
    try:
        await bot.delete_message(rel["user_id"], rel["user_msg_id"])
    except BadRequest as exc:
        m = str(exc).lower()
        if "not found" in m:
            drop_relay(admin_msg_id)
            return "gone"
        if "can't be deleted" in m or "too old" in m:
            return "too_old"
        raise
    except Forbidden:
        deactivate_user(rel["user_id"])
        return "blocked"
    drop_relay(admin_msg_id)
    return "ok"


async def cmd_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reply to a message you sent the customer, then /del."""
    msg = update.effective_message
    if not msg.reply_to_message:
        return await msg.reply_text(
            f"{plain('info')} Reply to the message you sent the customer, "
            "then send /del.  (Or use /sent to pick from a list.)"
        )
    result = await _unsend(context.bot, msg.reply_to_message.message_id)
    await msg.reply_text(
        f"{plain('check') if result == 'ok' else plain('cross')} "
        f"{_UNSEND_LABEL[result]}"
    )


def sent_panel(user_id: int | None = None):
    rows = recent_relays(user_id)
    scope = f"customer <code>{user_id}</code>" if user_id else "all customers"
    if not rows:
        return f"{e('info')} No tracked replies for {scope}.", None
    kb = [[InlineKeyboardButton(
        f"🗑  {r['sent_at'][5:16].replace('T', ' ')} · {r['preview'] or '…'}",
        callback_data=f"rm:{r['admin_msg_id']}:{user_id or 0}",
    )] for r in rows]
    kb.append([InlineKeyboardButton("✕  Close", callback_data="rm:close:0")])
    text = (
        f"{e('rocket')} <b>Sent replies</b> — {scope}\n\n"
        f"{e('info')} Tap a line to remove it from the customer's chat.\n"
        f"{e('hourglass')} Telegram only allows deletion within 48h of sending."
    )
    return text, InlineKeyboardMarkup(kb)


async def cmd_sent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    uid = None
    if context.args:
        try:
            uid = int(context.args[0])
        except ValueError:
            return await msg.reply_text("Usage: /sent [user_id]")
    elif msg.reply_to_message:
        uid = lookup_msg(msg.reply_to_message.message_id)
    text, kb = sent_panel(uid)
    await send_html(context.bot, ADMIN_ID, text, reply_markup=kb)


async def on_relay_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != ADMIN_ID:
        return await q.answer("Not authorised.", show_alert=True)

    parts = q.data.split(":")

    if parts[1] == "close":
        await q.answer()
        try:
            return await q.delete_message()
        except Exception:
            return await edit_html(q, f"{e('check')} Closed.")

    admin_msg_id = int(parts[1])
    from_panel = len(parts) > 2
    result = await _unsend(context.bot, admin_msg_id)
    await q.answer(_UNSEND_LABEL[result], show_alert=result not in ("ok", "gone"))

    if from_panel:
        panel_user = int(parts[2]) or None
        text, kb = sent_panel(panel_user)
        return await edit_html(q, text, reply_markup=kb)

    if result in ("ok", "gone"):
        await edit_html(
            q,
            f"{e('cross')} <b>Unsent</b> — the customer no longer sees that "
            f"message.\n{e('info')} Delete your own copy above manually; "
            "Telegram won't let a bot remove your messages.",
        )


async def admin_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin edited a relayed message — mirror the edit on the customer's side."""
    msg = update.edited_message
    rel = lookup_relay(msg.message_id)
    if not rel:
        return
    try:
        if msg.text is not None:
            body = f"{e('user')} <b>Support Agent</b>\n\n{msg.text_html}"
            try:
                await context.bot.edit_message_text(
                    body, chat_id=rel["user_id"], message_id=rel["user_msg_id"],
                    parse_mode=ParseMode.HTML,
                )
            except BadRequest as exc:
                if "custom emoji" not in str(exc).lower():
                    raise
                await context.bot.edit_message_text(
                    degrade(body), chat_id=rel["user_id"],
                    message_id=rel["user_msg_id"], parse_mode=ParseMode.HTML,
                )
        elif msg.caption is not None:
            await context.bot.edit_message_caption(
                chat_id=rel["user_id"], message_id=rel["user_msg_id"],
                caption=msg.caption_html, parse_mode=ParseMode.HTML,
            )
        else:
            return  # media swapped out — nothing sensible to mirror

        map_relay(msg.message_id, rel["user_id"], rel["user_msg_id"],
                  relay_preview(msg))
        await msg.reply_text(
            f"{plain('check')} Edit synced to the customer.",
            reply_markup=unsend_button(msg.message_id),
        )
    except BadRequest as exc:
        m = str(exc).lower()
        if "not modified" in m:
            return
        await msg.reply_text(
            f"{plain('cross')} Couldn't sync that edit: {exc}\n"
            "Tip: /del it and send a fresh message."
        )
    except Forbidden:
        deactivate_user(rel["user_id"])
        await msg.reply_text(f"{plain('cross')} User has blocked the bot.")


# ----------------------------------------------------------------------------
# Tool manager (admin)
# ----------------------------------------------------------------------------


def tool_panel_text() -> str:
    rows = list_tools()
    if not rows:
        body = f"{e('cross')} <i>No active tools — customers can't buy.</i>"
    else:
        body = "\n".join(f"{e('star')} {r['name']}" for r in rows)
    return (
        f"{e('rocket')} <b>Tool Manager</b>\n\n{body}\n\n"
        f"{e('info')} Tap a name to remove it, or add a new one below."
    )


def tool_panel_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            f"{plain('cross')}  {r['name']}", callback_data=f"tl:del:{r['id']}"
        )]
        for r in list_tools()
    ]
    rows.append([InlineKeyboardButton("➕  Add tool", callback_data="tl:add")])
    return InlineKeyboardMarkup(rows)


async def cmd_tools(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_html(
        context.bot, ADMIN_ID, tool_panel_text(),
        reply_markup=tool_panel_keyboard(),
    )


async def on_tool_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != ADMIN_ID:
        return await q.answer("Not authorised.", show_alert=True)

    parts = q.data.split(":")
    action = parts[1]

    if action == "add":
        context.bot_data["awaiting_tool"] = True
        await q.answer()
        return await edit_html(
            q,
            f"{e('rocket')} <b>Add a tool</b>\n\n"
            f"{e('info')} Send the tool name as your next message.\n"
            "Send <code>/cancel</code> to abort.",
        )

    if action == "del":
        row = tool_by_id(int(parts[2]))
        if row:
            remove_tool(row["id"])
            await q.answer(f"Removed {row['name']}")
        await edit_html(
            q, tool_panel_text(), reply_markup=tool_panel_keyboard()
        )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.bot_data.pop("awaiting_tool", None)
    await update.effective_message.reply_text(f"{plain('check')} Cancelled.")


# ----------------------------------------------------------------------------
# Scheduled jobs
# ----------------------------------------------------------------------------


async def job_autoclose(context: ContextTypes.DEFAULT_TYPE):
    """Close tickets with no activity for IDLE_HOURS."""
    purge_relays()
    rows = stale_tickets(IDLE_HOURS)
    if not rows:
        return
    for row in rows:
        ref, user_id = row["ref"], row["user_id"]
        set_ticket_status(ref, "closed")
        set_state(user_id, mode="idle", tool=None, last_ref=None)
        await _sync_admin_ticket(context.bot, ref)
        try:
            await send_html(
                context.bot, user_id,
                f"{e('hourglass')} <b>Ticket auto-closed</b>\n\n"
                f"{e('info')} <code>{ref}</code> was inactive for "
                f"{IDLE_HOURS} hours, so we've closed it.\n"
                f"{e('sparkle')} Start again any time — we're here.",
                reply_markup=main_menu(),
            )
        except Forbidden:
            deactivate_user(user_id)
        except Exception as exc:
            log.debug("Auto-close notice failed for %s: %s", user_id, exc)
        await asyncio.sleep(0.05)

    log.info("Auto-closed %d idle ticket(s)", len(rows))
    await send_html(
        context.bot, ADMIN_ID,
        f"{e('hourglass')} Auto-closed <b>{len(rows)}</b> ticket(s) "
        f"idle for over {IDLE_HOURS}h.",
    )


async def job_keepalive(context: ContextTypes.DEFAULT_TYPE):
    """
    Ping our own public URL so Render's free tier doesn't spin the service down.
    Any HTTP status counts as traffic — a 404 on / is fine.
    """
    if not WEBHOOK_URL:
        return
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(f"{WEBHOOK_URL}/", follow_redirects=True)
        log.info("Keep-alive ping → %s", r.status_code)
    except Exception as exc:
        log.warning("Keep-alive ping failed: %s", exc)


# ----------------------------------------------------------------------------
# Admin handlers
# ----------------------------------------------------------------------------


async def admin_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_html(
        context.bot, ADMIN_ID,
        f"{e('bolt')} <b>Admin Console</b>\n\n"
        f"{e('user')} Reply to any message to answer that customer\n"
        f"{e('star')} <code>/reply &lt;user_id&gt; &lt;text&gt;</code> — direct reply\n"
        f"{e('cross')} <code>/del</code> — reply to one of your messages to "
        "unsend it\n"
        f"{e('rocket')} <code>/sent [user_id]</code> — list your replies, "
        "tap to unsend\n"
        f"{e('gem')} <code>/tickets</code> — recent tickets\n"
        f"{e('hourglass')} <code>/open</code> — tickets still open\n"
        f"{e('rocket')} <code>/tools</code> — add or remove tools\n"
        f"{e('sparkle')} <code>/broadcast &lt;text&gt;</code> — message all users\n"
        f"{e('cross')} <code>/close &lt;user_id&gt;</code> — close their ticket\n"
        f"{e('globe')} <code>/status</code> — hours, users, jobs\n"
        f"{e('info')} <code>/emoji</code> — premium emoji status\n\n"
        f"{e('info')} <i>Editing one of your sent messages updates the "
        "customer's copy automatically.</i>\n"
        f"Hours: <b>{hours_label()}</b> · auto-close after <b>{IDLE_HOURS}h</b> idle",
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
        relays = conn.execute("SELECT COUNT(*) c FROM relay_map").fetchone()["c"]
    ping = f"every {PING_MINUTES} min" if PING_MINUTES else "disabled"
    await send_html(
        context.bot, ADMIN_ID,
        f"{e('globe')} <b>Status</b>\n\n"
        f"{e('bolt')} Now: <b>{now_wat().strftime('%H:%M')} WAT</b> — "
        f"{'open' if within_hours() else 'closed'}\n"
        f"{e('info')} Hours: {hours_label()}\n"
        f"{e('user')} Reachable users: <b>{users}</b>\n"
        f"{e('sparkle')} Live chats: <b>{live}</b>\n"
        f"{e('hourglass')} Open tickets: <b>{open_t}</b>\n"
        f"{e('star')} Active tools: <b>{len(list_tools())}</b>\n"
        f"{e('cross')} Unsendable replies tracked: <b>{relays}</b>\n"
        f"{e('rocket')} Keep-alive: <b>{ping}</b>\n"
        f"{e('gem')} Auto-close: <b>{IDLE_HOURS}h idle</b>",
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
        context.bot, ADMIN_ID,
        f"{e('rocket')} <b>Broadcast preview</b> → {recipients} users\n"
        f"{'─' * 24}\n\n{text}",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(f"{plain('check')} Send", callback_data="bc:send"),
            InlineKeyboardButton(f"{plain('cross')} Cancel", callback_data="bc:cancel"),
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
        await asyncio.sleep(0.06)

    context.bot_data.pop("broadcast", None)
    await send_html(
        context.bot, ADMIN_ID,
        f"{e('check')} <b>Broadcast complete</b>\n\n"
        f"{e('rocket')} Delivered: <b>{sent}</b>\n"
        f"{e('cross')} Blocked the bot: <b>{blocked}</b>\n"
        f"{e('info')} Other failures: <b>{failed}</b>",
    )


async def admin_relay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message

    # tool manager is waiting for a name
    if context.bot_data.get("awaiting_tool"):
        context.bot_data.pop("awaiting_tool", None)
        name = (msg.text or "").strip()
        if not name or len(name) > 40:
            return await msg.reply_text(
                f"{plain('cross')} Send a plain tool name under 40 characters."
            )
        ok = add_tool(name)
        await msg.reply_text(
            f"{plain('check')} Added “{name}”." if ok
            else f"{plain('info')} “{name}” is already on the list."
        )
        return await send_html(
            context.bot, ADMIN_ID, tool_panel_text(),
            reply_markup=tool_panel_keyboard(),
        )

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
            sent = await send_html(
                context.bot, user_id,
                f"{e('user')} <b>Support Agent</b>\n\n{msg.text_html}",
            )
        else:
            sent = await context.bot.copy_message(
                chat_id=user_id, from_chat_id=msg.chat_id, message_id=msg.message_id
            )
        # remember which of their messages this produced, so we can unsend it
        map_relay(msg.message_id, user_id, sent.message_id, relay_preview(msg))

        row = open_ticket_for(user_id)
        ref = row["ref"] if row else None
        set_state(user_id, mode="live", last_ref=ref)
        touch_ticket(ref)
        await msg.reply_text(
            f"{plain('check')} Delivered.",
            reply_markup=unsend_button(msg.message_id),
        )
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
        sent = await send_html(
            context.bot, user_id, f"{e('user')} <b>Support Agent</b>\n\n{text}"
        )
        map_relay(msg.message_id, user_id, sent.message_id,
                  text.replace("\n", " ")[:38])
        row = open_ticket_for(user_id)
        set_state(user_id, mode="live", last_ref=row["ref"] if row else None)
        if row:
            touch_ticket(row["ref"])
        await msg.reply_text(
            f"{plain('check')} Delivered.",
            reply_markup=unsend_button(msg.message_id),
        )
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
        await _sync_admin_ticket(context.bot, row["ref"])
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
    app.add_handler(CommandHandler("del", cmd_del, filters=admin))
    app.add_handler(CommandHandler("sent", cmd_sent, filters=admin))
    app.add_handler(CommandHandler("tickets", cmd_tickets, filters=admin))
    app.add_handler(CommandHandler("open", cmd_open, filters=admin))
    app.add_handler(CommandHandler("close", cmd_close, filters=admin))
    app.add_handler(CommandHandler("tools", cmd_tools, filters=admin))
    app.add_handler(CommandHandler("cancel", cmd_cancel, filters=admin))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast, filters=admin))
    app.add_handler(CommandHandler("emoji", cmd_emoji, filters=admin))
    app.add_handler(CommandHandler("status", cmd_status, filters=admin))
    app.add_handler(CommandHandler("admin", admin_help, filters=admin))

    app.add_handler(CommandHandler(["start", "menu", "help"], start))

    app.add_handler(CallbackQueryHandler(on_menu, pattern=r"^menu:"))
    app.add_handler(CallbackQueryHandler(on_tool, pattern=r"^tool:"))
    app.add_handler(CallbackQueryHandler(on_days, pattern=r"^days:"))
    app.add_handler(CallbackQueryHandler(on_ticket_action, pattern=r"^tk:"))
    app.add_handler(CallbackQueryHandler(on_tool_admin, pattern=r"^tl:"))
    app.add_handler(CallbackQueryHandler(on_broadcast_action, pattern=r"^bc:"))
    app.add_handler(CallbackQueryHandler(on_relay_delete, pattern=r"^rm:"))

    # Edited messages must be caught BEFORE the relay handler, and both message
    # handlers need an explicit update type, or every edit is relayed as new.
    app.add_handler(MessageHandler(
        admin & filters.UpdateType.EDITED_MESSAGE, admin_edit))
    app.add_handler(MessageHandler(
        admin & ~filters.COMMAND & filters.UpdateType.MESSAGE, admin_relay))
    app.add_handler(MessageHandler(
        ~filters.COMMAND & filters.UpdateType.MESSAGE, on_user_message))

    app.add_error_handler(on_error)

    if app.job_queue:
        app.job_queue.run_repeating(
            job_autoclose, interval=900, first=120, name="autoclose"
        )
        if PING_MINUTES and WEBHOOK_URL:
            app.job_queue.run_repeating(
                job_keepalive, interval=PING_MINUTES * 60, first=60, name="keepalive"
            )
        log.info("Jobs scheduled (autoclose every 15m, ping every %sm)", PING_MINUTES)
    else:
        log.warning(
            "JobQueue unavailable — install python-telegram-bot[job-queue] "
            "for auto-close and keep-alive"
        )

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
