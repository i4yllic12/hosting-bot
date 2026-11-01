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
from discord.ext import commands, tasks


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
                status TEXT NOT NULL DEFAULT 'open',
                roster TEXT NOT NULL,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                last_reminder_at TEXT
            )
        """)

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
                hosting_match_id INTEGER UNIQUE NOT NULL,
                guild_id INTEGER NOT NULL,
                mode TEXT NOT NULL,
                result_data TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS elo_adjustments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                mode TEXT NOT NULL,
                amount INTEGER NOT NULL,
                reason TEXT,
                staff_id INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        # Add the reminder column to older databases if it is missing.
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(matches)").fetchall()
        }

        if "last_reminder_at" not in columns:
            conn.execute(
                "ALTER TABLE matches ADD COLUMN last_reminder_at TEXT"
            )


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def mode_to_elo_mode(mode):
    return mode[:-1] if mode.endswith("s") else mode


def ensure_rating(conn, guild_id, user_id, mode):
    conn.execute("""
        INSERT OR IGNORE INTO ratings
        (guild_id, user_id, mode, elo, wins, losses, matches)
        VALUES (?, ?, ?, ?, 0, 0, 0)
    """, (guild_id, user_id, mode, STARTING_ELO))

    return conn.execute("""
        SELECT *
        FROM ratings
        WHERE guild_id = ? AND user_id = ? AND mode = ?
    """, (guild_id, user_id, mode)).fetchone()


def get_rating(guild_id, user_id, mode):
    with db_connect() as conn:
        row = ensure_rating(conn, guild_id, user_id, mode)
        return dict(row)


def get_team_average(players, guild_id, mode):
    if not players:
        return STARTING_ELO

    values = [
        get_rating(guild_id, player["user_id"], mode)["elo"]
        for player in players
    ]

    return sum(values) / len(values)


# ============================================================
# ELO CALCULATIONS
# ============================================================

def expected_score(player_elo, opponent_elo):
    return 1 / (
        1 + 10 ** ((opponent_elo - player_elo) / 400)
    )


def calculate_team_elo_change(team_elo, opponent_elo, won):
    expected = expected_score(team_elo, opponent_elo)
    actual = 1.0 if won else 0.0
    return K_FACTOR * (actual - expected)


def calculate_individual_changes(players, scores, guild_id, mode):
    """
    Weighted ELO system:
    70% match result
    20% individual performance
    10% opponent difficulty

    Team averages are used to calculate the expected match result.
    """

    red_players = [p for p in players if p["team"] == "red"]
    blue_players = [p for p in players if p["team"] == "blue"]

    red_average = get_team_average(red_players, guild_id, mode)
    blue_average = get_team_average(blue_players, guild_id, mode)

    red_score = scores["red"]
    blue_score = scores["blue"]

    if red_score == blue_score:
        raise ValueError("Tied match scores are not supported.")

    red_won = red_score > blue_score

    red_points = [int(p["score"]) for p in red_players]
    blue_points = [int(p["score"]) for p in blue_players]

    red_mean = sum(red_points) / max(1, len(red_points))
    blue_mean = sum(blue_points) / max(1, len(blue_points))

    changes = {}

    for player in players:
        is_red = player["team"] == "red"
        team_average = red_average if is_red else blue_average
        opponent_average = blue_average if is_red else red_average
        won = red_won if is_red else not red_won

        expected = expected_score(team_average, opponent_average)
        actual = 1.0 if won else 0.0

        # 70%: match result.
        result_part = 0.70 * K_FACTOR * (actual - expected)

        # 20%: individual performance compared with teammates.
        team_mean = red_mean if is_red else blue_mean
        performance = (
            int(player["score"]) - team_mean
        ) / max(10.0, team_mean)

        performance = max(-1.0, min(1.0, performance))
        performance_part = 0.20 * K_FACTOR * performance

        # 10%: difficulty of the opposing team's rating.
        difficulty = (
            opponent_average - team_average
        ) / 400.0

        difficulty = max(-1.0, min(1.0, difficulty))
        difficulty_part = 0.10 * K_FACTOR * difficulty

        delta = round(
            result_part + performance_part + difficulty_part
        )

        # Keep the direction consistent with the match outcome.
        if won:
            delta = max(1, delta)
        else:
            delta = min(-1, delta)

        changes[player["user_id"]] = {
            "delta": delta,
            "won": won,
            "score": int(player["score"]),
            "team": player["team"],
        }

    return changes


def apply_elo_result(
    hosting_match_id,
    guild_id,
    mode,
    players,
    scores,
):
    changes = calculate_individual_changes(
        players,
        scores,
        guild_id,
        mode,
    )

    old_values = {}

    with db_connect() as conn:
        existing = conn.execute("""
            SELECT id
            FROM elo_matches
            WHERE hosting_match_id = ?
        """, (hosting_match_id,)).fetchone()

        if existing:
            raise ValueError("ELO has already been applied to this match.")

        for user_id, result in changes.items():
            row = ensure_rating(conn, guild_id, user_id, mode)
            old_values[str(user_id)] = dict(row)

            conn.execute("""
                UPDATE ratings
                SET elo = elo + ?,
                    wins = wins + ?,
                    losses = losses + ?,
                    matches = matches + 1
                WHERE guild_id = ? AND user_id = ? AND mode = ?
            """, (
                result["delta"],
                1 if result["won"] else 0,
                0 if result["won"] else 1,
                guild_id,
                user_id,
                mode,
            ))

        result_data = {
            "old_values": old_values,
            "changes": {
                str(user_id): result
                for user_id, result in changes.items()
            },
            "scores": scores,
        }

        conn.execute("""
            INSERT INTO elo_matches
            (hosting_match_id, guild_id, mode, result_data, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (
            hosting_match_id,
            guild_id,
            mode,
            json.dumps(result_data),
            now_iso(),
        ))

    return changes# ============================================================
# DISCORD BOT SETUP
# ============================================================

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.messages = True
intents.message_content = True


class HostingBot(commands.Bot):
    async def setup_hook(self):
        init_db()

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


# ============================================================
# PERMISSIONS AND MEMBER HELPERS
# ============================================================

def has_role(member, role_id):
    if not role_id:
        return False

    return any(role.id == role_id for role in member.roles)


def is_staff(member):
    return (
        member.guild_permissions.administrator
        or has_role(member, STAFF_ROLE_ID)
    )


def can_manage_elo(member):
    return (
        member.guild_permissions.administrator
        or has_role(member, STAFF_ROLE_ID)
        or has_role(member, ELO_MANAGER_ROLE_ID)
    )


def can_host(member):
    return (
        member.guild_permissions.administrator
        or has_role(member, HOST_ROLE_ID)
        or is_staff(member)
    )


def is_verified(member):
    if not BLOXLINK_VERIFIED_ROLE_ID:
        return True

    return has_role(member, BLOXLINK_VERIFIED_ROLE_ID)


async def require_verified(interaction):
    if not is_verified(interaction.user):
        await interaction.response.send_message(
            "You must be verified before using this feature.",
            ephemeral=True,
        )
        return False

    return True


async def require_host(interaction):
    if not can_host(interaction.user):
        await interaction.response.send_message(
            "You do not have permission to host matches.",
            ephemeral=True,
        )
        return False

    return True


async def require_staff(interaction):
    if not is_staff(interaction.user):
        await interaction.response.send_message(
            "This command is restricted to tournament staff.",
            ephemeral=True,
        )
        return False

    return True


def get_match(match_id):
    with db_connect() as conn:
        row = conn.execute(
            "SELECT * FROM matches WHERE id = ?",
            (match_id,),
        ).fetchone()

    return dict(row) if row else None


def save_roster(match_id, roster):
    with db_connect() as conn:
        conn.execute("""
            UPDATE matches
            SET roster = ?
            WHERE id = ?
        """, (json.dumps(roster), match_id))

def get_roster(match):
    if not match:
        return []

    roster_data = match.get("roster", "[]")

    try:
        return json.loads(roster_data)
    except (TypeError, json.JSONDecodeError):
        return []


def get_mode_slots(mode):
    return SLOT_COUNTS.get(mode, 0)


def match_is_open(match):
    return bool(match and match["status"] == "open")


def roster_is_full(match):
    if not match:
        return False

    roster = get_roster(match)
    required_slots = get_mode_slots(match.get("mode", ""))

    if required_slots == 0:
        return False

    return len(roster) >= required_slots


def member_in_roster(roster, user_id):
    return any(
        int(player["user_id"]) == int(user_id)
        for player in roster
    )


def format_roster(roster):
    if not roster:
        return "No players have joined yet."

    lines = []

    for index, player in enumerate(roster, start=1):
        lines.append(
            f"`{index}.` <@{player['user_id']}>"
        )

    return "\n".join(lines)


def get_match_role_mention(mode):
    role_id = MATCH_ROLE_IDS.get(mode)

    if not role_id:
        return ""

    return f"<@&{role_id}>"


# ============================================================
# MATCH DATABASE HELPERS
# ============================================================

def create_match(
    guild_id,
    channel_id,
    host_id,
    mode,
    region,
):
    created_at = now_iso()

    roster = [{
        "user_id": int(host_id),
    }]

    with db_connect() as conn:
        cursor = conn.execute("""
            INSERT INTO matches (
                guild_id,
                channel_id,
                host_id,
                mode,
                region,
                status,
                roster,
                created_at,
                last_reminder_at
            )
            VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?)
        """, (
            guild_id,
            channel_id,
            host_id,
            mode,
            region,
            json.dumps(roster),
            created_at,
            created_at,
        ))

        return cursor.lastrowid


def update_match_status(match_id, status):
    with db_connect() as conn:
        conn.execute("""
            UPDATE matches
            SET status = ?
            WHERE id = ?
        """, (status, match_id))


def update_match_message(match_id, message_id):
    with db_connect() as conn:
        conn.execute("""
            UPDATE matches
            SET message_id = ?
            WHERE id = ?
        """, (message_id, match_id))


def get_open_matches():
    with db_connect() as conn:
        rows = conn.execute("""
            SELECT *
            FROM matches
            WHERE status = 'open'
        """).fetchall()

    return [dict(row) for row in rows]


# ============================================================
# RESULT PARSING
# ============================================================

def parse_result_message(content, roster, mode):
    """
    Expected format:

    <@123456789> red 32
    <@987654321> blue 25

    Every player must be listed exactly once.
    The bot calculates the team totals and determines the winner.
    """

    if not roster:
        raise ValueError("This match has no players.")

    expected_players = {
        int(player["user_id"])
        for player in roster
    }

    entries = {}
    lines = [
        line.strip()
        for line in content.splitlines()
        if line.strip()
    ]

    pattern = re.compile(
        r"^<@!?(\d+)>\s+(red|blue)\s+(\d+)$",
        re.IGNORECASE,
    )

    for line in lines:
        match = pattern.match(line)

        if not match:
            continue

        user_id = int(match.group(1))
        team = match.group(2).lower()
        score = int(match.group(3))

        if user_id not in expected_players:
            raise ValueError(
                f"<@{user_id}> is not part of this match."
            )

        if user_id in entries:
            raise ValueError(
                f"<@{user_id}> was entered more than once."
            )

        entries[user_id] = {
            "user_id": user_id,
            "team": team,
            "score": score,
        }

    missing_players = expected_players - set(entries)

    if missing_players:
        missing = ", ".join(
            f"<@{user_id}>"
            for user_id in sorted(missing_players)
        )

        raise ValueError(
            f"Missing players: {missing}"
        )

    red_players = [
        player for player in entries.values()
        if player["team"] == "red"
    ]

    blue_players = [
        player for player in entries.values()
        if player["team"] == "blue"
    ]

    required_team_size = SLOT_COUNTS[mode] // 2

    if len(red_players) != required_team_size:
        raise ValueError(
            f"Red team must have {required_team_size} players."
        )

    if len(blue_players) != required_team_size:
        raise ValueError(
            f"Blue team must have {required_team_size} players."
        )

    red_total = sum(
        player["score"]
        for player in red_players
    )

    blue_total = sum(
        player["score"]
        for player in blue_players
    )

    if red_total == blue_total:
        raise ValueError(
            "The team scores are tied. Please contact staff."
        )

    scores = {
        "red": red_total,
        "blue": blue_total,
    }

    players = list(entries.values())

    winner = "red" if red_total > blue_total else "blue"

    return {
        "players": players,
        "scores": scores,
        "winner": winner,
        "red_players": red_players,
        "blue_players": blue_players,
    }


# ============================================================
# MATCH EMBED
# ============================================================

def build_match_embed(match):
    roster = get_roster(match)
    slots = get_mode_slots(match["mode"])

    embed = discord.Embed(
        title="꒰ა・ARSENAL ANGELS HOST",
        color=discord.Color.from_rgb(255, 160, 200),
    )

    embed.add_field(
        name="Mode",
        value=match["mode"].upper(),
        inline=True,
    )

    embed.add_field(
        name="Region",
        value=match["region"],
        inline=True,
    )

    embed.add_field(
        name="Host",
        value=f"<@{match['host_id']}>",
        inline=True,
    )

    embed.add_field(
        name=f"Players ({len(roster)}/{slots})",
        value=format_roster(roster),
        inline=False,
    )

    embed.add_field(
        name="Status",
        value=match["status"].upper(),
        inline=True,
    )

    embed.set_footer(
        text=f"Match ID: {match['id']}"
    )

    return embed


# ============================================================
# RESULT MESSAGE FORMAT
# ============================================================

def result_instructions(match):
    roster = get_roster(match)

    player_lines = "\n".join(
        f"<@{player['user_id']}> red 0"
        for player in roster
    )

    return (
        "꒰ა・**SUBMIT MATCH RESULTS**\n\n"
        "Attach your match screenshot and list every player "
        "with their team and individual points.\n\n"
        "**Format:**\n"
        "`@player red points`\n"
        "`@player blue points`\n\n"
        "**Example:**\n"
        "```text\n"
        f"{player_lines}\n"
        "```\n"
        "Use `red` or `blue` for each player's team. "
        "The bot adds the points together to determine the winner.\n\n"
        "Do not label players as winners or losers."
    )


# ============================================================
# MATCH VALIDATION
# ============================================================

def validate_match_mode(mode):
    if mode not in MODES:
        raise ValueError("Invalid match mode.")

    return mode


def validate_region(region):
    region = region.strip().upper()

    if region not in REGIONS:
        raise ValueError(
            "Region must be NA, EU, OCE, or Other."
        )

    return region


def can_edit_match(member, match):
    return (
        int(member.id) == int(match["host_id"])
        or is_staff(member)
    )


def validate_open_match(match):
    if not match:
        raise ValueError("Match not found.")

    if match["status"] != "open":
        raise ValueError("This match is no longer open.")

    return True# ============================================================
# MATCH VIEW
# ============================================================

class MatchView(discord.ui.View):
    def __init__(self, match_id):
        super().__init__(timeout=None)
        self.match_id = match_id

    async def refresh_message(self, interaction):
        match = get_match(self.match_id)

        if not match:
            return

        try:
            await interaction.message.edit(
                embed=build_match_embed(match),
                view=self,
            )
        except (discord.NotFound, discord.HTTPException):
            pass

    @discord.ui.button(
        label="Join Host",
        style=discord.ButtonStyle.success,
        custom_id="hosting:join",
    )
    async def join_button(self, interaction, button):
        match = get_match(self.match_id)

        if not match or not match_is_open(match):
            return await interaction.response.send_message(
                "This host is no longer open.",
                ephemeral=True,
            )

        if not await require_verified(interaction):
            return

        roster = get_roster(match)

        if member_in_roster(roster, interaction.user.id):
            return await interaction.response.send_message(
                "You are already in this host.",
                ephemeral=True,
            )

        if len(roster) >= get_mode_slots(match["mode"]):
            return await interaction.response.send_message(
                "This host is full.",
                ephemeral=True,
            )

        roster.append({
            "user_id": interaction.user.id,
        })

        save_roster(self.match_id, roster)

        await interaction.response.send_message(
            "You joined the host.",
            ephemeral=True,
        )

        await self.refresh_message(interaction)

    @discord.ui.button(
        label="Leave Host",
        style=discord.ButtonStyle.secondary,
        custom_id="hosting:leave",
    )
    async def leave_button(self, interaction, button):
        match = get_match(self.match_id)

        if not match or not match_is_open(match):
            return await interaction.response.send_message(
                "This host is no longer open.",
                ephemeral=True,
            )

        roster = get_roster(match)

        if interaction.user.id == int(match["host_id"]):
            return await interaction.response.send_message(
                "The host cannot leave their own lobby. "
                "Use Cancel Host instead.",
                ephemeral=True,
            )

        if not member_in_roster(roster, interaction.user.id):
            return await interaction.response.send_message(
                "You are not in this host.",
                ephemeral=True,
            )

        roster = [
            player for player in roster
            if int(player["user_id"]) != interaction.user.id
        ]

        save_roster(self.match_id, roster)

        await interaction.response.send_message(
            "You left the host.",
            ephemeral=True,
        )

        await self.refresh_message(interaction)

    @discord.ui.button(
        label="Post Results",
        style=discord.ButtonStyle.primary,
        custom_id="hosting:results",
    )
    async def results_button(self, interaction, button):
        match = get_match(self.match_id)

        if not match or not match_is_open(match):
            return await interaction.response.send_message(
                "This match is no longer open.",
                ephemeral=True,
            )

        if not can_edit_match(interaction.user, match):
            return await interaction.response.send_message(
                "Only the host or staff can submit results.",
                ephemeral=True,
            )

        if not roster_is_full(match):
            return await interaction.response.send_message(
                "The lobby must be full before submitting results.",
                ephemeral=True,
            )

        pending_results[
            (interaction.channel_id, interaction.user.id)
        ] = self.match_id

        await interaction.response.send_message(
            result_instructions(match),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Rehost",
        style=discord.ButtonStyle.secondary,
        custom_id="hosting:rehost",
    )
    async def rehost_button(self, interaction, button):
        match = get_match(self.match_id)

        if not match or not match_is_open(match):
            return await interaction.response.send_message(
                "This match cannot be rehosted.",
                ephemeral=True,
            )

        if not can_edit_match(interaction.user, match):
            return await interaction.response.send_message(
                "Only the host or staff can rehost.",
                ephemeral=True,
            )

        new_id = create_match(
            guild_id=match["guild_id"],
            channel_id=match["channel_id"],
            host_id=interaction.user.id,
            mode=match["mode"],
            region=match["region"],
        )

        new_match = get_match(new_id)
        view = MatchView(new_id)

        message = await interaction.channel.send(
            content=get_match_role_mention(match["mode"]),
            embed=build_match_embed(new_match),
            view=view,
        )

        update_match_message(new_id, message.id)

        await interaction.response.send_message(
            f"New host created. Match ID: `{new_id}`",
            ephemeral=True,
        )

    @discord.ui.button(
        label="Cancel Host",
        style=discord.ButtonStyle.danger,
        custom_id="hosting:cancel",
    )
    async def cancel_button(self, interaction, button):
        match = get_match(self.match_id)

        if not match or not match_is_open(match):
            return await interaction.response.send_message(
                "This host is already closed.",
                ephemeral=True,
            )

        if not can_edit_match(interaction.user, match):
            return await interaction.response.send_message(
                "Only the host or staff can cancel this host.",
                ephemeral=True,
            )

        update_match_status(self.match_id, "cancelled")

        await interaction.response.send_message(
            "Host cancelled.",
            ephemeral=True,
        )

        closed_match = get_match(self.match_id)

        try:
            await interaction.message.edit(
                embed=build_match_embed(closed_match),
                view=None,
            )
        except discord.HTTPException:
            pass


# ============================================================
# HOST COMMAND
# ============================================================

@bot.tree.command(
    name="host",
    description="Create an Arsenal Angels match host.",
)
@app_commands.describe(
    mode="Choose the match size.",
    region="Choose the region.",
)
@app_commands.choices(
    mode=[
        app_commands.Choice(name="1v1", value="1v1s"),
        app_commands.Choice(name="2v2", value="2v2s"),
        app_commands.Choice(name="3v3", value="3v3s"),
    ],
    region=[
        app_commands.Choice(name="NA", value="NA"),
        app_commands.Choice(name="EU", value="EU"),
        app_commands.Choice(name="OCE", value="OCE"),
        app_commands.Choice(name="Other", value="Other"),
    ],
)
async def host_command(
    interaction: discord.Interaction,
    mode: app_commands.Choice[str],
    region: app_commands.Choice[str],
):
    if not await require_verified(interaction):
        return

    if not await require_host(interaction):
        return

    match_id = create_match(
        guild_id=interaction.guild_id,
        channel_id=interaction.channel_id,
        host_id=interaction.user.id,
        mode=mode.value,
        region=region.value,
    )

    match = get_match(match_id)
    view = MatchView(match_id)

    await interaction.response.send_message(
        content=get_match_role_mention(mode.value),
        embed=build_match_embed(match),
        view=view,
    )

    message = await interaction.original_response()
    update_match_message(match_id, message.id)

@app_commands.describe(
    mode="Choose the match size.",
    region="Choose the region.",
    private_server_link="Required Roblox private-server link.",
)
async def host_command(
    interaction: discord.Interaction,
    mode: app_commands.Choice[str],
    region: app_commands.Choice[str],
    private_server_link: str,
):
   if not valid_private_server_link(private_server_link):
    return await interaction.response.send_message(
        "Please provide a valid Roblox private-server link.",
        ephemeral=True,
    )

# ============================================================
# KICK PLAYER
# ============================================================

@bot.tree.command(
    name="kickplayer",
    description="Remove a player from an open host.",
)
@app_commands.describe(
    match_id="The match ID.",
    player="The player to remove.",
)
async def kickplayer_command(
    interaction: discord.Interaction,
    match_id: int,
    player: discord.Member,
):
    match = get_match(match_id)

    if not match or not match_is_open(match):
        return await interaction.response.send_message(
            "That match is not open.",
            ephemeral=True,
        )

    if not can_edit_match(interaction.user, match):
        return await interaction.response.send_message(
            "Only the host or staff can kick players.",
            ephemeral=True,
        )

    if player.id == int(match["host_id"]):
        return await interaction.response.send_message(
            "The host cannot be kicked from their own lobby.",
            ephemeral=True,
        )

    roster = get_roster(match)

    if not member_in_roster(roster, player.id):
        return await interaction.response.send_message(
            "That player is not in this lobby.",
            ephemeral=True,
        )

    roster = [
        entry for entry in roster
        if int(entry["user_id"]) != player.id
    ]

    save_roster(match_id, roster)

    await interaction.response.send_message(
        f"Removed {player.mention} from match `{match_id}`.",
    )

    await refresh_match_post(match_id)


# ============================================================
# SUBSTITUTE PLAYER
# ============================================================

@bot.tree.command(
    name="subplayer",
    description="Replace a player in an open host.",
)
@app_commands.describe(
    match_id="The match ID.",
    outgoing="The player being replaced.",
    incoming="The substitute player.",
)
async def subplayer_command(
    interaction: discord.Interaction,
    match_id: int,
    outgoing: discord.Member,
    incoming: discord.Member,
):
    match = get_match(match_id)

    if not match or not match_is_open(match):
        return await interaction.response.send_message(
            "That match is not open.",
            ephemeral=True,
        )

    if not can_edit_match(interaction.user, match):
        return await interaction.response.send_message(
            "Only the host or staff can substitute players.",
            ephemeral=True,
        )

    if outgoing.id == int(match["host_id"]):
        return await interaction.response.send_message(
            "The host cannot be substituted.",
            ephemeral=True,
        )

    if incoming.id == int(match["host_id"]):
        return await interaction.response.send_message(
            "The host is already in the lobby.",
            ephemeral=True,
        )

    if not is_verified(incoming):
        return await interaction.response.send_message(
            "The substitute must be verified.",
            ephemeral=True,
        )

    roster = get_roster(match)

    if not member_in_roster(roster, outgoing.id):
        return await interaction.response.send_message(
            "The outgoing player is not in this lobby.",
            ephemeral=True,
        )

    if member_in_roster(roster, incoming.id):
        return await interaction.response.send_message(
            "The substitute is already in this lobby.",
            ephemeral=True,
        )

    for entry in roster:
        if int(entry["user_id"]) == outgoing.id:
            entry["user_id"] = incoming.id
            break

    save_roster(match_id, roster)

    await interaction.response.send_message(
        f"{outgoing.mention} was replaced by {incoming.mention}.",
    )

    await refresh_match_post(match_id)


# ============================================================
# REFRESH MATCH POST
# ============================================================

async def refresh_match_post(match_id):
    match = get_match(match_id)

    if not match or not match.get("message_id"):
        return

    channel = bot.get_channel(int(match["channel_id"]))

    if channel is None:
        try:
            channel = await bot.fetch_channel(
                int(match["channel_id"])
            )
        except discord.HTTPException:
            return

    try:
        message = await channel.fetch_message(
            int(match["message_id"])
        )

        view = MatchView(match_id) if match_is_open(match) else None

        await message.edit(
            embed=build_match_embed(match),
            view=view,
        )
    except discord.HTTPException:
        pass


# ============================================================
# MATCH RESULT SUBMISSION
# ============================================================

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    match_id = pending_results.get(
        (message.channel.id, message.author.id)
    )

    if match_id is None:
        await bot.process_commands(message)
        return

    match = get_match(match_id)

    if not match or not match_is_open(match):
        pending_results.pop(
            (message.channel.id, message.author.id),
            None,
        )
        return await message.reply(
            "This match is no longer open."
        )

    if not can_edit_match(message.author, match):
        return await message.reply(
            "Only the host or staff can submit results."
        )

    if not message.attachments:
        return await message.reply(
            "Please attach the match screenshot with your result."
        )

    image_attachment = next(
        (
            attachment
            for attachment in message.attachments
            if attachment.content_type
            and attachment.content_type.startswith("image/")
        ),
        None,
    )

    if image_attachment is None:
        return await message.reply(
            "The attachment must be an image screenshot."
        )

    if match_id in processing_matches:
        return await message.reply(
            "This result is already being processed."
        )

    processing_matches.add(match_id)

    try:
        parsed = parse_result_message(
            message.content,
            get_roster(match),
            match["mode"],
        )

        changes = apply_elo_result(
            hosting_match_id=match_id,
            guild_id=match["guild_id"],
            mode=mode_to_elo_mode(match["mode"]),
            players=parsed["players"],
            scores=parsed["scores"],
        )

        result_channel = bot.get_channel(
            MATCH_RESULTS_CHANNEL_ID
        )

        if result_channel is None and MATCH_RESULTS_CHANNEL_ID:
            try:
                result_channel = await bot.fetch_channel(
                    MATCH_RESULTS_CHANNEL_ID
                )
            except discord.HTTPException:
                result_channel = None

        winner = parsed["winner"]

        embed = discord.Embed(
            title="꒰ა・MATCH RESULT",
            color=discord.Color.from_rgb(255, 160, 200),
        )

        embed.add_field(
            name="Red Team",
            value=f"**{parsed['scores']['red']} points**",
            inline=True,
        )

        embed.add_field(
            name="Blue Team",
            value=f"**{parsed['scores']['blue']} points**",
            inline=True,
        )

        embed.add_field(
            name="Winner",
            value=winner.title(),
            inline=False,
        )

        change_lines = []

        for user_id, result in changes.items():
            sign = "+" if result["delta"] >= 0 else ""

            change_lines.append(
                f"<@{user_id}>: {sign}{result['delta']} ELO"
            )

        embed.add_field(
            name="ELO Changes",
            value="\n".join(change_lines),
            inline=False,
        )

        embed.set_image(url=image_attachment.url)

        if result_channel:
            await result_channel.send(embed=embed)

        update_match_status(match_id, "completed")

        pending_results.pop(
            (message.channel.id, message.author.id),
            None,
        )

        await message.reply(
            "Results submitted successfully. ELO has been updated."
        )

        await refresh_match_post(match_id)

    except ValueError as error:
        await message.reply(
            f"Could not process results: {error}"
        )

    except Exception:
        await message.reply(
            "An unexpected error occurred while processing results. "
            "Please contact staff."
        )
        raise

    finally:
        processing_matches.discard(match_id)

    await bot.process_commands(message)# ============================================================
# ELO COMMANDS
# ============================================================

@bot.tree.command(
    name="elo",
    description="Check your ELO rating.",
)
@app_commands.describe(
    mode="Choose the ELO category.",
    member="Optional player to check.",
)
@app_commands.choices(
    mode=[
        app_commands.Choice(name="1v1", value="1v1"),
        app_commands.Choice(name="2v2", value="2v2"),
        app_commands.Choice(name="3v3", value="3v3"),
    ],
)
async def elo_command(
    interaction: discord.Interaction,
    mode: app_commands.Choice[str],
    member: discord.Member = None,
):
    if not await require_verified(interaction):
        return

    target = member or interaction.user
    rating = get_rating(
        interaction.guild_id,
        target.id,
        mode.value,
    )

    embed = discord.Embed(
        title="꒰ა・PLAYER ELO",
        color=discord.Color.from_rgb(255, 160, 200),
    )

    embed.set_author(
        name=target.display_name,
        icon_url=target.display_avatar.url,
    )

    embed.add_field(
        name="Mode",
        value=mode.value.upper(),
        inline=True,
    )

    embed.add_field(
        name="ELO",
        value=str(rating["elo"]),
        inline=True,
    )

    embed.add_field(
        name="Record",
        value=(
            f"Wins: {rating['wins']}\n"
            f"Losses: {rating['losses']}\n"
            f"Matches: {rating['matches']}"
        ),
        inline=False,
    )

    await interaction.response.send_message(embed=embed)


# ============================================================
# LEADERBOARD
# ============================================================

@bot.tree.command(
    name="leaderboard",
    description="View the ELO leaderboard.",
)
@app_commands.describe(
    mode="Choose the ELO category.",
)
@app_commands.choices(
    mode=[
        app_commands.Choice(name="1v1", value="1v1"),
        app_commands.Choice(name="2v2", value="2v2"),
        app_commands.Choice(name="3v3", value="3v3"),
    ],
)
async def leaderboard_command(
    interaction: discord.Interaction,
    mode: app_commands.Choice[str],
):
    if not await require_verified(interaction):
        return

    with db_connect() as conn:
        rows = conn.execute("""
            SELECT user_id, elo, wins, losses, matches
            FROM ratings
            WHERE guild_id = ? AND mode = ?
            ORDER BY elo DESC
            LIMIT 10
        """, (
            interaction.guild_id,
            mode.value,
        )).fetchall()

    if not rows:
        return await interaction.response.send_message(
            "There are no ratings for this mode yet.",
            ephemeral=True,
        )

    lines = []

    for position, row in enumerate(rows, start=1):
        lines.append(
            f"`{position}.` <@{row['user_id']}> — "
            f"**{row['elo']} ELO** "
            f"({row['wins']}W / {row['losses']}L)"
        )

    embed = discord.Embed(
        title=f"꒰ა・{mode.value.upper()} LEADERBOARD",
        description="\n".join(lines),
        color=discord.Color.from_rgb(255, 160, 200),
    )

    await interaction.response.send_message(embed=embed)


# ============================================================
# ADD ELO
# ============================================================

@bot.tree.command(
    name="addelo",
    description="Manually add ELO to a player.",
)
@app_commands.describe(
    member="Player receiving ELO.",
    mode="ELO category.",
    amount="Amount to add.",
    reason="Reason for the adjustment.",
)
@app_commands.choices(
    mode=[
        app_commands.Choice(name="1v1", value="1v1"),
        app_commands.Choice(name="2v2", value="2v2"),
        app_commands.Choice(name="3v3", value="3v3"),
    ],
)
async def addelo_command(
    interaction: discord.Interaction,
    member: discord.Member,
    mode: app_commands.Choice[str],
    amount: app_commands.Range[int, 1, 10000],
    reason: str = "Staff adjustment",
):
    if not can_manage_elo(interaction.user):
        return await interaction.response.send_message(
            "You do not have permission to manage ELO.",
            ephemeral=True,
        )

    with db_connect() as conn:
        ensure_rating(
            conn,
            interaction.guild_id,
            member.id,
            mode.value,
        )

        conn.execute("""
            UPDATE ratings
            SET elo = elo + ?
            WHERE guild_id = ? AND user_id = ? AND mode = ?
        """, (
            amount,
            interaction.guild_id,
            member.id,
            mode.value,
        ))

        conn.execute("""
            INSERT INTO elo_adjustments (
                guild_id,
                user_id,
                mode,
                amount,
                reason,
                staff_id,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            interaction.guild_id,
            member.id,
            mode.value,
            amount,
            reason,
            interaction.user.id,
            now_iso(),
        ))

    await interaction.response.send_message(
        f"Added **{amount} ELO** to {member.mention} "
        f"in {mode.value.upper()}.\nReason: {reason}"
    )


# ============================================================
# REMOVE ELO
# ============================================================

@bot.tree.command(
    name="removeelo",
    description="Manually remove ELO from a player.",
)
@app_commands.describe(
    member="Player losing ELO.",
    mode="ELO category.",
    amount="Amount to remove.",
    reason="Reason for the adjustment.",
)
@app_commands.choices(
    mode=[
        app_commands.Choice(name="1v1", value="1v1"),
        app_commands.Choice(name="2v2", value="2v2"),
        app_commands.Choice(name="3v3", value="3v3"),
    ],
)
async def removeelo_command(
    interaction: discord.Interaction,
    member: discord.Member,
    mode: app_commands.Choice[str],
    amount: app_commands.Range[int, 1, 10000],
    reason: str = "Staff adjustment",
):
    if not can_manage_elo(interaction.user):
        return await interaction.response.send_message(
            "You do not have permission to manage ELO.",
            ephemeral=True,
        )

    with db_connect() as conn:
        rating = ensure_rating(
            conn,
            interaction.guild_id,
            member.id,
            mode.value,
        )

        new_elo = max(0, rating["elo"] - amount)
        actual_removed = rating["elo"] - new_elo

        conn.execute("""
            UPDATE ratings
            SET elo = ?
            WHERE guild_id = ? AND user_id = ? AND mode = ?
        """, (
            new_elo,
            interaction.guild_id,
            member.id,
            mode.value,
        ))

        conn.execute("""
            INSERT INTO elo_adjustments (
                guild_id,
                user_id,
                mode,
                amount,
                reason,
                staff_id,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            interaction.guild_id,
            member.id,
            mode.value,
            -actual_removed,
            reason,
            interaction.user.id,
            now_iso(),
        ))

    await interaction.response.send_message(
        f"Removed **{actual_removed} ELO** from {member.mention} "
        f"in {mode.value.upper()}.\nReason: {reason}"
    )


# ============================================================
# MATCH HISTORY
# ============================================================

@bot.tree.command(
    name="matchhistory",
    description="View a player's recent ELO matches.",
)
@app_commands.describe(
    member="Player to check. Leave blank for yourself.",
    mode="ELO category.",
)
@app_commands.choices(
    mode=[
        app_commands.Choice(name="1v1", value="1v1"),
        app_commands.Choice(name="2v2", value="2v2"),
        app_commands.Choice(name="3v3", value="3v3"),
    ],
)
async def matchhistory_command(
    interaction: discord.Interaction,
    mode: app_commands.Choice[str],
    member: discord.Member = None,
):
    if not await require_verified(interaction):
        return

    target = member or interaction.user

    with db_connect() as conn:
        rows = conn.execute("""
            SELECT result_data, created_at
            FROM elo_matches
            WHERE guild_id = ? AND mode = ?
            ORDER BY id DESC
            LIMIT 50
        """, (
            interaction.guild_id,
            mode.value,
        )).fetchall()

    history = []

    for row in rows:
        data = json.loads(row["result_data"])
        result = data.get("changes", {}).get(str(target.id))

        if result:
            outcome = "WIN" if result["won"] else "LOSS"
            delta = result["delta"]
            sign = "+" if delta >= 0 else ""

            history.append(
                f"**{outcome}** — {sign}{delta} ELO "
                f"({row['created_at'][:10]})"
            )

    if not history:
        return await interaction.response.send_message(
            "No match history found.",
            ephemeral=True,
        )

    embed = discord.Embed(
        title=f"꒰ა・{target.display_name}'s {mode.value.upper()} HISTORY",
        description="\n".join(history[:10]),
        color=discord.Color.from_rgb(255, 160, 200),
    )

    await interaction.response.send_message(embed=embed)


# ============================================================
# UNDO MATCH
# ============================================================

def undo_elo_result(hosting_match_id, guild_id):
    with db_connect() as conn:
        row = conn.execute("""
            SELECT *
            FROM elo_matches
            WHERE hosting_match_id = ? AND guild_id = ?
        """, (
            hosting_match_id,
            guild_id,
        )).fetchone()

        if not row:
            raise ValueError("No ELO result exists for that match.")

        data = json.loads(row["result_data"])
        old_values = data["old_values"]

        for user_id, old in old_values.items():
            conn.execute("""
                UPDATE ratings
                SET elo = ?,
                    wins = ?,
                    losses = ?,
                    matches = ?
                WHERE guild_id = ? AND user_id = ? AND mode = ?
            """, (
                old["elo"],
                old["wins"],
                old["losses"],
                old["matches"],
                guild_id,
                int(user_id),
                row["mode"],
            ))

        conn.execute("""
            DELETE FROM elo_matches
            WHERE hosting_match_id = ? AND guild_id = ?
        """, (
            hosting_match_id,
            guild_id,
        ))


@bot.tree.command(
    name="undomatch",
    description="Undo a match's ELO result.",
)
@app_commands.describe(
    match_id="The hosting match ID.",
)
async def undomatch_command(
    interaction: discord.Interaction,
    match_id: int,
):
    if not is_staff(interaction.user):
        return await interaction.response.send_message(
            "Only tournament staff can undo match results.",
            ephemeral=True,
        )

    match = get_match(match_id)

    if not match:
        return await interaction.response.send_message(
            "Match not found.",
            ephemeral=True,
        )

    try:
        undo_elo_result(
            match_id,
            interaction.guild_id,
        )

        update_match_status(match_id, "open")

        await interaction.response.send_message(
            f"ELO for match `{match_id}` has been undone. "
            "The match is marked open again."
        )

    except ValueError as error:
        await interaction.response.send_message(
            str(error),
            ephemeral=True,
        )


# ============================================================
# HELP COMMANDS
# ============================================================

@bot.tree.command(
    name="elohelp",
    description="View ELO commands and rules.",
)
async def elohelp_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="꒰ა・ARSENAL ANGELS ELO HELP",
        color=discord.Color.from_rgb(255, 160, 200),
    )

    embed.description = (
        "**Player commands**\n"
        "`/elo` — View a player's rating.\n"
        "`/leaderboard` — View the top players.\n"
        "`/matchhistory` — View recent results.\n\n"
        "**Staff commands**\n"
        "`/addelo` — Add ELO manually.\n"
        "`/removeelo` — Remove ELO manually.\n"
        "`/undomatch` — Undo a match result.\n\n"
        "**ELO calculation**\n"
        "70% match result\n"
        "20% individual performance\n"
        "10% opponent difficulty\n\n"
        "Separate ratings are maintained for 1v1, 2v2, and 3v3."
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True,
    )


@bot.tree.command(
    name="hostinghelp",
    description="View hosting commands and result instructions.",
)
async def hostinghelp_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="꒰ა・HOSTING HELP",
        color=discord.Color.from_rgb(255, 160, 200),
    )

    embed.description = (
        "**Host commands**\n"
        "`/host` — Create a lobby.\n"
        "`/kickplayer` — Remove a player.\n"
        "`/subplayer` — Replace a player.\n\n"
        "**Lobby buttons**\n"
        "Join Host — Join the lobby.\n"
        "Leave Host — Leave the lobby.\n"
        "Post Results — Submit the match result.\n"
        "Rehost — Create another lobby.\n"
        "Cancel Host — Close the lobby.\n\n"
        "**Submitting results**\n"
        "Attach a screenshot and list every player:\n"
        "```text\n"
        "@player red 32\n"
        "@player blue 25\n"
        "```\n"
        "The bot totals the teams and determines the winner."
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True,
    )


# ============================================================
# AUTOMATIC 10-MINUTE LOBBY REMINDERS
# ============================================================

@tasks.loop(minutes=1)
async def lobby_reminders():
    matches = get_open_matches()
    current_time = datetime.now(timezone.utc)

    for match in matches:
        if roster_is_full(match):
            continue

        try:
            last_reminder = datetime.fromisoformat(
                match["last_reminder_at"]
            )

            if last_reminder.tzinfo is None:
                last_reminder = last_reminder.replace(
                    tzinfo=timezone.utc
                )

        except (TypeError, ValueError):
            last_reminder = datetime.fromisoformat(
                match["created_at"]
            )

            if last_reminder.tzinfo is None:
                last_reminder = last_reminder.replace(
                    tzinfo=timezone.utc
                )

        elapsed = (
            current_time - last_reminder
        ).total_seconds()

        if elapsed < 600:
            continue

        channel = bot.get_channel(
            int(match["channel_id"])
        )

        if channel is None:
            try:
                channel = await bot.fetch_channel(
                    int(match["channel_id"])
                )
            except discord.HTTPException:
                continue

        role_mention = get_match_role_mention(
            match["mode"]
        )

        try:
            await channel.send(
                f"{role_mention} Match `{match['id']}` "
                f"is still waiting for players. "
                f"({len(get_roster(match))}/"
                f"{get_mode_slots(match['mode'])})"
            )

            with db_connect() as conn:
                conn.execute("""
                    UPDATE matches
                    SET last_reminder_at = ?
                    WHERE id = ?
                """, (
                    now_iso(),
                    match["id"],
                ))

        except discord.HTTPException:
            continue


@lobby_reminders.before_loop
async def before_lobby_reminders():
    await bot.wait_until_ready()


# ============================================================
# BOT EVENTS
# ============================================================

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}.")

    if not lobby_reminders.is_running():
        lobby_reminders.start()


@bot.event
async def on_member_join(member):
    # No automatic verification is granted here.
    # Your server's verification system should assign the verified role.
    return


# ============================================================
# START BOT
# ============================================================

if not TOKEN:
    raise RuntimeError(
        "Missing DISCORD_TOKEN or DISCORD_BOT_TOKEN environment variable."
    )

bot.run(TOKEN)
