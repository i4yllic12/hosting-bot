import os
import sqlite3
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

# ---------------- CONFIGURATION ----------------

HOST_ROLE_ID = 1565048538042208466

GUILD_ID = int(os.getenv("GUILD_ID", "0") or "0")
MATCH_RESULTS_CHANNEL_ID = int(
    os.getenv("MATCH_RESULTS_CHANNEL_ID", "0") or "0"
)
DB_PATH = os.getenv("DB_PATH", "./data/hosting.sqlite3")

TOKEN = (
    os.getenv("DISCORD_TOKEN")
    or os.getenv("DISCORD_BOT_TOKEN")
    or os.getenv("BOT_TOKEN")
    or os.getenv("TOKEN")
)

MODES = ("1v1s", "2v2s", "3v3s")
REGIONS = ("NA", "EU", "OCE", "Other")

# The next message from a host who clicked Post Results is their result.
# Key: (channel_id, user_id), value: match_id
pending_results = {}


# ---------------- DATABASE ----------------

def db_connect():
    folder = os.path.dirname(DB_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
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
                players TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'open',
                result TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
        """)


def get_match(match_id):
    with db_connect() as conn:
        return conn.execute(
            "SELECT * FROM matches WHERE id = ?", (match_id,)
        ).fetchone()


def create_match(guild_id, channel_id, host_id, mode, region, players=""):
    with db_connect() as conn:
        cur = conn.execute("""
            INSERT INTO matches
                (guild_id, channel_id, host_id, mode, region,
                 players, status, result, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'open', '', ?)
        """, (
            guild_id,
            channel_id,
            host_id,
            mode,
            region,
            players.strip(),
            datetime.now(timezone.utc).isoformat(),
        ))
        return cur.lastrowid


def update_match(match_id, **fields):
    allowed = {
        "message_id", "host_id", "mode", "region", "players",
        "status", "result", "channel_id"
    }

    fields = {
        key: value for key, value in fields.items()
        if key in allowed
    }
    if not fields:
        return

    assignments = ", ".join(f"{key} = ?" for key in fields)
    values = list(fields.values()) + [match_id]

    with db_connect() as conn:
        conn.execute(
            f"UPDATE matches SET {assignments} WHERE id = ?",
            values,
        )


# ---------------- PERMISSIONS ----------------

def has_host_role(member):
    return any(role.id == HOST_ROLE_ID for role in member.roles)


def can_manage_match(interaction, match):
    member = interaction.user

    if not isinstance(member, discord.Member):
        return False

    return (
        member.id == match["host_id"]
        or has_host_role(member)
        or member.guild_permissions.administrator
        or member.guild_permissions.manage_guild
    )


# ---------------- EMBEDS ----------------

def make_match_embed(match):
    color = (
        discord.Color.green()
        if match["status"] == "completed"
        else discord.Color.blurple()
    )

    embed = discord.Embed(
        title=f"🎮 {match['mode']} Match",
        color=color,
    )

    embed.add_field(name="Region", value=match["region"], inline=True)
    embed.add_field(
        name="Hosted by",
        value=f"<@{match['host_id']}>",
        inline=True,
    )
    embed.add_field(
        name="Status",
        value=match["status"].title(),
        inline=True,
    )
    embed.add_field(
        name="Players",
        value=match["players"] or "Not listed",
        inline=False,
    )

    if match["result"]:
        embed.add_field(
            name="Result",
            value=match["result"][:1024],
            inline=False,
        )

    embed.set_footer(
        text=f"Match ID: {match['id']} • Arsenal Angels Hosting"
    )
    return embed


async def find_results_channel(guild):
    if MATCH_RESULTS_CHANNEL_ID:
        channel = guild.get_channel(MATCH_RESULTS_CHANNEL_ID)
        if isinstance(channel, discord.TextChannel):
            return channel

    for channel in guild.text_channels:
        if channel.name.lower() in {"match-results", "match results"}:
            return channel

    return None


async def send_match(guild, match_id):
    match = get_match(match_id)
    if not match:
        return

    channel = guild.get_channel(match["channel_id"])
    if not isinstance(channel, discord.TextChannel):
        return

    message = await channel.send(
        embed=make_match_embed(match),
        view=MatchView(match_id),
    )
    update_match(match_id, message_id=message.id)


async def refresh_match(guild, match_id):
    match = get_match(match_id)
    if not match or not match["message_id"]:
        return

    channel = guild.get_channel(match["channel_id"])
    if not isinstance(channel, discord.TextChannel):
        return

    try:
        message = await channel.fetch_message(match["message_id"])
        await message.edit(
            embed=make_match_embed(match),
            view=MatchView(match_id),
        )
    except discord.HTTPException:
        pass


# ---------------- MATCH BUTTONS ----------------

class MatchView(discord.ui.View):
    """Persistent buttons for a particular match."""

    def __init__(self, match_id):
        super().__init__(timeout=None)
        self.match_id = match_id

        results_button = discord.ui.Button(
            label="Post Results",
            emoji="📨",
            style=discord.ButtonStyle.success,
            custom_id=f"hosting:{match_id}:results",
        )
        results_button.callback = self.post_results
        self.add_item(results_button)

        rehost_button = discord.ui.Button(
            label="Rehost",
            emoji="🔁",
            style=discord.ButtonStyle.primary,
            custom_id=f"hosting:{match_id}:rehost",
        )
        rehost_button.callback = self.rehost
        self.add_item(rehost_button)

    async def post_results(self, interaction):
        match = get_match(self.match_id)

        if not match:
            await interaction.response.send_message(
                "This match could not be found.", ephemeral=True
            )
            return

        if match["status"] == "completed":
            await interaction.response.send_message(
                "This match already has results.", ephemeral=True
            )
            return

        if not can_manage_match(interaction, match):
            await interaction.response.send_message(
                "Only the match host, Host role members, or authorized staff "
                "can post results.",
                ephemeral=True,
            )
            return

        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message(
                "Use this button in the hosting channel.", ephemeral=True
            )
            return

        key = (interaction.channel.id, interaction.user.id)
        pending_results[key] = self.match_id

        await interaction.response.send_message(
            "Now type the match result as your next message in this channel. "
            "Example: `Team A 30 - 24 Team B`. "
            "The bot will forward it to #match-results.",
            ephemeral=True,
        )

    async def rehost(self, interaction):
        match = get_match(self.match_id)

        if not match:
            await interaction.response.send_message(
                "This match could not be found.", ephemeral=True
            )
            return

        if match["status"] != "completed":
            await interaction.response.send_message(
                "Post the results before rehosting this match.",
                ephemeral=True,
            )
            return

        if not can_manage_match(interaction, match):
            await interaction.response.send_message(
                "Only the match host, Host role members, or authorized staff "
                "can rehost this match.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "Choose a rehost option:",
            view=RehostView(self.match_id),
            ephemeral=True,
        )


class RehostView(discord.ui.View):
    def __init__(self, match_id):
        super().__init__(timeout=120)
        self.match_id = match_id

    async def create_rehost(self, interaction, same_players):
        old = get_match(self.match_id)

        if not old or old["status"] != "completed":
            await interaction.response.send_message(
                "This match is not completed.", ephemeral=True
            )
            return

        if not can_manage_match(interaction, old):
            await interaction.response.send_message(
                "You are not authorized to rehost this match.",
                ephemeral=True,
            )
            return

        new_players = old["players"] if same_players else ""

        new_id = create_match(
            guild_id=old["guild_id"],
            channel_id=old["channel_id"],
            host_id=interaction.user.id,
            mode=old["mode"],
            region=old["region"],
            players=new_players,
        )

        await send_match(interaction.guild, new_id)

        description = (
            "New match created with the same players and region."
            if same_players
            else "Fresh match created with an empty player list."
        )

        await interaction.response.send_message(
            description, ephemeral=True
        )

    @discord.ui.button(
        label="Fresh Host",
        emoji="🆕",
        style=discord.ButtonStyle.primary,
    )
    async def fresh_host(self, interaction, button):
        await self.create_rehost(interaction, same_players=False)

    @discord.ui.button(
        label="Rehost Same Players",
        emoji="🔁",
        style=discord.ButtonStyle.secondary,
    )
    async def same_players(self, interaction, button):
        await self.create_rehost(interaction, same_players=True)


# ---------------- BOT SETUP ----------------

intents = discord.Intents.default()
intents.message_content = True

class HostingBot(commands.Bot):
    async def setup_hook(self):
        init_db()

        # Restore match buttons after bot restarts.
        with db_connect() as conn:
            matches = conn.execute(
                "SELECT id FROM matches"
            ).fetchall()

        for match in matches:
            self.add_view(MatchView(int(match["id"])))

        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            print(f"Synced {len(synced)} commands to guild {GUILD_ID}")
        else:
            synced = await self.tree.sync()
            print(f"Synced {len(synced)} global commands")


bot = HostingBot(command_prefix="!", intents=intents)


# ---------------- RESULT MESSAGE HANDLING ----------------

@bot.event
async def on_message(message):
    if message.author.bot or not message.guild:
        return

    key = (message.channel.id, message.author.id)
    match_id = pending_results.get(key)

    if match_id is not None:
        match = get_match(match_id)

        if not message.content.strip():
            await message.reply(
                "Please type the result as text.", mention_author=False
            )
            return

        if not match or match["status"] != "open":
            pending_results.pop(key, None)
            await message.reply(
                "This match is no longer open for results.",
                mention_author=False,
            )
            return

        if not can_manage_message(message, match):
            pending_results.pop(key, None)
            await message.reply(
                "You are not authorized to post results for this match.",
                mention_author=False,
            )
            return

        results_channel = await find_results_channel(message.guild)

        if results_channel is None:
            await message.reply(
                "I couldn't find #match-results. Create that channel or set "
                "`MATCH_RESULTS_CHANNEL_ID` in FadeHost.",
                mention_author=False,
            )
            return

        result_text = message.content.strip()

        result_embed = make_match_embed(match)
        result_embed.title = f"📋 Match Results — {match['mode']}"
        result_embed.description = result_text

        try:
            await results_channel.send(embed=result_embed)
        except discord.Forbidden:
            await message.reply(
                "I can't send messages in #match-results. Give me View "
                "Channel, Send Messages, and Embed Links permissions.",
                mention_author=False,
            )
            return

        pending_results.pop(key, None)
        update_match(
            match_id,
            status="completed",
            result=result_text,
        )

        await message.add_reaction("✅")
        await refresh_match(message.guild, match_id)
        await message.reply(
            f"Results posted in {results_channel.mention}. "
            "You can now use the Rehost button.",
            mention_author=False,
        )

    await bot.process_commands(message)


def can_manage_message(message, match):
    member = message.author

    return (
        member.id == match["host_id"]
        or (
            isinstance(member, discord.Member)
            and (
                has_host_role(member)
                or member.guild_permissions.administrator
                or member.guild_permissions.manage_guild
            )
        )
    )


# ---------------- SLASH COMMANDS ----------------

@bot.tree.command(
    name="host",
    description="Host a 1v1, 2v2, or 3v3 match.",
)
@app_commands.describe(
    mode="Choose the match format",
    region="Choose the match region",
    players="Optional player names or mentions, separated by commas",
)
@app_commands.choices(
    mode=[
        app_commands.Choice(name="1v1s", value="1v1s"),
        app_commands.Choice(name="2v2s", value="2v2s"),
        app_commands.Choice(name="3v3s", value="3v3s"),
    ],
    region=[
        app_commands.Choice(name="NA", value="NA"),
        app_commands.Choice(name="EU", value="EU"),
        app_commands.Choice(name="OCE", value="OCE"),
        app_commands.Choice(name="Other", value="Other"),
    ],
)
async def host(
    interaction: discord.Interaction,
    mode: app_commands.Choice[str],
    region: app_commands.Choice[str],
    players: str = "",
):
    if not interaction.guild or not isinstance(
        interaction.user, discord.Member
    ):
        await interaction.response.send_message(
            "Use this command inside the server.", ephemeral=True
        )
        return

    # Only the Host role can create a match.
    if not has_host_role(interaction.user):
        await interaction.response.send_message(
            "You need the Host role to use `/host`.",
            ephemeral=True,
        )
        return

    if not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message(
            "Use `/host` in a text channel.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    match_id = create_match(
        guild_id=interaction.guild.id,
        channel_id=interaction.channel.id,
        host_id=interaction.user.id,
        mode=mode.value,
        region=region.value,
        players=players,
    )

    await send_match(interaction.guild, match_id)
    await interaction.followup.send(
        "Your match has been posted in this channel.", ephemeral=True
    )


@bot.tree.command(
    name="hostinghelp",
    description="Show how to use the hosting bot.",
)
async def hostinghelp(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Arsenal Angels Hosting Bot",
        color=discord.Color.blurple(),
        description=(
            "**/host** — create a match (Host role required).\n"
            "Choose 1v1s, 2v2s, or 3v3s and a region: NA, EU, OCE, or Other.\n\n"
            "**Post Results** — click the button, then type the result as "
            "your next message in the hosting channel.\n"
            "**Rehost** — after results are posted, choose Fresh Host or "
            "Rehost Same Players."
        ),
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


if not TOKEN:
    raise RuntimeError(
        "Set DISCORD_TOKEN in your FadeHost environment variables."
    )

bot.run(TOKEN)
