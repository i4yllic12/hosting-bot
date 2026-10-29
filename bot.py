import os
import re
import json
import math
import sqlite3
import asyncio
from datetime import datetime, timezone

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands


# ============================================================
# CONFIGURATION
# ============================================================

TOKEN = os.getenv("DISCORD_TOKEN") or os.getenv("DISCORD_BOT_TOKEN")

HOST_ROLE_ID = int(os.getenv("HOST_ROLE_ID", "1565048538042208466"))
STAFF_ROLE_ID = int(os.getenv("STAFF_ROLE_ID", "1564857946599456878"))
ELO_MANAGER_ROLE_ID = int(
    os.getenv("ELO_MANAGER_ROLE_ID", "1565158910292398184")
)

GUILD_ID = int(os.getenv("GUILD_ID", "0") or "0")
MATCH_RESULTS_CHANNEL_ID = int(
    os.getenv("MATCH_RESULTS_CHANNEL_ID", "0") or "0"
)

BLOXLINK_VERIFIED_ROLE_ID = int(
    os.getenv("BLOXLINK_VERIFIED_ROLE_ID", "0") or "0"
)
BLOXLINK_API_KEY = os.getenv("BLOXLINK_API_KEY", "").strip()

DB_PATH = os.getenv("DB_PATH", "./data/hosting.sqlite3")

MODES = ("1v1s", "2v2s", "3v3s")
REGIONS = ("NA", "EU", "OCE", "Other")

SLOT_COUNTS = {
    "1v1s": 2,
    "2v2s": 4,
    "3v3s": 6,
}

ROLE_1V1_ID = 1564896767257084036
ROLE_2V2_ID = 1564896807673270382
ROLE_3V3_ID = 1564896826723672135

MATCH_ROLE_IDS = {
    "1v1s": ROLE_1V1_ID,
    "2v2s": ROLE_2V2_ID,
    "3v3s": ROLE_3V3_ID,
}

ELO_MODES = {
    "1v1": 1,
    "2v2": 2,
    "3v3": 3,
}

STARTING_ELO = 1000
K_FACTOR = 32

pending_results = {}
roblox_username_cache = {}

# Prevent simultaneous submissions from processing the same match twice.
processing_matches = set()


# ============================================================
# DATABASE
# ============================================================

def db_connect():
    folder = os.path.dirname(DB_PATH)

    if folder:
        os.makedirs(folder, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with db_connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                message_id INTEGER,
                host_id INTEGER NOT NULL,
                mode TEXT NOT NULL,
                region TEXT NOT NULL,
                players TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'open',
                result TEXT,
                created_at TEXT NOT NULL,
                player_ids TEXT NOT NULL DEFAULT '[]',
                private_server_link TEXT
            )
        """)

        # Migrate older hosting databases.
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(matches)").fetchall()
        }

        migrations = {
            "player_ids": "TEXT NOT NULL DEFAULT '[]'",
            "private_server_link": "TEXT",
        }

        for column, definition in migrations.items():
            if column not in columns:
                conn.execute(
                    f"ALTER TABLE matches ADD COLUMN {column} {definition}"
                )

        # Elo ratings are separate for each game mode.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ratings (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                mode TEXT NOT NULL,
                elo INTEGER NOT NULL DEFAULT 1000,
                wins INTEGER NOT NULL DEFAULT 0,
                losses INTEGER NOT NULL DEFAULT 0,
                matches INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id, mode)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS player_platforms (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                platform TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS elo_matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                hosting_match_id INTEGER NOT NULL UNIQUE,
                mode TEXT NOT NULL,
                winner TEXT NOT NULL,
                red_team TEXT NOT NULL,
                blue_team TEXT NOT NULL,
                scores TEXT NOT NULL,
                elo_changes TEXT NOT NULL,
                reported_by INTEGER NOT NULL,
                screenshot_url TEXT,
                created_at TEXT NOT NULL,
                undone INTEGER NOT NULL DEFAULT 0
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS elo_adjustments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                mode TEXT NOT NULL,
                amount INTEGER NOT NULL,
                old_elo INTEGER NOT NULL,
                new_elo INTEGER NOT NULL,
                reason TEXT NOT NULL,
                adjusted_by INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ratings_leaderboard
            ON ratings(guild_id, mode, elo DESC)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_elo_matches_history
            ON elo_matches(guild_id, created_at DESC)
        """)


def get_match(match_id):
    with db_connect() as conn:
        return conn.execute(
            "SELECT * FROM matches WHERE id = ?",
            (match_id,),
        ).fetchone()


def get_players(match):
    try:
        return json.loads(match["player_ids"] or "[]")
    except (json.JSONDecodeError, TypeError):
        return []


def create_match(
    guild_id,
    channel_id,
    host_id,
    mode,
    region,
    private_server_link=None,
):
    created_at = datetime.now(timezone.utc).isoformat()

    with db_connect() as conn:
        cursor = conn.execute("""
            INSERT INTO matches (
                guild_id,
                channel_id,
                host_id,
                mode,
                region,
                players,
                status,
                created_at,
                player_ids,
                private_server_link
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            guild_id,
            channel_id,
            host_id,
            mode,
            region,
            json.dumps([]),
            "open",
            created_at,
            json.dumps([host_id]),
            private_server_link,
        ))

        return cursor.lastrowid


def update_match(match_id, **fields):
    allowed = {
        "message_id",
        "players",
        "status",
        "result",
        "player_ids",
        "private_server_link",
    }

    updates = {
        key: value
        for key, value in fields.items()
        if key in allowed
    }

    if not updates:
        return

    assignments = ", ".join(f"{key} = ?" for key in updates)

    with db_connect() as conn:
        conn.execute(
            f"UPDATE matches SET {assignments} WHERE id = ?",
            (*updates.values(), match_id),
        )


def save_players(match_id, players):
    update_match(
        match_id,
        player_ids=json.dumps(players),
        players=json.dumps(players),
    )


# ============================================================
# ELO SYSTEM
# ============================================================

def ensure_rating(conn, guild_id, user_id, mode):
    conn.execute("""
        INSERT OR IGNORE INTO ratings (
            guild_id, user_id, mode, elo, wins, losses, matches
        )
        VALUES (?, ?, ?, ?, 0, 0, 0)
    """, (guild_id, user_id, mode, STARTING_ELO))

    return conn.execute("""
        SELECT *
        FROM ratings
        WHERE guild_id = ? AND user_id = ? AND mode = ?
    """, (guild_id, user_id, mode)).fetchone()


def expected_score(team_elo, opponent_elo):
    return 1 / (1 + 10 ** ((opponent_elo - team_elo) / 400))


def calculate_team_elo_change(team_elo, opponent_elo, won):
    """
    Standard Elo calculation.

    Equal-rated teams:
      Winner: approximately +16
      Loser: approximately -16

    Upsets result in larger changes.
    """
    expected = expected_score(team_elo, opponent_elo)
    actual = 1.0 if won else 0.0

    return round(K_FACTOR * (actual - expected))


def calculate_individual_changes(players, scores, guild_id, mode):
    """
    Players are supplied as:
    [
        {"user_id": 123, "score": 32, "team": "red", "won": True},
        ...
    ]

    Team Elo is based on average team ratings.

    Individual scores also provide a small performance adjustment,
    capped at 8 Elo, so the result depends on the match score without
    overriding the win/loss result.
    """
    with db_connect() as conn:
        ratings = {}

        for player in players:
            user_id = int(player["user_id"])
            row = ensure_rating(conn, guild_id, user_id, mode)
            ratings[user_id] = int(row["elo"])

        red = [p for p in players if p["team"] == "red"]
        blue = [p for p in players if p["team"] == "blue"]

        if not red or not blue:
            raise ValueError("Both teams must contain players.")

        red_average = sum(
            ratings[int(p["user_id"])] for p in red
        ) / len(red)

        blue_average = sum(
            ratings[int(p["user_id"])] for p in blue
        ) / len(blue)

        red_won = bool(red[0]["won"])

        red_team_change = calculate_team_elo_change(
            red_average, blue_average, red_won
        )

        blue_team_change = calculate_team_elo_change(
            blue_average, red_average, not red_won
        )

        changes = {}

        for team, team_players, team_change, opponents in (
            ("red", red, red_team_change, blue),
            ("blue", blue, blue_team_change, red),
        ):
            opponent_average_score = sum(
                int(p["score"]) for p in opponents
            ) / len(opponents)

            for player in team_players:
                user_id = int(player["user_id"])
                player_score = int(player["score"])

                # Small performance adjustment: maximum +/- 8 Elo.
                performance = round(
                    (player_score - opponent_average_score) * 0.5
                )
                performance = max(-8, min(8, performance))

                # A losing player cannot turn a loss into a rating gain.
                # A winning player cannot turn a win into a rating loss.
                if player["won"]:
                    change = max(1, team_change + performance)
                else:
                    change = min(-1, team_change + performance)

                changes[str(user_id)] = {
                    "old_elo": ratings[user_id],
                    "change": change,
                    "new_elo": ratings[user_id] + change,
                    "score": player_score,
                    "team": team,
                    "won": bool(player["won"]),
                }

        return changes


def apply_elo_result(
    hosting_match_id,
    guild_id,
    mode,
    players,
    reported_by,
    screenshot_url=None,
):
    """
    Applies Elo once per hosted match.
    The UNIQUE hosting_match_id prevents duplicate Elo awards.
    """
    changes = calculate_individual_changes(
        players,
        None,
        guild_id,
        mode,
    )

    red_ids = [
        int(p["user_id"])
        for p in players
        if p["team"] == "red"
    ]
    blue_ids = [
        int(p["user_id"])
        for p in players
        if p["team"] == "blue"
    ]

    winner = "red" if players[0]["won"] else "blue"

    with db_connect() as conn:
        existing = conn.execute("""
            SELECT id
            FROM elo_matches
            WHERE hosting_match_id = ?
        """, (hosting_match_id,)).fetchone()

        if existing:
            raise ValueError("Elo has already been applied to this match.")

        for user_id_string, info in changes.items():
            user_id = int(user_id_string)

            conn.execute("""
                UPDATE ratings
                SET elo = ?,
                    wins = wins + ?,
                    losses = losses + ?,
                    matches = matches + 1
                WHERE guild_id = ? AND user_id = ? AND mode = ?
            """, (
                info["new_elo"],
                1 if info["won"] else 0,
                0 if info["won"] else 1,
                guild_id,
                user_id,
                mode,
            ))

        conn.execute("""
            INSERT INTO elo_matches (
                guild_id,
                hosting_match_id,
                mode,
                winner,
                red_team,
                blue_team,
                scores,
                elo_changes,
                reported_by,
                screenshot_url,
                created_at,
                undone
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        """, (
            guild_id,
            hosting_match_id,
            mode,
            winner,
            json.dumps(red_ids),
            json.dumps(blue_ids),
            json.dumps([
                {
                    "user_id": int(p["user_id"]),
                    "score": int(p["score"]),
                    "team": p["team"],
                    "won": bool(p["won"]),
                }
                for p in players
            ]),
            json.dumps(changes),
            reported_by,
            screenshot_url,
            datetime.now(timezone.utc).isoformat(),
        ))

    return changes


def undo_elo_result(hosting_match_id, guild_id):
    with db_connect() as conn:
        match = conn.execute("""
            SELECT *
            FROM elo_matches
            WHERE hosting_match_id = ?
              AND guild_id = ?
              AND undone = 0
        """, (hosting_match_id, guild_id)).fetchone()

        if not match:
            raise ValueError("No active Elo result was found for this match.")

        changes = json.loads(match["elo_changes"])

        for user_id_string, info in changes.items():
            user_id = int(user_id_string)

            conn.execute("""
                UPDATE ratings
                SET elo = ?,
                    wins = MAX(0, wins - ?),
                    losses = MAX(0, losses - ?),
                    matches = MAX(0, matches - 1)
                WHERE guild_id = ? AND user_id = ? AND mode = ?
            """, (
                info["old_elo"],
                1 if info["won"] else 0,
                0 if info["won"] else 1,
                guild_id,
                user_id,
                match["mode"],
            ))

        conn.execute("""
            UPDATE elo_matches
            SET undone = 1
            WHERE id = ?
        """, (match["id"],))

        return match


def get_rating(guild_id, user_id, mode):
    with db_connect() as conn:
        return ensure_rating(conn, guild_id, user_id, mode)


# ============================================================
# PERMISSIONS
# ============================================================

def has_host_role(member):
    return (
        isinstance(member, discord.Member)
        and any(role.id == HOST_ROLE_ID for role in member.roles)
    )


def has_elo_manager_role(member):
    return (
        isinstance(member, discord.Member)
        and any(
            role.id == ELO_MANAGER_ROLE_ID
            for role in member.roles
        )
    )


def is_staff(interaction):
    if not isinstance(interaction.user, discord.Member):
        return False

    return (
        interaction.user.guild_permissions.administrator
        or interaction.user.guild_permissions.manage_guild
        or any(
            role.id == STAFF_ROLE_ID
            for role in interaction.user.roles
        )
    )


def is_elo_manager(interaction):
    if not isinstance(interaction.user, discord.Member):
        return False

    return (
        interaction.user.guild_permissions.administrator
        or interaction.user.guild_permissions.manage_guild
        or has_elo_manager_role(interaction.user)
    )


async def require_verification(interaction):
    if not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(
            "This command can only be used in a server.",
            ephemeral=True,
        )
        return False

    if BLOXLINK_VERIFIED_ROLE_ID:
        if not any(
            role.id == BLOXLINK_VERIFIED_ROLE_ID
            for role in interaction.user.roles
        ):
            await interaction.response.send_message(
                "You must verify through Bloxlink before using this feature.",
                ephemeral=True,
            )
            return False

    return True


# ============================================================
# BLOXLINK / ROBLOX
# ============================================================

async def get_roblox_username(user_id):
    if user_id in roblox_username_cache:
        return roblox_username_cache[user_id]

    if not BLOXLINK_API_KEY:
        return None

    headers = {
        "Authorization": BLOXLINK_API_KEY,
        "Content-Type": "application/json",
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://api.blox.link/v4/public/guilds/{GUILD_ID}/discord-to-roblox/{user_id}",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                if response.status != 200:
                    return None

                data = await response.json()
                roblox_id = data.get("robloxID")

                if not roblox_id:
                    return None

            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://users.roblox.com/v1/users",
                    json={"userIds": [int(roblox_id)]},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as response:
                    if response.status != 200:
                        return None

                    data = await response.json()
                    users = data.get("data", [])

                    if not users:
                        return None

                    username = users[0].get("name")
                    roblox_username_cache[user_id] = username
                    return username

    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError):
        return None


# ============================================================
# MATCH EMBEDS AND CHANNELS
# ============================================================

def mode_to_elo_mode(mode):
    return mode.replace("s", "")


def make_match_embed(match):
    players = get_players(match)

    status = match["status"].replace("_", " ").title()

    embed = discord.Embed(
        title=f"Arsenal Angels • Match #{match['id']}",
        color=discord.Color.from_rgb(255, 170, 210),
        timestamp=datetime.now(timezone.utc),
    )

    embed.add_field(
        name="Mode",
        value=match["mode"],
        inline=True,
    )
    embed.add_field(
        name="Region",
        value=match["region"],
        inline=True,
    )
    embed.add_field(
        name="Status",
        value=status,
        inline=True,
    )
    embed.add_field(
        name="Host",
        value=f"<@{match['host_id']}>",
        inline=True,
    )
    embed.add_field(
        name=f"Players ({len(players)}/{SLOT_COUNTS.get(match['mode'], 2)})",
        value="\n".join(f"<@{player_id}>" for player_id in players)
        if players else "No players yet.",
        inline=False,
    )

    link = match["private_server_link"]
    if link:
        embed.add_field(
            name="Private Server",
            value=f"[Join Private Server]({link})",
            inline=False,
        )

    if match["result"]:
        embed.add_field(
            name="Result",
            value=match["result"][:1024],
            inline=False,
        )

    embed.set_footer(text=f"Match ID: {match['id']}")
    return embed


def find_results_channel(guild):
    if not guild:
        return None

    if MATCH_RESULTS_CHANNEL_ID:
        channel = guild.get_channel(MATCH_RESULTS_CHANNEL_ID)
        if channel:
            return channel

    for channel in guild.text_channels:
        if channel.name.lower() == "match-results":
            return channel

    return None


async def refresh_match(bot, match_id):
    match = get_match(match_id)

    if not match or not match["message_id"]:
        return

    channel = bot.get_channel(match["channel_id"])
    if not channel:
        return

    try:
        message = await channel.fetch_message(match["message_id"])
        await message.edit(
            embed=make_match_embed(match),
            view=MatchView(bot, match_id),
        )
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass


async def send_match(bot, interaction, match_id):
    match = get_match(match_id)
    if not match:
        return None

    view = MatchView(bot, match_id)
    message = await interaction.channel.send(
        embed=make_match_embed(match),
        view=view,
    )

    update_match(match_id, message_id=message.id)
    return message


# ============================================================
# MATCH AUTHORIZATION
# ============================================================

def can_manage_match(interaction, match):
    if not isinstance(interaction.user, discord.Member):
        return False

    return (
        interaction.user.id == match["host_id"]
        or has_host_role(interaction.user)
        or interaction.user.guild_permissions.administrator
        or interaction.user.guild_permissions.manage_guild
    )


def can_manage_message(member, match):
    if not isinstance(member, discord.Member):
        return False

    return (
        member.id == match["host_id"]
        or has_host_role(member)
        or member.guild_permissions.administrator
        or member.guild_permissions.manage_guild
    )


# ============================================================
# RESULT MESSAGE PARSER
# ============================================================

def parse_result_message(content, roster, mode):
    """
    Required format: one player per line.

    Example:
    <@123456789> 32 winner
    <@987654321> 25 loser

    For 2v2 and 3v3, every player must be included.
    All members of one team must be marked winner and all members
    of the opposing team loser.

    The first half of the hosted roster is Team Red.
    The second half is Team Blue.
    """
    rows = [
        line.strip()
        for line in content.splitlines()
        if line.strip()
    ]

    parsed = {}

    for line in rows:
        mention_match = re.search(r"<@!?(\d+)>", line)

        if not mention_match:
            continue

        user_id = int(mention_match.group(1))

        if user_id in parsed:
            raise ValueError(
                f"<@{user_id}> appears more than once in the results."
            )

        score_match = re.search(
            r"<@!?\d+>\s+(\d+)\s+(winner|loser)\b",
            line,
            re.IGNORECASE,
        )

        if not score_match:
            raise ValueError(
                "Each line must use this format: "
                "`@Player 32 winner` or `@Player 25 loser`."
            )

        score = int(score_match.group(1))
        status = score_match.group(2).lower()

        parsed[user_id] = {
            "user_id": user_id,
            "score": score,
            "status": status,
        }

    if set(parsed) != set(roster):
        missing = set(roster) - set(parsed)
        extra = set(parsed) - set(roster)

        message = []

        if missing:
            message.append(
                "Missing roster players: "
                + ", ".join(f"<@{user_id}>" for user_id in missing)
            )

        if extra:
            message.append(
                "Players not in this match: "
                + ", ".join(f"<@{user_id}>" for user_id in extra)
            )

        raise ValueError("\n".join(message))

    if len(roster) != SLOT_COUNTS.get(mode, 2):
        raise ValueError("The hosted match roster is incomplete.")

    half = len(roster) // 2
    red_ids = roster[:half]
    blue_ids = roster[half:]

    red_statuses = {parsed[user_id]["status"] for user_id in red_ids}
    blue_statuses = {parsed[user_id]["status"] for user_id in blue_ids}

    if len(red_statuses) != 1 or len(blue_statuses) != 1:
        raise ValueError(
            "All players on the same team must have the same status."
        )

    if red_statuses == blue_statuses:
        raise ValueError(
            "One team must be marked winner and the other team loser."
        )

    result = []

    for user_id in roster:
        info = parsed[user_id]

        if user_id in red_ids:
            team = "red"
        else:
            team = "blue"

        result.append({
            "user_id": user_id,
            "score": info["score"],
            "team": team,
            "won": info["status"] == "winner",
        })

    return result


# ============================================================
# MATCH VIEW
# ============================================================

class MatchView(discord.ui.View):
    def __init__(self, bot, match_id):
        super().__init__(timeout=None)
        self.bot = bot
        self.match_id = match_id

        self.cancel_button = discord.ui.Button(
            label="Cancel Host",
            style=discord.ButtonStyle.danger,
            custom_id=f"hosting:{match_id}:cancel",
        )
        self.cancel_button.callback = self.cancel_host
        self.add_item(self.cancel_button)

        match = get_match(match_id)
        if match and match["private_server_link"]:
            self.private_button = discord.ui.Button(
                label="Join Private Server",
                style=discord.ButtonStyle.link,
                url=match["private_server_link"],
            )
            self.add_item(self.private_button)

    async def interaction_check(self, interaction):
        match = get_match(self.match_id)

        if not match:
            await interaction.response.send_message(
                "This match no longer exists.",
                ephemeral=True,
            )
            return False

        return True

    @discord.ui.button(
        label="Join Host",
        style=discord.ButtonStyle.success,
        custom_id="hosting:join",
    )
    async def join_match(self, interaction, button):
        match = get_match(self.match_id)

        if match["status"] != "open":
            await interaction.response.send_message(
                "This match is no longer open.",
                ephemeral=True,
            )
            return

        if not await require_verification(interaction):
            return

        players = get_players(match)

        if interaction.user.id in players:
            await interaction.response.send_message(
                "You are already in this match.",
                ephemeral=True,
            )
            return

        capacity = SLOT_COUNTS.get(match["mode"], 2)

        if len(players) >= capacity:
            await interaction.response.send_message(
                "This match is full.",
                ephemeral=True,
            )
            return

        players.append(interaction.user.id)
        save_players(self.match_id, players)

        await interaction.response.send_message(
            "You joined the match!",
            ephemeral=True,
        )
        await refresh_match(self.bot, self.match_id)

    @discord.ui.button(
        label="Leave Host",
        style=discord.ButtonStyle.secondary,
        custom_id="hosting:leave",
    )
    async def leave_match(self, interaction, button):
        match = get_match(self.match_id)

        if match["status"] != "open":
            await interaction.response.send_message(
                "This match is no longer open.",
                ephemeral=True,
            )
            return

        players = get_players(match)

        if interaction.user.id not in players:
            await interaction.response.send_message(
                "You are not in this match.",
                ephemeral=True,
            )
            return

        if interaction.user.id == match["host_id"]:
            await interaction.response.send_message(
                "The host cannot leave their own match. Cancel it instead.",
                ephemeral=True,
            )
            return

        players.remove(interaction.user.id)
        save_players(self.match_id, players)

        await interaction.response.send_message(
            "You left the match.",
            ephemeral=True,
        )
        await refresh_match(self.bot, self.match_id)

    @discord.ui.button(
        label="Post Results",
        style=discord.ButtonStyle.primary,
        custom_id="hosting:results",
    )
    async def post_results(self, interaction, button):
        match = get_match(self.match_id)

        if match["status"] != "open":
            await interaction.response.send_message(
                "Results can only be submitted for an open match.",
                ephemeral=True,
            )
            return

        if not can_manage_match(interaction, match):
            await interaction.response.send_message(
                "Only the host or authorized staff can submit results.",
                ephemeral=True,
            )
            return

        players = get_players(match)

        if len(players) != SLOT_COUNTS.get(match["mode"], 2):
            await interaction.response.send_message(
                "The match roster is incomplete.",
                ephemeral=True,
            )
            return

        pending_results[
            (interaction.channel_id, interaction.user.id)
        ] = self.match_id

        await interaction.response.send_message(
            "Upload the result screenshot and put every player on a "
            "separate line in the same message using this format:\n\n"
            "`@Player 32 winner`\n"
            "`@Player 25 loser`\n\n"
            "For team modes, mark every player on the winning team "
            "`winner` and every player on the losing team `loser`. "
            "The first half of the roster is Team Red and the second "
            "half is Team Blue.\n\n"
            "The screenshot and result lines must be in the same message.",
            ephemeral=True,
        )

    @discord.ui.button(
        label="Rehost",
        style=discord.ButtonStyle.secondary,
        custom_id="hosting:rehost",
    )
    async def rehost(self, interaction, button):
        match = get_match(self.match_id)

        if match["status"] != "completed":
            await interaction.response.send_message(
                "Only completed matches can be rehosted.",
                ephemeral=True,
            )
            return

        if not can_manage_match(interaction, match):
            await interaction.response.send_message(
                "Only the host or authorized staff can rehost.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "Choose how you want to rehost:",
            view=RehostView(self.bot, self.match_id),
            ephemeral=True,
        )

    async def cancel_host(self, interaction):
        match = get_match(self.match_id)

        if not match or match["status"] != "open":
            await interaction.response.send_message(
                "This match is not open.",
                ephemeral=True,
            )
            return

        if not can_manage_match(interaction, match):
            await interaction.response.send_message(
                "You cannot cancel this match.",
                ephemeral=True,
            )
            return

        update_match(self.match_id, status="cancelled")

        await interaction.response.send_message(
            "Match cancelled.",
            ephemeral=True,
        )
        await refresh_match(self.bot, self.match_id)


# ============================================================
# REHOST VIEW
# ============================================================

class RehostView(discord.ui.View):
    def __init__(self, bot, old_match_id):
        super().__init__(timeout=120)
        self.bot = bot
        self.old_match_id = old_match_id

    async def create_rehost(self, interaction, keep_players):
        old_match = get_match(self.old_match_id)

        if not old_match or old_match["status"] != "completed":
            await interaction.response.send_message(
                "The original match is no longer available to rehost.",
                ephemeral=True,
            )
            return

        if not can_manage_match(interaction, old_match):
            await interaction.response.send_message(
                "You cannot rehost this match.",
                ephemeral=True,
            )
            return

        if not await require_verification(interaction):
            return

        new_id = create_match(
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            host_id=interaction.user.id,
            mode=old_match["mode"],
            region=old_match["region"],
            private_server_link=old_match["private_server_link"],
        )

        if keep_players:
            players = get_players(old_match)
            players = players[:SLOT_COUNTS.get(old_match["mode"], 2)]
            save_players(new_id, players)

        new_match = get_match(new_id)
        message = await interaction.channel.send(
            embed=make_match_embed(new_match),
            view=MatchView(self.bot, new_id),
        )
        update_match(new_id, message_id=message.id)

        await interaction.response.send_message(
            f"New match created: #{new_id}",
            ephemeral=True,
        )

    @discord.ui.button(
        label="Same Players",
        style=discord.ButtonStyle.success,
    )
    async def same_players(self, interaction, button):
        await self.create_rehost(interaction, True)

    @discord.ui.button(
        label="Fresh Lobby",
        style=discord.ButtonStyle.primary,
    )
    async def fresh_lobby(self, interaction, button):
        await self.create_rehost(interaction, False)


# ============================================================
# BOT
# ============================================================

intents = discord.Intents.default()
intents.message_content = True
intents.members = True


class HostingBot(commands.Bot):
    async def setup_hook(self):
        init_db()

        with db_connect() as conn:
            rows = conn.execute(
                "SELECT id FROM matches"
            ).fetchall()

        for row in rows:
            self.add_view(MatchView(self, row["id"]))

        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()


bot = HostingBot(
    command_prefix="!",
    intents=intents,
)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")


# ============================================================
# RESULT SUBMISSION AND AUTOMATIC ELO
# ============================================================

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    key = (message.channel.id, message.author.id)
    match_id = pending_results.get(key)

    if match_id:
        match = get_match(match_id)

        if not match:
            pending_results.pop(key, None)
            await bot.process_commands(message)
            return

        if not can_manage_message(message.author, match):
            await message.reply(
                "Only the host or authorized staff can submit these results."
            )
            await bot.process_commands(message)
            return

        if match["status"] != "open":
            pending_results.pop(key, None)
            await message.reply("This match is no longer open.")
            await bot.process_commands(message)
            return

        screenshot = next(
            (
                attachment
                for attachment in message.attachments
                if attachment.content_type
                and attachment.content_type.startswith("image/")
            ),
            None,
        )

        if not screenshot:
            await message.reply(
                "Please attach the result screenshot and include the "
                "player result lines in the same message."
            )
            await bot.process_commands(message)
            return

        roster = get_players(match)

        try:
            parsed_players = parse_result_message(
                message.content,
                roster,
                match["mode"],
            )
        except ValueError as exc:
            await message.reply(
                f"Invalid results:\n{exc}\n\n"
                "Example:\n"
                "`@Player 32 winner`\n"
                "`@Player 25 loser`"
            )
            await bot.process_commands(message)
            return

        if match_id in processing_matches:
            await message.reply("These results are already being processed.")
            await bot.process_commands(message)
            return

        processing_matches.add(match_id)

        try:
            results_channel = find_results_channel(message.guild)

            if not results_channel:
                await message.reply(
                    "I couldn't find the match-results channel. "
                    "Set MATCH_RESULTS_CHANNEL_ID or create a channel "
                    "named `match-results`."
                )
                return

            # Send the evidence first. If it fails, Elo is not changed.
            results_message = await results_channel.send(
                content=(
                    f"Results for Match #{match_id}\n"
                    f"Host: <@{match['host_id']}>\n"
                    f"Reported by: {message.author.mention}\n"
                    f"Mode: {match['mode']}\n"
                    f"Region: {match['region']}"
                ),
                file=await screenshot.to_file(),
            )

            elo_mode = mode_to_elo_mode(match["mode"])

            try:
                changes = apply_elo_result(
                    hosting_match_id=match_id,
                    guild_id=match["guild_id"],
                    mode=elo_mode,
                    players=parsed_players,
                    reported_by=message.author.id,
                    screenshot_url=screenshot.url,
                )
            except Exception:
                try:
                    await results_message.delete()
                except discord.HTTPException:
                    pass
                raise

            winner_names = []
            loser_names = []

            for player in parsed_players:
                user_id = player["user_id"]
                change = changes[str(user_id)]
                line = (
                    f"<@{user_id}>: {change['old_elo']} "
                    f"→ {change['new_elo']} "
                    f"({change['change']:+d} Elo, "
                    f"{player['score']} points)"
                )

                if player["won"]:
                    winner_names.append(line)
                else:
                    loser_names.append(line)

            result_text = (
                "**Winners**\n"
                + "\n".join(winner_names)
                + "\n\n**Losers**\n"
                + "\n".join(loser_names)
            )

            await results_channel.send(
                content=result_text[:2000],
                reference=results_message,
                mention_author=False,
            )

            update_match(
                match_id,
                status="completed",
                result=(
                    f"Winner: {', '.join(
                        str(p['user_id'])
                        for p in parsed_players
                        if p['won']
                    )}"
                ),
            )

            pending_results.pop(key, None)

            await message.reply(
                f"Match #{match_id} completed! Elo has been updated "
                f"automatically for every player."
            )

            await refresh_match(bot, match_id)

            try:
                await message.delete()
            except discord.HTTPException:
                pass

        except ValueError as exc:
            await message.reply(str(exc))
        except Exception as exc:
            print(f"Result processing error for match #{match_id}: {exc}")
            await message.reply(
                "I couldn't finish processing these results. "
                "Check the bot logs and try again if Elo was not applied."
            )
        finally:
            processing_matches.discard(match_id)

    await bot.process_commands(message)


# ============================================================
# HOST COMMAND
# ============================================================

@bot.tree.command(
    name="host",
    description="Create an Arsenal match.",
)
@app_commands.choices(
    mode=[
        app_commands.Choice(name="1v1", value="1v1s"),
        app_commands.Choice(name="2v2", value="2v2s"),
        app_commands.Choice(name="3v3", value="3v3s"),
    ],
    region=[
        app_commands.Choice(name="North America", value="NA"),
        app_commands.Choice(name="Europe", value="EU"),
        app_commands.Choice(name="Oceania", value="OCE"),
        app_commands.Choice(name="Other", value="Other"),
    ],
)
async def host(
    interaction: discord.Interaction,
    mode: app_commands.Choice[str],
    region: app_commands.Choice[str],
    private_server_link: str = None,
):
    if not has_host_role(interaction.user):
        await interaction.response.send_message(
            "You need the host role to create matches.",
            ephemeral=True,
        )
        return

    if not interaction.guild or not isinstance(
        interaction.channel, discord.TextChannel
    ):
        await interaction.response.send_message(
            "Use this command in a server text channel.",
            ephemeral=True,
        )
        return

    if not await require_verification(interaction):
        return

    if private_server_link:
        if not private_server_link.startswith("https://"):
            await interaction.response.send_message(
                "The private server link must start with `https://`.",
                ephemeral=True,
            )
            return

        if "roblox.com" not in private_server_link.lower():
            await interaction.response.send_message(
                "Please provide a valid Roblox private server link.",
                ephemeral=True,
            )
            return

    match_id = create_match(
        guild_id=interaction.guild_id,
        channel_id=interaction.channel_id,
        host_id=interaction.user.id,
        mode=mode.value,
        region=region.value,
        private_server_link=private_server_link,
    )

    match = get_match(match_id)

    await interaction.response.send_message(
        content="Match created!",
        ephemeral=True,
    )

    message = await interaction.channel.send(
        content=(
            f"<@&{MATCH_ROLE_IDS[mode.value]}>"
            if MATCH_ROLE_IDS.get(mode.value)
            else None
        ),
        embed=make_match_embed(match),
        view=MatchView(bot, match_id),
        allowed_mentions=discord.AllowedMentions(roles=True),
    )

    update_match(match_id, message_id=message.id)


# ============================================================
# ELO COMMANDS
# ============================================================

@bot.tree.command(
    name="elo",
    description="Check a player's Elo rating.",
)
@app_commands.choices(
    mode=[
        app_commands.Choice(name="1v1", value="1v1"),
        app_commands.Choice(name="2v2", value="2v2"),
        app_commands.Choice(name="3v3", value="3v3"),
    ]
)
async def elo(
    interaction: discord.Interaction,
    mode: app_commands.Choice[str],
    player: discord.Member = None,
):
    if not interaction.guild:
        await interaction.response.send_message(
            "Use this command in a server.",
            ephemeral=True,
        )
        return

    target = player or interaction.user
    rating = get_rating(
        interaction.guild_id,
        target.id,
        mode.value,
    )

    embed = discord.Embed(
        title=f"{target.display_name}'s {mode.value} Elo",
        color=discord.Color.from_rgb(255, 170, 210),
    )
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name="Elo", value=str(rating["elo"]), inline=True)
    embed.add_field(name="Wins", value=str(rating["wins"]), inline=True)
    embed.add_field(name="Losses", value=str(rating["losses"]), inline=True)
    embed.add_field(
        name="Matches",
        value=str(rating["matches"]),
        inline=True,
    )

    await interaction.response.send_message(embed=embed)


@bot.tree.command(
    name="leaderboard",
    description="View Elo and hosting leaderboards.",
)
@app_commands.choices(
    category=[
        app_commands.Choice(name="Overall Elo", value="overall"),
        app_commands.Choice(name="Top Hosts", value="hosts"),
        app_commands.Choice(name="1v1 Elo", value="1v1"),
        app_commands.Choice(name="2v2 Elo", value="2v2"),
        app_commands.Choice(name="3v3 Elo", value="3v3"),
    ]
)
async def leaderboard(
    interaction: discord.Interaction,
    category: app_commands.Choice[str],
):
    if not interaction.guild:
        await interaction.response.send_message(
            "Use this command in a server.",
            ephemeral=True,
        )
        return

    guild_id = interaction.guild_id
    category_value = category.value

    with db_connect() as conn:
        if category_value == "hosts":
            rows = conn.execute("""
                SELECT host_id, COUNT(*) AS hosted
                FROM matches
                WHERE guild_id = ?
                  AND status = 'completed'
                GROUP BY host_id
                ORDER BY hosted DESC
                LIMIT 10
            """, (guild_id,)).fetchall()

            title = "Top Hosts"

            description = "\n".join(
                f"**{index}.** <@{row['host_id']}> — "
                f"{row['hosted']} completed matches"
                for index, row in enumerate(rows, start=1)
            ) or "No completed hosted matches yet."

        elif category_value == "overall":
            rows = conn.execute("""
                SELECT user_id, SUM(elo) AS total_elo,
                       SUM(wins) AS wins, SUM(losses) AS losses
                FROM ratings
                WHERE guild_id = ?
                GROUP BY user_id
                ORDER BY total_elo DESC
                LIMIT 10
            """, (guild_id,)).fetchall()

            title = "Overall Elo Leaderboard"

            description = "\n".join(
                f"**{index}.** <@{row['user_id']}> — "
                f"{row['total_elo']} combined Elo"
                for index, row in enumerate(rows, start=1)
            ) or "No ratings yet."

        else:
            rows = conn.execute("""
                SELECT user_id, elo, wins, losses, matches
                FROM ratings
                WHERE guild_id = ? AND mode = ?
                ORDER BY elo DESC, wins DESC
                LIMIT 10
            """, (guild_id, category_value)).fetchall()

            title = f"{category_value} Elo Leaderboard"

            description = "\n".join(
                f"**{index}.** <@{row['user_id']}> — "
                f"{row['elo']} Elo | "
                f"{row['wins']}W-{row['losses']}L"
                for index, row in enumerate(rows, start=1)
            ) or "No ratings yet."

    embed = discord.Embed(
        title=title,
        description=description,
        color=discord.Color.from_rgb(255, 170, 210),
    )

    await interaction.response.send_message(embed=embed)


@bot.tree.command(
    name="setplatform",
    description="Set your platform.",
)
@app_commands.choices(
    platform=[
        app_commands.Choice(name="PC", value="PC"),
        app_commands.Choice(name="Mobile", value="Mobile"),
        app_commands.Choice(name="Console", value="Console"),
    ]
)
async def setplatform(
    interaction: discord.Interaction,
    platform: app_commands.Choice[str],
):
    with db_connect() as conn:
        conn.execute("""
            INSERT INTO player_platforms (guild_id, user_id, platform)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id, user_id)
            DO UPDATE SET platform = excluded.platform
        """, (
            interaction.guild_id,
            interaction.user.id,
            platform.value,
        ))

    await interaction.response.send_message(
        f"Your platform is now set to {platform.value}.",
        ephemeral=True,
    )


@bot.tree.command(
    name="addelo",
    description="Add Elo to a player.",
)
@app_commands.choices(
    mode=[
        app_commands.Choice(name="1v1", value="1v1"),
        app_commands.Choice(name="2v2", value="2v2"),
        app_commands.Choice(name="3v3", value="3v3"),
    ]
)
async def addelo(
    interaction: discord.Interaction,
    player: discord.Member,
    mode: app_commands.Choice[str],
    amount: app_commands.Range[int, 1, 10000],
    reason: str,
):
    if not is_elo_manager(interaction):
        await interaction.response.send_message(
            "You need the Elo Manager role to use this command.",
            ephemeral=True,
        )
        return

    with db_connect() as conn:
        rating = ensure_rating(
            conn,
            interaction.guild_id,
            player.id,
            mode.value,
        )

        old_elo = rating["elo"]
        new_elo = old_elo + amount

        conn.execute("""
            UPDATE ratings
            SET elo = ?
            WHERE guild_id = ? AND user_id = ? AND mode = ?
        """, (
            new_elo,
            interaction.guild_id,
            player.id,
            mode.value,
        ))

        conn.execute("""
            INSERT INTO elo_adjustments (
                guild_id, user_id, mode, amount, old_elo, new_elo,
                reason, adjusted_by, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            interaction.guild_id,
            player.id,
            mode.value,
            amount,
            old_elo,
            new_elo,
            reason,
            interaction.user.id,
            datetime.now(timezone.utc).isoformat(),
        ))

    await interaction.response.send_message(
        f"Added {amount} Elo to {player.mention} in {mode.value}.\n"
        f"{old_elo} → {new_elo}\nReason: {reason}"
    )


@bot.tree.command(
    name="removeelo",
    description="Remove Elo from a player.",
)
@app_commands.choices(
    mode=[
        app_commands.Choice(name="1v1", value="1v1"),
        app_commands.Choice(name="2v2", value="2v2"),
        app_commands.Choice(name="3v3", value="3v3"),
    ]
)
async def removeelo(
    interaction: discord.Interaction,
    player: discord.Member,
    mode: app_commands.Choice[str],
    amount: app_commands.Range[int, 1, 10000],
    reason: str,
):
    if not is_elo_manager(interaction):
        await interaction.response.send_message(
            "You need the Elo Manager role to use this command.",
            ephemeral=True,
        )
        return

    with db_connect() as conn:
        rating = ensure_rating(
            conn,
            interaction.guild_id,
            player.id,
            mode.value,
        )

        old_elo = rating["elo"]
        new_elo = max(0, old_elo - amount)
        actual_change = new_elo - old_elo

        conn.execute("""
            UPDATE ratings
            SET elo = ?
            WHERE guild_id = ? AND user_id = ? AND mode = ?
        """, (
            new_elo,
            interaction.guild_id,
            player.id,
            mode.value,
        ))

        conn.execute("""
            INSERT INTO elo_adjustments (
                guild_id, user_id, mode, amount, old_elo, new_elo,
                reason, adjusted_by, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            interaction.guild_id,
            player.id,
            mode.value,
            actual_change,
            old_elo,
            new_elo,
            reason,
            interaction.user.id,
            datetime.now(timezone.utc).isoformat(),
        ))

    await interaction.response.send_message(
        f"Removed {abs(actual_change)} Elo from {player.mention} "
        f"in {mode.value}.\n"
        f"{old_elo} → {new_elo}\nReason: {reason}"
    )


@bot.tree.command(
    name="matchhistory",
    description="View a player's recent Elo matches.",
)
@app_commands.choices(
    mode=[
        app_commands.Choice(name="1v1", value="1v1"),
        app_commands.Choice(name="2v2", value="2v2"),
        app_commands.Choice(name="3v3", value="3v3"),
    ]
)
async def matchhistory(
    interaction: discord.Interaction,
    mode: app_commands.Choice[str],
    player: discord.Member = None,
):
    if not interaction.guild:
        await interaction.response.send_message(
            "Use this command in a server.",
            ephemeral=True,
        )
        return

    target = player or interaction.user

    with db_connect() as conn:
        rows = conn.execute("""
            SELECT *
            FROM elo_matches
            WHERE guild_id = ?
              AND mode = ?
              AND undone = 0
              AND (
                red_team LIKE ?
                OR blue_team LIKE ?
              )
            ORDER BY id DESC
            LIMIT 10
        """, (
            interaction.guild_id,
            mode.value,
            f'%{target.id}%',
            f'%{target.id}%',
        )).fetchall()

    lines = []

    for row in rows:
        changes = json.loads(row["elo_changes"])
        info = changes.get(str(target.id))

        if not info:
            continue

        outcome = "WIN" if info["won"] else "LOSS"

        lines.append(
            f"Match #{row['hosting_match_id']}: **{outcome}** — "
            f"{info['old_elo']} → {info['new_elo']} "
            f"({info['change']:+d} Elo)"
        )

    embed = discord.Embed(
        title=f"{target.display_name}'s {mode.value} Match History",
        description="\n".join(lines) or "No match history found.",
        color=discord.Color.from_rgb(255, 170, 210),
    )

    await interaction.response.send_message(embed=embed)


@bot.tree.command(
    name="undomatch",
    description="Undo Elo for a completed match.",
)
async def undomatch(
    interaction: discord.Interaction,
    match_id: app_commands.Range[int, 1, 2147483647],
):
    if not is_elo_manager(interaction) and not is_staff(interaction):
        await interaction.response.send_message(
            "You need Elo Manager or staff permissions to undo Elo.",
            ephemeral=True,
        )
        return

    match = get_match(match_id)

    if not match or match["guild_id"] != interaction.guild_id:
        await interaction.response.send_message(
            "That hosted match wasn't found in this server.",
            ephemeral=True,
        )
        return

    try:
        undo_elo_result(match_id, interaction.guild_id)
    except ValueError as exc:
        await interaction.response.send_message(
            str(exc),
            ephemeral=True,
        )
        return

    update_match(
        match_id,
        status="open",
        result="Elo result undone by staff.",
    )

    await refresh_match(bot, match_id)

    await interaction.response.send_message(
        f"Elo for Match #{match_id} has been undone. "
        "The original ratings and win/loss counts were restored."
    )


@bot.tree.command(
    name="elohelp",
    description="Learn how the Elo system works.",
)
async def elohelp(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Arsenal Angels Elo System",
        color=discord.Color.from_rgb(255, 170, 210),
        description=(
            "Everyone starts at 1,000 Elo in each game mode.\n\n"
            "**Automatic Elo**\n"
            "• Winning increases your Elo.\n"
            "• Losing decreases your Elo.\n"
            "• Beating higher-rated teams earns more Elo.\n"
            "• Losing to lower-rated teams costs more Elo.\n"
            "• Team ratings use the average Elo of the players.\n"
            "• Player points provide a small performance adjustment.\n"
            "• Equal-rated teams change by approximately 16 Elo.\n\n"
            "**Commands**\n"
            "• `/elo` — Check a player's rating.\n"
            "• `/leaderboard` — View Elo or host rankings.\n"
            "• `/matchhistory` — View recent match results.\n"
            "• `/setplatform` — Set your platform.\n"
            "• `/addelo` — Elo Manager only.\n"
            "• `/removeelo` — Elo Manager only.\n"
            "• `/undomatch` — Undo a match result.\n\n"
            "**Submitting results**\n"
            "Upload a screenshot and include every roster player's "
            "Discord mention, points, and winner/loser status in the "
            "same message. Elo is applied automatically."
        ),
    )

    await interaction.response.send_message(embed=embed)


# ============================================================
# HOSTING HELP
# ============================================================

@bot.tree.command(
    name="hostinghelp",
    description="Learn how to use the hosting bot.",
)
async def hostinghelp(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Arsenal Angels Hosting Help",
        color=discord.Color.from_rgb(255, 170, 210),
        description=(
            "• `/host` — Create a match.\n"
            "• Join Host — Join an open match.\n"
            "• Leave Host — Leave a match.\n"
            "• Post Results — Submit the screenshot and score lines.\n"
            "• Rehost — Create another match after a completed match.\n"
            "• Cancel Host — Cancel an open match.\n\n"
            "Match results automatically update Elo for all players."
        ),
    )

    await interaction.response.send_message(embed=embed)


# ============================================================
# START
# ============================================================

if not TOKEN:
    raise RuntimeError(
        "Set DISCORD_TOKEN in your FadeHost environment variables."
    )

bot.run(TOKEN)
