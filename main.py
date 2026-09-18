"""Discord bot: a "sticky" message that always stays at the bottom of the channel.

Principle: every time a member posts a new message in a channel with an
active sticky, a counter is incremented. Once the threshold is reached, the
old sticky message is deleted and a new one is sent (so it's always at the
bottom of the conversation), then the counter is reset to 0.
"""
import asyncio
import logging
import os
import random
import re
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from db import Database


load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("sticky-bot")

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")
DATA_DIR = os.getenv("DATA_DIR", "/data")
DEFAULT_THRESHOLD = int(os.getenv("DEFAULT_THRESHOLD", "1"))

INTENTS = discord.Intents.default()

ACTIVITY_TYPES = {
    "playing": discord.ActivityType.playing,
    "watching": discord.ActivityType.watching,
    "listening": discord.ActivityType.listening,
    "competing": discord.ActivityType.competing,
}
STATUS_TYPES = {
    "online": discord.Status.online,
    "idle": discord.Status.idle,
    "dnd": discord.Status.dnd,
    "invisible": discord.Status.invisible,
}


def build_presence(status_key: str, activity_type_key: str, activity_text: str):
    """Construit les objets discord.Status / discord.Activity à partir de valeurs stockées en base."""
    status_obj = STATUS_TYPES.get(status_key, discord.Status.online)
    activity_obj = None
    if activity_type_key and activity_text:
        activity_obj = discord.Activity(
            type=ACTIVITY_TYPES.get(activity_type_key, discord.ActivityType.playing),
            name=activity_text,
        )
    return status_obj, activity_obj


class GivewayBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=INTENTS)
        self.db = Database()

    async def setup_hook(self):
        await self.db.connect()

        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Commandes synchronisées sur le serveur de test %s", GUILD_ID)
        else:
            await self.tree.sync()
            log.info("Commandes synchronisées globalement (jusqu'à 1h pour apparaître partout)")


    async def on_ready(self):
        log.info("Connecté en tant que %s (id: %s)", self.user, self.user.id)

        target_guild = self.get_guild(1498448873000144896)
        if target_guild:
            log.info("Salons du serveur %s (%s) :", target_guild.name, target_guild.id)
            for channel in target_guild.channels:
                log.info("  %s - %s", channel.id, channel.name)
        else:
            log.warning("Serveur 1498448873000144896 introuvable (le bot n'y est peut-être pas présent).")

        presence = await self.db.get_presence()
        if presence and presence["status"]:
            status_obj, activity_obj = build_presence(
                presence["status"], presence["activity_type"], presence["activity_text"]
            )
            await self.change_presence(status=status_obj, activity=activity_obj)
            log.info("Présence restaurée : %s / %s", presence["status"], presence["activity_text"])

        await self.resume_giveaways()

    async def resume_giveaways(self):
        """Reschedules giveaways that were still active before the bot restarted."""
        rows = await self.db.get_active_giveaways()
        for row in rows:
            channel = self.get_channel(row["channel_id"])
            if channel is None:
                try:
                    channel = await self.fetch_channel(row["channel_id"])
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    log.warning("Giveaway %s: channel %s unreachable, dropping it.", row["message_id"], row["channel_id"])
                    await self.db.remove_giveaway(row["message_id"])
                    continue

            try:
                message = await channel.fetch_message(row["message_id"])
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                log.warning("Giveaway %s: message not found, dropping it.", row["message_id"])
                await self.db.remove_giveaway(row["message_id"])
                continue

            try:
                host = await self.fetch_user(row["host_id"])
            except (discord.NotFound, discord.HTTPException):
                host = message.author  # fallback: the bot itself

            log.info("Reprise du giveaway %s (fin prévue à %s)", row["message_id"], row["end_ts"])
            asyncio.create_task(
                run_giveaway(
                    channel=channel,
                    message=message,
                    prize=row["prize"],
                    winners_count=row["winners"],
                    end_ts=row["end_ts"],
                    color=discord.Color(row["color"]),
                    host=host,
                    db=self.db,
                )
            )


bot = GivewayBot()


# --- People/role allowed to change the bot's name/avatar ---
# Comma-separated Discord IDs, e.g. "111111111111111111,222222222222222222"
BOT_ADMIN_IDS = {
    int(x) for x in os.getenv("BOT_ADMIN_IDS", "").split(",") if x.strip().isdigit()
}
# Allowed role ID (optional), e.g. "333333333333333333"
_role_env = os.getenv("BOT_ADMIN_ROLE_ID", "").strip()
BOT_ADMIN_ROLE_ID = int(_role_env) if _role_env.isdigit() else None


def is_admin(member: discord.Member) -> bool:
    """Vérifie si le membre est autorisé (via le .env ou permissions du serveur)."""
    # 1. Vérifie si l'utilisateur est dans la liste ADMINS du .env
    if member.id in BOT_ADMIN_IDS:
        return True
    
    # 2. Vérifie si l'utilisateur est un administrateur sur le serveur Discord
    if hasattr(member, "guild_permissions") and member.guild_permissions.manage_guild:
        return True
        
    return False


@bot.tree.command(name="bot-config", description="Change the bot's name, profile picture, banner, and/or status")
@app_commands.describe(
    name="New bot name (leave empty to keep the current name)",
    picture="New profile picture (leave empty to keep the current picture)",
    banner="New banner image (leave empty to keep the current banner)",
    status="Online status",
    activity_type="Type of activity shown next to the status text",
    activity_text="Text shown next to the status (e.g. 'over the server')",
)
@app_commands.choices(
    status=[
        app_commands.Choice(name="Online", value="online"),
        app_commands.Choice(name="Idle", value="idle"),
        app_commands.Choice(name="Do Not Disturb", value="dnd"),
        app_commands.Choice(name="Invisible", value="invisible"),
    ],
    activity_type=[
        app_commands.Choice(name="Playing", value="playing"),
        app_commands.Choice(name="Watching", value="watching"),
        app_commands.Choice(name="Listening to", value="listening"),
        app_commands.Choice(name="Competing in", value="competing"),
    ],
)
async def bot_config(
    interaction: discord.Interaction,
    name: str = None,
    picture: discord.Attachment = None,
    banner: discord.Attachment = None,
    status: app_commands.Choice[str] = None,
    activity_type: app_commands.Choice[str] = None,
    activity_text: str = None,
):
    if not is_admin(interaction.user):
        await interaction.response.send_message(
            "❌ You don't have permission to use this command.", ephemeral=True
        )
        return

    if not any([name, picture, banner, status, activity_text]):
        await interaction.response.send_message(
            "Please provide at least one field to change.", ephemeral=True
        )
        return

    for attachment, label in ((picture, "picture"), (banner, "banner")):
        if attachment and not (attachment.content_type or "").startswith("image/"):
            await interaction.response.send_message(f"❌ The {label} file is not an image.", ephemeral=True)
            return

    await interaction.response.defer(ephemeral=True)
    changes = []

    # --- Name / picture / banner : profile edit via REST API ---
    kwargs = {}
    if name:
        kwargs["username"] = name
    if picture:
        kwargs["avatar"] = await picture.read()
    if banner:
        kwargs["banner"] = await banner.read()

    if kwargs:
        try:
            await bot.user.edit(**kwargs)
        except discord.HTTPException as e:
            await interaction.followup.send(
                f"❌ Too many changes (max 2 name changes/hour): {e}",
                ephemeral=True,
            )
            return
        if name:
            changes.append(f"name → **{name}**")
        if picture:
            changes.append("profile picture updated")
        if banner:
            changes.append("banner updated")

    # --- Status / activity : live presence via the gateway (not the REST profile) ---
    if status or activity_text:
        current = await bot.db.get_presence()
        status_key = status.value if status else (current["status"] if current else "online")
        activity_type_key = activity_type.value if activity_type else (current["activity_type"] if current else None)
        text_key = activity_text if activity_text is not None else (current["activity_text"] if current else None)

        await bot.db.set_presence(status_key, activity_type_key, text_key)
        status_obj, activity_obj = build_presence(status_key, activity_type_key, text_key)
        await bot.change_presence(status=status_obj, activity=activity_obj)

        if status:
            changes.append(f"status → **{status.name}**")
        if activity_text:
            changes.append(f"activity → **{activity_text}**")

    await interaction.followup.send("✅ " + " and ".join(changes), ephemeral=True)


# --- Giveaways ---

GIVEAWAY_EMOJI = "🎉"

DURATION_UNIT_SECONDS = {"d": 86400, "h": 3600, "m": 60, "s": 1}
DURATION_RE = re.compile(r"(\d+)\s*([dhms])", re.IGNORECASE)

COLOR_NAMES = {
    "red": discord.Color.red(),
    "dark_red": discord.Color.dark_red(),
    "green": discord.Color.green(),
    "dark_green": discord.Color.dark_green(),
    "blue": discord.Color.blue(),
    "dark_blue": discord.Color.dark_blue(),
    "blurple": discord.Color.blurple(),
    "gold": discord.Color.gold(),
    "orange": discord.Color.orange(),
    "purple": discord.Color.purple(),
    "dark_purple": discord.Color.dark_purple(),
    "magenta": discord.Color.magenta(),
    "teal": discord.Color.teal(),
    "dark_teal": discord.Color.dark_teal(),
    "yellow": discord.Color.from_rgb(255, 221, 0),
    "pink": discord.Color.from_rgb(255, 105, 180),
    "black": discord.Color.from_rgb(0, 0, 0),
    "white": discord.Color.from_rgb(255, 255, 255),
    "grey": discord.Color.greyple(),
    "gray": discord.Color.greyple(),
}


def parse_duration(duration_str: str) -> int:
    """Parses a duration like '10m', '2h', '1d12h' into a number of seconds."""
    matches = DURATION_RE.findall(duration_str.strip())
    if not matches:
        raise ValueError(
            "Invalid duration. Use a combination of d/h/m/s, e.g. `30m`, `2h`, `1d12h`."
        )
    seconds = sum(int(value) * DURATION_UNIT_SECONDS[unit.lower()] for value, unit in matches)
    if seconds <= 0:
        raise ValueError("Duration must be greater than 0.")
    if seconds > 30 * 86400:
        raise ValueError("Duration is too long (max 30 days).")
    return seconds


def parse_color(color_str: Optional[str]) -> discord.Color:
    """Parses a color name or hex code into a discord.Color. Defaults to blurple."""
    if not color_str or not color_str.strip():
        return discord.Color.blurple()
    key = color_str.strip().lower().replace(" ", "_")
    if key in COLOR_NAMES:
        return COLOR_NAMES[key]
    hex_str = key.lstrip("#")
    if re.fullmatch(r"[0-9a-f]{6}", hex_str):
        return discord.Color(int(hex_str, 16))
    if re.fullmatch(r"[0-9a-f]{3}", hex_str):
        hex_str = "".join(c * 2 for c in hex_str)
        return discord.Color(int(hex_str, 16))
    raise ValueError(
        f"Unrecognized color `{color_str}`. Use a name (e.g. `red`, `blurple`, `gold`) "
        "or a hex code (e.g. `#ff5733`)."
    )


def build_giveaway_embed(
    prize: str,
    winners: int,
    end_ts: int,
    color: discord.Color,
    host: discord.abc.User,
    ended: bool = False,
) -> discord.Embed:
    embed = discord.Embed(
        title="🎉 A giveaway is in progress! 🎉" if not ended else "🎉 Giveaway ended 🎉",
        description=prize,
        color=color if not ended else discord.Color.greyple(),
    )
    embed.add_field(name="Winners", value=str(winners), inline=True)
    if ended:
        embed.add_field(name="Ended", value=f"<t:{end_ts}:R>", inline=True)
    else:
        embed.add_field(name="Time remaining", value=f"<t:{end_ts}:R>", inline=True)
        embed.set_footer(text=f"React with {GIVEAWAY_EMOJI} to enter! • Hosted by {host.display_name}")
    return embed


async def run_giveaway(
    channel: discord.abc.Messageable,
    message: discord.Message,
    prize: str,
    winners_count: int,
    end_ts: int,
    color: discord.Color,
    host: discord.abc.User,
    db: Database,
):
    """Waits until end_ts, then picks winners among the people who reacted."""
    remaining = end_ts - int(time.time())
    if remaining > 0:
        await asyncio.sleep(remaining)

    final_ts = int(time.time())

    try:
        message = await channel.fetch_message(message.id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        log.warning("Giveaway message %s could not be fetched at draw time.", message.id)
        await db.remove_giveaway(message.id)
        return

    entrants = []
    for reaction in message.reactions:
        if str(reaction.emoji) == GIVEAWAY_EMOJI:
            async for user in reaction.users():
                if not user.bot and user.id not in {u.id for u in entrants}:
                    entrants.append(user)
            break

    ended_embed = build_giveaway_embed(prize, winners_count, final_ts, color, host, ended=True)

    if not entrants:
        ended_embed.add_field(name="Result", value="No valid entries — no winner could be drawn.", inline=False)
        try:
            await message.edit(embed=ended_embed)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
        await channel.send(f"🎉 The giveaway for **{prize}** ended, but nobody entered. No winner this time!")
        await db.remove_giveaway(message.id)
        return

    chosen = random.sample(entrants, k=min(winners_count, len(entrants)))
    mentions = ", ".join(winner.mention for winner in chosen)

    ended_embed.add_field(name="Winner(s)", value=mentions, inline=False)
    try:
        await message.edit(embed=ended_embed)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass

    congrats_embed = discord.Embed(
        title="🎉 Congratulations! 🎉",
        description=f"{mentions} — you won **{prize}**!",
        color=discord.Color.gold(),
    )
    congrats_embed.set_footer(text=f"Hosted by {host.display_name}")
    await channel.send(content=mentions, embed=congrats_embed)
    await db.remove_giveaway(message.id)


@bot.tree.command(name="giveaway", description="Start a giveaway")
@app_commands.describe(
    duration="How long the giveaway runs, e.g. 30m, 2h, 1d12h",
    prize="The text to display for the giveaway (what's being won)",
    winners="Number of winners to draw",
    color="Embed color: a name (red, blue, gold, blurple...) or hex code (#ff5733). Optional.",
)

async def giveaway(
    interaction: discord.Interaction,
    duration: str,
    prize: str,
    winners: app_commands.Range[int, 1, 50],
    color: Optional[str] = None,
):
    if not is_admin(interaction.user):
        await interaction.response.send_message(
            "❌ You don't have permission to create giveaways.", ephemeral=True
        )
        return
    try:
        duration_seconds = parse_duration(duration)
    except ValueError as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        return

    try:
        embed_color = parse_color(color)
    except ValueError as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        return

    end_ts = int(time.time()) + duration_seconds
    embed = build_giveaway_embed(prize, winners, end_ts, embed_color, interaction.user)

    await interaction.response.send_message(embed=embed)
    message = await interaction.original_response()
    await message.add_reaction(GIVEAWAY_EMOJI)

    await bot.db.add_giveaway(
        message_id=message.id,
        channel_id=interaction.channel.id,
        guild_id=interaction.guild.id if interaction.guild else None,
        host_id=interaction.user.id,
        prize=prize,
        winners=winners,
        color=embed_color.value,
        end_ts=end_ts,
    )

    asyncio.create_task(
        run_giveaway(
            channel=interaction.channel,
            message=message,
            prize=prize,
            winners_count=winners,
            end_ts=end_ts,
            color=embed_color,
            host=interaction.user,
            db=bot.db,
        )
    )


async def main():
    if not TOKEN:
        raise SystemExit("❌ La variable d'environnement DISCORD_TOKEN est manquante (voir .env.example).")
    async with bot:
        await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())