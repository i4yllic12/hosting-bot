
import os
import asyncio
import sqlite3
import logging
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks


# ============================================================
# CONFIGURATION
# ============================================================

TOKEN = os.getenv("DISCORD_TOKEN") or os.getenv("DISCORD_BOT_TOKEN")

GUILD_ID = int(os.getenv("GUILD_ID", "0") or "0")
MATCH_RESULTS_CHANNEL_ID = int(os.getenv("MATCH_RESULTS_CHANNEL_ID", "0") or "0")

HOSTING_CHANNEL_ID = 1565049435606622348

HOST_ROLE_ID = 1565048538042208466
STAFF_ROLE_ID = 1564857946599456878
ELO_MANAGER_ROLE_ID = 1565158910292398184

MODE_ROLES = {
    "1v1s": 1564896767257084036,
    "2v2s": 1564896807673270382,
    "3v3s": 1564896826723672135,
}

MODE_SLOTS = {
    "1v1s": 2,
    "2v2s": 4,
    "3v3s": 6,
}

STARTING_ELO = 1000
K_FACTOR = 32

DB_PATH = os.getenv("DB_PATH", "./data/hosting.sqlite3")

REMINDER_MINUTES = 10

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("arsenal-angels")


# ============================================================
# BOT SETUP
# ============================================================

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.messages = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

pending_results = {}


# ============================================================
# DATABASE
# ============================================================

def db_connection():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)

    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row

    return connection


def initialize_db():
    with db_connection() as db:

        db.execute("""
            CREATE TABLE IF NOT EXISTS matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                message_id INTEGER,
                host_id INTEGER NOT NULL,
                mode TEXT NOT NULL,
                region TEXT NOT NULL,
                private_server_link TEXT,
                roster TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL,
                last_reminder_at TEXT,
                winner TEXT,
                red_score INTEGER,
                blue_score INTEGER
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS ratings (
                user_id INTEGER NOT NULL,
                mode TEXT NOT NULL,
                elo INTEGER NOT NULL DEFAULT 1000,
                PRIMARY KEY (user_id, mode)
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS match_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id INTEGER,
                mode TEXT NOT NULL,
                red_players TEXT NOT NULL,
                blue_players TEXT NOT NULL,
                red_score INTEGER NOT NULL,
                blue_score INTEGER NOT NULL,
                winner TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS elo_adjustments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                mode TEXT NOT NULL,
                amount INTEGER NOT NULL,
                reason TEXT,
                staff_id INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
        """)


def get_match(match_id):
    with db_connection() as db:
        row = db.execute(
            "SELECT * FROM matches WHERE id = ?",
            (match_id,)
        ).fetchone()

    if not row:
        return None

    match = dict(row)

    import json
    match["roster"] = json.loads(match["roster"])

    return match


def save_match(match_id, **fields):
    import json

    if "roster" in fields:
        fields["roster"] = json.dumps(fields["roster"])

    if not fields:
        return

    assignments = ", ".join(
        f"{column} = ?" for column in fields
    )

    values = list(fields.values())
    values.append(match_id)

    with db_connection() as db:
        db.execute(
            f"UPDATE matches SET {assignments} WHERE id = ?",
            values
        )


def is_open(match):
    return bool(match and match["status"] == "open")


def get_rating(user_id, mode):
    with db_connection() as db:
        row = db.execute(
            """
            SELECT elo FROM ratings
            WHERE user_id = ? AND mode = ?
            """,
            (user_id, mode)
        ).fetchone()

    return row["elo"] if row else STARTING_ELO


def set_rating(user_id, mode, elo):
    with db_connection() as db:
        db.execute(
            """
            INSERT INTO ratings (user_id, mode, elo)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, mode)
            DO UPDATE SET elo = excluded.elo
            """,
            (user_id, mode, elo)
        )


def get_roster_ids(match):
    return [int(user_id) for user_id in match["roster"]]


# ============================================================
# PERMISSIONS
# ============================================================

def has_role(member, role_id):
    return any(role.id == role_id for role in member.roles)


def is_staff(member):
    return (
        member.guild_permissions.administrator
        or has_role(member, STAFF_ROLE_ID)
    )


def is_elo_manager(member):
    return (
        member.guild_permissions.administrator
        or has_role(member, ELO_MANAGER_ROLE_ID)
    )


def can_manage_match(member, match):
    return member.id == match["host_id"] or is_staff(member)


# ============================================================
# MATCH EMBEDS
# ============================================================

async def roster_text(guild, roster):
    lines = []

    for user_id in roster:
        member = guild.get_member(int(user_id))

        if member:
            lines.append(f"• {member.mention}")
        else:
            lines.append(f"• <@{user_id}>")

    return "\n".join(lines) if lines else "No players."


def make_match_embed(match, guild):
    mode = match["mode"]
    maximum = MODE_SLOTS[mode]
    roster = match["roster"]

    embed = discord.Embed(
        title=f"꒰ა・{mode.upper()} HOST",
        color=discord.Color.from_rgb(255, 153, 204)
    )

    embed.add_field(
        name="Host",
        value=f"<@{match['host_id']}>",
        inline=True
    )

    embed.add_field(
        name="Mode",
        value=mode,
        inline=True
    )

    embed.add_field(
        name="Region",
        value=match["region"],
        inline=True
    )

    embed.add_field(
        name="Players",
        value=f"{len(roster)}/{maximum}",
        inline=True
    )

    embed.add_field(
        name="Roster",
        value=asyncio.run(roster_text(guild, roster))
        if False else "\n".join(
            f"• <@{user_id}>" for user_id in roster
        ),
        inline=False
    )

    if match["status"] == "completed":
        embed.add_field(
            name="Status",
            value="Completed",
            inline=False
        )

        embed.color = discord.Color.green()

    elif match["status"] == "cancelled":
        embed.add_field(
            name="Status",
            value="Cancelled",
            inline=False
        )

        embed.color = discord.Color.red()

    else:
        embed.add_field(
            name="Status",
            value="Waiting for players",
            inline=False
        )

    embed.set_footer(text="Arsenal Angels")

    return embed


# ============================================================
# MODE PINGS
# ============================================================

async def ping_mode(match, message):
    role_id = MODE_ROLES.get(match["mode"])

    if not role_id:
        return

    channel = bot.get_channel(match["channel_id"])

    if not channel:
        return

    await channel.send(
        f"<@&{role_id}> {message}",
        allowed_mentions=discord.AllowedMentions(
            roles=True,
            users=False
        )
    )


async def announce_open_slot(match_id):
    match = get_match(match_id)

    if not is_open(match):
        return

    needed = MODE_SLOTS[match["mode"]] - len(match["roster"])

    if needed <= 0:
        return

    await ping_mode(
        match,
        f"An opening is available in a {match['mode']} lobby! "
        f"{needed} player(s) needed."
    )


# ============================================================
# MATCH CREATION AND REFRESHING
# ============================================================

async def create_match(
    guild,
    host_id,
    mode,
    region,
    private_server_link=None,
    initial_roster=None
):
    channel = bot.get_channel(HOSTING_CHANNEL_ID)

    if not channel:
        raise RuntimeError("Hosting channel could not be found.")

    roster = initial_roster or [host_id]

    if host_id not in roster:
        roster.insert(0, host_id)

    import json

    with db_connection() as db:
        cursor = db.execute(
            """
            INSERT INTO matches (
                guild_id,
                channel_id,
                host_id,
                mode,
                region,
                private_server_link,
                roster,
                status,
                created_at,
                last_reminder_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
            """,
            (
                guild.id,
                channel.id,
                host_id,
                mode,
                region,
                private_server_link,
                json.dumps(roster),
                datetime.now(timezone.utc).isoformat(),
                datetime.now(timezone.utc).isoformat()
            )
        )

        match_id = cursor.lastrowid

    match = get_match(match_id)

    message = await channel.send(
        embed=make_match_embed(match, guild),
        view=MatchView(match_id, private_server_link)
    )

    save_match(match_id, message_id=message.id)

    await announce_open_slot(match_id)

    return match_id


async def refresh_match_post(match_id):
    match = get_match(match_id)

    if not match:
        return

    channel = bot.get_channel(match["channel_id"])

    if not channel:
        return

    try:
        message = await channel.fetch_message(match["message_id"])
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return

    await message.edit(
        embed=make_match_embed(match, channel.guild),
        view=MatchView(
            match_id,
            match["private_server_link"]
        )
    )


# ============================================================
# BUTTON CALLBACKS
# ============================================================

async def join_match(interaction, match_id):
    match = get_match(match_id)

    if not is_open(match):
        return await interaction.response.send_message(
            "This lobby is no longer open.",
            ephemeral=True
        )

    roster = get_roster_ids(match)

    if interaction.user.id in roster:
        return await interaction.response.send_message(
            "You're already in this lobby.",
            ephemeral=True
        )

    maximum = MODE_SLOTS[match["mode"]]

    if len(roster) >= maximum:
        return await interaction.response.send_message(
            "This lobby is full.",
            ephemeral=True
        )

    roster.append(interaction.user.id)

    save_match(match_id, roster=roster)

    await refresh_match_post(match_id)

    await interaction.response.send_message(
        "You joined the lobby!",
        ephemeral=True
    )


async def leave_match(interaction, match_id):
    match = get_match(match_id)

    if not is_open(match):
        return await interaction.response.send_message(
            "This lobby is no longer open.",
            ephemeral=True
        )

    roster = get_roster_ids(match)

    if interaction.user.id not in roster:
        return await interaction.response.send_message(
            "You're not in this lobby.",
            ephemeral=True
        )

    if interaction.user.id == match["host_id"]:
        return await interaction.response.send_message(
            "The host cannot leave their own lobby. Cancel or rehost it instead.",
            ephemeral=True
        )

    roster.remove(interaction.user.id)

    save_match(match_id, roster=roster)

    await refresh_match_post(match_id)

    await interaction.response.send_message(
        "You left the lobby.",
        ephemeral=True
    )

    await announce_open_slot(match_id)


# ============================================================
# SUBS DROPDOWN
# ============================================================

class SubsSelect(discord.ui.Select):

    def __init__(self, match_id, players):
        self.match_id = match_id

        options = []

        for user_id in players:
            member = None

            for guild in bot.guilds:
                member = guild.get_member(int(user_id))
                if member:
                    break

            if member:
                options.append(
                    discord.SelectOption(
                        label=member.display_name[:100],
                        description=f"Remove {member.name} from this lobby.",
                        value=str(user_id)
                    )
                )

        super().__init__(
            placeholder="Choose a player to remove...",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction):
        match = get_match(self.match_id)

        if not is_open(match):
            return await interaction.response.send_message(
                "This lobby is no longer open.",
                ephemeral=True
            )

        if not can_manage_match(interaction.user, match):
            return await interaction.response.send_message(
                "Only the host or staff can remove players.",
                ephemeral=True
            )

        user_id = int(self.values[0])

        if user_id == match["host_id"]:
            return await interaction.response.send_message(
                "You cannot remove the host.",
                ephemeral=True
            )

        roster = get_roster_ids(match)

        if user_id not in roster:
            return await interaction.response.send_message(
                "That player is no longer in the lobby.",
                ephemeral=True
            )

        roster.remove(user_id)

        save_match(self.match_id, roster=roster)

        await refresh_match_post(self.match_id)

        await interaction.response.send_message(
            f"<@{user_id}> was removed from the lobby.",
            ephemeral=True
        )

        await announce_open_slot(self.match_id)


class SubsView(discord.ui.View):

    def __init__(self, match_id, players):
        super().__init__(timeout=120)

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
                label="Same players",
                description="Create a new lobby with the previous roster.",
                value="same"
            ),
            discord.SelectOption(
                label="Fresh lobby",
                description="Create a new lobby with only the host.",
                value="fresh"
            )
        ]

        super().__init__(
            placeholder="Choose how to rehost...",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction):
        match = get_match(self.match_id)

        if not match:
            return await interaction.response.send_message(
                "Match not found.",
                ephemeral=True
            )

        if not can_manage_match(interaction.user, match):
            return await interaction.response.send_message(
                "Only the original host or staff can rehost.",
                ephemeral=True
            )

        choice = self.values[0]

        if choice == "same":
            roster = get_roster_ids(match)
        else:
            roster = [match["host_id"]]

        await interaction.response.defer(ephemeral=True)

        new_match_id = await create_match(
            guild=interaction.guild,
            host_id=match["host_id"],
            mode=match["mode"],
            region=match["region"],
            private_server_link=match["private_server_link"],
            initial_roster=roster
        )

        await interaction.followup.send(
            f"Your new lobby has been created: **Match #{new_match_id}**.",
            ephemeral=True
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

        if match and is_open(match):

            join_button = discord.ui.Button(
                label="Join Host",
                style=discord.ButtonStyle.success,
                custom_id=f"aa:join:{match_id}"
            )
            join_button.callback = self.join_callback
            self.add_item(join_button)

            leave_button = discord.ui.Button(
                label="Leave Host",
                style=discord.ButtonStyle.secondary,
                custom_id=f"aa:leave:{match_id}"
            )
            leave_button.callback = self.leave_callback
            self.add_item(leave_button)

            subs_button = discord.ui.Button(
                label="Subs",
                style=discord.ButtonStyle.primary,
                custom_id=f"aa:subs:{match_id}"
            )
            subs_button.callback = self.subs_callback
            self.add_item(subs_button)

            results_button = discord.ui.Button(
                label="Post Results",
                style=discord.ButtonStyle.primary,
                custom_id=f"aa:results:{match_id}"
            )
            results_button.callback = self.results_callback
            self.add_item(results_button)

            cancel_button = discord.ui.Button(
                label="Cancel Host",
                style=discord.ButtonStyle.danger,
                custom_id=f"aa:cancel:{match_id}"
            )
            cancel_button.callback = self.cancel_callback
            self.add_item(cancel_button)

        # Rehost remains available after completion.
        if match and match["status"] != "cancelled":
            rehost_button = discord.ui.Button(
                label="Rehost",
                style=discord.ButtonStyle.secondary,
                custom_id=f"aa:rehost:{match_id}"
            )
            rehost_button.callback = self.rehost_callback
            self.add_item(rehost_button)

        if private_server_link:
            self.add_item(
                discord.ui.Button(
                    label="Join Private Server",
                    style=discord.ButtonStyle.link,
                    url=private_server_link
                )
            )

    async def join_callback(self, interaction):
        await join_match(interaction, self.match_id)

    async def leave_callback(self, interaction):
        await leave_match(interaction, self.match_id)

    async def subs_callback(self, interaction):
        match = get_match(self.match_id)

        if not is_open(match):
            return await interaction.response.send_message(
                "This lobby is no longer open.",
                ephemeral=True
            )

        if not can_manage_match(interaction.user, match):
            return await interaction.response.send_message(
                "Only the host or staff can use Subs.",
                ephemeral=True
            )

        players = [
            user_id for user_id in get_roster_ids(match)
            if user_id != match["host_id"]
        ]

        if not players:
            return await interaction.response.send_message(
                "There are no players to remove.",
                ephemeral=True
            )

        await interaction.response.send_message(
            "Choose the player you want to remove:",
            view=SubsView(self.match_id, players),
            ephemeral=True
        )

    async def results_callback(self, interaction):
        match = get_match(self.match_id)

        if not is_open(match):
            return await interaction.response.send_message(
                "This match is no longer open.",
                ephemeral=True
            )

        if not can_manage_match(interaction.user, match):
            return await interaction.response.send_message(
                "Only the host or staff can post results.",
                ephemeral=True
            )

        pending_results[
            (interaction.channel_id, interaction.user.id)
        ] = self.match_id

        await interaction.response.send_message(
            "Send your results screenshot and then list the players and scores.\n\n"
            "Example:\n"
            "`<@123456789> red 32`\n"
            "`<@987654321> blue 25`\n\n"
            "Your next message will be used as the results submission.",
            ephemeral=True
        )

    async def rehost_callback(self, interaction):
        match = get_match(self.match_id)

        if not match:
            return await interaction.response.send_message(
                "Match not found.",
                ephemeral=True
            )

        if not can_manage_match(interaction.user, match):
            return await interaction.response.send_message(
                "Only the original host or staff can rehost.",
                ephemeral=True
            )

        await interaction.response.send_message(
            "Choose your rehost option:",
            view=RehostView(self.match_id),
            ephemeral=True
        )

    async def cancel_callback(self, interaction):
        match = get_match(self.match_id)

        if not is_open(match):
            return await interaction.response.send_message(
                "This lobby is no longer open.",
                ephemeral=True
            )

        if not can_manage_match(interaction.user, match):
            return await interaction.response.send_message(
                "Only the host or staff can cancel this lobby.",
                ephemeral=True
            )

        save_match(self.match_id, status="cancelled")

        await refresh_match_post(self.match_id)

        await interaction.response.send_message(
            "The lobby has been cancelled.",
            ephemeral=True
        )


# ============================================================
# HOST COMMAND
# ============================================================

@bot.tree.command(name="host", description="Create an Arsenal Angels lobby.")
@app_commands.describe(
    mode="Choose your game mode.",
    region="Enter the server region.",
    private_server_link="Optional private server link."
)
@app_commands.choices(
    mode=[
        app_commands.Choice(name="1v1", value="1v1s"),
        app_commands.Choice(name="2v2", value="2v2s"),
        app_commands.Choice(name="3v3", value="3v3s")
    ]
)
async def host_command(
    interaction: discord.Interaction,
    mode: app_commands.Choice[str],
    region: str,
    private_server_link: str = None
):
    if not has_role(interaction.user, HOST_ROLE_ID) and not is_staff(interaction.user):
        return await interaction.response.send_message(
            "You need the Host role to create a lobby.",
            ephemeral=True
        )

    await interaction.response.defer(ephemeral=True)

    match_id = await create_match(
        guild=interaction.guild,
        host_id=interaction.user.id,
        mode=mode.value,
        region=region,
        private_server_link=private_server_link
    )

    await interaction.followup.send(
        f"Your lobby has been created! Match #{match_id}.",
        ephemeral=True
    )


# ============================================================
# CANCEL COMMAND
# ============================================================

@bot.tree.command(name="cancel", description="Cancel one of your open lobbies.")
async def cancel_command(interaction: discord.Interaction, match_id: int):
    match = get_match(match_id)

    if not is_open(match):
        return await interaction.response.send_message(
            "That lobby is not open.",
            ephemeral=True
        )

    if not can_manage_match(interaction.user, match):
        return await interaction.response.send_message(
            "You cannot cancel that lobby.",
            ephemeral=True
        )

    save_match(match_id, status="cancelled")

    await refresh_match_post(match_id)

    await interaction.response.send_message(
        "Lobby cancelled.",
        ephemeral=True
    )


# ============================================================
# RESULT PROCESSING
# ============================================================

def parse_result_lines(content):
    red_players = []
    blue_players = []

    red_score = None
    blue_score = None

    for line in content.splitlines():
        parts = line.split()

        if len(parts) < 3:
            continue

        mention = parts[0]
        team = parts[1].lower()

        try:
            score = int(parts[2])
        except ValueError:
            continue

        digits = "".join(character for character in mention if character.isdigit())

        if not digits:
            continue

        user_id = int(digits)

        if team == "red":
            red_players.append(user_id)
            red_score = score

        elif team == "blue":
            blue_players.append(user_id)
            blue_score = score

    if not red_players or not blue_players:
        return None

    if red_score is None or blue_score is None:
        return None

    return red_players, blue_players, red_score, blue_score


def calculate_elo_changes(red_players, blue_players, red_score, blue_score, mode):
    red_average = sum(
        get_rating(user_id, mode) for user_id in red_players
    ) / len(red_players)

    blue_average = sum(
        get_rating(user_id, mode) for user_id in blue_players
    ) / len(blue_players)

    red_expected = 1 / (
        1 + 10 ** ((blue_average - red_average) / 400)
    )

    blue_expected = 1 - red_expected

    if red_score > blue_score:
        red_actual = 1
        blue_actual = 0
        winner = "red"

    elif blue_score > red_score:
        red_actual = 0
        blue_actual = 1
        winner = "blue"

    else:
        red_actual = 0.5
        blue_actual = 0.5
        winner = "tie"

    red_change = round(K_FACTOR * (red_actual - red_expected))
    blue_change = round(K_FACTOR * (blue_actual - blue_expected))

    changes = {}

    for user_id in red_players:
        changes[user_id] = red_change

    for user_id in blue_players:
        changes[user_id] = blue_change

    return changes, winner


async def process_results(message, match_id):
    match = get_match(match_id)

    if not is_open(match):
        return False, "That match is no longer open."

    if not message.attachments:
        return False, "Please include a screenshot."

    parsed = parse_result_lines(message.content)

    if not parsed:
        return False, (
            "I couldn't read the results. Use lines like:\n"
            "`<@123456789> red 32`\n"
            "`<@987654321> blue 25`"
        )

    red_players, blue_players, red_score, blue_score = parsed

    roster = set(get_roster_ids(match))

    if not set(red_players + blue_players).issubset(roster):
        return False, "Every player listed must be in the lobby roster."

    if set(red_players) & set(blue_players):
        return False, "A player cannot be on both teams."

    if set(red_players + blue_players) != roster:
        return False, "Every player in the lobby must be included in the results."

    changes, winner = calculate_elo_changes(
        red_players,
        blue_players,
        red_score,
        blue_score,
        match["mode"]
    )

    for user_id, change in changes.items():
        old_elo = get_rating(user_id, match["mode"])
        set_rating(user_id, match["mode"], old_elo + change)

    import json

    with db_connection() as db:
        db.execute(
            """
            INSERT INTO match_history (
                match_id,
                mode,
                red_players,
                blue_players,
                red_score,
                blue_score,
                winner,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                match_id,
                match["mode"],
                json.dumps(red_players),
                json.dumps(blue_players),
                red_score,
                blue_score,
                winner,
                datetime.now(timezone.utc).isoformat()
            )
        )

    save_match(
        match_id,
        status="completed",
        winner=winner,
        red_score=red_score,
        blue_score=blue_score
    )

    results_channel = bot.get_channel(MATCH_RESULTS_CHANNEL_ID)

    screenshot = message.attachments[0]

    if results_channel:
        filename = screenshot.filename or "match-results.png"
        image_file = await screenshot.to_file()

        embed = discord.Embed(
            title=f"Match #{match_id} Results",
            description=(
                f"**Mode:** {match['mode']}\n"
                f"**Red:** {red_score}\n"
                f"**Blue:** {blue_score}\n"
                f"**Winner:** {winner.upper()}"
            ),
            color=discord.Color.from_rgb(255, 153, 204)
        )

        embed.set_image(url=f"attachment://{filename}")

        await results_channel.send(
            embed=embed,
            file=image_file
        )

    await refresh_match_post(match_id)

    return True, "Results successfully submitted."


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    key = (message.channel.id, message.author.id)
    match_id = pending_results.get(key)

    if match_id is None:
        await bot.process_commands(message)
        return

    success, response = await process_results(message, match_id)

    if success:
        pending_results.pop(key, None)

        try:
            await message.delete()
        except discord.HTTPException:
            pass

        try:
            await message.author.send(
                f"꒰ა・{response} Match #{match_id} is complete."
            )
        except discord.HTTPException:
            pass

    else:
        try:
            await message.author.send(response)
        except discord.HTTPException:
            pass

    await bot.process_commands(message)


# ============================================================
# ELO COMMANDS
# ============================================================

@bot.tree.command(name="elo", description="Check a player's ELO.")
@app_commands.choices(
    mode=[
        app_commands.Choice(name="1v1", value="1v1s"),
        app_commands.Choice(name="2v2", value="2v2s"),
        app_commands.Choice(name="3v3", value="3v3s")
    ]
)
async def elo_command(
    interaction: discord.Interaction,
    mode: app_commands.Choice[str],
    player: discord.Member = None
):
    target = player or interaction.user

    rating = get_rating(target.id, mode.value)

    await interaction.response.send_message(
        f"**{target.display_name}** has **{rating} ELO** in **{mode.value}**."
    )


@bot.tree.command(name="leaderboard", description="View the ELO leaderboard.")
@app_commands.choices(
    mode=[
        app_commands.Choice(name="1v1", value="1v1s"),
        app_commands.Choice(name="2v2", value="2v2s"),
        app_commands.Choice(name="3v3", value="3v3s")
    ]
)
async def leaderboard_command(
    interaction: discord.Interaction,
    mode: app_commands.Choice[str]
):
    with db_connection() as db:
        rows = db.execute(
            """
            SELECT user_id, elo
            FROM ratings
            WHERE mode = ?
            ORDER BY elo DESC
            LIMIT 10
            """,
            (mode.value,)
        ).fetchall()

    if not rows:
        return await interaction.response.send_message(
            "No ELO ratings have been recorded yet."
        )

    lines = []

    for index, row in enumerate(rows, start=1):
        lines.append(
            f"**{index}.** <@{row['user_id']}> — {row['elo']} ELO"
        )

    embed = discord.Embed(
        title=f"꒰ა・{mode.value.upper()} LEADERBOARD",
        description="\n".join(lines),
        color=discord.Color.from_rgb(255, 153, 204)
    )

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="addelo", description="Add ELO to a player.")
@app_commands.choices(
    mode=[
        app_commands.Choice(name="1v1", value="1v1s"),
        app_commands.Choice(name="2v2", value="2v2s"),
        app_commands.Choice(name="3v3", value="3v3s")
    ]
)
async def addelo_command(
    interaction: discord.Interaction,
    player: discord.Member,
    mode: app_commands.Choice[str],
    amount: int,
    reason: str = "Staff adjustment"
):
    if not is_elo_manager(interaction.user):
        return await interaction.response.send_message(
            "You do not have permission to adjust ELO.",
            ephemeral=True
        )

    old_elo = get_rating(player.id, mode.value)
    new_elo = old_elo + amount

    set_rating(player.id, mode.value, new_elo)

    with db_connection() as db:
        db.execute(
            """
            INSERT INTO elo_adjustments (
                user_id, mode, amount, reason, staff_id, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                player.id,
                mode.value,
                amount,
                reason,
                interaction.user.id,
                datetime.now(timezone.utc).isoformat()
            )
        )

    await interaction.response.send_message(
        f"Added **{amount} ELO** to {player.mention}.\n"
        f"New rating: **{new_elo}**.",
        ephemeral=True
    )


@bot.tree.command(name="removeelo", description="Remove ELO from a player.")
@app_commands.choices(
    mode=[
        app_commands.Choice(name="1v1", value="1v1s"),
        app_commands.Choice(name="2v2", value="2v2s"),
        app_commands.Choice(name="3v3", value="3v3s")
    ]
)
async def removeelo_command(
    interaction: discord.Interaction,
    player: discord.Member,
    mode: app_commands.Choice[str],
    amount: int,
    reason: str = "Staff adjustment"
):
    if not is_elo_manager(interaction.user):
        return await interaction.response.send_message(
            "You do not have permission to adjust ELO.",
            ephemeral=True
        )

    old_elo = get_rating(player.id, mode.value)
    new_elo = max(0, old_elo - amount)

    set_rating(player.id, mode.value, new_elo)

    with db_connection() as db:
        db.execute(
            """
            INSERT INTO elo_adjustments (
                user_id, mode, amount, reason, staff_id, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                player.id,
                mode.value,
                -amount,
                reason,
                interaction.user.id,
                datetime.now(timezone.utc).isoformat()
            )
        )

    await interaction.response.send_message(
        f"Removed **{amount} ELO** from {player.mention}.\n"
        f"New rating: **{new_elo}**.",
        ephemeral=True
    )


@bot.tree.command(name="matchhistory", description="View recent match results.")
async def matchhistory_command(interaction: discord.Interaction):
    with db_connection() as db:
        rows = db.execute(
            """
            SELECT *
            FROM match_history
            ORDER BY id DESC
            LIMIT 10
            """
        ).fetchall()

    if not rows:
        return await interaction.response.send_message(
            "No matches have been recorded."
        )

    lines = []

    for row in rows:
        lines.append(
            f"Match #{row['match_id']} — {row['mode']} — "
            f"Red {row['red_score']} : {row['blue_score']} Blue — "
            f"{row['winner'].upper()}"
        )

    embed = discord.Embed(
        title="Recent Match History",
        description="\n".join(lines),
        color=discord.Color.from_rgb(255, 153, 204)
    )

    await interaction.response.send_message(embed=embed)


# ============================================================
# ELO HELP
# ============================================================

@bot.tree.command(name="elohelp", description="View ELO commands.")
async def elohelp_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="꒰ა・ELO COMMANDS",
        description=(
            "`/elo` — Check a player's ELO.\n"
            "`/leaderboard` — View the leaderboard.\n"
            "`/matchhistory` — View recent match results.\n"
            "`/addelo` — Add ELO (ELO managers only).\n"
            "`/removeelo` — Remove ELO (ELO managers only)."
        ),
        color=discord.Color.from_rgb(255, 153, 204)
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )


@bot.tree.command(name="hostinghelp", description="View hosting commands.")
async def hostinghelp_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="꒰ა・HOSTING COMMANDS",
        description=(
            "`/host` — Create a lobby.\n"
            "`/cancel` — Cancel an open lobby.\n\n"
            "**Lobby buttons:**\n"
            "Join Host — Join a lobby.\n"
            "Leave Host — Leave a lobby.\n"
            "Subs — Host/staff can remove a player.\n"
            "Post Results — Submit match results.\n"
            "Rehost — Create another lobby with the same or fresh roster.\n"
            "Cancel Host — Cancel a lobby."
        ),
        color=discord.Color.from_rgb(255, 153, 204)
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )


# ============================================================
# 10-MINUTE OPEN LOBBY REMINDERS
# ============================================================

@tasks.loop(minutes=1)
async def lobby_reminders():
    now = datetime.now(timezone.utc)

    with db_connection() as db:
        rows = db.execute(
            """
            SELECT id
            FROM matches
            WHERE status = 'open'
            """
        ).fetchall()

    for row in rows:
        match = get_match(row["id"])

        if not match:
            continue

        maximum = MODE_SLOTS[match["mode"]]

        if len(match["roster"]) >= maximum:
            continue

        last_reminder = match["last_reminder_at"]

        if last_reminder:
            try:
                last_time = datetime.fromisoformat(last_reminder)
                if last_time.tzinfo is None:
                    last_time = last_time.replace(tzinfo=timezone.utc)
            except ValueError:
                last_time = now
        else:
            last_time = datetime.fromisoformat(match["created_at"])

            if last_time.tzinfo is None:
                last_time = last_time.replace(tzinfo=timezone.utc)

        if now - last_time < timedelta(minutes=REMINDER_MINUTES):
            continue

        needed = maximum - len(match["roster"])

        await ping_mode(
            match,
            f"Reminder: a {match['mode']} lobby is still open! "
            f"{needed} player(s) needed to fill it."
        )

        save_match(
            match["id"],
            last_reminder_at=now.isoformat()
        )


@lobby_reminders.before_loop
async def before_lobby_reminders():
    await bot.wait_until_ready()


# ============================================================
# STARTUP AND PERSISTENT VIEWS
# ============================================================

@bot.event
async def on_ready():
    log.info("Logged in as %s", bot.user)

    if not lobby_reminders.is_running():
        lobby_reminders.start()


@bot.event
async def setup_hook():
    initialize_db()

    if GUILD_ID:
        guild_object = discord.Object(id=GUILD_ID)

        bot.tree.copy_global_to(guild=guild_object)
        await bot.tree.sync(guild=guild_object)

    else:
        await bot.tree.sync()

    # Restore persistent buttons after restarting.
    with db_connection() as db:
        rows = db.execute(
            """
            SELECT id, private_server_link
            FROM matches
            WHERE status IN ('open', 'completed')
            """
        ).fetchall()

    for row in rows:
        bot.add_view(
            MatchView(
                row["id"],
                row["private_server_link"]
            )
        )

    log.info("Arsenal Angels bot setup complete.")


# ============================================================
# RUN
# ============================================================

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing from your environment variables.")

bot.run(TOKEN)
