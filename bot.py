
import os
import json
import sqlite3
from datetime import datetime, timezone

import aiohttp
import discord
import asyncio
from discord import app_commands
from discord.ext import commands

# ---------------- CONFIGURATION ----------------

HOST_ROLE_ID = int(os.getenv("HOST_ROLE_ID", "1565048538042208466"))
GUILD_ID = int(os.getenv("GUILD_ID", "0") or "0")
MATCH_RESULTS_CHANNEL_ID = int(
    os.getenv("MATCH_RESULTS_CHANNEL_ID", "0") or "0"
)
BLOXLINK_VERIFIED_ROLE_ID = int(
    os.getenv("BLOXLINK_VERIFIED_ROLE_ID", "0") or "0"
)
BLOXLINK_API_KEY = os.getenv("BLOXLINK_API_KEY", "").strip()

DB_PATH = os.getenv("DB_PATH", "./data/hosting.sqlite3")
TOKEN = os.getenv("DISCORD_TOKEN") or os.getenv("DISCORD_BOT_TOKEN")

MODES = ("1v1s", "2v2s", "3v3s")
REGIONS = ("NA", "EU", "OCE", "Other")
SLOT_COUNTS = {"1v1s": 2, "2v2s": 4, "3v3s": 6}

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

        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(matches)")
        }

        if "player_ids" not in columns:
            conn.execute(
                "ALTER TABLE matches ADD COLUMN player_ids TEXT NOT NULL DEFAULT '[]'"
            )

        if "private_server_link" not in columns:
            conn.execute(
                "ALTER TABLE matches ADD COLUMN private_server_link TEXT NOT NULL DEFAULT ''"
            )


def get_match(match_id):
    with db_connect() as conn:
        return conn.execute(
            "SELECT * FROM matches WHERE id = ?", (match_id,)
        ).fetchone()


def get_players(match):
    try:
        value = json.loads(match["player_ids"] or "[]")
        return value if isinstance(value, list) else []
    except (ValueError, TypeError, IndexError):
        return []


def create_match(
    guild_id, channel_id, host_id, mode, region,
    private_server_link="", player_ids=None
):
    with db_connect() as conn:
        cur = conn.execute("""
            INSERT INTO matches (
                guild_id, channel_id, host_id, mode, region,
                players, status, result, created_at,
                player_ids, private_server_link
            )
            VALUES (?, ?, ?, ?, ?, '', 'open', '', ?, ?, ?)
        """, (
            guild_id,
            channel_id,
            host_id,
            mode,
            region,
            datetime.now(timezone.utc).isoformat(),
            json.dumps(player_ids or []),
            private_server_link,
        ))
        return cur.lastrowid


def update_match(match_id, **fields):
    allowed = {
        "message_id", "host_id", "mode", "region", "players",
        "status", "result", "channel_id", "player_ids",
        "private_server_link",
    }
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return

    assignments = ", ".join(f"{k} = ?" for k in fields)
    with db_connect() as conn:
        conn.execute(
            f"UPDATE matches SET {assignments} WHERE id = ?",
            list(fields.values()) + [match_id],
        )


def save_players(match_id, player_ids):
    update_match(match_id, player_ids=json.dumps(player_ids))


# ---------------- VERIFICATION ----------------

def has_host_role(member):
    return any(role.id == HOST_ROLE_ID for role in member.roles)


def is_bloxlink_verified(member):
    return (
        BLOXLINK_VERIFIED_ROLE_ID != 0
        and any(
            role.id == BLOXLINK_VERIFIED_ROLE_ID
            for role in member.roles
        )
    )





async def get_roblox_username(member):
    try:
        bloxlink_timeout = aiohttp.ClientTimeout(total=15, connect=5)

        async with aiohttp.ClientSession(
            timeout=bloxlink_timeout
        ) as session:
            url = (
                "https://api.blox.link/v4/public/guilds/"
                f"{GUILD_ID}/discord-to-roblox/{member.id}"
            )

            async with session.get(
                url,
                headers={"Authorization": BLOXLINK_API_KEY},
            ) as response:
                print(f"Bloxlink HTTP status: {response.status}")

                if response.status != 200:
                    print(f"Bloxlink returned: {(await response.text())[:300]}")
                    return None

                data = await response.json(content_type=None)

        roblox_id = data.get("robloxID") or data.get("robloxId")
        print(f"Bloxlink returned a Roblox ID: {bool(roblox_id)}")

        if not roblox_id:
            print(f"Bloxlink response keys: {list(data.keys())}")
            return None

        roblox_timeout = aiohttp.ClientTimeout(total=15, connect=5)

        print("Starting Roblox username lookup...")

        async with aiohttp.ClientSession(
            timeout=roblox_timeout
        ) as session:
            async with session.get(
                f"https://users.roblox.com/v1/users/{roblox_id}"
            ) as response:
                print(f"Roblox HTTP status: {response.status}")

                if response.status != 200:
                    print(f"Roblox returned: {(await response.text())[:300]}")
                    return None

                roblox_data = await response.json()
                return roblox_data.get("name")

    except asyncio.TimeoutError:
        print("Username lookup timed out. Check the last HTTP status printed.")
        return None
    except (aiohttp.ClientError, ValueError) as error:
        print(f"Username lookup failed: {type(error).__name__}: {error!r}")
        return None


# ---------------- MATCH EMBEDS ----------------

def make_match_embed(match):
    completed = match["status"] == "completed"
    embed = discord.Embed(
        title=f"🎮 {match['mode']} Match",
        color=discord.Color.green() if completed else discord.Color.blurple(),
    )

    embed.add_field(name="Region", value=match["region"], inline=True)
    embed.add_field(
        name="Hosted by", value=f"<@{match['host_id']}>", inline=True
    )
    embed.add_field(
        name="Status", value=match["status"].title(), inline=True
    )

    players = get_players(match)
    slots = []

    for index in range(SLOT_COUNTS.get(match["mode"], 2)):
        if index < len(players):
            player_id = players[index]
            slots.append(f"Player {index + 1}: <@{player_id}>")
        else:
            slots.append(f"Player {index + 1}: N/A")

    embed.add_field(
        name="Players",
        value="\n".join(slots),
        inline=False,
    )

    if match["private_server_link"]:
        embed.add_field(
            name="Private Server",
            value="Use the Join Private Server button below.",
            inline=False,
        )

    if match["result"]:
        embed.add_field(
            name="Result", value=match["result"][:1024], inline=False
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
    def __init__(self, match_id):
        super().__init__(timeout=None)
        self.match_id = match_id

        join = discord.ui.Button(
            label="Join Host",
            emoji="➕",
            style=discord.ButtonStyle.success,
            custom_id=f"hosting:{match_id}:join",
        )
        join.callback = self.join_match
        self.add_item(join)

        leave = discord.ui.Button(
            label="Leave Host",
            emoji="➖",
            style=discord.ButtonStyle.secondary,
            custom_id=f"hosting:{match_id}:leave",
        )
        leave.callback = self.leave_match
        self.add_item(leave)

        results = discord.ui.Button(
            label="Post Results",
            emoji="📨",
            style=discord.ButtonStyle.success,
            custom_id=f"hosting:{match_id}:results",
        )
        results.callback = self.post_results
        self.add_item(results)

        rehost = discord.ui.Button(
            label="Rehost",
            emoji="🔁",
            style=discord.ButtonStyle.primary,
            custom_id=f"hosting:{match_id}:rehost",
        )
        rehost.callback = self.rehost
        self.add_item(rehost)

        match = get_match(match_id)
        if match and match["private_server_link"]:
            link_button = discord.ui.Button(
                label="Join Private Server",
                emoji="🔗",
                style=discord.ButtonStyle.link,
                url=match["private_server_link"],
            )
            self.add_item(link_button)

    async def join_match(self, interaction):
        match = get_match(self.match_id)
        if not match or match["status"] != "open":
            await interaction.response.send_message(
                "This match is no longer open.", ephemeral=True
            )
            return

        if not await require_verification(interaction):
            return

        players = get_players(match)
        user_id = interaction.user.id

        if user_id in players:
            await interaction.response.send_message(
                "You're already in this match.", ephemeral=True
            )
            return

        capacity = SLOT_COUNTS[match["mode"]]
        if len(players) >= capacity:
            await interaction.response.send_message(
                "This match is full.", ephemeral=True
            )
            return

        players.append(user_id)
        save_players(self.match_id, players)
        await refresh_match(interaction.guild, self.match_id)

        await interaction.response.send_message(
            "You joined the match!", ephemeral=True
        )

    async def leave_match(self, interaction):
        match = get_match(self.match_id)
        if not match or match["status"] != "open":
            await interaction.response.send_message(
                "This match is no longer open.", ephemeral=True
            )
            return

        if not await require_verification(interaction):
            return

        players = get_players(match)
        if interaction.user.id not in players:
            await interaction.response.send_message(
                "You're not in this match.", ephemeral=True
            )
            return

        players.remove(interaction.user.id)
        save_players(self.match_id, players)
        await refresh_match(interaction.guild, self.match_id)

        await interaction.response.send_message(
            "You left the match.", ephemeral=True
        )

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
                "Only the host or authorized staff can post results.",
                ephemeral=True,
            )
            return

        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message(
                "Use this button in the hosting channel.", ephemeral=True
            )
            return

        pending_results[(interaction.channel.id, interaction.user.id)] = (
            self.match_id
        )
        await interaction.response.send_message(
            "Type the match result as your next message in this channel.",
            ephemeral=True,
        )

    async def rehost(self, interaction):
        match = get_match(self.match_id)
        if not match or match["status"] != "completed":
            await interaction.response.send_message(
                "Post the results before rehosting.", ephemeral=True
            )
            return

        if not can_manage_match(interaction, match):
            await interaction.response.send_message(
                "You are not authorized to rehost this match.",
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

        if not await require_verification(interaction):
            return

        new_id = create_match(
            guild_id=old["guild_id"],
            channel_id=old["channel_id"],
            host_id=interaction.user.id,
            mode=old["mode"],
            region=old["region"],
            private_server_link=old["private_server_link"],
            player_ids=get_players(old) if same_players else [],
        )

        await send_match(interaction.guild, new_id)
        await interaction.response.send_message(
            "Rehost created.", ephemeral=True
        )

    @discord.ui.button(label="Fresh Host", emoji="🆕")
    async def fresh_host(self, interaction, button):
        await self.create_rehost(interaction, False)

    @discord.ui.button(label="Rehost Same Players", emoji="🔁")
    async def same_players(self, interaction, button):
        await self.create_rehost(interaction, True)


def can_manage_match(interaction, match):
    member = interaction.user
    return isinstance(member, discord.Member) and (
        member.id == match["host_id"]
        or has_host_role(member)
        or member.guild_permissions.administrator
        or member.guild_permissions.manage_guild
    )


def can_manage_message(message, match):
    member = message.author
    return isinstance(member, discord.Member) and (
        member.id == match["host_id"]
        or has_host_role(member)
        or member.guild_permissions.administrator
        or member.guild_permissions.manage_guild
    )


# ---------------- BOT SETUP ----------------

intents = discord.Intents.default()
intents.message_content = True


class HostingBot(commands.Bot):
    async def setup_hook(self):
        init_db()

        with db_connect() as conn:
            matches = conn.execute("SELECT id FROM matches").fetchall()

        for row in matches:
            self.add_view(MatchView(int(row["id"])))

        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            print(f"Synced {len(synced)} commands to guild {GUILD_ID}")
        else:
            await self.tree.sync()


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
            await message.reply("Please type the result.", mention_author=False)
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
                "You are not authorized to post these results.",
                mention_author=False,
            )
            return

        results_channel = await find_results_channel(message.guild)
        if results_channel is None:
            await message.reply(
                "I couldn't find #match-results. Check its ID in FadeHost.",
                mention_author=False,
            )
            return

        result_text = message.content.strip()
        update_match(match_id, status="completed", result=result_text)
        completed = get_match(match_id)

        try:
            await results_channel.send(
                embed=make_match_embed(completed)
            )
        except discord.HTTPException:
            update_match(match_id, status="open", result="")
            await message.reply(
                "I couldn't post the results. Check my channel permissions.",
                mention_author=False,
            )
            return

        pending_results.pop(key, None)
        await message.add_reaction("✅")
        await refresh_match(message.guild, match_id)
        await message.reply(
            f"Results posted in {results_channel.mention}.",
            mention_author=False,
        )

    await bot.process_commands(message)


# ---------------- SLASH COMMANDS ----------------


@bot.tree.command(name="host", description="Create a match.")
@app_commands.describe(
    mode="Match format",
    region="Match region",
    link="Optional Arsenal private server link",
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
    link: str = "",
):
    # Respond to Discord immediately so the interaction doesn't time out.
    await interaction.response.defer(ephemeral=True, thinking=True)

    try:
        if not interaction.guild or not isinstance(
            interaction.user, discord.Member
        ):
            await interaction.followup.send(
                "Use this command inside the server.", ephemeral=True
            )
            return

        if not has_host_role(interaction.user):
            await interaction.followup.send(
                "You need the Host role to use /host.", ephemeral=True
            )
            return

        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.followup.send(
                "Use /host in a text channel.", ephemeral=True
            )
            return

        if link and not (
            link.startswith("https://")
            and "roblox.com" in link.lower()
        ):
            await interaction.followup.send(
                "Please provide a valid HTTPS Roblox private-server link.",
                ephemeral=True,
            )
            return

        if not await require_verification(interaction):
            return

        match_id = create_match(
            guild_id=interaction.guild.id,
            channel_id=interaction.channel.id,
            host_id=interaction.user.id,
            mode=mode.value,
            region=region.value,
            private_server_link=link,
        )

        await send_match(interaction.guild, match_id)

        await interaction.followup.send(
            "Your match has been posted.", ephemeral=True
        )

    except Exception as error:
        print(f"/host command failed: {error!r}")

        await interaction.followup.send(
            "Something went wrong while creating your match. "
            "Please try again later or contact server staff.",
            ephemeral=True,
        )


@bot.tree.command(name="hostinghelp", description="Hosting bot help.")
async def hostinghelp(interaction: discord.Interaction):
    await interaction.response.send_message(
        "**Arsenal Angels Hosting**\n"
        "`/host` — create a match (Host role required).\n"
        "Use Join Host or Leave Host to manage player slots.\n"
        "Verified Roblox usernames are retrieved through Bloxlink.\n"
        "Post Results lets the host submit the score; Rehost creates "
        "another match after completion.",
        ephemeral=True,
    )


if not TOKEN:
    raise RuntimeError("Set DISCORD_TOKEN in FadeHost.")

bot.run(TOKEN)

