"""
FropTools Support Bot — v8
--------------------------
v6 added the intent gate (screening + confirm step), strikes, cooldowns
and bans. v7 added the payment-details trap. v8 puts prices on the tools:
every tool carries a USD and NGN price and a fixed access length, shown
in the buy menu, the order confirmation and the ticket card, and editable
from Telegram with /price. The duration menu is gone — all licenses run
for DEFAULT_DAYS (60).

The payment-details trap:

The moment you send account/wallet details, the bot recognises them and
starts a countdown. The customer must reply within the window or the
ticket closes and they take a double strike. Anyone who collects your
details twice without paying is banned automatically.

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
  MIN_CHARS        - shortest accepted opening message (default 12)
  MIN_WORDS        - fewest words in an opening message (default 3)
  MAX_JUNK         - low-effort tries before a cooldown (default 3)
  GHOST_HOURS      - silence after an agent reply = ghost (default 6)
  STRIKE_LIMIT     - ghosts before auto-restriction (default 3)
  COOLDOWN_HOURS   - length of an automatic cooldown (default 12)
  PAY_WINDOW_MIN   - minutes to pay after you send details (default 30)
  PAY_EXTEND_MIN   - extra minutes if they ask once (default 20)
  PAY_STRIKE       - strikes for ghosting after details (default 2)
  DETAIL_LIMIT     - details-taken-without-paying before auto-ban (default 2)
  DB_PATH          - defaults to support.db                       (optional)

Note: Telegram only permits a bot to delete a message within 48 hours of it
being sent, and never permits it to delete the admin's own messages. Unsend
therefore removes the customer's copy only, and reports when it is too old.

Requires: python-telegram-bot[webhooks,job-queue]==21.6
"""

import asyncio
import html as html_lib
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

MIN_CHARS = int(os.environ.get("MIN_CHARS", "12"))
MIN_WORDS = int(os.environ.get("MIN_WORDS", "3"))
MAX_JUNK = int(os.environ.get("MAX_JUNK", "3"))
GHOST_HOURS = int(os.environ.get("GHOST_HOURS", "6"))
STRIKE_LIMIT = int(os.environ.get("STRIKE_LIMIT", "3"))
COOLDOWN_HOURS = int(os.environ.get("COOLDOWN_HOURS", "12"))

PAY_WINDOW_MIN = int(os.environ.get("PAY_WINDOW_MIN", "30"))
PAY_EXTEND_MIN = int(os.environ.get("PAY_EXTEND_MIN", "20"))
PAY_STRIKE = int(os.environ.get("PAY_STRIKE", "2"))
DETAIL_LIMIT = int(os.environ.get("DETAIL_LIMIT", "2"))

DEFAULT_DAYS = int(os.environ.get("DEFAULT_DAYS", "60"))

# name, USD, NGN — every tool is DEFAULT_DAYS of access
DEFAULT_TOOLS = [
    ("FropLogger",        30, 45000),
    ("FropTrack",         25, 35000),
    ("FropPaypalFlash",   20, 28000),
    ("FropCashappFlash",  20, 28000),
    ("FropBybitFlash",    20, 28000),
    ("FropBinanceFlash",  20, 28000),
    ("FropZelleFlash",    20, 28000),
    ("FropFlight",        25, 35000),
]

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


def esc(text: str) -> str:
    return html_lib.escape(text or "")


# ----------------------------------------------------------------------------
# Business hours
# ----------------------------------------------------------------------------


def now_wat() -> datetime:
    return datetime.now(WAT)


def stamp() -> str:
    return now_wat().isoformat(timespec="seconds")


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
_block_notice: dict[int, float] = {}


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


def should_renotify(user_id: int, seconds: int = 300) -> bool:
    """Don't repeat the 'you're restricted' notice on every message."""
    now = time.time()
    if now - _block_notice.get(user_id, 0) < seconds:
        return False
    _block_notice[user_id] = now
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
                user_id      INTEGER PRIMARY KEY,
                mode         TEXT,
                tool         TEXT,
                last_ref     TEXT,
                pending_msg  INTEGER,
                pending_text TEXT
            );
            CREATE TABLE IF NOT EXISTS users (
                user_id        INTEGER PRIMARY KEY,
                username       TEXT,
                full_name      TEXT,
                first_seen     TEXT,
                active         INTEGER DEFAULT 1,
                banned         INTEGER DEFAULT 0,
                ban_reason     TEXT,
                banned_at      TEXT,
                strikes        INTEGER DEFAULT 0,
                junk           INTEGER DEFAULT 0,
                trusted        INTEGER DEFAULT 0,
                cooldown_until TEXT,
                detail_hits    INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS tools (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                name     TEXT UNIQUE NOT NULL,
                active   INTEGER DEFAULT 1,
                position INTEGER DEFAULT 0,
                usd      REAL,
                ngn      INTEGER,
                days     INTEGER
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
        # v6: ghost tracking
        if "agent_replied" not in cols:
            conn.execute(
                "ALTER TABLE tickets ADD COLUMN agent_replied INTEGER DEFAULT 0"
            )
        if "awaiting_since" not in cols:
            conn.execute("ALTER TABLE tickets ADD COLUMN awaiting_since TEXT")
        # v7: payment window
        for col, ddl in (
            ("got_details", "got_details INTEGER DEFAULT 0"),
            ("pay_deadline", "pay_deadline TEXT"),
            ("pay_reminded", "pay_reminded INTEGER DEFAULT 0"),
            ("pay_extended", "pay_extended INTEGER DEFAULT 0"),
            ("price_usd", "price_usd REAL"),
            ("price_ngn", "price_ngn INTEGER"),
        ):
            if col not in cols:
                conn.execute(f"ALTER TABLE tickets ADD COLUMN {ddl}")

        rcols = {r["name"] for r in conn.execute("PRAGMA table_info(relay_map)")}
        if "preview" not in rcols:
            conn.execute("ALTER TABLE relay_map ADD COLUMN preview TEXT")

        # v6: screening + moderation columns on existing installs
        ucols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
        for col, ddl in (
            ("banned", "banned INTEGER DEFAULT 0"),
            ("ban_reason", "ban_reason TEXT"),
            ("banned_at", "banned_at TEXT"),
            ("strikes", "strikes INTEGER DEFAULT 0"),
            ("junk", "junk INTEGER DEFAULT 0"),
            ("trusted", "trusted INTEGER DEFAULT 0"),
            ("cooldown_until", "cooldown_until TEXT"),
            ("detail_hits", "detail_hits INTEGER DEFAULT 0"),
        ):
            if col not in ucols:
                conn.execute(f"ALTER TABLE users ADD COLUMN {ddl}")

        scols = {r["name"] for r in conn.execute("PRAGMA table_info(state)")}
        if "pending_msg" not in scols:
            conn.execute("ALTER TABLE state ADD COLUMN pending_msg INTEGER")
        if "pending_text" not in scols:
            conn.execute("ALTER TABLE state ADD COLUMN pending_text TEXT")

        # v8: prices on tools
        tcols = {r["name"] for r in conn.execute("PRAGMA table_info(tools)")}
        for col, ddl in (("usd", "usd REAL"), ("ngn", "ngn INTEGER"),
                         ("days", "days INTEGER")):
            if col not in tcols:
                conn.execute(f"ALTER TABLE tools ADD COLUMN {ddl}")

        # seed the tool list on first run
        if not conn.execute("SELECT 1 FROM tools LIMIT 1").fetchone():
            conn.executemany(
                "INSERT INTO tools (name, position, usd, ngn, days) "
                "VALUES (?,?,?,?,?)",
                [(n, i, u, ngn, DEFAULT_DAYS)
                 for i, (n, u, ngn) in enumerate(DEFAULT_TOOLS)],
            )
        else:
            # existing install — fill in prices that aren't set yet
            for name, usd, ngn in DEFAULT_TOOLS:
                conn.execute(
                    "UPDATE tools SET usd = ?, ngn = ?, "
                    "days = COALESCE(days, ?) WHERE lower(name) = lower(?) "
                    "AND usd IS NULL",
                    (usd, ngn, DEFAULT_DAYS, name),
                )
            conn.execute(
                "UPDATE tools SET days = ? WHERE days IS NULL", (DEFAULT_DAYS,)
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


def price_label(row) -> str:
    """'$25 · ₦35,000' — whichever halves are set."""
    bits = []
    if row["usd"]:
        usd = row["usd"]
        bits.append(f"${usd:g}")
    if row["ngn"]:
        bits.append(f"₦{int(row['ngn']):,}")
    return " · ".join(bits) if bits else "price on request"


def tool_days(row) -> int:
    return int(row["days"] or DEFAULT_DAYS)


def add_tool(name: str, usd=None, ngn=None, days: int = DEFAULT_DAYS) -> bool:
    with db() as conn:
        row = conn.execute(
            "SELECT id, active FROM tools WHERE lower(name) = lower(?)", (name,)
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE tools SET active = 1, usd = ?, ngn = ?, days = ? "
                "WHERE id = ?", (usd, ngn, days, row["id"]),
            )
            return not row["active"]
        pos = conn.execute(
            "SELECT COALESCE(MAX(position), 0) + 1 p FROM tools"
        ).fetchone()["p"]
        conn.execute(
            "INSERT INTO tools (name, position, usd, ngn, days) VALUES (?,?,?,?,?)",
            (name, pos, usd, ngn, days),
        )
    return True


def set_price(tool_id: int, usd=None, ngn=None, days=None):
    with db() as conn:
        conn.execute(
            "UPDATE tools SET usd = COALESCE(?, usd), ngn = COALESCE(?, ngn), "
            "days = COALESCE(?, days) WHERE id = ?", (usd, ngn, days, tool_id),
        )


def find_tool(term: str):
    """Look a tool up by id or by (partial) name."""
    with db() as conn:
        if term.isdigit():
            row = conn.execute(
                "SELECT * FROM tools WHERE id = ?", (int(term),)
            ).fetchone()
            if row:
                return row
        return conn.execute(
            "SELECT * FROM tools WHERE lower(name) LIKE lower(?) "
            "ORDER BY active DESC, position LIMIT 1", (f"%{term}%",)
        ).fetchone()


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
            (user.id, user.username, user.full_name, stamp()),
        )


def get_user(user_id: int):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()


def deactivate_user(user_id: int):
    with db() as conn:
        conn.execute("UPDATE users SET active = 0 WHERE user_id = ?", (user_id,))


def all_active_users() -> list[int]:
    with db() as conn:
        rows = conn.execute(
            "SELECT user_id FROM users WHERE active = 1 AND banned = 0 "
            "AND user_id != ?",
            (ADMIN_ID,),
        ).fetchall()
    return [r["user_id"] for r in rows]


# --- moderation ------------------------------------------------------------


def ban_user(user_id: int, reason: str = ""):
    with db() as conn:
        conn.execute(
            "INSERT INTO users (user_id, banned, ban_reason, banned_at, first_seen) "
            "VALUES (?,1,?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
            "banned=1, ban_reason=excluded.ban_reason, banned_at=excluded.banned_at",
            (user_id, reason, stamp(), stamp()),
        )


def unban_user(user_id: int):
    with db() as conn:
        conn.execute(
            "UPDATE users SET banned = 0, ban_reason = NULL, banned_at = NULL, "
            "strikes = 0, junk = 0, cooldown_until = NULL WHERE user_id = ?",
            (user_id,),
        )


def trust_user(user_id: int, on: bool = True):
    with db() as conn:
        conn.execute(
            "UPDATE users SET trusted = ?, strikes = 0, junk = 0, "
            "cooldown_until = NULL WHERE user_id = ?",
            (1 if on else 0, user_id),
        )


def is_trusted(user_id: int) -> bool:
    row = get_user(user_id)
    return bool(row and row["trusted"])


def set_cooldown(user_id: int, hours: int):
    until = (now_wat() + timedelta(hours=hours)).isoformat(timespec="seconds")
    with db() as conn:
        conn.execute(
            "UPDATE users SET cooldown_until = ? WHERE user_id = ?", (until, user_id)
        )
    return until


def add_strike(user_id: int, weight: int = 1) -> int:
    with db() as conn:
        conn.execute(
            "UPDATE users SET strikes = COALESCE(strikes, 0) + ? WHERE user_id = ?",
            (weight, user_id),
        )
        row = conn.execute(
            "SELECT strikes FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
    return row["strikes"] if row else 0


def bump_detail_hit(user_id: int) -> int:
    """Count how many times this person took payment details and never paid."""
    with db() as conn:
        conn.execute(
            "UPDATE users SET detail_hits = COALESCE(detail_hits, 0) + 1 "
            "WHERE user_id = ?", (user_id,),
        )
        row = conn.execute(
            "SELECT detail_hits FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
    return row["detail_hits"] if row else 0


def bump_junk(user_id: int) -> int:
    with db() as conn:
        conn.execute(
            "UPDATE users SET junk = COALESCE(junk, 0) + 1 WHERE user_id = ?",
            (user_id,),
        )
        row = conn.execute(
            "SELECT junk FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
    return row["junk"] if row else 0


def clear_junk(user_id: int):
    with db() as conn:
        conn.execute("UPDATE users SET junk = 0 WHERE user_id = ?", (user_id,))


def banned_list():
    with db() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE banned = 1 OR cooldown_until IS NOT NULL "
            "OR strikes > 0 OR detail_hits > 0 "
            "ORDER BY banned DESC, detail_hits DESC, strikes DESC LIMIT 25"
        ).fetchall()


def block_status(user_id: int):
    """None, ('banned', reason) or ('cooldown', iso_until)."""
    row = get_user(user_id)
    if not row:
        return None
    if row["banned"]:
        return ("banned", row["ban_reason"] or "")
    cd = row["cooldown_until"]
    if cd and cd > stamp():
        return ("cooldown", cd)
    if cd:  # expired — clean it up
        with db() as conn:
            conn.execute(
                "UPDATE users SET cooldown_until = NULL WHERE user_id = ?",
                (user_id,),
            )
    return None


def block_text(kind: str, detail: str) -> str:
    if kind == "banned":
        why = f"\n{e('info')} Reason: <i>{esc(detail)}</i>" if detail else ""
        return (
            f"{e('cross')} <b>Access restricted</b>\n"
            f"<b>You are banned.</b>\n\n"
            f"Your access to this support bot has been revoked.{why}"
        )
    when = detail[11:16] if len(detail) >= 16 else detail
    day = "today" if detail[:10] == stamp()[:10] else "tomorrow"
    return (
        f"{e('hourglass')} <b>Temporarily paused</b>\n\n"
        "Tickets you opened were left unanswered, so new requests are on hold.\n"
        f"{e('info')} You can start again {day} after <b>{when} WAT</b>."
    )


# --- opening-message screening ---------------------------------------------

LOW_EFFORT = {
    "hi", "hii", "hiii", "hello", "helo", "hey", "heyy", "yo", "sup", "hola",
    "good morning", "good afternoon", "good evening", "good day", "gm", "gn",
    "test", "testing", "test test", "ok", "okay", "k", "yes", "no", "yeah",
    "lol", "lmao", "haha", "hehe", "hmm", "hm", "abeg", "bro", "boss",
    "chief", "guy", "man", "please", "pls", "abi", "wetin", "sup bro",
    "hello sir", "hi sir", "hi boss", "hello boss", "hello there", "hi there",
    "you dey", "how far", "info", "help", "hey man", "are you there",
    "is anyone there", "anybody there", "who are you", "are you a bot",
    "are you real", "is this real", "what is this", "u there", "you there",
}

_WORD_RE = re.compile(r"[a-z0-9]+")
_REF_RE = re.compile(r"\bFT-[A-Z0-9]{4}-[A-Z0-9]{4}\b", re.I)


def screen_opening(text: str | None) -> tuple[bool, str]:
    """Decide whether a first message is worth an agent's time."""
    if not text or not text.strip():
        return False, (
            "Start with a written message, please — photos and files can "
            "follow once your ticket is open."
        )

    body = text.strip()
    low = body.lower().strip(" .!?,-")

    if _REF_RE.search(body):          # quoting a real ticket ref = genuine
        return True, ""

    if low in LOW_EFFORT:
        return False, (
            "Tell us the actual issue in one message — which tool, and what "
            "went wrong. A greeting on its own doesn't reach an agent."
        )

    words = _WORD_RE.findall(low)
    if len(body) < MIN_CHARS or len(words) < MIN_WORDS:
        return False, (
            f"That's a bit short. Give us at least a sentence "
            f"({MIN_CHARS}+ characters) describing what you need."
        )

    if len(set(low.replace(" ", ""))) <= 3:
        return False, "That doesn't read like a real message. Please try again."

    letters = sum(c.isalpha() for c in body)
    if letters < len(body) * 0.4:
        return False, "Please write your request in plain words."

    return True, ""


# --- state -----------------------------------------------------------------


def get_state(user_id: int) -> dict:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM state WHERE user_id = ?", (user_id,)
        ).fetchone()
    return dict(row) if row else {
        "user_id": user_id, "mode": "idle", "tool": None, "last_ref": None,
        "pending_msg": None, "pending_text": None,
    }


def set_state(user_id: int, **kw):
    cur = get_state(user_id)
    cur.update(kw)
    with db() as conn:
        conn.execute(
            "INSERT INTO state (user_id, mode, tool, last_ref, pending_msg, "
            "pending_text) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET mode=excluded.mode, "
            "tool=excluded.tool, last_ref=excluded.last_ref, "
            "pending_msg=excluded.pending_msg, pending_text=excluded.pending_text",
            (user_id, cur.get("mode"), cur.get("tool"), cur.get("last_ref"),
             cur.get("pending_msg"), cur.get("pending_text")),
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
            (admin_msg_id, user_id, user_msg_id, stamp(), preview),
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


def create_ticket(user, kind, tool=None, days=None, usd=None, ngn=None) -> str:
    ref = new_reference()
    now = stamp()
    with db() as conn:
        conn.execute(
            "INSERT INTO tickets (ref, user_id, username, tool, days, status, "
            "created_at, kind, last_activity, price_usd, price_ngn) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (ref, user.id, user.username or user.full_name, tool, days,
             "awaiting_payment" if kind == "license" else "open",
             now, kind, now, usd, ngn),
        )
    return ref


def ticket_price(row) -> str:
    bits = []
    if row["price_usd"]:
        bits.append(f"${row['price_usd']:g}")
    if row["price_ngn"]:
        bits.append(f"₦{int(row['price_ngn']):,}")
    return " · ".join(bits) if bits else "—"


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


def touch_ticket(ref: str, by_user: bool = False):
    """Reset the idle clock; a customer message also clears the ghost clock."""
    if not ref:
        return
    with db() as conn:
        if by_user:
            # They answered — the ghost and payment clocks both stop. The
            # got_details flag stays, so a later no-show still costs double.
            conn.execute(
                "UPDATE tickets SET last_activity = ?, awaiting_since = NULL, "
                "pay_deadline = NULL WHERE ref = ?", (stamp(), ref),
            )
        else:
            conn.execute(
                "UPDATE tickets SET last_activity = ? WHERE ref = ?",
                (stamp(), ref),
            )


def mark_agent_reply(ref: str):
    """Start the ghost clock: the ball is now in the customer's court."""
    if not ref:
        return
    with db() as conn:
        conn.execute(
            "UPDATE tickets SET agent_replied = 1, awaiting_since = ?, "
            "last_activity = ? WHERE ref = ?", (stamp(), stamp(), ref),
        )


# --- payment details & window ----------------------------------------------

_ACCT_RE = re.compile(r"(?<!\d)\d{10}(?!\d)")            # NUBAN
_CRYPTO_RE = re.compile(
    r"\b(0x[a-fA-F0-9]{40}|T[1-9A-HJ-NP-Za-km-z]{33}|"
    r"bc1[ac-hj-np-z02-9]{20,}|[13][a-km-zA-HJ-NP-Z1-9]{25,34})\b"
)
_PAY_WORDS = re.compile(
    r"\b(acct|account|opay|palmpay|moniepoint|kuda|gtb|gtbank|uba|zenith|"
    r"access bank|first bank|fidelity|wema|sterling|bank|transfer|send to|"
    r"pay to|usdt|trc20|erc20|bep20|btc|wallet address|binance id)\b", re.I
)


def looks_like_payment_details(text: str | None) -> bool:
    if not text:
        return False
    if _CRYPTO_RE.search(text):
        return True
    if _ACCT_RE.search(text) and _PAY_WORDS.search(text):
        return True
    # an account number alone, on its own line, still counts
    return bool(_ACCT_RE.search(text) and len(text) < 120)


def start_pay_window(ref: str, minutes: int = PAY_WINDOW_MIN) -> str:
    deadline = (now_wat() + timedelta(minutes=minutes)).isoformat(
        timespec="seconds")
    with db() as conn:
        conn.execute(
            "UPDATE tickets SET got_details = 1, pay_deadline = ?, "
            "pay_reminded = 0, agent_replied = 1, awaiting_since = ?, "
            "last_activity = ? WHERE ref = ?",
            (deadline, stamp(), stamp(), ref),
        )
    return deadline


def extend_pay_window(ref: str, minutes: int = PAY_EXTEND_MIN) -> str | None:
    row = get_ticket(ref)
    if not row or row["pay_extended"]:
        return None
    deadline = (now_wat() + timedelta(minutes=minutes)).isoformat(
        timespec="seconds")
    with db() as conn:
        conn.execute(
            "UPDATE tickets SET pay_deadline = ?, pay_extended = 1, "
            "pay_reminded = 0 WHERE ref = ?", (deadline, ref),
        )
    return deadline


def clear_pay_window(ref: str):
    with db() as conn:
        conn.execute(
            "UPDATE tickets SET pay_deadline = NULL WHERE ref = ?", (ref,)
        )


def settle_ticket(ref: str):
    """They paid — drop every penalty flag on this ticket."""
    with db() as conn:
        conn.execute(
            "UPDATE tickets SET got_details = 0, pay_deadline = NULL, "
            "awaiting_since = NULL WHERE ref = ?", (ref,)
        )


def mark_reminded(ref: str):
    with db() as conn:
        conn.execute(
            "UPDATE tickets SET pay_reminded = 1 WHERE ref = ?", (ref,)
        )


def pay_due_tickets():
    """Tickets whose payment window has run out."""
    with db() as conn:
        return conn.execute(
            "SELECT * FROM tickets WHERE status != 'closed' "
            "AND pay_deadline IS NOT NULL AND pay_deadline < ?", (stamp(),)
        ).fetchall()


def pay_reminder_tickets(minutes: int = 5):
    cutoff = (now_wat() + timedelta(minutes=minutes)).isoformat(
        timespec="seconds")
    with db() as conn:
        return conn.execute(
            "SELECT * FROM tickets WHERE status != 'closed' "
            "AND pay_deadline IS NOT NULL AND pay_reminded = 0 "
            "AND pay_deadline <= ? AND pay_deadline > ?", (cutoff, stamp())
        ).fetchall()


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


def ghosted_tickets(hours: int):
    cutoff = (now_wat() - timedelta(hours=hours)).isoformat(timespec="seconds")
    with db() as conn:
        return conn.execute(
            "SELECT * FROM tickets WHERE status != 'closed' AND agent_replied = 1 "
            "AND awaiting_since IS NOT NULL AND awaiting_since < ?",
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
            f"{plain('star')}  {t['name']} — {price_label(t)}",
            callback_data=f"tool:{t['id']}",
        )]
        for t in list_tools()
    ]
    rows.append([InlineKeyboardButton("‹  Back", callback_data="menu:home")])
    return InlineKeyboardMarkup(rows)


def price_list_text() -> str:
    rows = list_tools()
    if not rows:
        return f"{e('cross')} No tools are listed right now."
    body = "\n".join(
        f"{e('star')} <b>{r['name']}</b> — {price_label(r)} "
        f"<i>({tool_days(r)} days)</i>" for r in rows
    )
    return (
        f"{e('gem')} <b>Froptools Price List</b>\n\n{body}\n\n"
        f"{e('info')} Every license is {DEFAULT_DAYS} days of access."
    )


def order_summary(t) -> str:
    return (
        f"{e('gem')} <b>Confirm your order</b>\n\n"
        f"{e('star')} Tool: <b>{t['name']}</b>\n"
        f"{e('rocket')} Price: <b>{price_label(t)}</b>\n"
        f"{e('hourglass')} Access: <b>{tool_days(t)} days</b>\n\n"
        f"{e('info')} Confirming opens a live ticket with a real agent and you'll "
        "be sent payment details. Only continue if you're paying today."
    )


def order_confirm_menu(tool_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"{plain('check')}  Yes, I'm ready to pay",
            callback_data=f"ord:{tool_id}",
        )],
        [InlineKeyboardButton("‹  Choose another tool", callback_data="menu:buy")],
    ])


def send_confirm_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"{plain('rocket')}  Send to agent", callback_data="ok:send"
        )],
        [InlineKeyboardButton(
            f"{plain('cross')}  Rewrite it", callback_data="ok:edit"
        )],
        [InlineKeyboardButton("‹  Cancel", callback_data="menu:home")],
    ])


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


def delivered_menu(admin_msg_id: int, ref: str | None) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(
        f"{plain('cross')}  Unsend for customer",
        callback_data=f"rm:{admin_msg_id}",
    )]]
    if ref:
        rows.append([InlineKeyboardButton(
            f"💳  Payment details sent — start {PAY_WINDOW_MIN}m timer",
            callback_data=f"pd:start:{ref}",
        )])
    return InlineKeyboardMarkup(rows)


def pay_window_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"{plain('hourglass')}  I need more time", callback_data="pay:more"
        )],
        [InlineKeyboardButton(
            f"{plain('cross')}  Cancel — I'm not paying", callback_data="pay:cancel"
        )],
    ])


def strike_menu(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🚫  Ban permanently", callback_data=f"bn:ban:{user_id}"),
        InlineKeyboardButton("♻️  Clear strikes", callback_data=f"bn:clear:{user_id}"),
    ]])


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
    rows.append([InlineKeyboardButton(
        "🚫  Ban this customer", callback_data=f"tk:ban:{ref}"
    )])
    return InlineKeyboardMarkup(rows)


def ticket_admin_text(row) -> str:
    status = STATUS_LABEL.get(row["status"], row["status"])
    urow = get_user(row["user_id"])
    flags = ""
    if urow:
        if urow["trusted"]:
            flags += f"\n{e('check')} <i>Trusted customer</i>"
        if urow["strikes"]:
            flags += f"\n{e('cross')} <i>Strikes: {urow['strikes']}</i>"
        if urow["detail_hits"]:
            flags += (f"\n{e('cross')} <i>Took details without paying: "
                      f"{urow['detail_hits']}×</i>")
    if row["pay_deadline"]:
        flags += (f"\n💳 <i>Payment window ends "
                  f"{row['pay_deadline'][11:16]} WAT</i>")
    elif row["got_details"]:
        flags += f"\n💳 <i>Has your payment details</i>"
    if row["kind"] == "license":
        head = f"{e('gem')} <b>LICENSE REQUEST</b>"
        body = (f"{e('rocket')} Tool: <b>{row['tool']}</b>\n"
                f"{e('gem')} Amount: <b>{ticket_price(row)}</b>\n"
                f"{e('hourglass')} Access: <b>{row['days']} days</b>\n")
    else:
        head = f"{e('sparkle')} <b>SUPPORT TICKET</b>"
        body = ""
    return (
        f"{head}\n\n"
        f"{e('star')} Ref: <code>{row['ref']}</code>\n"
        f"{body}"
        f"{e('user')} User: @{row['username']} — <code>{row['user_id']}</code>\n"
        f"{e('info')} Status: <b>{status}</b>{flags}\n\n"
        f"<i>Reply to this message to talk to the customer.</i>"
    )


def duplicate_notice(row) -> str:
    detail = (f"{row['tool']} · {ticket_price(row)}"
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
# Access gate
# ----------------------------------------------------------------------------


async def gate(context, user, q=None) -> bool:
    """True if this user is banned or cooling down (notice already shown)."""
    if user.id == ADMIN_ID:
        return False
    st = block_status(user.id)
    if not st:
        return False
    kind, detail = st
    text = block_text(kind, detail)
    if q:
        await q.answer(
            "Access restricted." if kind == "banned" else "Temporarily paused.",
            show_alert=True,
        )
        try:
            await edit_html(q, text)
        except Exception:
            pass
    elif should_renotify(user.id):
        try:
            await send_html(context.bot, user.id, text)
        except Exception:
            pass
    return True


# ----------------------------------------------------------------------------
# User handlers
# ----------------------------------------------------------------------------


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id == ADMIN_ID:
        return await admin_help(update, context)
    if await gate(context, user):
        return
    remember_user(user)
    set_state(user.id, mode="idle", tool=None, pending_msg=None, pending_text=None)
    await send_html(context.bot, user.id, welcome_text(), reply_markup=main_menu())


async def on_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    user = q.from_user
    if await gate(context, user, q):
        return
    await q.answer()
    action = q.data.split(":", 1)[1]
    remember_user(user)

    if action == "home":
        set_state(user.id, mode="idle", tool=None,
                  pending_msg=None, pending_text=None)
        return await edit_html(q, welcome_text(), reply_markup=main_menu())

    if action == "resume":
        row = open_ticket_for(user.id)
        if not row:
            set_state(user.id, mode="idle", tool=None, last_ref=None)
            return await edit_html(q, welcome_text(), reply_markup=main_menu())
        set_state(user.id, mode="live", last_ref=row["ref"])
        touch_ticket(row["ref"], by_user=True)
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
        set_state(user.id, mode="idle", tool=None, last_ref=None,
                  pending_msg=None, pending_text=None)
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
        set_state(user.id, mode="await_message",
                  pending_msg=None, pending_text=None)
        text = (
            f"{e('user')} <b>Live Agent</b>\n\n"
            "In <b>one message</b>, tell us:\n"
            f"{e('star')} which tool you're using\n"
            f"{e('star')} what exactly went wrong\n\n"
            f"{e('info')} <i>You'll see a preview and confirm before it's sent. "
            "One-word messages and jokes aren't forwarded, and repeat "
            "time-wasting gets your access paused.</i>\n"
        )
        text += (f"\n{e('bolt')} An agent will reply right here."
                 if within_hours() else f"\n{offline_notice()}")
        await edit_html(q, text)


async def on_tool(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if await gate(context, q.from_user, q):
        return
    await q.answer()
    row = tool_by_id(int(q.data.split(":", 1)[1]))
    if not row:
        return await edit_html(
            q,
            f"{e('cross')} That tool is no longer available.",
            reply_markup=tools_menu(),
        )
    user = q.from_user

    existing = open_ticket_for(user.id)
    if existing:
        return await edit_html(
            q, duplicate_notice(existing), reply_markup=duplicate_menu()
        )

    set_state(user.id, tool=row["name"])
    await edit_html(
        q, order_summary(row), reply_markup=order_confirm_menu(row["id"])
    )


async def on_order_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    user = q.from_user
    if await gate(context, user, q):
        return
    await q.answer()

    t = tool_by_id(int(q.data.split(":", 1)[1]))
    if not t:
        return await edit_html(
            q, f"{e('cross')} That tool is no longer available.",
            reply_markup=tools_menu(),
        )

    existing = open_ticket_for(user.id)
    if existing:
        return await edit_html(
            q, duplicate_notice(existing), reply_markup=duplicate_menu()
        )

    days = tool_days(t)
    ref = create_ticket(user, "license", t["name"], days,
                        usd=t["usd"], ngn=t["ngn"])
    set_state(user.id, mode="live", tool=t["name"], last_ref=ref)

    text = (
        f"{e('check')} <b>Order Reference Generated</b>\n\n"
        f"{e('gem')} Reference: <code>{ref}</code>\n"
        f"{e('star')} Tool: <b>{t['name']}</b>\n"
        f"{e('rocket')} Amount: <b>{price_label(t)}</b>\n"
        f"{e('hourglass')} Access: <b>{days} days</b>\n\n"
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


async def on_send_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The customer confirmed (or wants to rewrite) their screened message."""
    q = update.callback_query
    user = q.from_user
    if await gate(context, user, q):
        return
    action = q.data.split(":", 1)[1]
    st = get_state(user.id)

    if action == "edit":
        await q.answer()
        set_state(user.id, mode="await_message",
                  pending_msg=None, pending_text=None)
        return await edit_html(
            q,
            f"{e('user')} <b>Go ahead</b>\n\n"
            "Send the message you'd like the agent to read.",
        )

    pending = st.get("pending_msg")
    if not pending:
        await q.answer("Nothing to send.", show_alert=True)
        return await edit_html(q, welcome_text(), reply_markup=main_menu())

    await q.answer("Sending…")

    existing = open_ticket_for(user.id)
    ref = existing["ref"] if existing else create_ticket(user, "support")
    set_state(user.id, mode="live", last_ref=ref,
              pending_msg=None, pending_text=None)
    touch_ticket(ref, by_user=True)
    clear_junk(user.id)

    await typing(context.bot, ADMIN_ID)
    row = get_ticket(ref)
    header = await send_html(
        context.bot, ADMIN_ID, ticket_admin_text(row),
        reply_markup=ticket_keyboard(ref, row["status"], row["kind"]),
    )
    set_ticket_admin_msg(ref, header.message_id)
    map_msg(header.message_id, user.id)

    try:
        copied = await context.bot.copy_message(
            chat_id=ADMIN_ID, from_chat_id=user.id, message_id=pending
        )
        map_msg(copied.message_id, user.id)
    except Exception as exc:
        log.warning("Could not copy pending message: %s", exc)
        body = esc(st.get("pending_text") or "")
        copied = await send_html(context.bot, ADMIN_ID, body)
        map_msg(copied.message_id, user.id)

    ack = (f"{e('check')} Your message has been delivered to our support team.\n"
           f"{e('gem')} Ticket: <code>{ref}</code>\n")
    ack += (f"{e('hourglass')} An agent will reply here shortly. "
            "Please stay available — tickets left unanswered are closed and "
            "counted against your account."
            if within_hours() else f"\n{offline_notice()}")
    await edit_html(q, ack, reply_markup=end_chat_menu())


async def on_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user

    if await gate(context, user):
        return

    remember_user(user)
    st = get_state(user.id)
    mode = st.get("mode")

    if mode not in ("await_message", "confirm", "live"):
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

    # --- opening message: screen it, then ask for confirmation --------------
    if mode in ("await_message", "confirm"):
        if is_trusted(user.id):
            ok, why = True, ""
        else:
            ok, why = screen_opening(msg.text or msg.caption)

        if not ok:
            junk = bump_junk(user.id)
            left = MAX_JUNK - junk
            if junk >= MAX_JUNK:
                until = set_cooldown(user.id, COOLDOWN_HOURS)
                clear_junk(user.id)
                set_state(user.id, mode="idle", pending_msg=None,
                          pending_text=None)
                await send_html(
                    context.bot, user.id, block_text("cooldown", until)
                )
                return await send_html(
                    context.bot, ADMIN_ID,
                    f"{e('hourglass')} <b>Auto-paused</b> — "
                    f"{user_label(user)} sent {MAX_JUNK} low-effort openers. "
                    f"On hold for {COOLDOWN_HOURS}h.",
                    reply_markup=strike_menu(user.id),
                )
            return await send_html(
                context.bot, user.id,
                f"{e('info')} <b>Not sent yet</b>\n\n{why}\n\n"
                f"{e('hourglass')} <i>{left} attempt(s) left before your "
                "access is paused.</i>",
            )

        preview = esc((msg.text or msg.caption or "").strip())[:600]
        set_state(user.id, mode="confirm",
                  pending_msg=msg.message_id, pending_text=preview)
        return await send_html(
            context.bot, user.id,
            f"{e('rocket')} <b>Ready to send</b>\n\n"
            f"<blockquote>{preview}</blockquote>\n\n"
            f"{e('info')} An agent will be pulled into this chat. "
            "Only send if you'll be here to reply.",
            reply_markup=send_confirm_menu(),
        )

    # --- live conversation --------------------------------------------------
    ref = st.get("last_ref")
    if not ref:
        row = open_ticket_for(user.id)
        ref = row["ref"] if row else None
    if not ref:
        set_state(user.id, mode="await_message")
        return await send_html(
            context.bot, user.id,
            f"{e('info')} Your ticket is closed. Send your request again and "
            "we'll open a new one.",
        )

    set_state(user.id, mode="live", last_ref=ref)
    touch_ticket(ref, by_user=True)

    await typing(context.bot, ADMIN_ID)
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

    if not within_hours():
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


async def _do_ban(bot, user_id: int, reason: str = "") -> None:
    ban_user(user_id, reason)
    row = open_ticket_for(user_id)
    if row:
        set_ticket_status(row["ref"], "closed")
        await _sync_admin_ticket(bot, row["ref"])
    set_state(user_id, mode="idle", tool=None, last_ref=None,
              pending_msg=None, pending_text=None)
    try:
        await send_html(bot, user_id, block_text("banned", reason))
    except Exception:
        pass


async def penalise(bot, row, why: str):
    """A ticket died because the customer went quiet. Charge for it."""
    user_id = row["user_id"]
    if is_trusted(user_id):
        return
    took_details = bool(row["got_details"])
    strikes = add_strike(user_id, PAY_STRIKE if took_details else 1)
    hits = bump_detail_hit(user_id) if took_details else 0

    if took_details:
        note = (
            f"{e('cross')} <b>Ticket closed — no payment</b>\n\n"
            f"{e('info')} <code>{row['ref']}</code>: you were sent payment "
            f"details and didn't respond.\n"
            f"{e('hourglass')} Strikes: <b>{strikes}/{STRIKE_LIMIT}</b> · "
            f"unpaid details taken: <b>{hits}/{DETAIL_LIMIT}</b>."
        )
    else:
        note = (
            f"{e('cross')} <b>Ticket closed — no reply</b>\n\n"
            f"{e('info')} <code>{row['ref']}</code> was closed because you "
            f"stopped responding.\n"
            f"{e('hourglass')} Strikes: <b>{strikes}/{STRIKE_LIMIT}</b>."
        )
    try:
        await send_html(bot, user_id, note, reply_markup=main_menu())
    except Forbidden:
        deactivate_user(user_id)
    except Exception as exc:
        log.debug("Penalty notice failed for %s: %s", user_id, exc)

    # repeat details-farming = automatic ban
    if took_details and hits >= DETAIL_LIMIT:
        await _do_ban(bot, user_id, "took payment details repeatedly, never paid")
        return await send_html(
            bot, ADMIN_ID,
            f"🚫 <b>Auto-banned — details farmer</b>\n\n"
            f"{e('user')} @{row['username']} — <code>{user_id}</code>\n"
            f"{e('info')} Took your payment details {hits}× without paying "
            f"(latest: <code>{row['ref']}</code>).\n"
            f"{e('star')} Undo with <code>/unban {user_id}</code>",
        )

    if strikes >= STRIKE_LIMIT:
        until = set_cooldown(user_id, COOLDOWN_HOURS)
        try:
            await send_html(bot, user_id, block_text("cooldown", until))
        except Exception:
            pass
        await send_html(
            bot, ADMIN_ID,
            f"{e('cross')} <b>Time-waster paused</b>\n\n"
            f"{e('user')} @{row['username']} — <code>{user_id}</code>\n"
            f"{e('info')} {why} · strikes <b>{strikes}</b>"
            + (f" · details taken <b>{hits}×</b>" if took_details else "") + "\n"
            f"{e('hourglass')} On hold for {COOLDOWN_HOURS}h.",
            reply_markup=strike_menu(user_id),
        )
    elif took_details:
        await send_html(
            bot, ADMIN_ID,
            f"💳 <b>No payment</b> — <code>{row['ref']}</code>\n"
            f"{e('user')} @{row['username']} — <code>{user_id}</code>\n"
            f"{e('info')} Details taken {hits}× · strikes {strikes}",
            reply_markup=strike_menu(user_id),
        )


async def on_pay_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """You confirmed that the last message contained payment details."""
    q = update.callback_query
    if q.from_user.id != ADMIN_ID:
        return await q.answer("Not authorised.", show_alert=True)
    ref = q.data.split(":", 2)[2]
    row = get_ticket(ref)
    if not row or row["status"] == "closed":
        return await q.answer("Ticket is closed.", show_alert=True)
    deadline = start_pay_window(ref)
    await q.answer(f"Timer running — ends {deadline[11:16]} WAT")
    await _sync_admin_ticket(context.bot, ref)
    try:
        await send_html(
            context.bot, row["user_id"], pay_prompt(deadline),
            reply_markup=pay_window_menu(),
        )
    except Exception as exc:
        log.warning("Pay prompt failed: %s", exc)
    await q.edit_message_text(
        f"{plain('check')} Delivered · 💳 payment window ends "
        f"{deadline[11:16]} WAT"
    )


def pay_prompt(deadline: str) -> str:
    return (
        f"{e('gem')} <b>Payment details sent</b>\n\n"
        f"{e('hourglass')} Please pay and send your receipt before "
        f"<b>{deadline[11:16]} WAT</b>.\n\n"
        f"{e('info')} If you go silent, this ticket closes automatically and "
        f"a strike is recorded. Collecting our details "
        f"{DETAIL_LIMIT} times without paying gets your access removed.\n"
        f"{e('star')} Not ready? Tap <b>Cancel</b> — no strike for being honest."
    )


async def on_pay_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Customer tapped 'need more time' or 'cancel' during a payment window."""
    q = update.callback_query
    user = q.from_user
    if await gate(context, user, q):
        return
    action = q.data.split(":", 1)[1]
    row = open_ticket_for(user.id)
    if not row:
        await q.answer()
        return await edit_html(q, welcome_text(), reply_markup=main_menu())

    if action == "more":
        deadline = extend_pay_window(row["ref"])
        if not deadline:
            await q.answer("Already extended once.", show_alert=True)
            return
        await q.answer("Extended.")
        await send_html(
            context.bot, ADMIN_ID,
            f"{e('hourglass')} <code>{row['ref']}</code> — customer asked for "
            f"{PAY_EXTEND_MIN} more minutes (until {deadline[11:16]} WAT).",
        )
        return await edit_html(
            q,
            f"{e('hourglass')} <b>Extended</b>\n\n"
            f"New deadline: <b>{deadline[11:16]} WAT</b>. This is the only "
            "extension available.",
        )

    # cancel — honest exit, no strike
    clear_pay_window(row["ref"])
    settle_ticket(row["ref"])
    set_ticket_status(row["ref"], "closed")
    set_state(user.id, mode="idle", tool=None, last_ref=None,
              pending_msg=None, pending_text=None)
    await _sync_admin_ticket(context.bot, row["ref"])
    await q.answer("Order cancelled.")
    await edit_html(
        q,
        f"{e('check')} <b>Order cancelled</b>\n\n"
        "No strike recorded — thanks for telling us. Come back when you're "
        "ready.",
        reply_markup=main_menu(),
    )
    await send_html(
        context.bot, ADMIN_ID,
        f"{e('info')} <code>{row['ref']}</code> — customer cancelled before "
        "paying. No strike applied.",
    )


async def on_ticket_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != ADMIN_ID:
        return await q.answer("Not authorised.", show_alert=True)

    _, action, ref = q.data.split(":", 2)
    row = get_ticket(ref)
    if not row:
        return await q.answer("Ticket not found.", show_alert=True)

    if action == "ban":
        await _do_ban(context.bot, row["user_id"], f"ticket {ref}")
        await q.answer(f"Banned {row['user_id']}", show_alert=True)
        return await edit_html(
            q,
            f"{e('cross')} <b>Customer banned</b>\n\n"
            f"{e('user')} <code>{row['user_id']}</code> (@{row['username']})\n"
            f"{e('star')} Ticket <code>{ref}</code> closed.\n\n"
            f"{e('info')} Undo with <code>/unban {row['user_id']}</code>",
        )

    new_status = {
        "paid": "paid",
        "delivered": "delivered",
        "close": "closed",
        "reopen": "open" if row["kind"] == "support" else "awaiting_payment",
    }[action]

    set_ticket_status(ref, new_status)
    touch_ticket(ref)
    if new_status in ("paid", "delivered"):
        settle_ticket(ref)          # they paid — no penalty can follow
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


async def on_strike_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Buttons attached to the auto-restriction alert."""
    q = update.callback_query
    if q.from_user.id != ADMIN_ID:
        return await q.answer("Not authorised.", show_alert=True)

    _, action, raw = q.data.split(":", 2)
    user_id = int(raw)

    if action == "ban":
        await _do_ban(context.bot, user_id, "repeat time-waster")
        await q.answer("Banned.")
        return await edit_html(
            q,
            f"{e('cross')} <b>Banned</b> <code>{user_id}</code>\n"
            f"{e('info')} Undo with <code>/unban {user_id}</code>",
        )

    unban_user(user_id)
    await q.answer("Strikes cleared.")
    await edit_html(
        q, f"{e('check')} Strikes cleared for <code>{user_id}</code>."
    )


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
        body = "\n".join(
            f"{e('star')} <b>{r['name']}</b> — {price_label(r)} "
            f"<i>({tool_days(r)}d)</i>  <code>#{r['id']}</code>"
            for r in rows
        )
    return (
        f"{e('rocket')} <b>Tool Manager</b>\n\n{body}\n\n"
        f"{e('info')} Tap a name to remove it, or add one below.\n"
        f"{e('gem')} Change a price: "
        f"<code>/price FropTrack 25 35000</code>\n"
        f"{e('hourglass')} Add days too: <code>/price FropTrack 25 35000 90</code>"
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
            f"{e('info')} Send it as: <code>Name USD NGN [days]</code>\n"
            f"{e('star')} Example: <code>FropVaultFlash 20 28000</code>\n"
            f"{e('hourglass')} Days default to {DEFAULT_DAYS}.\n"
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


async def cmd_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/price <tool> <usd> <ngn> [days]"""
    msg = update.effective_message
    args = list(context.args or [])
    if len(args) < 2:
        return await msg.reply_text(
            "Usage: /price <tool> <usd> <ngn> [days]\n"
            "Example: /price FropTrack 25 35000"
        )
    row = find_tool(args[0])
    if not row:
        return await msg.reply_text(f"{plain('cross')} No tool matching “{args[0]}”.")
    nums = [a.replace("$", "").replace("₦", "").replace(",", "") for a in args[1:]]
    try:
        usd = float(nums[0])
        ngn = int(float(nums[1])) if len(nums) > 1 else None
        days = int(float(nums[2])) if len(nums) > 2 else None
    except ValueError:
        return await msg.reply_text("Prices must be numbers, e.g. 25 35000")
    set_price(row["id"], usd, ngn, days)
    updated = tool_by_id(row["id"])
    await send_html(
        context.bot, ADMIN_ID,
        f"{e('check')} <b>{updated['name']}</b> → {price_label(updated)} "
        f"({tool_days(updated)} days)",
    )


async def cmd_pricelist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_html(context.bot, ADMIN_ID, price_list_text())


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.bot_data.pop("awaiting_tool", None)
    await update.effective_message.reply_text(f"{plain('check')} Cancelled.")


# ----------------------------------------------------------------------------
# Scheduled jobs
# ----------------------------------------------------------------------------


async def _close_and_charge(bot, row, why: str):
    ref, user_id = row["ref"], row["user_id"]
    set_ticket_status(ref, "closed")
    set_state(user_id, mode="idle", tool=None, last_ref=None,
              pending_msg=None, pending_text=None)
    await _sync_admin_ticket(bot, ref)
    await penalise(bot, row, why)
    await asyncio.sleep(0.05)


async def job_payments(context: ContextTypes.DEFAULT_TYPE):
    """Nudge, then close, tickets where payment details went unanswered."""
    for row in pay_reminder_tickets():
        mark_reminded(row["ref"])
        try:
            await send_html(
                context.bot, row["user_id"],
                f"{e('hourglass')} <b>5 minutes left</b>\n\n"
                f"Send your payment receipt for <code>{row['ref']}</code>, or "
                "tap below. Silence closes the ticket and costs you a strike.",
                reply_markup=pay_window_menu(),
            )
        except Forbidden:
            deactivate_user(row["user_id"])
        except Exception as exc:
            log.debug("Pay reminder failed: %s", exc)

    expired = pay_due_tickets()
    for row in expired:
        await _close_and_charge(
            context.bot, row, "went silent after receiving payment details"
        )
    if expired:
        log.info("Closed %d unpaid ticket(s)", len(expired))


async def job_ghosts(context: ContextTypes.DEFAULT_TYPE):
    """Close tickets where the agent replied and the customer vanished."""
    rows = ghosted_tickets(GHOST_HOURS)
    for row in rows:
        await _close_and_charge(
            context.bot, row, f"no reply for {GHOST_HOURS}h after an agent reply"
        )
    if rows:
        log.info("Ghost-closed %d ticket(s)", len(rows))


async def job_autoclose(context: ContextTypes.DEFAULT_TYPE):
    """Close tickets with no activity for IDLE_HOURS."""
    purge_relays()
    await job_ghosts(context)

    rows = stale_tickets(IDLE_HOURS)
    if not rows:
        return
    for row in rows:
        ref, user_id = row["ref"], row["user_id"]
        if row["got_details"]:
            # they had the account details and never came back — charge it
            await _close_and_charge(
                context.bot, row, f"idle {IDLE_HOURS}h holding payment details"
            )
            continue
        set_ticket_status(ref, "closed")
        set_state(user_id, mode="idle", tool=None, last_ref=None,
                  pending_msg=None, pending_text=None)
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
        f"{e('gem')} <code>/prices</code> — current price list\n"
        f"{e('star')} <code>/price &lt;tool&gt; &lt;usd&gt; &lt;ngn&gt; [days]</code> — set a price\n"
        f"{e('sparkle')} <code>/broadcast &lt;text&gt;</code> — message all users\n"
        f"{e('cross')} <code>/close &lt;user_id&gt;</code> — close their ticket\n\n"
        f"{e('cross')} <b>Moderation</b>\n"
        f"<code>/ban &lt;user_id&gt; [reason]</code> — or reply to their message\n"
        f"<code>/unban &lt;user_id&gt;</code> — also clears strikes\n"
        f"<code>/banned</code> — banned, paused and striked users\n"
        f"<code>/trust &lt;user_id&gt;</code> — skip screening for a regular\n\n"
        f"{e('globe')} <code>/status</code> — hours, users, jobs\n"
        f"{e('info')} <code>/emoji</code> — premium emoji status\n\n"
        f"{e('info')} <i>Editing one of your sent messages updates the "
        "customer's copy automatically.</i>\n"
        f"{e('info')} <i>Send an account number or wallet address and a "
        f"{PAY_WINDOW_MIN}-minute payment window starts by itself. If it "
        "doesn't fire, tap the 💳 button under \"Delivered.\"</i>\n"
        f"Hours: <b>{hours_label()}</b> · idle close <b>{IDLE_HOURS}h</b> · "
        f"ghost close <b>{GHOST_HOURS}h</b> · pause at <b>{STRIKE_LIMIT}</b> "
        f"strikes · auto-ban at <b>{DETAIL_LIMIT}</b> unpaid details",
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with db() as conn:
        users = conn.execute(
            "SELECT COUNT(*) c FROM users WHERE active = 1"
        ).fetchone()["c"]
        live = conn.execute(
            "SELECT COUNT(*) c FROM state WHERE mode = 'live'"
        ).fetchone()["c"]
        pending = conn.execute(
            "SELECT COUNT(*) c FROM state WHERE mode = 'confirm'"
        ).fetchone()["c"]
        open_t = conn.execute(
            "SELECT COUNT(*) c FROM tickets WHERE status != 'closed'"
        ).fetchone()["c"]
        relays = conn.execute("SELECT COUNT(*) c FROM relay_map").fetchone()["c"]
        banned = conn.execute(
            "SELECT COUNT(*) c FROM users WHERE banned = 1"
        ).fetchone()["c"]
        paused = conn.execute(
            "SELECT COUNT(*) c FROM users WHERE cooldown_until > ?", (stamp(),)
        ).fetchone()["c"]
    ping = f"every {PING_MINUTES} min" if PING_MINUTES else "disabled"
    await send_html(
        context.bot, ADMIN_ID,
        f"{e('globe')} <b>Status</b>\n\n"
        f"{e('bolt')} Now: <b>{now_wat().strftime('%H:%M')} WAT</b> — "
        f"{'open' if within_hours() else 'closed'}\n"
        f"{e('info')} Hours: {hours_label()}\n"
        f"{e('user')} Reachable users: <b>{users}</b>\n"
        f"{e('sparkle')} Live chats: <b>{live}</b>\n"
        f"{e('hourglass')} Awaiting confirm: <b>{pending}</b>\n"
        f"{e('hourglass')} Open tickets: <b>{open_t}</b>\n"
        f"{e('star')} Active tools: <b>{len(list_tools())}</b>\n"
        f"{e('cross')} Banned: <b>{banned}</b> · paused: <b>{paused}</b>\n"
        f"{e('cross')} Unsendable replies tracked: <b>{relays}</b>\n"
        f"{e('rocket')} Keep-alive: <b>{ping}</b>\n"
        f"{e('gem')} Auto-close: <b>{IDLE_HOURS}h idle · {GHOST_HOURS}h ghost</b>",
    )


async def cmd_emoji(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = "active" if PREMIUM_ENABLED else "disabled (unicode fallback)"
    sample = "  ".join(e(k) for k in EMOJI)
    await send_html(
        context.bot, ADMIN_ID,
        f"{e('info')} Premium emoji: <b>{state}</b>\n\n{sample}",
    )


# --- moderation commands ----------------------------------------------------


def _target_user(update: Update, context) -> tuple[int | None, str]:
    """Resolve a user id from /cmd <id> [reason] or from a replied-to message."""
    msg = update.effective_message
    args = list(context.args or [])
    if args:
        try:
            return int(args[0]), " ".join(args[1:]).strip()
        except ValueError:
            pass
    if msg.reply_to_message:
        uid = lookup_msg(msg.reply_to_message.message_id)
        if uid:
            return uid, " ".join(args).strip()
    return None, ""


async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user_id, reason = _target_user(update, context)
    if not user_id:
        return await msg.reply_text(
            "Usage: /ban <user_id> [reason] — or reply to the customer's "
            "message with /ban."
        )
    if user_id == ADMIN_ID:
        return await msg.reply_text("You can't ban yourself.")
    await _do_ban(context.bot, user_id, reason)
    await msg.reply_text(
        f"{plain('cross')} Banned {user_id}."
        + (f" Reason: {reason}" if reason else "")
        + f"\nUndo: /unban {user_id}"
    )


async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user_id, _ = _target_user(update, context)
    if not user_id:
        return await msg.reply_text("Usage: /unban <user_id>")
    unban_user(user_id)
    try:
        await send_html(
            context.bot, user_id,
            f"{e('check')} <b>Access restored</b>\n\n"
            "You can use the support portal again.",
            reply_markup=main_menu(),
        )
    except Exception:
        pass
    await msg.reply_text(f"{plain('check')} Unbanned {user_id} (strikes cleared).")


async def cmd_trust(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user_id, _ = _target_user(update, context)
    if not user_id:
        return await msg.reply_text("Usage: /trust <user_id>")
    trust_user(user_id, True)
    await msg.reply_text(
        f"{plain('check')} {user_id} is trusted — screening and strikes skipped."
    )


async def cmd_banned(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = banned_list()
    if not rows:
        return await send_html(
            context.bot, ADMIN_ID, f"{e('check')} No banned or flagged users."
        )
    out = [f"{e('cross')} <b>Banned & flagged</b>\n"]
    for r in rows:
        if r["banned"]:
            tag = f"{plain('cross')} BANNED"
        elif r["cooldown_until"] and r["cooldown_until"] > stamp():
            tag = f"{plain('hourglass')} paused → {r['cooldown_until'][5:16]}"
        else:
            tag = f"{plain('info')} {r['strikes']} strike(s)"
        handle = f"@{r['username']}" if r["username"] else (r["full_name"] or "—")
        if r["detail_hits"]:
            tag += f" · 💳 {r['detail_hits']}× unpaid details"
        reason = f"\n     <i>{esc(r['ban_reason'])}</i>" if r["ban_reason"] else ""
        out.append(
            f"{e('star')} <code>{r['user_id']}</code> — {esc(handle)}\n"
            f"     {tag}{reason}"
        )
    out.append(f"\n{e('info')} <code>/unban &lt;user_id&gt;</code> to restore.")
    await send_html(context.bot, ADMIN_ID, "\n".join(out))


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
        raw = (msg.text or "").strip()
        parts = [p for p in re.split(r"[|,]|\s+", raw) if p]
        if not parts:
            return await msg.reply_text(
                f"{plain('cross')} Send: Name USD NGN [days]"
            )
        nums, words = [], []
        for p in parts:
            cleaned = p.replace("$", "").replace("₦", "").replace(",", "")
            (nums if cleaned.replace(".", "", 1).isdigit() else words).append(
                cleaned if cleaned.replace(".", "", 1).isdigit() else p
            )
        name = " ".join(words).strip()
        if not name or len(name) > 40:
            return await msg.reply_text(
                f"{plain('cross')} Send a plain tool name under 40 characters, "
                "then the prices: FropVaultFlash 20 28000"
            )
        usd = float(nums[0]) if len(nums) > 0 else None
        ngn = int(float(nums[1])) if len(nums) > 1 else None
        days = int(float(nums[2])) if len(nums) > 2 else DEFAULT_DAYS
        add_tool(name, usd, ngn, days)
        row = find_tool(name)
        await msg.reply_text(
            f"{plain('check')} Saved “{name}” — "
            f"{degrade(price_label(row))} ({tool_days(row)} days)."
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
        mark_agent_reply(ref)

        body = msg.text or msg.caption
        if ref and looks_like_payment_details(body):
            deadline = start_pay_window(ref)
            await _sync_admin_ticket(context.bot, ref)
            try:
                await send_html(
                    context.bot, user_id, pay_prompt(deadline),
                    reply_markup=pay_window_menu(),
                )
            except Exception as exc:
                log.warning("Pay prompt failed: %s", exc)
            return await msg.reply_text(
                f"{plain('check')} Delivered · 💳 payment details detected — "
                f"they have until {deadline[11:16]} WAT",
                reply_markup=unsend_button(msg.message_id),
            )

        await msg.reply_text(
            f"{plain('check')} Delivered.",
            reply_markup=delivered_menu(msg.message_id, ref),
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
        ref = row["ref"] if row else None
        set_state(user_id, mode="live", last_ref=ref)
        if ref:
            mark_agent_reply(ref)

        if ref and looks_like_payment_details(text):
            deadline = start_pay_window(ref)
            await _sync_admin_ticket(context.bot, ref)
            await send_html(
                context.bot, user_id, pay_prompt(deadline),
                reply_markup=pay_window_menu(),
            )
            return await msg.reply_text(
                f"{plain('check')} Delivered · 💳 payment window until "
                f"{deadline[11:16]} WAT",
                reply_markup=unsend_button(msg.message_id),
            )

        await msg.reply_text(
            f"{plain('check')} Delivered.",
            reply_markup=delivered_menu(msg.message_id, ref),
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
        detail = (f"{r['tool']} · {ticket_price(r)}"
                  if r["kind"] == "license" else "support")
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
    set_state(user_id, mode="idle", tool=None, last_ref=None,
              pending_msg=None, pending_text=None)
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
    app.add_handler(CommandHandler("price", cmd_price, filters=admin))
    app.add_handler(CommandHandler("prices", cmd_pricelist, filters=admin))
    app.add_handler(CommandHandler("pricelist", cmd_pricelist, filters=admin))
    app.add_handler(CommandHandler("cancel", cmd_cancel, filters=admin))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast, filters=admin))
    app.add_handler(CommandHandler("emoji", cmd_emoji, filters=admin))
    app.add_handler(CommandHandler("status", cmd_status, filters=admin))
    app.add_handler(CommandHandler("admin", admin_help, filters=admin))
    app.add_handler(CommandHandler("ban", cmd_ban, filters=admin))
    app.add_handler(CommandHandler("unban", cmd_unban, filters=admin))
    app.add_handler(CommandHandler("banned", cmd_banned, filters=admin))
    app.add_handler(CommandHandler("trust", cmd_trust, filters=admin))

    app.add_handler(CommandHandler(["start", "menu", "help"], start))

    app.add_handler(CallbackQueryHandler(on_menu, pattern=r"^menu:"))
    app.add_handler(CallbackQueryHandler(on_tool, pattern=r"^tool:"))
    app.add_handler(CallbackQueryHandler(on_order_confirm, pattern=r"^ord:"))
    app.add_handler(CallbackQueryHandler(on_send_confirm, pattern=r"^ok:"))
    app.add_handler(CallbackQueryHandler(on_ticket_action, pattern=r"^tk:"))
    app.add_handler(CallbackQueryHandler(on_tool_admin, pattern=r"^tl:"))
    app.add_handler(CallbackQueryHandler(on_broadcast_action, pattern=r"^bc:"))
    app.add_handler(CallbackQueryHandler(on_relay_delete, pattern=r"^rm:"))
    app.add_handler(CallbackQueryHandler(on_strike_action, pattern=r"^bn:"))
    app.add_handler(CallbackQueryHandler(on_pay_admin, pattern=r"^pd:"))
    app.add_handler(CallbackQueryHandler(on_pay_user, pattern=r"^pay:"))

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
        app.job_queue.run_repeating(
            job_payments, interval=60, first=30, name="payments"
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
