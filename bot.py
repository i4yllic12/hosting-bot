import os
import re
import json
import sqlite3
import logging

from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs

import discord
from discord import app_commands
from discord.ext import commands, tasks


# ============================================================
# CONFIGURATION
# ============================================================

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0") or "0")

HOSTING_CHANNEL_ID = 1565049435606622348

HOST_ROLE_ID = 1565048538042208466
STAFF_ROLE_ID = 1564857946599456878
ELO_MANAGER_ROLE_ID = 1565158910292398184

RESULTS_CHANNEL_ID = int(
    os.getenv("MATCH_RESULTS_CHANNEL_ID", "0") or "0"
)

VERIFIED_ROLE_ID = int(
    os.getenv("BLOXLINK_VERIFIED_ROLE_ID", "0") or "0"
)

DB_PATH = os.getenv("DB_PATH", "./data/hosting.sqlite3")

STARTING_ELO = 1000
K_FACTOR = 32

SLOTS = {
    "1v1s": 2,
    "2v2s": 4,
    "3v3s": 6,
}

MATCH_ROLE_IDS = {
    "1v1s": 1564896767257084036,
    "2v2s": 1564896807673270382,
    "3v3s": 1564896826723672135,
}

ELO_CHOICES = [
    app_commands.Choice(name="1v1", value="1v1"),
    app_commands.Choice(name="2v2", value="2v2"),
    app_commands.Choice(name="3v3", value="3v3"),
]

pending_results = {}

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("arsenal_angels")


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


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def table_columns(conn, table):
    return {
        row["name"]
        for row in conn.execute(
            f"PRAGMA table_info({table})"
        ).fetchall()
    }


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
                roster TEXT NOT NULL DEFAULT '[]',
                private_server_link TEXT,
                created_at TEXT NOT NULL,
                last_reminder_at TEXT
            )
        """)

        columns = table_columns(conn, "matches")

        migrations = {
            "message_id": "INTEGER",
            "roster": "TEXT NOT NULL DEFAULT '[]'",
            "private_server_link": "TEXT",
            "last_reminder_at": "TEXT",
            "region": "TEXT NOT NULL DEFAULT 'Other'",
            "status": "TEXT NOT NULL DEFAULT 'open'",
        }

        for column, definition in migrations.items():
            if column not in columns:
                conn.execute(
                    f"ALTER TABLE matches ADD COLUMN "
                    f"{column} {definition}"
                )

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
            CREATE TABLE IF NOT EXISTS elo_results (
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

        conn.commit()


# ============================================================
# PERMISSIONS
# ============================================================

def has_role(member, role_id):
    return any(
        role.id == role_id
        for role in getattr(member, "roles", [])
    )


def is_staff(member):
    return (
        member.guild_permissions.administrator
        or has_role(member, STAFF_ROLE_ID)
    )


def can_manage_elo(member):
    return (
        is_staff(member)
        or has_role(member, ELO_MANAGER_ROLE_ID)
    )


def can_host(member):
    return (
        is_staff(member)
        or has_role(member, HOST_ROLE_ID)
    )


def is_verified(member):
    return (
        not VERIFIED_ROLE_ID
        or has_role(member, VERIFIED_ROLE_ID)
    )


def can_edit_match(member, match):
    return (
        member.id == int(match["host_id"])
        or is_staff(member)
    )


async def require_verified(interaction):
    if not is_verified(interaction.user):
        await interaction.response.send_message(
            "You must be verified before using this command.",
            ephemeral=True,
        )
        return False

    return True


# ============================================================
# MATCH DATABASE FUNCTIONS
# ============================================================

def create_match(
    guild_id,
    host_id,
    mode,
    region,
    private_server_link,
    initial_roster=None,
):
    created = now_iso()

    if initial_roster is None:
        roster = [{"user_id": int(host_id)}]
    else:
        roster = [
            {"user_id": int(player["user_id"])}
            for player in initial_roster
        ]

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
                private_server_link,
                created_at,
                last_reminder_at
            )
            VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)
        """, (
            guild_id,
            HOSTING_CHANNEL_ID,
            host_id,
            mode,
            region,
            json.dumps(roster),
            private_server_link,
            created,
            created,
        ))

        return cursor.lastrowid


def get_match(match_id):
    with db_connect() as conn:
        row = conn.execute(
            "SELECT * FROM matches WHERE id = ?",
            (match_id,),
        ).fetchone()

    return dict(row) if row else None


def get_open_matches():
    with db_connect() as conn:
        rows = conn.execute("""
            SELECT * FROM matches
            WHERE status = 'open'
        """).fetchall()

    return [dict(row) for row in rows]


def save_roster(match_id, roster):
    with db_connect() as conn:
        conn.execute(
            "UPDATE matches SET roster = ? WHERE id = ?",
            (json.dumps(roster), match_id),
        )


def update_match_status(match_id, status):
    with db_connect() as conn:
        conn.execute(
            "UPDATE matches SET status = ? WHERE id = ?",
            (status, match_id),
        )


def update_message_id(match_id, message_id):
    with db_connect() as conn:
        conn.execute(
            "UPDATE matches SET message_id = ? WHERE id = ?",
            (message_id, match_id),
        )


def reset_reminder(match_id):
    with db_connect() as conn:
        conn.execute(
            "UPDATE matches SET last_reminder_at = ? WHERE id = ?",
            (now_iso(), match_id),
        )


def get_roster(match):
    try:
        return json.loads(match.get("roster") or "[]")
    except (TypeError, json.JSONDecodeError):
        return []


def is_open(match):
    return bool(match and match["status"] == "open")


def is_full(match):
    return (
        len(get_roster(match))
        >= SLOTS.get(match["mode"], 0)
    )


def player_in_roster(roster, user_id):
    return any(
        int(player["user_id"]) == int(user_id)
        for player in roster
    )


# ============================================================
# ROLE PINGS
# ============================================================

def role_mention(mode):
    role_id = MATCH_ROLE_IDS.get(mode)
    return f"<@&{role_id}>" if role_id else ""


async def get_hosting_channel():
    channel = bot.get_channel(HOSTING_CHANNEL_ID)

    if channel is None:
        channel = await bot.fetch_channel(
            HOSTING_CHANNEL_ID
        )

    return channel


async def announce_open_slot(match_id, reason):
    match = get_match(match_id)

    if not is_open(match) or is_full(match):
        return

    try:
        channel = await get_hosting_channel()

        count = len(get_roster(match))
        needed = SLOTS[match["mode"]] - count

        await channel.send(
            f"{role_mention(match['mode'])} "
            f"**{reason}!**\n"
            f"Match `{match_id}` needs **{needed} more player(s)** "
            f"({count}/{SLOTS[match['mode']]}).",
            allowed_mentions=discord.AllowedMentions(
                roles=True,
                users=False,
                everyone=False,
            ),
        )

        reset_reminder(match_id)

    except discord.HTTPException:
        log.exception("Could not send open-slot ping.")


# ============================================================
# PRIVATE SERVER LINK VALIDATION
# ============================================================

def valid_private_server_link(link):
    if not link:
        return False

    try:
        parsed = urlparse(link.strip())

        if parsed.scheme != "https":
            return False

        if parsed.hostname not in (
            "www.roblox.com",
            "roblox.com",
        ):
            return False

        query = parse_qs(parsed.query)

        return (
            bool(query.get("privateServerLinkCode"))
            or (
                query.get("type", [""])[0].lower() == "server"
                and bool(query.get("code"))
            )
        )

    except (ValueError, AttributeError):
        return False


# ============================================================
# MATCH EMBEDS
# ============================================================

def make_embed(match):
    roster = get_roster(match)

    player_list = "\n".join(
        f"`{index}.` <@{player['user_id']}>"
        for index, player in enumerate(roster, 1)
    )

    if not player_list:
        player_list = "No players yet."

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
        name=f"Players ({len(roster)}/{SLOTS[match['mode']]})",
        value=player_list,
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


async def refresh_match_post(match_id):
    match = get_match(match_id)

    if not match or not match.get("message_id"):
        return

    try:
        channel = await get_hosting_channel()

        message = await channel.fetch_message(
            int(match["message_id"])
        )

        await message.edit(
            embed=make_embed(match),
            view=MatchView(
                match_id,
                match.get("private_server_link"),
            ),
        )

    except discord.HTTPException:
        log.exception("Could not refresh match post.")


# ============================================================
# SUBS DROPDOWN
# ============================================================

class SubsSelect(discord.ui.Select):

    def __init__(self, match_id, players):
        self.match_id = match_id

        options = []

        for player in players:
            user_id = int(player["user_id"])

            options.append(
                discord.SelectOption(
                    label=f"Player {user_id}",
                    description=f"Remove player ID {user_id} from this lobby.",
                    value=str(user_id),
                )
            )

        super().__init__(
            placeholder="Choose a player to remove...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction):
        match = get_match(self.match_id)

        if not is_open(match):
            return await interaction.response.send_message(
                "This lobby is no longer open.",
                ephemeral=True,
            )

        if not can_edit_match(interaction.user, match):
            return await interaction.response.send_message(
                "Only the host or staff can remove players.",
                ephemeral=True,
            )

        user_id = int(self.values[0])
        roster = get_roster(match)

        if user_id == int(match["host_id"]):
            return await interaction.response.send_message(
                "The host cannot be removed.",
                ephemeral=True,
            )

        if not player_in_roster(roster, user_id):
            return await interaction.response.send_message(
                "That player is no longer in the lobby.",
                ephemeral=True,
            )

        new_roster = [
            player for player in roster
            if int(player["user_id"]) != user_id
        ]

        save_roster(self.match_id, new_roster)

        await interaction.response.send_message(
            f"<@{user_id}> was removed from Match `{self.match_id}`.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

        await refresh_match_post(self.match_id)

        await announce_open_slot(
            self.match_id,
            "A player was removed from the lobby",
        )


class SubsView(discord.ui.View):

    def __init__(self, match_id, players):
        super().__init__(timeout=60)
        self.add_item(
            SubsSelect(match_id, players)
        )


# ============================================================
# REHOST DROPDOWN
# ============================================================

class RehostSelect(discord.ui.Select):

    def __init__(self, match_id):
        self.match_id = match_id

        options = [
            discord.SelectOption(
                label="Same Players",
                description="Create a new lobby with the previous players.",
                value="same",
                emoji="👥",
            ),
            discord.SelectOption(
                label="Fresh Lobby",
                description="Create a new lobby with only the host.",
                value="fresh",
                emoji="✨",
            ),
        ]

        super().__init__(
            placeholder="Choose how you want to rehost...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction):
        match = get_match(self.match_id)

        if not match:
            return await interaction.response.send_message(
                "This match could not be found.",
                ephemeral=True,
            )

        if not can_edit_match(interaction.user, match):
            return await interaction.response.send_message(
                "Only the original host or staff can rehost.",
                ephemeral=True,
            )

        choice = self.values[0]

        if choice == "same":
            roster = get_roster(match)
            new_host_id = int(match["host_id"])

            if not roster:
                roster = [{"user_id": new_host_id}]

        else:
            new_host_id = interaction.user.id
            roster = [{"user_id": new_host_id}]

        new_id = create_match(
            guild_id=match["guild_id"],
            host_id=new_host_id,
            mode=match["mode"],
            region=match["region"],
            private_server_link=match["private_server_link"],
            initial_roster=roster,
        )

        new_match = get_match(new_id)

        try:
            channel = await get_hosting_channel()

            message = await channel.send(
                content=role_mention(new_match["mode"]),
                embed=make_embed(new_match),
                view=MatchView(
                    new_id,
                    new_match["private_server_link"],
                ),
                allowed_mentions=discord.AllowedMentions(
                    roles=True,
                    users=False,
                    everyone=False,
                ),
            )

            update_message_id(new_id, message.id)

        except discord.HTTPException:
            update_match_status(new_id, "cancelled")

            return await interaction.response.send_message(
                "I could not create the new lobby. Check my channel permissions.",
                ephemeral=True,
            )

        await interaction.response.send_message(
            f"New lobby created! Match ID: `{new_id}`",
            ephemeral=True,
        )


class RehostView(discord.ui.View):

    def __init__(self, match_id):
        super().__init__(timeout=120)
        self.add_item(RehostSelect(match_id))


# ============================================================
# MATCH VIEW
# ============================================================

class MatchView(discord.ui.View):

    def __init__(self, match_id, private_server_link):
        super().__init__(timeout=None)

        self.match_id = match_id
        match = get_match(match_id)

        # Open-match buttons.
        if match and is_open(match):
            join_button = discord.ui.Button(
                label="Join Host",
                style=discord.ButtonStyle.success,
                custom_id=f"aa:join:{match_id}",
            )
            join_button.callback = self.join_callback
            self.add_item(join_button)

            leave_button = discord.ui.Button(
                label="Leave Host",
                style=discord.ButtonStyle.secondary,
                custom_id=f"aa:leave:{match_id}",
            )
            leave_button.callback = self.leave_callback
            self.add_item(leave_button)

            subs_button = discord.ui.Button(
                label="Subs",
                style=discord.ButtonStyle.primary,
                custom_id=f"aa:subs:{match_id}",
            )
            subs_button.callback = self.subs_callback
            self.add_item(subs_button)

            results_button = discord.ui.Button(
                label="Post Results",
                style=discord.ButtonStyle.primary,
                custom_id=f"aa:results:{match_id}",
            )
            results_button.callback = self.results_callback
            self.add_item(results_button)

        # Rehost remains available even after completion/cancellation.
        rehost_button = discord.ui.Button(
            label="Rehost",
            style=discord.ButtonStyle.secondary,
            custom_id=f"aa:rehost:{match_id}",
        )
        rehost_button.callback = self.rehost_callback
        self.add_item(rehost_button)

        if private_server_link:
            self.add_item(
                discord.ui.Button(
                    label="Join Private Server",
                    style=discord.ButtonStyle.link,
                    url=private_server_link,
                )
            )

    async def join_callback(self, interaction):
        match = get_match(self.match_id)

        if not is_open(match):
            return await interaction.response.send_message(
                "This host is closed.",
                ephemeral=True,
            )

        if not await require_verified(interaction):
            return

        roster = get_roster(match)

        if player_in_roster(roster, interaction.user.id):
            return await interaction.response.send_message(
                "You are already in this lobby.",
                ephemeral=True,
            )

        if is_full(match):
            return await interaction.response.send_message(
                "This lobby is full.",
                ephemeral=True,
            )

        roster.append({"user_id": interaction.user.id})
        save_roster(self.match_id, roster)
        reset_reminder(self.match_id)

        await interaction.response.send_message(
            "You joined the lobby.",
            ephemeral=True,
        )

        await refresh_match_post(self.match_id)

    async def leave_callback(self, interaction):
        match = get_match(self.match_id)

        if not is_open(match):
            return await interaction.response.send_message(
                "This host is closed.",
                ephemeral=True,
            )

        if interaction.user.id == int(match["host_id"]):
            return await interaction.response.send_message(
                "The host cannot leave their own lobby. Cancel it instead.",
                ephemeral=True,
            )

        roster = get_roster(match)

        if not player_in_roster(roster, interaction.user.id):
            return await interaction.response.send_message(
                "You are not in this lobby.",
                ephemeral=True,
            )

        roster = [
            player for player in roster
            if int(player["user_id"]) != interaction.user.id
        ]

        save_roster(self.match_id, roster)

        await interaction.response.send_message(
            "You left the lobby.",
            ephemeral=True,
        )

        await refresh_match_post(self.match_id)

        await announce_open_slot(
            self.match_id,
            "A player left the lobby",
        )

    async def subs_callback(self, interaction):
        match = get_match(self.match_id)

        if not is_open(match):
            return await interaction.response.send_message(
                "This lobby is closed.",
                ephemeral=True,
            )

        if not can_edit_match(interaction.user, match):
            return await interaction.response.send_message(
                "Only the host or staff can use Subs.",
                ephemeral=True,
            )

        roster = get_roster(match)

        removable_players = [
            player for player in roster
            if int(player["user_id"]) != int(match["host_id"])
        ]

        if not removable_players:
            return await interaction.response.send_message(
                "There are no players available to remove.",
                ephemeral=True,
            )

        view = SubsView(
            self.match_id,
            removable_players,
        )

        await interaction.response.send_message(
            "Select the player you want to remove:",
            view=view,
            ephemeral=True,
        )

    async def results_callback(self, interaction):
        match = get_match(self.match_id)

        if not is_open(match):
            return await interaction.response.send_message(
                "This match is closed.",
                ephemeral=True,
            )

        if not can_edit_match(interaction.user, match):
            return await interaction.response.send_message(
                "Only the host or staff can submit results.",
                ephemeral=True,
            )

        if not is_full(match):
            return await interaction.response.send_message(
                "The lobby must be full first.",
                ephemeral=True,
            )

        pending_results[
            (interaction.channel_id, interaction.user.id)
        ] = self.match_id

        await interaction.response.send_message(
            "Attach your match screenshot and list every player's "
            "team and points in this format:\n\n"
            "```text\n"
            "<@123456789> red 32\n"
            "<@987654321> blue 25\n"
            "```\n"
            "Use red or blue for the team. Send the screenshot "
            "and player list in the same message.",
            ephemeral=True,
        )

    async def rehost_callback(self, interaction):
        match = get_match(self.match_id)

        if not match:
            return await interaction.response.send_message(
                "This match could not be found.",
                ephemeral=True,
            )

        if not can_edit_match(interaction.user, match):
            return await interaction.response.send_message(
                "Only the original host or staff can rehost.",
                ephemeral=True,
            )

        await interaction.response.send_message(
            "Choose how you want to rehost:",
            view=RehostView(self.match_id),
            ephemeral=True,
        )

    async def cancel_callback(self, interaction):
        match = get_match(self.match_id)

        if not is_open(match):
            return await interaction.response.send_message(
                "This lobby is already closed.",
                ephemeral=True,
            )

        if not can_edit_match(interaction.user, match):
            return await interaction.response.send_message(
                "Only the host or staff can cancel this lobby.",
                ephemeral=True,
            )

        update_match_status(self.match_id, "cancelled")

        await interaction.response.send_message(
            "Lobby cancelled.",
            ephemeral=True,
        )

        await refresh_match_post(self.match_id)


# ============================================================
# BOT SETUP
# ============================================================

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.messages = True
intents.message_content = True


class ArsenalAngelsBot(commands.Bot):

    async def setup_hook(self):
        init_db()

        with db_connect() as conn:
            rows = conn.execute("""
                SELECT id, message_id, private_server_link
                FROM matches
                WHERE message_id IS NOT NULL
            """).fetchall()

        for row in rows:
            self.add_view(
                MatchView(
                    row["id"],
                    row["private_server_link"],
                ),
                message_id=int(row["message_id"]),
            )

        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()


bot = ArsenalAngelsBot(
    command_prefix="!",
    intents=intents,
)


# ============================================================
# HOST COMMAND
# ============================================================

@bot.tree.command(
    name="host",
    description="Create an Arsenal Angels match.",
)
@app_commands.describe(
    mode="Match size.",
    region="Match region.",
    private_server_link="Required Roblox private-server link.",
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
    private_server_link: str,
):
    if not await require_verified(interaction):
        return

    if not can_host(interaction.user):
        return await interaction.response.send_message(
            "You do not have permission to host.",
            ephemeral=True,
        )

    private_server_link = private_server_link.strip()

    if not valid_private_server_link(private_server_link):
        return await interaction.response.send_message(
            "You must provide a valid Roblox private-server link.",
            ephemeral=True,
        )

    try:
        channel = await get_hosting_channel()
    except discord.HTTPException:
        return await interaction.response.send_message(
            "I cannot access the hosting channel.",
            ephemeral=True,
        )

    match_id = create_match(
        interaction.guild_id,
        interaction.user.id,
        mode.value,
        region.value,
        private_server_link,
    )

    match = get_match(match_id)

    try:
        message = await channel.send(
            content=role_mention(mode.value),
            embed=make_embed(match),
            view=MatchView(
                match_id,
                private_server_link,
            ),
            allowed_mentions=discord.AllowedMentions(
                roles=True,
                users=False,
                everyone=False,
            ),
        )

        update_message_id(match_id, message.id)

    except discord.HTTPException:
        update_match_status(match_id, "cancelled")

        return await interaction.response.send_message(
            "Could not post the lobby. Check the bot's channel permissions.",
            ephemeral=True,
        )

    await interaction.response.send_message(
        f"Your lobby was posted in {channel.mention}. "
        f"Match ID: `{match_id}`",
        ephemeral=True,
    )


# ============================================================
# CANCEL COMMAND
# ============================================================

@bot.tree.command(
    name="cancel",
    description="Cancel an open match by match ID.",
)
@app_commands.describe(match_id="The match ID to cancel.")
async def cancel_command(
    interaction: discord.Interaction,
    match_id: int,
):
    match = get_match(match_id)

    if not is_open(match):
        return await interaction.response.send_message(
            "Match not found or already closed.",
            ephemeral=True,
        )

    if not can_edit_match(interaction.user, match):
        return await interaction.response.send_message(
            "Only the host or staff can cancel this match.",
            ephemeral=True,
        )

    update_match_status(match_id, "cancelled")
    await refresh_match_post(match_id)

    await interaction.response.send_message(
        f"Match `{match_id}` cancelled.",
        ephemeral=True,
    )


# ============================================================
# ELO FUNCTIONS
# ============================================================

def ensure_rating(conn, guild_id, user_id, mode):
    conn.execute("""
        INSERT OR IGNORE INTO ratings
        (guild_id, user_id, mode, elo, wins, losses, matches)
        VALUES (?, ?, ?, ?, 0, 0, 0)
    """, (
        guild_id,
        user_id,
        mode,
        STARTING_ELO,
    ))

    return conn.execute("""
        SELECT * FROM ratings
        WHERE guild_id = ? AND user_id = ? AND mode = ?
    """, (
        guild_id,
        user_id,
        mode,
    )).fetchone()


def get_rating(guild_id, user_id, mode):
    with db_connect() as conn:
        row = ensure_rating(
            conn,
            guild_id,
            user_id,
            mode,
        )
        return dict(row)


def expected_score(player_elo, opponent_elo):
    return 1 / (
        1 + 10 ** (
            (opponent_elo - player_elo) / 400
        )
    )


def calculate_changes(players, scores, guild_id, mode):
    red = [p for p in players if p["team"] == "red"]
    blue = [p for p in players if p["team"] == "blue"]

    if not red or not blue:
        raise ValueError("Both teams must have players.")

    def average(team):
        return sum(
            get_rating(
                guild_id,
                p["user_id"],
                mode,
            )["elo"]
            for p in team
        ) / len(team)

    red_average = average(red)
    blue_average = average(blue)

    red_score_average = sum(
        p["score"] for p in red
    ) / len(red)

    blue_score_average = sum(
        p["score"] for p in blue
    ) / len(blue)

    red_won = scores["red"] > scores["blue"]

    changes = {}

    for player in players:
        is_red = player["team"] == "red"

        team_elo = red_average if is_red else blue_average
        opponent_elo = blue_average if is_red else red_average

        won = red_won if is_red else not red_won

        team_average_score = (
            red_score_average if is_red
            else blue_score_average
        )

        actual = 1 if won else 0

        result_component = (
            0.70 * K_FACTOR
            * (
                actual
                - expected_score(team_elo, opponent_elo)
            )
        )

        performance = (
            player["score"] - team_average_score
        ) / max(10, team_average_score)

        performance = max(-1, min(1, performance))

        performance_component = (
            0.20 * K_FACTOR * performance
        )

        difficulty = (opponent_elo - team_elo) / 400
        difficulty = max(-1, min(1, difficulty))

        difficulty_component = (
            0.10 * K_FACTOR * difficulty
        )

        delta = round(
            result_component
            + performance_component
            + difficulty_component
        )

        if won:
            delta = max(1, delta)
        else:
            delta = min(-1, delta)

        changes[player["user_id"]] = {
            "delta": delta,
            "won": won,
            "score": player["score"],
        }

    return changes


def apply_result(match_id, guild_id, mode, players, scores):
    old_values = {}

    with db_connect() as conn:
        existing = conn.execute(
            "SELECT id FROM elo_results WHERE hosting_match_id = ?",
            (match_id,),
        ).fetchone()

        if existing:
            raise ValueError(
                "ELO has already been applied to this match."
            )

        changes = calculate_changes(
            players,
            scores,
            guild_id,
            mode,
        )

        for user_id, result in changes.items():
            old = ensure_rating(
                conn,
                guild_id,
                user_id,
                mode,
            )

            old_values[str(user_id)] = dict(old)

            conn.execute("""
                UPDATE ratings
                SET elo = elo + ?,
                    wins = wins + ?,
                    losses = losses + ?,
                    matches = matches + 1
                WHERE guild_id = ?
                AND user_id = ?
                AND mode = ?
            """, (
                result["delta"],
                int(result["won"]),
                int(not result["won"]),
                guild_id,
                user_id,
                mode,
            ))

        winner = (
            "red" if scores["red"] > scores["blue"]
            else "blue"
        )

        data = {
            "old_values": old_values,
            "changes": {
                str(uid): result
                for uid, result in changes.items()
            },
            "scores": scores,
            "winner": winner,
        }

        conn.execute("""
            INSERT INTO elo_results
            (hosting_match_id, guild_id, mode, result_data, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (
            match_id,
            guild_id,
            mode,
            json.dumps(data),
            now_iso(),
        ))

    return changes


# ============================================================
# RESULT PARSING
# ============================================================

def parse_results(content, roster, mode):
    expected_ids = {
        int(p["user_id"])
        for p in roster
    }

    entries = {}

    pattern = re.compile(
        r"^<@!?(\d+)>\s+(red|blue)\s+(\d+)$",
        re.I,
    )

    for line in content.splitlines():
        found = pattern.match(line.strip())

        if not found:
            continue

        user_id = int(found.group(1))
        team = found.group(2).lower()
        score = int(found.group(3))

        if user_id not in expected_ids:
            raise ValueError(
                f"<@{user_id}> is not in this match."
            )

        if user_id in entries:
            raise ValueError(
                f"<@{user_id}> was entered twice."
            )

        entries[user_id] = {
            "user_id": user_id,
            "team": team,
            "score": score,
        }

    missing = expected_ids - set(entries)

    if missing:
        raise ValueError(
            "Missing players: "
            + ", ".join(f"<@{uid}>" for uid in missing)
        )

    team_size = SLOTS[mode] // 2

    red = [
        p for p in entries.values()
        if p["team"] == "red"
    ]

    blue = [
        p for p in entries.values()
        if p["team"] == "blue"
    ]

    if len(red) != team_size or len(blue) != team_size:
        raise ValueError(
            f"Each team needs {team_size} players."
        )

    scores = {
        "red": sum(p["score"] for p in red),
        "blue": sum(p["score"] for p in blue),
    }

    if scores["red"] == scores["blue"]:
        raise ValueError(
            "The team scores are tied. Staff must resolve the tie."
        )

    return list(entries.values()), scores


# ============================================================
# RESULT SUBMISSION
# ============================================================

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    key = (message.channel.id, message.author.id)
    match_id = pending_results.get(key)

    if match_id is None:
        await bot.process_commands(message)
        return

    match = get_match(match_id)

    if not is_open(match):
        pending_results.pop(key, None)
        await message.reply("This match is closed.")
        return

    if not can_edit_match(message.author, match):
        await message.reply(
            "Only the host or staff can submit results."
        )
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

    if screenshot is None:
        await message.reply(
            "Please attach an image screenshot."
        )
        return

    try:
        players, scores = parse_results(
            message.content,
            get_roster(match),
            match["mode"],
        )

        mode = match["mode"].removesuffix("s")

        changes = apply_result(
            match_id,
            match["guild_id"],
            mode,
            players,
            scores,
        )

        embed = discord.Embed(
            title="꒰ა・MATCH RESULTS",
            color=discord.Color.from_rgb(255, 160, 200),
        )

        embed.add_field(
            name="Match ID",
            value=str(match_id),
            inline=True,
        )

        embed.add_field(
            name="Mode",
            value=mode.upper(),
            inline=True,
        )

        embed.add_field(
            name="Red Team",
            value=str(scores["red"]),
            inline=True,
        )

        embed.add_field(
            name="Blue Team",
            value=str(scores["blue"]),
            inline=True,
        )

        lines = []

        for user_id, result in changes.items():
            sign = "+" if result["delta"] > 0 else ""
            outcome = "WIN" if result["won"] else "LOSS"

            lines.append(
                f"<@{user_id}> — {outcome} — "
                f"{sign}{result['delta']} ELO"
            )

        embed.add_field(
            name="ELO Changes",
            value="\n".join(lines),
            inline=False,
        )

        screenshot_file = await screenshot.to_file(
            filename=screenshot.filename
        )

        embed.set_image(
            url=f"attachment://{screenshot.filename}"
        )

        results_channel = bot.get_channel(RESULTS_CHANNEL_ID)

        if results_channel is None:
            results_channel = await bot.fetch_channel(
                RESULTS_CHANNEL_ID
            )

        await results_channel.send(
            embed=embed,
            file=screenshot_file,
            allowed_mentions=discord.AllowedMentions.none(),
        )

        update_match_status(match_id, "completed")
        pending_results.pop(key, None)

        # Delete only the submitted results message.
        try:
            await message.delete()
        except discord.HTTPException:
            log.exception(
                "Could not delete the submitted results message."
            )

        # Keep the original hosting post and its Rehost button.
        await refresh_match_post(match_id)

        try:
            await message.author.send(
                f"Results for Match `{match_id}` were posted "
                "successfully in the match results channel!"
            )
        except discord.HTTPException:
            pass

    except ValueError as error:
        await message.reply(
            f"Could not process results: {error}"
        )

    except Exception:
        log.exception("Unexpected match-results error.")
        await message.reply(
            "An unexpected error occurred. Check the bot console."
        )


# ============================================================
# ELO COMMANDS
# ============================================================

@bot.tree.command(
    name="elo",
    description="Check a player's ELO.",
)
@app_commands.describe(
    mode="ELO category.",
    member="Optional player.",
)
@app_commands.choices(mode=ELO_CHOICES)
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


@bot.tree.command(
    name="leaderboard",
    description="View the ELO leaderboard.",
)
@app_commands.describe(mode="ELO category.")
@app_commands.choices(mode=ELO_CHOICES)
async def leaderboard_command(
    interaction: discord.Interaction,
    mode: app_commands.Choice[str],
):
    if not await require_verified(interaction):
        return

    with db_connect() as conn:
        rows = conn.execute("""
            SELECT user_id, elo, wins, losses
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
            "No ratings exist for this mode yet.",
            ephemeral=True,
        )

    lines = []

    for index, row in enumerate(rows, 1):
        lines.append(
            f"`{index}.` <@{row['user_id']}> — "
            f"{row['elo']} ELO "
            f"({row['wins']}W / {row['losses']}L)"
        )

    embed = discord.Embed(
        title=f"꒰ა・{mode.value.upper()} LEADERBOARD",
        description="\n".join(lines),
        color=discord.Color.from_rgb(255, 160, 200),
    )

    await interaction.response.send_message(embed=embed)


# ============================================================
# MANUAL ELO ADJUSTMENTS
# ============================================================

async def adjust_elo(
    interaction,
    member,
    mode,
    amount,
    reason,
    adding,
):
    if not can_manage_elo(interaction.user):
        return await interaction.response.send_message(
            "You cannot manage ELO.",
            ephemeral=True,
        )

    with db_connect() as conn:
        rating = ensure_rating(
            conn,
            interaction.guild_id,
            member.id,
            mode,
        )

        if adding:
            new_elo = rating["elo"] + amount
            actual = amount
        else:
            new_elo = max(0, rating["elo"] - amount)
            actual = new_elo - rating["elo"]

        conn.execute("""
            UPDATE ratings
            SET elo = ?
            WHERE guild_id = ?
            AND user_id = ?
            AND mode = ?
        """, (
            new_elo,
            interaction.guild_id,
            member.id,
            mode,
        ))

        conn.execute("""
            INSERT INTO elo_adjustments
            (guild_id, user_id, mode, amount, reason, staff_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            interaction.guild_id,
            member.id,
            mode,
            actual,
            reason,
            interaction.user.id,
            now_iso(),
        ))

    action = "Added" if adding else "Removed"

    await interaction.response.send_message(
        f"{action} {abs(actual)} ELO for {member.mention} "
        f"in {mode.upper()}.\nReason: {reason}"
    )


@bot.tree.command(
    name="addelo",
    description="Add ELO to a player.",
)
@app_commands.describe(
    member="Player.",
    mode="ELO category.",
    amount="Amount to add.",
    reason="Reason for adjustment.",
)
@app_commands.choices(mode=ELO_CHOICES)
async def addelo_command(
    interaction: discord.Interaction,
    member: discord.Member,
    mode: app_commands.Choice[str],
    amount: app_commands.Range[int, 1, 10000],
    reason: str = "Staff adjustment",
):
    await adjust_elo(
        interaction,
        member,
        mode.value,
        amount,
        reason,
        True,
    )


@bot.tree.command(
    name="removeelo",
    description="Remove ELO from a player.",
)
@app_commands.describe(
    member="Player.",
    mode="ELO category.",
    amount="Amount to remove.",
    reason="Reason for adjustment.",
)
@app_commands.choices(mode=ELO_CHOICES)
async def removeelo_command(
    interaction: discord.Interaction,
    member: discord.Member,
    mode: app_commands.Choice[str],
    amount: app_commands.Range[int, 1, 10000],
    reason: str = "Staff adjustment",
):
    await adjust_elo(
        interaction,
        member,
        mode.value,
        amount,
        reason,
        False,
    )


# ============================================================
# MATCH HISTORY
# ============================================================

@bot.tree.command(
    name="matchhistory",
    description="View a player's recent match history.",
)
@app_commands.describe(
    mode="ELO category.",
    member="Optional player.",
)
@app_commands.choices(mode=ELO_CHOICES)
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
            FROM elo_results
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

        result = data.get(
            "changes", {}
        ).get(str(target.id))

        if result:
            outcome = "WIN" if result["won"] else "LOSS"
            delta = result["delta"]
            sign = "+" if delta > 0 else ""

            history.append(
                f"{outcome} — {sign}{delta} ELO "
                f"({row['created_at'][:10]})"
            )

    if not history:
        return await interaction.response.send_message(
            "No match history found.",
            ephemeral=True,
        )

    embed = discord.Embed(
        title=f"꒰ა・{target.display_name}'S HISTORY",
        description="\n".join(history[:10]),
        color=discord.Color.from_rgb(255, 160, 200),
    )

    await interaction.response.send_message(embed=embed)


# ============================================================
# UNDO MATCH
# ============================================================

@bot.tree.command(
    name="undomatch",
    description="Undo a match's ELO result.",
)
@app_commands.describe(match_id="Hosting match ID.")
async def undomatch_command(
    interaction: discord.Interaction,
    match_id: int,
):
    if not is_staff(interaction.user):
        return await interaction.response.send_message(
            "Only staff can undo results.",
            ephemeral=True,
        )

    with db_connect() as conn:
        row = conn.execute("""
            SELECT *
            FROM elo_results
            WHERE hosting_match_id = ?
            AND guild_id = ?
        """, (
            match_id,
            interaction.guild_id,
        )).fetchone()

        if not row:
            return await interaction.response.send_message(
                "No ELO result exists for that match.",
                ephemeral=True,
            )

        data = json.loads(row["result_data"])

        later = conn.execute("""
            SELECT id
            FROM elo_results
            WHERE guild_id = ?
            AND mode = ?
            AND id > ?
            LIMIT 1
        """, (
            interaction.guild_id,
            row["mode"],
            row["id"],
        )).fetchone()

        if later:
            return await interaction.response.send_message(
                "A later match has already changed ratings. "
                "Undoing this result could overwrite newer ratings.",
                ephemeral=True,
            )

        for user_id, old in data["old_values"].items():
            conn.execute("""
                UPDATE ratings
                SET elo = ?,
                    wins = ?,
                    losses = ?,
                    matches = ?
                WHERE guild_id = ?
                AND user_id = ?
                AND mode = ?
            """, (
                old["elo"],
                old["wins"],
                old["losses"],
                old["matches"],
                interaction.guild_id,
                int(user_id),
                row["mode"],
            ))

        conn.execute(
            "DELETE FROM elo_results WHERE id = ?",
            (row["id"],),
        )

    update_match_status(match_id, "open")
    reset_reminder(match_id)

    await refresh_match_post(match_id)

    await interaction.response.send_message(
        f"ELO result for match `{match_id}` was undone. "
        "The hosting match has been reopened."
    )


# ============================================================
# HELP COMMANDS
# ============================================================

@bot.tree.command(
    name="elohelp",
    description="View ELO commands.",
)
async def elohelp_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="꒰ა・ARSENAL ANGELS ELO HELP",
        description=(
            "**Player commands**\n"
            "`/elo` — Check a player's rating.\n"
            "`/leaderboard` — View the top 10 players.\n"
            "`/matchhistory` — View recent match history.\n\n"
            "**Staff commands**\n"
            "`/addelo` — Manually add ELO.\n"
            "`/removeelo` — Manually remove ELO.\n"
            "`/undomatch` — Undo a match result.\n\n"
            "**ELO calculation**\n"
            "70% match result.\n"
            "20% individual performance.\n"
            "10% opponent difficulty.\n\n"
            "Separate ratings for 1v1, 2v2, and 3v3."
        ),
        color=discord.Color.from_rgb(255, 160, 200),
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True,
    )


@bot.tree.command(
    name="hostinghelp",
    description="View hosting commands.",
)
async def hostinghelp_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="꒰ა・HOSTING HELP",
        description=(
            "**Commands**\n"
            "`/host` — Create a lobby.\n"
            "`/cancel` — Cancel a match by ID.\n\n"
            "**Lobby buttons**\n"
            "Join Host · Leave Host · Subs · Post Results · Rehost\n"
            "Cancel Host · Join Private Server\n\n"
            "**Subs**\n"
            "The host or staff can use Subs to select and remove "
            "a player. The matching game-mode role is then pinged "
            "to fill the open spot.\n\n"
            "**Rehosting**\n"
            "Same Players — Recreate the previous roster.\n"
            "Fresh Lobby — Start with only the host.\n\n"
            "**Automatic role pings**\n"
            "Open lobbies receive a mode-role reminder every "
            "10 minutes while they need players.\n\n"
            "**Results**\n"
            "Attach a screenshot and list every player with their "
            "team and points."
        ),
        color=discord.Color.from_rgb(255, 160, 200),
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True,
    )


# ============================================================
# AUTOMATIC LOBBY REMINDERS
# ============================================================

@tasks.loop(minutes=1)
async def lobby_reminders():
    current = datetime.now(timezone.utc)

    try:
        channel = await get_hosting_channel()
    except discord.HTTPException:
        log.exception("Could not fetch hosting channel.")
        return

    for match in get_open_matches():
        if is_full(match):
            continue

        try:
            last = datetime.fromisoformat(
                match.get("last_reminder_at")
                or match["created_at"]
            )

            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)

        except (TypeError, ValueError):
            last = current

        if (current - last).total_seconds() < 600:
            continue

        count = len(get_roster(match))
        needed = SLOTS[match["mode"]] - count

        try:
            await channel.send(
                f"{role_mention(match['mode'])} "
                f"**Lobby still needs players!**\n"
                f"Match `{match['id']}` needs **{needed} more player(s)** "
                f"({count}/{SLOTS[match['mode']]}).",
                allowed_mentions=discord.AllowedMentions(
                    roles=True,
                    users=False,
                    everyone=False,
                ),
            )

            reset_reminder(match["id"])

        except discord.HTTPException:
            log.exception(
                "Could not send lobby reminder for match %s.",
                match["id"],
            )


@lobby_reminders.before_loop
async def before_lobby_reminders():
    await bot.wait_until_ready()


# ============================================================
# EVENTS
# ============================================================

@bot.event
async def on_ready():
    log.info("Logged in as %s", bot.user)

    if not lobby_reminders.is_running():
        lobby_reminders.start()


# ============================================================
# START BOT
# ============================================================

if not TOKEN:
    raise RuntimeError(
        "Set the DISCORD_TOKEN environment variable."
    )

bot.run(TOKEN)
