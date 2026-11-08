"""
🌙 NightFrost Bot — single-file Discord bot
  • Gem donation tracking + auto-updating leaderboard
  • Moderation commands with numbered case logs
  • AutoMod blocked-word filter
  • CAPTCHA verification (image code -> verified role)

Setup: copy .env.example to .env, fill it in, `pip install -r requirements.txt`, `python bot.py`
"""
from __future__ import annotations

import asyncio
import datetime as dt
import io
import json
import logging
import os
import random
import re
import secrets
import sys
import time
import unicodedata
from dataclasses import dataclass
from collections import defaultdict
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    import aiosqlite
    import discord
    from discord import app_commands
    from discord.ext import commands, tasks
    from dotenv import load_dotenv
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
except ImportError as exc:  # pragma: no cover
    sys.exit(f"❌ Missing package ({exc.name}). Run:  pip install -r requirements.txt")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-7s │ %(name)s │ %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("nightfrost")

discord.VoiceClient.warn_nacl = False  # we don't use voice; hides the PyNaCl/davey warnings

ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"

# Banner shown above the verification panel. Discord CDN links EXPIRE, so the bot prefers a local
# file named verify_banner.png next to bot.py (it gets uploaded and never expires). This URL is the fallback.
BANNER_FILENAME = "verify_banner.png"
DEFAULT_GIVEAWAY_CLAIM_CHANNEL_ID = 1440760203976118414  # channel winners are told to open a claim ticket in
DEFAULT_VERIFY_BANNER_URL = (
    "https://cdn.discordapp.com/attachments/1553465204946370770/1568997079685275910/"
    "e956f943-8f12-4e02-ab4a-006cea49900d.png?ex=6af1d9e6&is=6af08866&"
    "hm=9165c850258d32dbffb3a21a536fbb93d39649c3cabe58eb41e990bbd859ee1d&"
)


# ════════════════════════════════════════════════════════════════════════
#  CONFIG  (.env)
# ════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class Config:
    token: str
    guild_id: int | None
    donation_channel_id: int
    leaderboard_channel_id: int
    mod_log_channel_id: int
    automod_log_channel_id: int
    verify_channel_id: int
    verified_role_id: int
    verify_log_channel_id: int | None
    verify_banner_url: str
    top_donor_role_id: int | None
    daily_reward_channel_id: int
    giveaway_claim_channel_id: int
    daily_reward_prize: str
    daily_reward_timezone: str
    donation_admin_role_ids: tuple[int, ...]
    mod_role_ids: tuple[int, ...]
    leaderboard_size: int
    database_path: str


def _config_fail(problems: list[str]) -> None:
    print("\n❌ Configuration problems found in your .env file:\n")
    for p in problems:
        print(f"   • {p}")
    print("\nCopy .env.example to .env and fill in every required value.\n")
    sys.exit(1)


def load_config() -> Config:
    if not ENV_PATH.exists():
        _config_fail(["No .env file found. Copy .env.example to .env first."])

    load_dotenv(ENV_PATH)
    problems: list[str] = []

    def text(name: str, default: str | None = None) -> str:
        value = (os.getenv(name) or "").strip()
        if not value:
            if default is not None:
                return default
            problems.append(f"{name} is missing or empty")
        return value

    def number(name: str, required: bool = True) -> int | None:
        value = (os.getenv(name) or "").strip()
        if not value:
            if required:
                problems.append(f"{name} is missing or empty")
            return None
        if not value.isdigit():
            problems.append(f"{name} must be a number (got {value!r})")
            return None
        return int(value)

    def id_list(name: str) -> tuple[int, ...]:
        raw = (os.getenv(name) or "").replace(" ", "")
        ids: list[int] = []
        for part in raw.split(","):
            if not part:
                continue
            if not part.isdigit():
                problems.append(f"{name} must be comma-separated numbers (got {part!r})")
                continue
            ids.append(int(part))
        return tuple(ids)

    token = text("DISCORD_TOKEN")
    token = token.strip().strip("\"'").strip()          # tolerate quotes / stray spaces
    if token.lower().startswith("bot "):                 # tolerate a pasted "Bot " prefix
        token = token[4:].strip()
    if token and (token.count(".") != 2 or len(token) < 50 or " " in token):
        problems.append(
            "DISCORD_TOKEN doesn't look like a bot token (it should be one long string with two dots). "
            "Copy it from Developer Portal -> your app -> Bot -> Reset Token, "
            "NOT the Application ID, Public Key or Client Secret"
        )
    guild_id = number("GUILD_ID", required=False)
    donation_channel = number("DONATION_CHANNEL_ID")
    leaderboard_channel = number("LEADERBOARD_CHANNEL_ID")
    mod_log_channel = number("MOD_LOG_CHANNEL_ID")
    automod_log_channel = number("AUTOMOD_LOG_CHANNEL_ID")
    verify_channel = number("VERIFY_CHANNEL_ID")
    verified_role = number("VERIFIED_ROLE_ID")
    verify_log_channel = number("VERIFY_LOG_CHANNEL_ID", required=False)
    banner_url = text("VERIFY_BANNER_URL", DEFAULT_VERIFY_BANNER_URL)
    top_donor_role = number("TOP_DONOR_ROLE_ID", required=False)
    daily_channel = number("DAILY_REWARD_CHANNEL_ID")
    claim_channel = number("GIVEAWAY_CLAIM_CHANNEL_ID", required=False) or DEFAULT_GIVEAWAY_CLAIM_CHANNEL_ID
    daily_prize = text("DAILY_REWARD_PRIZE", "200m Gems Exp")
    tz_name = text("DAILY_REWARD_TIMEZONE", "UTC")
    try:
        ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        problems.append(f"DAILY_REWARD_TIMEZONE {tz_name!r} isn't a valid timezone (try UTC or Asia/Jerusalem)")
    donation_roles = id_list("DONATION_ADMIN_ROLE_IDS")
    mod_roles = id_list("MOD_ROLE_IDS")
    size = number("LEADERBOARD_SIZE", required=False) or 10
    db_path = text("DATABASE_PATH", "data/nightfrost.db")

    if problems:
        _config_fail(problems)

    return Config(
        token=token,
        guild_id=guild_id,
        donation_channel_id=donation_channel,  # type: ignore[arg-type]
        leaderboard_channel_id=leaderboard_channel,  # type: ignore[arg-type]
        mod_log_channel_id=mod_log_channel,  # type: ignore[arg-type]
        automod_log_channel_id=automod_log_channel,  # type: ignore[arg-type]
        verify_channel_id=verify_channel,  # type: ignore[arg-type]
        verified_role_id=verified_role,  # type: ignore[arg-type]
        verify_log_channel_id=verify_log_channel,
        verify_banner_url=banner_url,
        top_donor_role_id=top_donor_role,
        daily_reward_channel_id=daily_channel,  # type: ignore[arg-type]
        giveaway_claim_channel_id=claim_channel,
        daily_reward_prize=daily_prize,
        daily_reward_timezone=tz_name,
        donation_admin_role_ids=donation_roles,
        mod_role_ids=mod_roles,
        leaderboard_size=max(1, min(25, size)),
        database_path=db_path,
    )


# ════════════════════════════════════════════════════════════════════════
#  AMOUNTS & DURATIONS
# ════════════════════════════════════════════════════════════════════════
UNITS = {"k": 10**3, "m": 10**6, "b": 10**9, "t": 10**12}
MAX_DONATION = 10**15  # 1000T per single donation keeps totals safely inside SQLite ints
_AMOUNT_RE = re.compile(r"^(\d+(?:\.\d+)?)([kmbt]?)$", re.IGNORECASE)


class AmountError(ValueError):
    """Raised when an amount string can't be understood."""


def parse_amount(text: str) -> int:
    """'500k', '1.5m', '2b', '3.25t', '1,000,000' or '12345' -> int."""
    cleaned = text.strip().replace(",", "").replace("_", "").replace(" ", "")
    match = _AMOUNT_RE.match(cleaned)
    if not match:
        raise AmountError("Use a number with an optional suffix: `k`, `m`, `b` or `t` (e.g. `500k`, `1.5m`, `2b`).")
    number, suffix = match.groups()
    value = Decimal(number) * UNITS.get(suffix.lower(), 1)
    if value != value.to_integral_value():
        raise AmountError("That amount isn't a whole number of gems.")
    amount = int(value)
    if amount <= 0:
        raise AmountError("The amount must be greater than zero.")
    if amount > MAX_DONATION:
        raise AmountError("That amount is too large (max `1000t` per donation).")
    return amount


def format_amount(amount: int) -> str:
    """Compact display: 1500000 -> '1.5M'."""
    for suffix, unit in (("T", 10**12), ("B", 10**9), ("M", 10**6), ("K", 10**3)):
        if amount >= unit:
            value = (Decimal(amount) / Decimal(unit)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            return f"{value:f}".rstrip("0").rstrip(".") + suffix
    return str(amount)


_DURATION_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_DURATION_TOKEN = re.compile(r"(\d+)([smhdw])")
MAX_TIMEOUT = 28 * 86400  # Discord's limit


def parse_duration(text: str) -> int:
    """'10m', '2h', '1d12h' -> seconds. Raises ValueError on bad input."""
    cleaned = text.strip().lower().replace(" ", "")
    matches = list(_DURATION_TOKEN.finditer(cleaned))
    if not matches or "".join(m.group(0) for m in matches) != cleaned:
        raise ValueError("Use durations like `30s`, `10m`, `2h`, `1d` or `1d12h`.")
    total = sum(int(m.group(1)) * _DURATION_UNIT[m.group(2)] for m in matches)
    if total <= 0:
        raise ValueError("The duration must be greater than zero.")
    if total > MAX_TIMEOUT:
        raise ValueError("The maximum timeout is 28 days.")
    return total


def format_duration(seconds: int) -> str:
    parts = []
    for label, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if seconds >= size:
            parts.append(f"{seconds // size}{label}")
            seconds %= size
    return " ".join(parts) or "0s"


# ════════════════════════════════════════════════════════════════════════
#  EMBEDS
# ════════════════════════════════════════════════════════════════════════
class C:
    SUCCESS = 0x2ECC71
    ERROR = 0xE74C3C
    WARN = 0xF1C40F
    INFO = 0x3498DB
    GEM = 0x9B59B6
    AUTOMOD = 0xE67E22
    NEUTRAL = 0x95A5A6


def make_embed(
    title: str,
    description: str | None = None,
    color: int = C.INFO,
    *,
    fields: Iterable[tuple[str, str, bool]] = (),
    footer: str | None = None,
    thumbnail: str | None = None,
    timestamp: bool = True,
) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=color)
    for name, value, inline in fields:
        embed.add_field(name=name, value=value or "—", inline=inline)
    if footer:
        embed.set_footer(text=footer)
    if thumbnail:
        embed.set_thumbnail(url=thumbnail)
    if timestamp:
        embed.timestamp = dt.datetime.now(dt.timezone.utc)
    return embed


def success_embed(message: str) -> discord.Embed:
    return make_embed("✅ Done", message, C.SUCCESS)


def error_embed(message: str) -> discord.Embed:
    return make_embed("❌ Error", message, C.ERROR, timestamp=False)


async def respond(interaction: discord.Interaction, embed: discord.Embed, *, ephemeral: bool = False) -> None:
    """Send a reply whether or not the interaction was already deferred."""
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=ephemeral)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=ephemeral)


# ════════════════════════════════════════════════════════════════════════
#  DATABASE (SQLite)
# ════════════════════════════════════════════════════════════════════════
SCHEMA = """
CREATE TABLE IF NOT EXISTS donations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    amount     INTEGER NOT NULL,
    admin_id   INTEGER NOT NULL,
    note       TEXT,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_donations_user ON donations(user_id);

CREATE TABLE IF NOT EXISTS cases (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    type         TEXT NOT NULL,
    user_id      INTEGER NOT NULL,
    moderator_id INTEGER NOT NULL,
    reason       TEXT,
    active       INTEGER NOT NULL DEFAULT 1,
    created_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cases_user ON cases(user_id);

CREATE TABLE IF NOT EXISTS banned_words (word TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS daily_entries (
    day     TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    entries INTEGER NOT NULL,
    PRIMARY KEY (day, user_id)
);

CREATE TABLE IF NOT EXISTS daily_rewards (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    day               TEXT NOT NULL UNIQUE,
    winner_id         INTEGER NOT NULL,
    entries           INTEGER NOT NULL,
    total_entries     INTEGER NOT NULL,
    prize             TEXT NOT NULL,
    message_id        INTEGER,
    created_at        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rewards_message ON daily_rewards(message_id);
"""


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    # ---- helpers
    async def _one(self, sql: str, params: tuple = ()):
        async with self.conn.execute(sql, params) as cur:
            return await cur.fetchone()

    async def _all(self, sql: str, params: tuple = ()):
        async with self.conn.execute(sql, params) as cur:
            return await cur.fetchall()

    async def _exec(self, sql: str, params: tuple = ()) -> tuple[int, int]:
        cur = await self.conn.execute(sql, params)
        await self.conn.commit()
        result = (cur.lastrowid, cur.rowcount)
        await cur.close()
        return result

    # ---- key/value
    async def kv_get(self, key: str, default: str | None = None) -> str | None:
        row = await self._one("SELECT value FROM kv WHERE key = ?", (key,))
        return row["value"] if row else default

    async def kv_set(self, key: str, value: str) -> None:
        await self._exec(
            "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # ---- donations
    async def add_donation(self, user_id: int, amount: int, admin_id: int, note: str | None) -> tuple[int, int]:
        donation_id, _ = await self._exec(
            "INSERT INTO donations(user_id, amount, admin_id, note, created_at) VALUES(?, ?, ?, ?, ?)",
            (user_id, amount, admin_id, note, int(time.time())),
        )
        return donation_id, await self.user_total(user_id)

    async def get_donation(self, donation_id: int):
        return await self._one("SELECT * FROM donations WHERE id = ?", (donation_id,))

    async def remove_donation(self, donation_id: int) -> bool:
        _, count = await self._exec("DELETE FROM donations WHERE id = ?", (donation_id,))
        return count > 0

    async def user_total(self, user_id: int) -> int:
        row = await self._one("SELECT COALESCE(SUM(amount), 0) AS t FROM donations WHERE user_id = ?", (user_id,))
        return int(row["t"])

    async def user_rank(self, user_id: int) -> int | None:
        total = await self.user_total(user_id)
        if total <= 0:
            return None
        row = await self._one(
            "SELECT COUNT(*) + 1 AS r FROM (SELECT user_id FROM donations GROUP BY user_id HAVING SUM(amount) > ?)",
            (total,),
        )
        return int(row["r"])

    async def user_donations(self, user_id: int, limit: int = 10):
        return await self._all("SELECT * FROM donations WHERE user_id = ? ORDER BY id DESC LIMIT ?", (user_id, limit))

    async def top_donors(self, limit: int):
        return await self._all(
            "SELECT user_id, SUM(amount) AS total, COUNT(*) AS count "
            "FROM donations GROUP BY user_id ORDER BY total DESC, MIN(id) ASC LIMIT ?",
            (limit,),
        )

    async def donation_stats(self):
        return await self._one(
            "SELECT COALESCE(SUM(amount), 0) AS total, COUNT(DISTINCT user_id) AS donors, COUNT(*) AS n FROM donations"
        )

    # ---- moderation cases
    async def add_case(self, type_: str, user_id: int, moderator_id: int, reason: str | None) -> int:
        case_id, _ = await self._exec(
            "INSERT INTO cases(type, user_id, moderator_id, reason, created_at) VALUES(?, ?, ?, ?, ?)",
            (type_, user_id, moderator_id, reason, int(time.time())),
        )
        return case_id

    async def get_case(self, case_id: int):
        return await self._one("SELECT * FROM cases WHERE id = ?", (case_id,))

    async def user_cases(self, user_id: int, limit: int = 10):
        return await self._all("SELECT * FROM cases WHERE user_id = ? ORDER BY id DESC LIMIT ?", (user_id, limit))

    async def active_warnings(self, user_id: int):
        return await self._all(
            "SELECT * FROM cases WHERE user_id = ? AND type = 'warn' AND active = 1 ORDER BY id DESC", (user_id,)
        )

    async def clear_warnings(self, user_id: int) -> int:
        _, count = await self._exec(
            "UPDATE cases SET active = 0 WHERE user_id = ? AND type = 'warn' AND active = 1", (user_id,)
        )
        return count

    # ---- automod words
    async def list_words(self) -> list[str]:
        return [r["word"] for r in await self._all("SELECT word FROM banned_words ORDER BY word")]

    async def add_word(self, word: str) -> bool:
        _, count = await self._exec("INSERT OR IGNORE INTO banned_words(word) VALUES(?)", (word,))
        return count > 0

    async def remove_word(self, word: str) -> bool:
        _, count = await self._exec("DELETE FROM banned_words WHERE word = ?", (word,))
        return count > 0

    # ---- daily chat rewards
    async def add_entries(self, rows: list[tuple[str, int, int]]) -> None:
        """rows = [(day, user_id, entries_to_add), ...] written in one transaction."""
        await self.conn.executemany(
            "INSERT INTO daily_entries(day, user_id, entries) VALUES(?, ?, ?) "
            "ON CONFLICT(day, user_id) DO UPDATE SET entries = daily_entries.entries + excluded.entries",
            rows,
        )
        await self.conn.commit()

    async def day_entries(self, day: str):
        return await self._all(
            "SELECT user_id, entries FROM daily_entries WHERE day = ? ORDER BY entries DESC, user_id ASC", (day,)
        )

    async def user_entries(self, day: str, user_id: int) -> int:
        row = await self._one("SELECT entries FROM daily_entries WHERE day = ? AND user_id = ?", (day, user_id))
        return int(row["entries"]) if row else 0

    async def prune_entries(self, before_day: str) -> None:
        await self._exec("DELETE FROM daily_entries WHERE day < ?", (before_day,))

    async def create_reward(self, day: str, winner_id: int, entries: int, total: int, prize: str) -> int | None:
        """Claims the day. Returns the reward id, or None if that day was already drawn."""
        reward_id, count = await self._exec(
            "INSERT OR IGNORE INTO daily_rewards(day, winner_id, entries, total_entries, prize, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (day, winner_id, entries, total, prize, int(time.time())),
        )
        return reward_id if count > 0 else None

    async def delete_reward(self, reward_id: int) -> None:
        await self._exec("DELETE FROM daily_rewards WHERE id = ?", (reward_id,))

    async def set_reward_message(self, reward_id: int, message_id: int) -> None:
        await self._exec("UPDATE daily_rewards SET message_id = ? WHERE id = ?", (message_id, reward_id))

    async def get_reward_by_day(self, day: str):
        return await self._one("SELECT * FROM daily_rewards WHERE day = ?", (day,))

    async def get_reward_by_message(self, message_id: int):
        return await self._one("SELECT * FROM daily_rewards WHERE message_id = ?", (message_id,))

    async def last_reward(self):
        return await self._one("SELECT * FROM daily_rewards ORDER BY day DESC LIMIT 1")


# ════════════════════════════════════════════════════════════════════════
#  PERMISSION CHECKS
# ════════════════════════════════════════════════════════════════════════
def _has_role(member: discord.Member, role_ids: tuple[int, ...]) -> bool:
    return any(role.id in role_ids for role in member.roles)


def is_moderator(member: discord.abc.User, role_ids: tuple[int, ...]) -> bool:
    return isinstance(member, discord.Member) and (
        member.guild_permissions.administrator or _has_role(member, role_ids)
    )


def donation_admin():
    async def predicate(interaction: discord.Interaction) -> bool:
        cfg = interaction.client.cfg  # type: ignore[attr-defined]
        user = interaction.user
        if isinstance(user, discord.Member) and (
            user.guild_permissions.administrator or _has_role(user, cfg.donation_admin_role_ids)
        ):
            return True
        raise app_commands.CheckFailure("🚫 Only donation admins can use this command.")

    return app_commands.check(predicate)


def moderator():
    async def predicate(interaction: discord.Interaction) -> bool:
        cfg = interaction.client.cfg  # type: ignore[attr-defined]
        if is_moderator(interaction.user, cfg.mod_role_ids):
            return True
        raise app_commands.CheckFailure("🚫 Only moderators can use this command.")

    return app_commands.check(predicate)


# ════════════════════════════════════════════════════════════════════════
#  LOGGER  (clean embeds -> log channels)
# ════════════════════════════════════════════════════════════════════════
# action key -> (emoji, label, color)
ACTIONS: dict[str, tuple[str, str, int]] = {
    "warn": ("⚠️", "Warn", C.WARN),
    "clearwarns": ("🧽", "Warnings Cleared", C.SUCCESS),
    "timeout": ("🔇", "Timeout", C.WARN),
    "untimeout": ("🔊", "Timeout Removed", C.SUCCESS),
    "kick": ("👢", "Kick", C.AUTOMOD),
    "ban": ("🔨", "Ban", C.ERROR),
    "unban": ("🔓", "Unban", C.SUCCESS),
    "automod": ("🛡️", "AutoMod", C.AUTOMOD),
    "purge": ("🧹", "Messages Purged", C.INFO),
    "slowmode": ("🐢", "Slowmode Changed", C.INFO),
    "lock": ("🔒", "Channel Locked", C.WARN),
    "unlock": ("🔓", "Channel Unlocked", C.SUCCESS),
    "donation_add": ("💎", "Donation Added", C.GEM),
    "donation_remove": ("🗑️", "Donation Removed", C.ERROR),
    "verify_pass": ("✅", "Member Verified", C.SUCCESS),
    "verify_lock": ("🚫", "Verification Lockout", C.ERROR),
    "verify_manual": ("🛡️", "Manually Verified", C.SUCCESS),
    "verify_revoke": ("↩️", "Verification Revoked", C.WARN),
    "daily_draw": ("🎉", "Daily Chat Reward Drawn", C.GEM),
}


class ModLogger:
    def __init__(self, bot: "NightFrostBot") -> None:
        self.bot = bot

    async def _channel(self, channel_id: int):
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.HTTPException:
                log.warning("Could not find log channel %s", channel_id)
                return None
        return channel

    async def send(self, channel_id: int, embed: discord.Embed) -> None:
        channel = await self._channel(channel_id)
        if channel is None:
            return
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            log.warning("Could not send log message to channel %s", channel_id)

    async def log_case(
        self,
        action: str,
        target: discord.abc.User,
        moderator: discord.abc.User,
        reason: str,
        *,
        fields: Iterable[tuple[str, str, bool]] = (),
        channel_id: int | None = None,
    ) -> int:
        """Store a numbered case in the database and post it to the log channel."""
        case_id = await self.bot.db.add_case(action, target.id, moderator.id, reason)
        emoji, label, color = ACTIONS[action]
        embed = make_embed(
            f"{emoji} {label} • Case #{case_id}",
            color=color,
            fields=[
                ("👤 User", f"{target.mention}\n`{target.id}`", True),
                ("🛡️ Moderator", f"{moderator.mention}\n`{moderator.id}`", True),
                ("📝 Reason", reason, False),
                *fields,
            ],
            thumbnail=target.display_avatar.url,
            footer=f"User ID: {target.id}",
        )
        await self.send(channel_id or self.bot.cfg.mod_log_channel_id, embed)
        return case_id

    async def log_event(
        self,
        action: str,
        moderator: discord.abc.User,
        *,
        fields: Iterable[tuple[str, str, bool]] = (),
        description: str | None = None,
        channel_id: int | None = None,
    ) -> None:
        """Log an action that isn't tied to a single user (purge, lock, donations...)."""
        emoji, label, color = ACTIONS[action]
        embed = make_embed(
            f"{emoji} {label}",
            description,
            color,
            fields=[("🛡️ By", f"{moderator.mention}\n`{moderator.id}`", True), *fields],
            thumbnail=moderator.display_avatar.url,
        )
        await self.send(channel_id or self.bot.cfg.mod_log_channel_id, embed)

    async def log_verify(
        self,
        action: str,
        member: discord.abc.User,
        *,
        fields: Iterable[tuple[str, str, bool]] = (),
    ) -> None:
        """Verification events (not moderation cases)."""
        emoji, label, color = ACTIONS[action]
        embed = make_embed(
            f"{emoji} {label}",
            color=color,
            fields=[("👤 Member", f"{member.mention}\n`{member.id}`", True), *fields],
            thumbnail=member.display_avatar.url,
            footer=f"User ID: {member.id}",
        )
        await self.send(self.bot.cfg.verify_log_channel_id or self.bot.cfg.mod_log_channel_id, embed)

    async def log_member_event(
        self,
        action: str,
        member: discord.abc.User,
        *,
        fields: Iterable[tuple[str, str, bool]] = (),
    ) -> None:
        """Generic member-centred event (daily rewards) -> mod log."""
        emoji, label, color = ACTIONS[action]
        embed = make_embed(
            f"{emoji} {label}",
            color=color,
            fields=[("👤 Member", f"{member.mention}\n`{member.id}`", True), *fields],
            thumbnail=member.display_avatar.url,
            footer=f"User ID: {member.id}",
        )
        await self.send(self.bot.cfg.mod_log_channel_id, embed)


# ════════════════════════════════════════════════════════════════════════
#  CAPTCHA ENGINE  (image rendering + attempt tracking, no Discord code)
# ════════════════════════════════════════════════════════════════════════
CAPTCHA_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I confusion
CAPTCHA_LENGTH = 5
CAPTCHA_TTL = 180        # seconds a code stays valid
CAPTCHA_MAX_ATTEMPTS = 3  # wrong answers before a lockout
CAPTCHA_LOCKOUT = 300    # seconds locked out after too many wrong answers

_FONT_CANDIDATES = (
    "DejaVuSans-Bold.ttf", "arialbd.ttf", "Arial Bold.ttf", "LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
)


def _load_font(size: int):
    for name in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def render_captcha(code: str) -> io.BytesIO:
    """Draw `code` as a noisy, slightly rotated PNG that's easy for humans, annoying for OCR."""
    width, height = 300, 110
    img = Image.new("RGB", (width, height), tuple(random.randint(228, 250) for _ in range(3)))
    draw = ImageDraw.Draw(img)

    for _ in range(9):  # background lines
        draw.line(
            [(random.randint(0, width), random.randint(0, height)), (random.randint(0, width), random.randint(0, height))],
            fill=tuple(random.randint(150, 215) for _ in range(3)), width=random.randint(1, 3),
        )

    font = _load_font(56)
    step = (width - 50) // len(code)
    for i, ch in enumerate(code):
        layer = Image.new("RGBA", (90, 96), (0, 0, 0, 0))
        ImageDraw.Draw(layer).text((18, 8), ch, font=font, fill=(*(random.randint(20, 120) for _ in range(3)), 255))
        layer = layer.rotate(random.randint(-28, 28), resample=Image.Resampling.BICUBIC, expand=True)
        img.paste(layer, (14 + i * step + random.randint(-3, 3), random.randint(0, 14)), layer)

    for _ in range(4):  # strike-through lines over the text
        y = random.randint(25, height - 25)
        draw.line([(0, y + random.randint(-15, 15)), (width, y + random.randint(-15, 15))],
                  fill=tuple(random.randint(60, 140) for _ in range(3)), width=2)
    for _ in range(350):  # speckles
        draw.point((random.randint(0, width - 1), random.randint(0, height - 1)),
                   fill=tuple(random.randint(80, 200) for _ in range(3)))

    img = img.filter(ImageFilter.SMOOTH)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    buf.seek(0)
    return buf


@dataclass
class Challenge:
    code: str
    expires: float
    attempts: int = 0


class CaptchaManager:
    """Tracks pending codes, wrong attempts and lockouts per user (in memory)."""

    def __init__(self) -> None:
        self.pending: dict[int, Challenge] = {}
        self.locked_until: dict[int, float] = {}

    def lock_remaining(self, user_id: int) -> int:
        until = self.locked_until.get(user_id, 0.0)
        remaining = int(until - time.monotonic())
        if remaining <= 0:
            self.locked_until.pop(user_id, None)
            return 0
        return remaining

    def issue(self, user_id: int) -> str:
        """New code for the user. Wrong-attempt count carries over so 'New image' can't be used to farm guesses."""
        old = self.pending.get(user_id)
        code = "".join(secrets.choice(CAPTCHA_CHARS) for _ in range(CAPTCHA_LENGTH))
        self.pending[user_id] = Challenge(code, time.monotonic() + CAPTCHA_TTL, old.attempts if old else 0)
        return code

    def check(self, user_id: int, answer: str) -> tuple[str, int]:
        """Returns (status, attempts_left). status: ok | wrong | locked_now | locked | expired | missing"""
        if self.lock_remaining(user_id) > 0:
            return "locked", 0
        challenge = self.pending.get(user_id)
        if challenge is None:
            return "missing", 0
        if time.monotonic() > challenge.expires:
            del self.pending[user_id]
            return "expired", 0
        if secrets.compare_digest(answer.strip().upper(), challenge.code):
            del self.pending[user_id]
            return "ok", 0
        challenge.attempts += 1
        if challenge.attempts >= CAPTCHA_MAX_ATTEMPTS:
            del self.pending[user_id]
            self.locked_until[user_id] = time.monotonic() + CAPTCHA_LOCKOUT
            return "locked_now", 0
        return "wrong", CAPTCHA_MAX_ATTEMPTS - challenge.attempts


# ════════════════════════════════════════════════════════════════════════
#  BOT
# ════════════════════════════════════════════════════════════════════════
class NightFrostTree(app_commands.CommandTree):
    async def on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.CheckFailure):
            message = str(error) or "🚫 You can't use this command."
        elif isinstance(error, app_commands.CommandInvokeError):
            name = interaction.command.qualified_name if interaction.command else "?"
            log.error("Command error in /%s", name, exc_info=error.original)
            message = "Something went wrong while running that command. The error was logged."
        else:
            message = str(error)

        embed = error_embed(message)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except discord.HTTPException:
            pass


class NightFrostBot(commands.Bot):
    def __init__(self, cfg: Config) -> None:
        intents = discord.Intents.default()
        intents.message_content = True  # needed for automod
        super().__init__(
            command_prefix=commands.when_mentioned, intents=intents, help_command=None, tree_cls=NightFrostTree
        )
        self.cfg = cfg
        self.db = Database(cfg.database_path)
        self.modlog = ModLogger(self)

    async def setup_hook(self) -> None:
        await self.db.connect()
        for cog in (Donations(self), Moderation(self), AutoMod(self), Verification(self), DailyRewards(self)):
            await self.add_cog(cog)
            log.info("Loaded %s", type(cog).__name__)

        if self.cfg.guild_id:
            guild = discord.Object(id=self.cfg.guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
        else:
            synced = await self.tree.sync()
        log.info("Synced %d command group(s)/command(s)", len(synced))

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s)", self.user, self.user.id)
        await self.change_presence(
            activity=discord.Activity(type=discord.ActivityType.watching, name="💎 /leaderboard")
        )

    async def close(self) -> None:
        await self.db.close()
        await super().close()


# ════════════════════════════════════════════════════════════════════════
#  💎 DONATIONS
# ════════════════════════════════════════════════════════════════════════
MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}
KV_LEADERBOARD_MESSAGE = "leaderboard_message_id"
KV_TOP_DONORS = "top_donor_ids"
TOP_DONOR_COUNT = 5  # how many top donors get TOP_DONOR_ROLE_ID


class Donations(commands.Cog):
    """💎 Gem donation tracking, announcements and the live leaderboard."""

    donation = app_commands.Group(name="donation", description="💎 Gem donation system", guild_only=True)

    def __init__(self, bot: NightFrostBot) -> None:
        self.bot = bot
        self._lock = asyncio.Lock()
        self._role_lock = asyncio.Lock()

    # ---- leaderboard
    async def build_leaderboard_embed(self) -> discord.Embed:
        rows = await self.bot.db.top_donors(self.bot.cfg.leaderboard_size)
        stats = await self.bot.db.donation_stats()

        if not rows:
            description = "No donations yet. Be the first to donate! 💜"
        else:
            lines = []
            for i, row in enumerate(rows, 1):
                prefix = MEDALS.get(i, f"`#{i}`")
                lines.append(f"{prefix} <@{row['user_id']}> — **{format_amount(row['total'])}** 💎")
            description = "\n".join(lines)

        return make_embed(
            "🏆 Gem Donation Leaderboard",
            description,
            C.GEM,
            fields=[
                ("💰 Total Donated", f"**{format_amount(stats['total'])}** 💎", True),
                ("👥 Donors", f"**{stats['donors']}**", True),
                ("🎁 Donations", f"**{stats['n']}**", True),
            ],
            footer="🔄 Updates automatically • Last update",
        )

    async def refresh_leaderboard(self) -> None:
        """Edit the leaderboard message in place (or post a new one if it's missing)."""
        async with self._lock:
            channel = self.bot.get_channel(self.bot.cfg.leaderboard_channel_id)
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(self.bot.cfg.leaderboard_channel_id)
                except discord.HTTPException:
                    log.warning("Leaderboard channel not found")
                    return

            embed = await self.build_leaderboard_embed()
            message_id = await self.bot.db.kv_get(KV_LEADERBOARD_MESSAGE)

            if message_id:
                try:
                    message = await channel.fetch_message(int(message_id))
                    await message.edit(embed=embed)
                    return
                except discord.NotFound:
                    pass  # deleted -> post a fresh one below
                except discord.HTTPException:
                    log.exception("Could not edit the leaderboard message")
                    return

            try:
                message = await channel.send(embed=embed)
                await self.bot.db.kv_set(KV_LEADERBOARD_MESSAGE, str(message.id))
            except discord.HTTPException:
                log.exception("Could not post the leaderboard message")

    # ---- top-5 donor role
    def _guild(self) -> discord.Guild | None:
        cfg = self.bot.cfg
        if cfg.guild_id:
            return self.bot.get_guild(cfg.guild_id)
        channel = self.bot.get_channel(cfg.leaderboard_channel_id)
        return getattr(channel, "guild", None)

    async def sync_top_role(self) -> None:
        """Give the current top-5 donors TOP_DONOR_ROLE_ID and take it away from everyone else."""
        role_id = self.bot.cfg.top_donor_role_id
        if not role_id:
            return
        async with self._role_lock:
            guild = self._guild()
            role = guild.get_role(role_id) if guild else None
            if role is None:
                log.warning("Top donor role %s not found - check TOP_DONOR_ROLE_ID and that the bot is in the server", role_id)
                return

            top = {row["user_id"] for row in await self.bot.db.top_donors(TOP_DONOR_COUNT)}
            try:
                previous = set(json.loads(await self.bot.db.kv_get(KV_TOP_DONORS, "[]")))
            except ValueError:
                previous = set()
            holders = {m.id for m in role.members}  # anyone we can currently see holding the role

            async def member_of(user_id: int) -> discord.Member | None:
                member = guild.get_member(user_id)
                if member is None:
                    try:
                        member = await guild.fetch_member(user_id)
                    except discord.HTTPException:
                        return None  # left the server / unreachable
                return member

            for user_id in (previous | holders) - top:
                member = await member_of(user_id)
                if member and role in member.roles:
                    try:
                        await member.remove_roles(role, reason="No longer a top-5 donor")
                    except discord.HTTPException:
                        log.warning("Couldn't remove the top donor role from %s (check my role position)", user_id)
            for user_id in top:
                member = await member_of(user_id)
                if member and role not in member.roles:
                    try:
                        await member.add_roles(role, reason="Top-5 donor")
                    except discord.HTTPException:
                        log.warning("Couldn't give the top donor role to %s (check my role position)", user_id)
            await self.bot.db.kv_set(KV_TOP_DONORS, json.dumps(sorted(top)))

    # ---- donation embeds / DM
    @staticmethod
    def donation_embed(
        user: discord.abc.User, value: int, total: int, rank: int | None, note: str | None,
        donation_id: int, *, dm_guild: str | None = None,
    ) -> discord.Embed:
        """The 'New Donation' embed. With dm_guild set it becomes the thank-you version sent to the donor."""
        if dm_guild:
            title = "💎 Thank you for your donation!"
            description = (
                f"Hey **{user.display_name}**! Your donation of **{format_amount(value)}** gems to "
                f"**{dm_guild}** has been recorded.\nThank you for supporting the community 💜"
            )
            if rank and rank <= TOP_DONOR_COUNT:
                description += f"\n\n🏅 You're currently one of our **Top {TOP_DONOR_COUNT} donors**!"
        else:
            title = "💎 New Donation!"
            description = (
                f"{user.mention} just donated **{format_amount(value)}** gems!\n"
                "Thank you for supporting the community 💜"
            )
        return make_embed(
            title,
            description,
            C.GEM,
            fields=[
                ("💰 Amount", f"**{format_amount(value)}**\n`{value:,}`", True),
                ("📊 Total Donated", f"**{format_amount(total)}**", True),
                ("🏆 Rank", f"**#{rank}**", True),
                *([("📝 Note", note, False)] if note else []),
            ],
            thumbnail=user.display_avatar.url,
            footer=f"Donation #{donation_id}",
        )

    async def _dm_donor(self, user: discord.abc.User, guild_name: str, value: int, total: int,
                        rank: int | None, note: str | None, donation_id: int) -> bool:
        embed = self.donation_embed(user, value, total, rank, note, donation_id, dm_guild=guild_name)
        try:
            await user.send(embed=embed)
            return True
        except discord.HTTPException:  # DMs closed / user blocked the bot
            return False

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        await self.refresh_leaderboard()
        await self.sync_top_role()

    # ---- commands
    @app_commands.command(name="leaderboard", description="🏆 Show the gem donation leaderboard")
    @app_commands.guild_only()
    async def leaderboard(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(embed=await self.build_leaderboard_embed())

    @donation.command(name="add", description="💎 Record a gem donation (admins only)")
    @app_commands.describe(
        user="The person who donated",
        amount="Amount with suffix: 500k, 1.5m, 2b, 1t",
        note="Optional note (e.g. what was traded)",
    )
    @donation_admin()
    async def add(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        amount: str,
        note: app_commands.Range[str, 1, 200] | None = None,
    ) -> None:
        if user.bot:
            await respond(interaction, error_embed("Bots can't donate."), ephemeral=True)
            return
        try:
            value = parse_amount(amount)
        except AmountError as exc:
            await respond(interaction, error_embed(str(exc)), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        donation_id, total = await self.bot.db.add_donation(user.id, value, interaction.user.id, note)
        rank = await self.bot.db.user_rank(user.id)

        # 1) Announce in the donation channel
        announce = self.donation_embed(user, value, total, rank, note, donation_id)
        await self.bot.modlog.send(self.bot.cfg.donation_channel_id, announce)

        # 2) Thank the donor by DM
        dm_sent = await self._dm_donor(user, interaction.guild.name, value, total, rank, note, donation_id)

        # 3) Update the live leaderboard and the top-5 role
        await self.refresh_leaderboard()
        await self.sync_top_role()

        # 4) Admin log
        await self.bot.modlog.log_event(
            "donation_add",
            interaction.user,
            fields=[
                ("👤 Donor", f"{user.mention}\n`{user.id}`", True),
                ("💰 Amount", f"**{format_amount(value)}** (`{value:,}`)", True),
                ("📊 New Total", f"**{format_amount(total)}**", True),
                ("📨 Thank-you DM", "✅ Sent" if dm_sent else "❌ DMs closed", True),
                *([("📝 Note", note, False)] if note else []),
            ],
        )

        await interaction.followup.send(
            embed=success_embed(
                f"Added **{format_amount(value)}** 💎 for {user.mention} (donation `#{donation_id}`).\n"
                f"Their total is now **{format_amount(total)}**.\n"
                f"📨 Thank-you DM: {'sent ✅' if dm_sent else 'not delivered (their DMs are closed) ❌'}"
            ),
            ephemeral=True,
        )

    @donation.command(name="remove", description="🗑️ Remove a donation by its ID (admins only)")
    @app_commands.describe(donation_id="The donation number shown in the announcement footer")
    @donation_admin()
    async def remove(self, interaction: discord.Interaction, donation_id: int) -> None:
        row = await self.bot.db.get_donation(donation_id)
        if row is None:
            await respond(interaction, error_embed(f"No donation with ID `#{donation_id}`."), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        await self.bot.db.remove_donation(donation_id)
        await self.refresh_leaderboard()
        await self.sync_top_role()

        await self.bot.modlog.log_event(
            "donation_remove",
            interaction.user,
            fields=[
                ("🆔 Donation", f"`#{donation_id}`", True),
                ("👤 Donor", f"<@{row['user_id']}>\n`{row['user_id']}`", True),
                ("💰 Amount", f"**{format_amount(row['amount'])}** (`{row['amount']:,}`)", True),
            ],
        )
        await interaction.followup.send(
            embed=success_embed(f"Removed donation `#{donation_id}` ({format_amount(row['amount'])} 💎)."),
            ephemeral=True,
        )

    @donation.command(name="history", description="📜 Show a user's recent donations (admins only)")
    @app_commands.describe(user="The donor to look up")
    @donation_admin()
    async def history(self, interaction: discord.Interaction, user: discord.User) -> None:
        rows = await self.bot.db.user_donations(user.id, 10)
        if not rows:
            await respond(interaction, error_embed(f"{user.mention} has no donations."), ephemeral=True)
            return
        lines = [
            f"`#{r['id']}` • **{format_amount(r['amount'])}** 💎 • <t:{r['created_at']}:R> • by <@{r['admin_id']}>"
            + (f"\n　↳ _{r['note']}_" if r["note"] else "")
            for r in rows
        ]
        embed = make_embed(
            f"📜 Donation history • {user.display_name}",
            "\n".join(lines),
            C.GEM,
            thumbnail=user.display_avatar.url,
            footer="Showing the latest 10",
        )
        await respond(interaction, embed, ephemeral=True)

    @donation.command(name="stats", description="📊 Show a user's donation total and rank")
    @app_commands.describe(user="Defaults to yourself")
    async def stats(self, interaction: discord.Interaction, user: discord.User | None = None) -> None:
        user = user or interaction.user
        total = await self.bot.db.user_total(user.id)
        rank = await self.bot.db.user_rank(user.id)
        embed = make_embed(
            f"📊 Donation stats • {user.display_name}",
            color=C.GEM,
            fields=[
                ("💰 Total Donated", f"**{format_amount(total)}** 💎", True),
                ("🏆 Rank", f"**#{rank}**" if rank else "—", True),
            ],
            thumbnail=user.display_avatar.url,
        )
        await respond(interaction, embed)

    @donation.command(name="refresh", description="🔄 Force the leaderboard to update (admins only)")
    @donation_admin()
    async def refresh(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await self.refresh_leaderboard()
        await self.sync_top_role()
        await interaction.followup.send(embed=success_embed("Leaderboard refreshed."), ephemeral=True)


# ════════════════════════════════════════════════════════════════════════
#  🛡️ MODERATION
# ════════════════════════════════════════════════════════════════════════
DEFAULT_REASON = "No reason provided"
Reason = app_commands.Range[str, 1, 400]


class Moderation(commands.Cog):
    """🛡️ Moderation commands with case logging."""

    mod = app_commands.Group(name="mod", description="🛡️ Moderation tools", guild_only=True)

    def __init__(self, bot: NightFrostBot) -> None:
        self.bot = bot

    # ---- helpers
    async def _can_act(self, interaction: discord.Interaction, target: discord.Member) -> bool:
        guild = interaction.guild
        author = interaction.user
        problem = None
        if target.id == author.id:
            problem = "You can't do that to yourself."
        elif target.id == self.bot.user.id:
            problem = "I can't do that to myself."
        elif target.id == guild.owner_id:
            problem = "You can't do that to the server owner."
        elif author.id != guild.owner_id and target.top_role >= author.top_role:
            problem = "That member's highest role is equal to or above yours."
        elif target.top_role >= guild.me.top_role:
            problem = "That member's highest role is above mine, so I can't act on them."
        if problem:
            await respond(interaction, error_embed(problem), ephemeral=True)
            return False
        return True

    async def _dm(self, user: discord.abc.User, guild: discord.Guild, title: str, reason: str, color: int, extra: str = "") -> bool:
        embed = make_embed(title, f"**Server:** {guild.name}\n**Reason:** {reason}{extra}", color)
        try:
            await user.send(embed=embed)
            return True
        except discord.HTTPException:
            return False

    @staticmethod
    def _audit(interaction: discord.Interaction, reason: str) -> str:
        return f"{reason} | by {interaction.user} ({interaction.user.id})"[:512]

    async def _fail(self, interaction: discord.Interaction, exc: discord.HTTPException) -> None:
        if isinstance(exc, discord.Forbidden):
            msg = "I don't have permission to do that. Check my role position and permissions."
        else:
            msg = f"Discord rejected the request: {exc.text or exc}"
        await respond(interaction, error_embed(msg), ephemeral=True)

    # ---- warnings
    @mod.command(name="warn", description="⚠️ Warn a member")
    @app_commands.describe(member="Who to warn", reason="Why they're being warned")
    @moderator()
    async def warn(self, interaction: discord.Interaction, member: discord.Member, reason: Reason = DEFAULT_REASON) -> None:
        if not await self._can_act(interaction, member):
            return
        await interaction.response.defer()
        case_id = await self.bot.modlog.log_case("warn", member, interaction.user, reason)
        count = len(await self.bot.db.active_warnings(member.id))
        dmed = await self._dm(member, interaction.guild, "⚠️ You were warned", reason, C.WARN, f"\n**Active warnings:** {count}")
        embed = make_embed(
            f"⚠️ Member Warned • Case #{case_id}",
            f"{member.mention} has been warned.",
            C.WARN,
            fields=[
                ("📝 Reason", reason, False),
                ("📊 Active Warnings", f"**{count}**", True),
                ("📨 DM Sent", "✅" if dmed else "❌", True),
            ],
            thumbnail=member.display_avatar.url,
        )
        await interaction.followup.send(embed=embed)

    @mod.command(name="warnings", description="📋 List a user's active warnings")
    @app_commands.describe(user="The user to look up")
    @moderator()
    async def warnings(self, interaction: discord.Interaction, user: discord.User) -> None:
        rows = await self.bot.db.active_warnings(user.id)
        if not rows:
            await respond(interaction, make_embed("📋 Warnings", f"{user.mention} has no active warnings. ✨", C.SUCCESS), ephemeral=True)
            return
        lines = [f"`#{r['id']}` • <t:{r['created_at']}:R> • by <@{r['moderator_id']}>\n　↳ {r['reason']}" for r in rows[:10]]
        embed = make_embed(
            f"📋 Warnings • {user.display_name}",
            "\n".join(lines),
            C.WARN,
            thumbnail=user.display_avatar.url,
            footer=f"{len(rows)} active warning(s)",
        )
        await respond(interaction, embed, ephemeral=True)

    @mod.command(name="clearwarns", description="🧽 Clear all active warnings for a user")
    @app_commands.describe(user="The user to clear", reason="Why they're being cleared")
    @moderator()
    async def clearwarns(self, interaction: discord.Interaction, user: discord.User, reason: Reason = DEFAULT_REASON) -> None:
        await interaction.response.defer()
        cleared = await self.bot.db.clear_warnings(user.id)
        if cleared == 0:
            await interaction.followup.send(embed=error_embed(f"{user.mention} has no active warnings."), ephemeral=True)
            return
        case_id = await self.bot.modlog.log_case(
            "clearwarns", user, interaction.user, reason, fields=[("🧽 Cleared", f"**{cleared}** warning(s)", True)]
        )
        await interaction.followup.send(embed=success_embed(f"Cleared **{cleared}** warning(s) for {user.mention} (Case #{case_id})."))

    # ---- timeouts
    @mod.command(name="timeout", description="🔇 Timeout a member")
    @app_commands.describe(member="Who to timeout", duration="e.g. 10m, 2h, 1d, 1d12h (max 28d)", reason="Why")
    @moderator()
    async def timeout(self, interaction: discord.Interaction, member: discord.Member, duration: str, reason: Reason = DEFAULT_REASON) -> None:
        try:
            seconds = parse_duration(duration)
        except ValueError as exc:
            await respond(interaction, error_embed(str(exc)), ephemeral=True)
            return
        if not await self._can_act(interaction, member):
            return
        await interaction.response.defer()
        until = discord.utils.utcnow() + dt.timedelta(seconds=seconds)
        try:
            await member.timeout(until, reason=self._audit(interaction, reason))
        except discord.HTTPException as exc:
            await self._fail(interaction, exc)
            return
        pretty = format_duration(seconds)
        expires = f"<t:{int(until.timestamp())}:R>"
        case_id = await self.bot.modlog.log_case(
            "timeout", member, interaction.user, reason,
            fields=[("⏱️ Duration", f"**{pretty}**", True), ("🕒 Expires", expires, True)],
        )
        dmed = await self._dm(member, interaction.guild, "🔇 You were timed out", reason, C.WARN, f"\n**Duration:** {pretty}")
        embed = make_embed(
            f"🔇 Member Timed Out • Case #{case_id}",
            f"{member.mention} was timed out for **{pretty}**.",
            C.WARN,
            fields=[("📝 Reason", reason, False), ("📨 DM Sent", "✅" if dmed else "❌", True)],
            thumbnail=member.display_avatar.url,
        )
        await interaction.followup.send(embed=embed)

    @mod.command(name="untimeout", description="🔊 Remove a member's timeout")
    @app_commands.describe(member="Who to un-timeout", reason="Why")
    @moderator()
    async def untimeout(self, interaction: discord.Interaction, member: discord.Member, reason: Reason = DEFAULT_REASON) -> None:
        if not member.is_timed_out():
            await respond(interaction, error_embed(f"{member.mention} isn't timed out."), ephemeral=True)
            return
        await interaction.response.defer()
        try:
            await member.timeout(None, reason=self._audit(interaction, reason))
        except discord.HTTPException as exc:
            await self._fail(interaction, exc)
            return
        case_id = await self.bot.modlog.log_case("untimeout", member, interaction.user, reason)
        await interaction.followup.send(embed=success_embed(f"Removed the timeout from {member.mention} (Case #{case_id})."))

    # ---- kick / ban / unban
    @mod.command(name="kick", description="👢 Kick a member")
    @app_commands.describe(member="Who to kick", reason="Why")
    @moderator()
    async def kick(self, interaction: discord.Interaction, member: discord.Member, reason: Reason = DEFAULT_REASON) -> None:
        if not await self._can_act(interaction, member):
            return
        await interaction.response.defer()
        dmed = await self._dm(member, interaction.guild, "👢 You were kicked", reason, C.AUTOMOD)
        try:
            await member.kick(reason=self._audit(interaction, reason))
        except discord.HTTPException as exc:
            await self._fail(interaction, exc)
            return
        case_id = await self.bot.modlog.log_case("kick", member, interaction.user, reason)
        embed = make_embed(
            f"👢 Member Kicked • Case #{case_id}",
            f"**{member}** was kicked.",
            C.AUTOMOD,
            fields=[("📝 Reason", reason, False), ("📨 DM Sent", "✅" if dmed else "❌", True)],
            thumbnail=member.display_avatar.url,
        )
        await interaction.followup.send(embed=embed)

    @mod.command(name="ban", description="🔨 Ban a member")
    @app_commands.describe(member="Who to ban", reason="Why", delete_days="Days of their messages to delete (0-7)")
    @moderator()
    async def ban(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: Reason = DEFAULT_REASON,
        delete_days: app_commands.Range[int, 0, 7] = 0,
    ) -> None:
        if not await self._can_act(interaction, member):
            return
        await interaction.response.defer()
        dmed = await self._dm(member, interaction.guild, "🔨 You were banned", reason, C.ERROR)
        try:
            await interaction.guild.ban(
                member, reason=self._audit(interaction, reason), delete_message_seconds=delete_days * 86400
            )
        except discord.HTTPException as exc:
            await self._fail(interaction, exc)
            return
        case_id = await self.bot.modlog.log_case(
            "ban", member, interaction.user, reason,
            fields=[("🗑️ Messages Deleted", f"{delete_days} day(s)", True)],
        )
        embed = make_embed(
            f"🔨 Member Banned • Case #{case_id}",
            f"**{member}** was banned.",
            C.ERROR,
            fields=[("📝 Reason", reason, False), ("📨 DM Sent", "✅" if dmed else "❌", True)],
            thumbnail=member.display_avatar.url,
        )
        await interaction.followup.send(embed=embed)

    @mod.command(name="unban", description="🔓 Unban a user by ID")
    @app_commands.describe(user_id="The user's Discord ID", reason="Why")
    @moderator()
    async def unban(self, interaction: discord.Interaction, user_id: str, reason: Reason = DEFAULT_REASON) -> None:
        if not user_id.strip().isdigit():
            await respond(interaction, error_embed("Please provide a numeric user ID."), ephemeral=True)
            return
        await interaction.response.defer()
        uid = int(user_id.strip())
        try:
            await interaction.guild.unban(discord.Object(id=uid), reason=self._audit(interaction, reason))
        except discord.NotFound:
            await interaction.followup.send(embed=error_embed("That user isn't banned."), ephemeral=True)
            return
        except discord.HTTPException as exc:
            await self._fail(interaction, exc)
            return
        try:
            user = await self.bot.fetch_user(uid)
        except discord.HTTPException:
            await interaction.followup.send(embed=success_embed(f"Unbanned `{uid}`."))
            return
        case_id = await self.bot.modlog.log_case("unban", user, interaction.user, reason)
        await interaction.followup.send(embed=success_embed(f"Unbanned **{user}** (Case #{case_id})."))

    # ---- history
    @mod.command(name="history", description="📚 Show a user's moderation history")
    @app_commands.describe(user="The user to look up")
    @moderator()
    async def history(self, interaction: discord.Interaction, user: discord.User) -> None:
        rows = await self.bot.db.user_cases(user.id, 10)
        if not rows:
            await respond(interaction, make_embed("📚 History", f"{user.mention} has a clean record. ✨", C.SUCCESS), ephemeral=True)
            return
        lines = [f"`#{r['id']}` • **{r['type']}** • <t:{r['created_at']}:R>\n　↳ {r['reason']}" for r in rows]
        embed = make_embed(
            f"📚 Moderation history • {user.display_name}",
            "\n".join(lines),
            C.INFO,
            thumbnail=user.display_avatar.url,
            footer="Showing the latest 10 cases",
        )
        await respond(interaction, embed, ephemeral=True)

    @mod.command(name="case", description="🔎 Look up a case by number")
    @app_commands.describe(case_id="The case number")
    @moderator()
    async def case(self, interaction: discord.Interaction, case_id: int) -> None:
        row = await self.bot.db.get_case(case_id)
        if row is None:
            await respond(interaction, error_embed(f"No case `#{case_id}`."), ephemeral=True)
            return
        embed = make_embed(
            f"🔎 Case #{row['id']} • {row['type']}",
            color=C.INFO,
            fields=[
                ("👤 User", f"<@{row['user_id']}>\n`{row['user_id']}`", True),
                ("🛡️ Moderator", f"<@{row['moderator_id']}>", True),
                ("🕒 When", f"<t:{row['created_at']}:F>", True),
                ("📝 Reason", row["reason"] or DEFAULT_REASON, False),
            ],
        )
        await respond(interaction, embed, ephemeral=True)

    # ---- channel tools
    @mod.command(name="purge", description="🧹 Bulk delete recent messages")
    @app_commands.describe(amount="How many messages to check (1-100)", member="Only delete this member's messages")
    @moderator()
    async def purge(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 1, 100],
        member: discord.Member | None = None,
    ) -> None:
        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread, discord.VoiceChannel)):
            await respond(interaction, error_embed("I can't purge here."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            options = {}
            if member:  # only pass `check` when filtering - discord.py breaks if it's given as None
                options["check"] = lambda m: m.author.id == member.id
            deleted = await channel.purge(limit=amount, reason=self._audit(interaction, "Purge"), **options)
        except discord.HTTPException as exc:
            await self._fail(interaction, exc)
            return
        fields = [("💬 Channel", channel.mention, True), ("🧹 Deleted", f"**{len(deleted)}** message(s)", True)]
        if member:
            fields.append(("👤 Filter", member.mention, True))
        await self.bot.modlog.log_event("purge", interaction.user, fields=fields)
        await interaction.followup.send(embed=success_embed(f"Deleted **{len(deleted)}** message(s)."), ephemeral=True)

    @mod.command(name="slowmode", description="🐢 Set slowmode for a channel")
    @app_commands.describe(seconds="Seconds between messages (0 turns it off)", channel="Defaults to this channel")
    @moderator()
    async def slowmode(
        self,
        interaction: discord.Interaction,
        seconds: app_commands.Range[int, 0, 21600],
        channel: discord.TextChannel | None = None,
    ) -> None:
        channel = channel or interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await respond(interaction, error_embed("Pick a text channel."), ephemeral=True)
            return
        await interaction.response.defer()
        try:
            await channel.edit(slowmode_delay=seconds, reason=self._audit(interaction, "Slowmode"))
        except discord.HTTPException as exc:
            await self._fail(interaction, exc)
            return
        await self.bot.modlog.log_event(
            "slowmode", interaction.user,
            fields=[("💬 Channel", channel.mention, True), ("🐢 Delay", f"**{seconds}s**" if seconds else "**Off**", True)],
        )
        text = f"Slowmode in {channel.mention} set to **{seconds}s**." if seconds else f"Slowmode disabled in {channel.mention}."
        await interaction.followup.send(embed=success_embed(text))

    async def _set_lock(self, interaction: discord.Interaction, channel: discord.TextChannel | None, locked: bool) -> None:
        channel = channel or interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await respond(interaction, error_embed("Pick a text channel."), ephemeral=True)
            return
        await interaction.response.defer()
        everyone = interaction.guild.default_role
        overwrite = channel.overwrites_for(everyone)
        overwrite.send_messages = False if locked else None
        try:
            await channel.set_permissions(everyone, overwrite=overwrite, reason=self._audit(interaction, "Lock" if locked else "Unlock"))
        except discord.HTTPException as exc:
            await self._fail(interaction, exc)
            return
        await self.bot.modlog.log_event(
            "lock" if locked else "unlock", interaction.user, fields=[("💬 Channel", channel.mention, True)]
        )
        text = f"🔒 {channel.mention} is now locked." if locked else f"🔓 {channel.mention} is now unlocked."
        await interaction.followup.send(embed=success_embed(text))

    @mod.command(name="lock", description="🔒 Stop @everyone from sending messages in a channel")
    @app_commands.describe(channel="Defaults to this channel")
    @moderator()
    async def lock(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None) -> None:
        await self._set_lock(interaction, channel, True)

    @mod.command(name="unlock", description="🔓 Let @everyone send messages in a channel again")
    @app_commands.describe(channel="Defaults to this channel")
    @moderator()
    async def unlock(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None) -> None:
        await self._set_lock(interaction, channel, False)


# ════════════════════════════════════════════════════════════════════════
#  🤖 AUTOMOD
# ════════════════════════════════════════════════════════════════════════
KV_AUTOMOD_ENABLED = "automod_enabled"
KV_AUTOMOD_STRICT = "automod_strict"

_ZERO_WIDTH = re.compile("[​-‏⁠﻿­]")
_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s", "!": "i"})


def normalize(text: str) -> str:
    """Lowercase, strip accents and invisible characters."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return _ZERO_WIDTH.sub("", text).casefold()


def compact(text: str) -> str:
    """Remove everything except letters/digits (catches 'b a d' or 'b.a.d')."""
    return re.sub(r"[\W_]+", "", text)


class AutoMod(commands.Cog):
    """🛡️ Deletes messages containing blocked words (inside words or sentences) and logs them."""

    automod = app_commands.Group(name="automod", description="🛡️ Blocked word filter", guild_only=True)

    def __init__(self, bot: NightFrostBot) -> None:
        self.bot = bot
        self.words: set[str] = set()
        self.enabled = True
        self.strict = False

    async def cog_load(self) -> None:
        self.words = set(await self.bot.db.list_words())
        self.enabled = (await self.bot.db.kv_get(KV_AUTOMOD_ENABLED, "1")) == "1"
        self.strict = (await self.bot.db.kv_get(KV_AUTOMOD_STRICT, "0")) == "1"

    # ---- matching
    def find_matches(self, content: str) -> list[str]:
        norm = normalize(content)
        plain = [norm, norm.translate(_LEET)]
        compacts = [compact(v) for v in plain] if self.strict else []

        hits: list[str] = []
        for word in self.words:
            if any(word in variant for variant in plain):
                hits.append(word)
            elif self.strict:
                cw = compact(word)
                if cw and any(cw in variant for variant in compacts):
                    hits.append(word)
        return sorted(hits)

    # ---- listeners
    def is_blocked(self, message: discord.Message) -> bool:
        """True if AutoMod would act on this message. Daily rewards use this so blocked messages never count."""
        if not self.enabled or not self.words:
            return False
        if message.guild is None or message.author.bot or message.webhook_id:
            return False
        author = message.author
        if not isinstance(author, discord.Member) or not message.content:
            return False
        if author.guild_permissions.manage_messages or is_moderator(author, self.bot.cfg.mod_role_ids):
            return False
        return bool(self.find_matches(message.content))

    async def _scan(self, message: discord.Message) -> None:
        if not self.is_blocked(message):
            return
        author = message.author
        hits = self.find_matches(message.content)

        deleted = True
        try:
            await message.delete()
        except discord.HTTPException:
            deleted = False

        try:
            await message.channel.send(f"🚫 {author.mention}, that language isn't allowed here.", delete_after=6)
        except discord.HTTPException:
            pass

        content = message.content.replace("```", "'''")
        if len(content) > 900:
            content = content[:900] + "…"

        await self.bot.modlog.log_case(
            "automod",
            author,
            self.bot.user,
            "Blocked word detected",
            channel_id=self.bot.cfg.automod_log_channel_id,
            fields=[
                ("💬 Channel", message.channel.mention, True),
                ("🔎 Matched", ", ".join(f"`{h}`" for h in hits), True),
                ("🗑️ Deleted", "✅ Yes" if deleted else "❌ No (missing permissions)", True),
                ("📄 Message", f"```{content}```", False),
            ],
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        await self._scan(message)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if before.content != after.content:
            await self._scan(after)

    # ---- commands
    @automod.command(name="add", description="➕ Block one or more words (comma-separated)")
    @app_commands.describe(words="e.g. badword, another one")
    @moderator()
    async def add(self, interaction: discord.Interaction, words: str) -> None:
        added: list[str] = []
        for raw in words.split(","):
            word = normalize(raw).strip()
            if len(word) < 2:
                continue
            if await self.bot.db.add_word(word):
                self.words.add(word)
                added.append(word)
        if not added:
            await respond(interaction, error_embed("Nothing new to add (words must be 2+ characters)."), ephemeral=True)
            return
        await respond(
            interaction,
            success_embed(f"Blocked **{len(added)}** word(s). They'll be caught inside words and sentences."),
            ephemeral=True,
        )

    @automod.command(name="remove", description="➖ Unblock a word")
    @app_commands.describe(word="The word to remove")
    @moderator()
    async def remove(self, interaction: discord.Interaction, word: str) -> None:
        word = normalize(word).strip()
        if await self.bot.db.remove_word(word):
            self.words.discard(word)
            await respond(interaction, success_embed("Word removed from the filter."), ephemeral=True)
        else:
            await respond(interaction, error_embed("That word isn't in the filter."), ephemeral=True)

    @automod.command(name="list", description="📋 Show blocked words (only you can see this)")
    @moderator()
    async def list_words(self, interaction: discord.Interaction) -> None:
        words = sorted(self.words)
        text = ", ".join(f"||{w}||" for w in words) if words else "No words blocked yet."
        embed = make_embed("📋 Blocked words", text[:4000], C.AUTOMOD, footer=f"{len(words)} word(s) • click to reveal")
        await respond(interaction, embed, ephemeral=True)

    @automod.command(name="toggle", description="🔌 Turn the filter on or off")
    @app_commands.describe(enabled="True = on, False = off")
    @moderator()
    async def toggle(self, interaction: discord.Interaction, enabled: bool) -> None:
        self.enabled = enabled
        await self.bot.db.kv_set(KV_AUTOMOD_ENABLED, "1" if enabled else "0")
        await respond(interaction, success_embed(f"AutoMod is now **{'on' if enabled else 'off'}**."), ephemeral=True)

    @automod.command(name="strict", description="🔍 Also catch spaced/punctuated evasion like b.a.d")
    @app_commands.describe(enabled="True = strict mode (may cause false positives)")
    @moderator()
    async def strict_mode(self, interaction: discord.Interaction, enabled: bool) -> None:
        self.strict = enabled
        await self.bot.db.kv_set(KV_AUTOMOD_STRICT, "1" if enabled else "0")
        await respond(interaction, success_embed(f"Strict mode is now **{'on' if enabled else 'off'}**."), ephemeral=True)

    @automod.command(name="status", description="📊 Show the filter's status")
    @moderator()
    async def status(self, interaction: discord.Interaction) -> None:
        embed = make_embed(
            "🛡️ AutoMod status",
            color=C.AUTOMOD,
            fields=[
                ("🔌 Filter", "🟢 On" if self.enabled else "🔴 Off", True),
                ("🔍 Strict mode", "🟢 On" if self.strict else "⚪ Off", True),
                ("🚫 Blocked words", f"**{len(self.words)}**", True),
            ],
        )
        await respond(interaction, embed, ephemeral=True)


# ════════════════════════════════════════════════════════════════════════
#  🔐 CAPTCHA VERIFICATION
# ════════════════════════════════════════════════════════════════════════
VERIFY_BUTTON_ID = "nightfrost:verify:start"
KV_VERIFY_PANEL = "verify_panel_message_id"


class VerifyPanelView(discord.ui.View):
    """Persistent view: the 'Verify' button on the public panel (survives restarts)."""

    def __init__(self, cog: "Verification") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Verify", emoji="✅", style=discord.ButtonStyle.success, custom_id=VERIFY_BUTTON_ID)
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.start_challenge(interaction)


class CaptchaModal(discord.ui.Modal, title="🔐 Enter the code"):
    answer = discord.ui.TextInput(
        label="Characters shown in the image",
        placeholder="e.g. K7M2P",
        min_length=3,
        max_length=10,
        required=True,
    )

    def __init__(self, cog: "Verification") -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.submit_answer(interaction, str(self.answer.value))


class CaptchaView(discord.ui.View):
    """Private (ephemeral) buttons under the captcha image."""

    def __init__(self, cog: "Verification", user_id: int) -> None:
        super().__init__(timeout=CAPTCHA_TTL)
        self.cog = cog
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    @discord.ui.button(label="Enter code", emoji="⌨️", style=discord.ButtonStyle.primary)
    async def enter(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(CaptchaModal(self.cog))

    @discord.ui.button(label="New image", emoji="🔄", style=discord.ButtonStyle.secondary)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        remaining = self.cog.captcha.lock_remaining(interaction.user.id)
        if remaining:
            await interaction.response.send_message(embed=self.cog.lockout_embed(remaining), ephemeral=True)
            return
        embed, file = await self.cog.build_challenge(interaction.user.id)
        await interaction.response.edit_message(embed=embed, attachments=[file], view=self)


class Verification(commands.Cog):
    """🔐 Image-CAPTCHA verification that grants the verified role."""

    verify = app_commands.Group(name="verify", description="🔐 CAPTCHA verification", guild_only=True)

    def __init__(self, bot: NightFrostBot) -> None:
        self.bot = bot
        self.captcha = CaptchaManager()

    async def cog_load(self) -> None:
        self.bot.add_view(VerifyPanelView(self))  # keeps the panel button alive after restarts

    # ---- embeds
    @staticmethod
    def panel_embed() -> discord.Embed:
        return make_embed(
            "🔐 Verification Required",
            "Welcome! To unlock the server, prove you're human:\n\n"
            "**1.** Press the **✅ Verify** button below\n"
            "**2.** Read the characters in the image\n"
            "**3.** Press **⌨️ Enter code** and type them in\n\n"
            f"⏱️ Codes last {CAPTCHA_TTL // 60} minutes • {CAPTCHA_MAX_ATTEMPTS} attempts before a "
            f"{CAPTCHA_LOCKOUT // 60}-minute cooldown.",
            C.INFO,
            timestamp=False,
        )

    @staticmethod
    def lockout_embed(remaining: int) -> discord.Embed:
        return make_embed(
            "⛔ Too many wrong answers",
            f"Please wait **{format_duration(remaining)}** before trying again.",
            C.ERROR,
            timestamp=False,
        )

    def panel_embeds(self) -> tuple[list[discord.Embed], discord.File | None]:
        """[banner image embed, original panel embed] (+ the banner file when a local one exists)."""
        banner = discord.Embed(color=C.INFO)
        local = ROOT / BANNER_FILENAME
        file = None
        if local.exists():  # preferred: uploaded by the bot, never expires
            file = discord.File(local, filename=BANNER_FILENAME)
            banner.set_image(url=f"attachment://{BANNER_FILENAME}")
        else:
            banner.set_image(url=self.bot.cfg.verify_banner_url)
        return [banner, self.panel_embed()], file

    async def build_challenge(self, user_id: int) -> tuple[discord.Embed, discord.File]:
        code = self.captcha.issue(user_id)
        buf = await asyncio.to_thread(render_captcha, code)  # keep the event loop free
        file = discord.File(buf, filename="captcha.png")
        embed = make_embed(
            "🔐 Type the characters you see",
            "Letters aren't case-sensitive. Can't read it? Press **🔄 New image**.",
            C.INFO,
            footer=f"Expires in {CAPTCHA_TTL // 60} minutes",
            timestamp=False,
        )
        embed.set_image(url="attachment://captcha.png")
        return embed, file

    # ---- panel
    async def ensure_panel(self, *, force: bool = False) -> discord.abc.GuildChannel | None:
        """Post the verify panel if it doesn't exist yet (or repost it when force=True)."""
        channel_id = self.bot.cfg.verify_channel_id
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.HTTPException:
                log.warning("Verify channel not found")
                return None

        message_id = await self.bot.db.kv_get(KV_VERIFY_PANEL)
        if message_id:
            try:
                old = await channel.fetch_message(int(message_id))
                if not force and len(old.embeds) >= 2:  # already the two-embed layout
                    return channel
                await old.delete()  # forced repost, or upgrading the old one-embed panel
            except discord.NotFound:
                pass
            except discord.HTTPException:
                log.exception("Could not check the verify panel")
                return channel

        try:
            embeds, file = self.panel_embeds()
            extra = {"file": file} if file else {}
            message = await channel.send(embeds=embeds, view=VerifyPanelView(self), **extra)
            await self.bot.db.kv_set(KV_VERIFY_PANEL, str(message.id))
        except discord.HTTPException:
            log.exception("Could not post the verify panel")
            return None
        return channel

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        await self.ensure_panel()

    # ---- user flow
    def _role(self, guild: discord.Guild) -> discord.Role | None:
        return guild.get_role(self.bot.cfg.verified_role_id)

    async def start_challenge(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        role = self._role(interaction.guild) if interaction.guild else None
        if not isinstance(member, discord.Member) or role is None:
            await respond(interaction, error_embed("Verification isn't set up correctly. Ask a moderator."), ephemeral=True)
            return
        if role in member.roles:
            await respond(interaction, success_embed("You're already verified! 🎉"), ephemeral=True)
            return
        remaining = self.captcha.lock_remaining(member.id)
        if remaining:
            await respond(interaction, self.lockout_embed(remaining), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        embed, file = await self.build_challenge(member.id)
        await interaction.followup.send(embed=embed, file=file, view=CaptchaView(self, member.id), ephemeral=True)

    async def submit_answer(self, interaction: discord.Interaction, answer: str) -> None:
        member = interaction.user
        status, left = self.captcha.check(member.id, answer)

        if status == "ok":
            await self._grant(interaction, member)
        elif status == "wrong":
            await respond(
                interaction,
                error_embed(f"That's not right. **{left}** attempt(s) left — you can press **🔄 New image** for a fresh code."),
                ephemeral=True,
            )
        elif status == "locked_now":
            await respond(interaction, self.lockout_embed(CAPTCHA_LOCKOUT), ephemeral=True)
            await self.bot.modlog.log_verify(
                "verify_lock", member,
                fields=[("⏳ Cooldown", f"**{format_duration(CAPTCHA_LOCKOUT)}**", True),
                        ("❌ Wrong answers", f"**{CAPTCHA_MAX_ATTEMPTS}**", True)],
            )
        elif status == "locked":
            await respond(interaction, self.lockout_embed(self.captcha.lock_remaining(member.id)), ephemeral=True)
        else:  # expired / missing
            await respond(interaction, error_embed("That code expired. Press **✅ Verify** again for a new one."), ephemeral=True)

    async def _grant(self, interaction: discord.Interaction, member: discord.Member) -> None:
        role = self._role(interaction.guild)
        if role is None:
            await respond(interaction, error_embed("The verified role no longer exists. Tell a moderator."), ephemeral=True)
            return
        try:
            await member.add_roles(role, reason="Passed CAPTCHA verification")
        except discord.HTTPException:
            await respond(
                interaction,
                error_embed("I couldn't give you the role (my role must be above it and I need **Manage Roles**). Tell a moderator."),
                ephemeral=True,
            )
            return
        await respond(interaction, success_embed("You're verified — welcome in! 🎉"), ephemeral=True)
        await self.bot.modlog.log_verify(
            "verify_pass", member,
            fields=[("🎭 Role", role.mention, True),
                    ("📅 Account created", f"<t:{int(member.created_at.timestamp())}:R>", True)],
        )

    # ---- admin commands
    @verify.command(name="setup", description="📌 Post (or repost) the verification panel")
    @moderator()
    async def setup_panel(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        channel = await self.ensure_panel(force=True)
        if channel is None:
            await interaction.followup.send(embed=error_embed("I couldn't post in the verify channel. Check VERIFY_CHANNEL_ID and my permissions."), ephemeral=True)
            return
        await interaction.followup.send(embed=success_embed(f"Verification panel posted in {channel.mention}."), ephemeral=True)

    @verify.command(name="manual", description="✅ Verify a member without a CAPTCHA")
    @app_commands.describe(member="Who to verify")
    @moderator()
    async def manual(self, interaction: discord.Interaction, member: discord.Member) -> None:
        role = self._role(interaction.guild)
        if role is None:
            await respond(interaction, error_embed("The verified role (VERIFIED_ROLE_ID) wasn't found."), ephemeral=True)
            return
        try:
            await member.add_roles(role, reason=f"Manually verified by {interaction.user} ({interaction.user.id})")
        except discord.HTTPException:
            await respond(interaction, error_embed("I couldn't give that role. Check my role position and permissions."), ephemeral=True)
            return
        await respond(interaction, success_embed(f"Verified {member.mention}."), ephemeral=True)
        await self.bot.modlog.log_verify(
            "verify_manual", member, fields=[("🛡️ Moderator", interaction.user.mention, True)]
        )

    @verify.command(name="revoke", description="↩️ Remove a member's verified role")
    @app_commands.describe(member="Who to un-verify")
    @moderator()
    async def revoke(self, interaction: discord.Interaction, member: discord.Member) -> None:
        role = self._role(interaction.guild)
        if role is None or role not in member.roles:
            await respond(interaction, error_embed(f"{member.mention} isn't verified."), ephemeral=True)
            return
        try:
            await member.remove_roles(role, reason=f"Verification revoked by {interaction.user} ({interaction.user.id})")
        except discord.HTTPException:
            await respond(interaction, error_embed("I couldn't remove that role. Check my role position and permissions."), ephemeral=True)
            return
        await respond(interaction, success_embed(f"Removed the verified role from {member.mention}."), ephemeral=True)
        await self.bot.modlog.log_verify(
            "verify_revoke", member, fields=[("🛡️ Moderator", interaction.user.mention, True)]
        )


# ════════════════════════════════════════════════════════════════════════
#  🎁 DAILY CHAT REWARDS  (1 message = 1 entry -> winner drawn at midnight -> announcement)
# ════════════════════════════════════════════════════════════════════════
KV_LAST_DRAW_CHECK = "daily_last_draw_check"
ENTRY_FLUSH_SECONDS = 20  # entries are buffered in memory and written to the database in batches


class DailyRewards(commands.Cog):
    """🎁 Every non-bot message earns an entry; once a day one entry-weighted winner is announced."""

    reward = app_commands.Group(name="reward", description="🎁 Daily chat rewards", guild_only=True)

    def __init__(self, bot: NightFrostBot) -> None:
        self.bot = bot
        self.tz = ZoneInfo(bot.cfg.daily_reward_timezone)
        self.buffer: dict[tuple[str, int], int] = defaultdict(int)
        self._flush_lock = asyncio.Lock()
        self._rng = random.SystemRandom()

    async def cog_load(self) -> None:
        self.flush_loop.start()
        self.draw_loop.start()

    async def cog_unload(self) -> None:
        self.flush_loop.cancel()
        self.draw_loop.cancel()
        await self.flush()

    # ---- time helpers
    def now(self) -> dt.datetime:
        return dt.datetime.now(self.tz)

    def today(self) -> str:
        return self.now().date().isoformat()

    def next_draw(self) -> dt.datetime:
        return (self.now() + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)

    # ---- entry counting
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot or message.webhook_id:
            return
        automod = self.bot.get_cog("AutoMod")
        if automod is not None and automod.is_blocked(message):  # automodded messages never count
            return
        self.buffer[(self.today(), message.author.id)] += 1

    async def flush(self) -> None:
        """Write buffered entries to the database in one transaction."""
        async with self._flush_lock:
            if not self.buffer:
                return
            snapshot, self.buffer = self.buffer, defaultdict(int)
            try:
                await self.bot.db.add_entries([(day, uid, n) for (day, uid), n in snapshot.items()])
            except Exception:
                log.exception("Could not save daily entries - will retry")
                for key, n in snapshot.items():
                    self.buffer[key] += n

    @tasks.loop(seconds=ENTRY_FLUSH_SECONDS)
    async def flush_loop(self) -> None:
        await self.flush()

    @tasks.loop(seconds=60)
    async def draw_loop(self) -> None:
        """Every minute: if yesterday hasn't been drawn yet, draw it (also catches up after downtime)."""
        try:
            await self.flush()
            yesterday = (self.now().date() - dt.timedelta(days=1)).isoformat()
            if await self.bot.db.kv_get(KV_LAST_DRAW_CHECK) == yesterday:
                return
            if await self.run_draw(yesterday) != "retry":
                await self.bot.db.kv_set(KV_LAST_DRAW_CHECK, yesterday)
        except Exception:
            log.exception("Daily draw check failed")

    @flush_loop.before_loop
    @draw_loop.before_loop
    async def _wait_until_ready(self) -> None:
        await self.bot.wait_until_ready()

    # ---- the draw
    async def _reward_channel(self):
        channel = self.bot.get_channel(self.bot.cfg.daily_reward_channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(self.bot.cfg.daily_reward_channel_id)
            except discord.HTTPException:
                log.warning("Daily reward channel not found - check DAILY_REWARD_CHANNEL_ID")
                return None
        return channel

    async def run_draw(self, day: str) -> str:
        """Returns 'drawn' | 'already' | 'none' (nobody chatted / no valid winner) | 'retry' (try again later)."""
        if await self.bot.db.get_reward_by_day(day):
            return "already"
        rows = await self.bot.db.day_entries(day)
        if not rows:
            return "none"
        channel = await self._reward_channel()
        if channel is None:
            return "retry"
        guild = channel.guild

        pool = {row["user_id"]: row["entries"] for row in rows}
        total = sum(pool.values())
        winner: discord.Member | None = None
        while pool:  # each entry is one ticket in the hat; skip people who left the server
            user_id = self._rng.choices(list(pool), weights=list(pool.values()))[0]
            try:
                winner = guild.get_member(user_id) or await guild.fetch_member(user_id)
                break
            except discord.NotFound:
                del pool[user_id]
            except discord.HTTPException:
                return "retry"
        if winner is None:
            return "none"
        winner_entries = pool[winner.id]

        prize = self.bot.cfg.daily_reward_prize
        reward_id = await self.bot.db.create_reward(day, winner.id, winner_entries, total, prize)
        if reward_id is None:
            return "already"
        try:
            message = await channel.send(
                content=(
                    f"🎉 Congratulations {winner.mention}\n\n"
                    f"You won {prize} in Daily Chat Reward.\n\n"
                    f"open a giveaway claim ticket at <#{self.bot.cfg.giveaway_claim_channel_id}> to claim your reward!"
                ),
                allowed_mentions=discord.AllowedMentions(users=True),
            )
        except discord.HTTPException:
            log.exception("Could not post the daily reward announcement")
            await self.bot.db.delete_reward(reward_id)  # un-claim the day so it retries
            return "retry"
        await self.bot.db.set_reward_message(reward_id, message.id)

        await self.bot.modlog.log_member_event(
            "daily_draw", winner,
            fields=[("📅 Day", day, True), ("🎁 Prize", prize, True),
                    ("🎟️ Entries", f"**{winner_entries}** of {total}", True),
                    ("👥 Participants", f"**{len(rows)}**", True)],
        )
        await self.bot.db.prune_entries((self.now().date() - dt.timedelta(days=14)).isoformat())
        return "drawn"

    # ---- commands
    @reward.command(name="entries", description="🎟️ See how many entries you (or someone) have today")
    @app_commands.describe(user="Defaults to yourself")
    async def entries(self, interaction: discord.Interaction, user: discord.User | None = None) -> None:
        user = user or interaction.user
        await self.flush()
        day = self.today()
        mine = await self.bot.db.user_entries(day, user.id)
        total = sum(r["entries"] for r in await self.bot.db.day_entries(day))
        chance = f"{mine / total * 100:.1f}%" if total else "—"
        embed = make_embed(
            f"🎟️ Daily entries • {user.display_name}",
            "Every message you send earns **1 entry**. Messages removed by AutoMod don't count.",
            C.GEM,
            fields=[
                ("🎟️ Entries today", f"**{mine}**", True),
                ("🎯 Win chance", f"**{chance}**", True),
                ("⏰ Draw", f"<t:{int(self.next_draw().timestamp())}:R>", True),
            ],
            thumbnail=user.display_avatar.url,
        )
        await respond(interaction, embed, ephemeral=True)

    @reward.command(name="status", description="📊 Today's entries and the last winner")
    @moderator()
    async def status(self, interaction: discord.Interaction) -> None:
        await self.flush()
        rows = await self.bot.db.day_entries(self.today())
        total = sum(r["entries"] for r in rows)
        top = "\n".join(f"`#{i}` <@{r['user_id']}> — **{r['entries']}**" for i, r in enumerate(rows[:5], 1)) or "No entries yet."
        last = await self.bot.db.last_reward()
        last_text = f"<@{last['winner_id']}> • {last['day']} • {last['prize']}" if last else "—"
        embed = make_embed(
            "📊 Daily chat reward status",
            color=C.GEM,
            fields=[
                ("📅 Day", self.today(), True),
                ("🎟️ Entries", f"**{total}**", True),
                ("👥 Participants", f"**{len(rows)}**", True),
                ("🎁 Prize", self.bot.cfg.daily_reward_prize, True),
                ("⏰ Next draw", f"<t:{int(self.next_draw().timestamp())}:R>", True),
                ("🌍 Timezone", self.bot.cfg.daily_reward_timezone, True),
                ("🏆 Top today", top, False),
                ("🎉 Last winner", last_text, False),
            ],
        )
        await respond(interaction, embed, ephemeral=True)

    @reward.command(name="draw", description="🎉 End today's draw now and pick a winner")
    @moderator()
    async def draw(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await self.flush()
        result = await self.run_draw(self.today())
        messages = {
            "drawn": success_embed("Winner drawn! Check the reward channel. Today's draw is now closed."),
            "already": error_embed("Today's reward was already drawn."),
            "none": error_embed("Nobody has entries today yet."),
            "retry": error_embed("I couldn't post in the reward channel. Check DAILY_REWARD_CHANNEL_ID and my permissions."),
        }
        await interaction.followup.send(embed=messages[result], ephemeral=True)


# ════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ════════════════════════════════════════════════════════════════════════
def main() -> None:
    cfg = load_config()
    bot = NightFrostBot(cfg)
    try:
        bot.run(cfg.token, log_handler=None)
    except discord.LoginFailure:
        sys.exit(
            "\n❌ Discord rejected your DISCORD_TOKEN.\n"
            "   • Developer Portal -> your app -> Bot -> Reset Token, then paste the NEW token\n"
            "   • Put it in .env as DISCORD_TOKEN=... (no quotes, no spaces)\n"
            "   • If your host has an Environment/Variables tab, make sure DISCORD_TOKEN isn't set there to "
            "something old - host variables override .env\n"
        )
    except discord.PrivilegedIntentsRequired:
        sys.exit(
            "\n❌ Message Content Intent is off.\n"
            "   Developer Portal -> your app -> Bot -> Privileged Gateway Intents -> enable MESSAGE CONTENT INTENT\n"
        )


if __name__ == "__main__":
    main()
