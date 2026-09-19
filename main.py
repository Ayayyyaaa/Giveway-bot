import asyncio
import io
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
INTENTS.message_content = True

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
        # In-memory cache of auto-responder triggers, kept in sync with the DB so
        # on_message doesn't need a database round-trip for every single message.
        # Shape: {guild_id: {word_lowercase: {"response": str, "reaction": str|None, "cooldown_seconds": int}}}
        self.trigger_cache: dict[int, dict[str, dict]] = {}
        # Last time (time.time()) each (guild_id, word) trigger fired, for cooldown checks.
        self.trigger_last_fired: dict[tuple[int, str], float] = {}

    async def setup_hook(self):
        await self.db.connect()
        await self.load_triggers()

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
                    reward=row.get("reward"),
                    banner_url=row.get("banner_url"),
                    picture_url=row.get("picture_url"),
                )
            )

    async def load_triggers(self):
        """Loads every auto-responder trigger from the DB into the in-memory cache."""
        self.trigger_cache.clear()
        rows = await self.db.get_triggers()
        for row in rows:
            self.trigger_cache.setdefault(row["guild_id"], {})[row["word"]] = {
                "response": row["response"],
                "reaction": row["reaction"],
                "cooldown_seconds": row["cooldown_seconds"],
            }
        log.info("Loaded %d auto-responder trigger(s) from the database.", len(rows))

    async def on_message(self, message: discord.Message):
        # Let discord.ext.commands still process the "!" prefix commands, if any are added later.
        await self.process_commands(message)

        if message.author.bot or not message.guild:
            return

        triggers = self.trigger_cache.get(message.guild.id)
        if not triggers:
            return

        content_lower = message.content.lower()
        now = time.time()

        for word, cfg in triggers.items():
            if not re.search(rf"\b{re.escape(word)}\b", content_lower):
                continue

            key = (message.guild.id, word)
            last_fired = self.trigger_last_fired.get(key, 0.0)
            if now - last_fired < cfg["cooldown_seconds"]:
                continue  # still on cooldown, skip silently
            self.trigger_last_fired[key] = now

            if cfg["response"]:
                try:
                    await message.channel.send(cfg["response"])
                except discord.HTTPException:
                    log.warning("Failed to send auto-responder message for trigger '%s'.", word)
            if cfg["reaction"]:
                try:
                    await message.add_reaction(cfg["reaction"])
                except discord.HTTPException:
                    log.warning("Failed to add reaction '%s' for trigger '%s'.", cfg["reaction"], word)


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
DURATION_FULL_RE = re.compile(r"^\s*(?:\d+\s*[dhms]\s*)+$", re.IGNORECASE)

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
    if not DURATION_FULL_RE.match(duration_str):
        raise ValueError(
            "Invalid duration. Use a combination of d/h/m/s, e.g. `30m`, `2h`, `1d12h`."
        )
    matches = DURATION_RE.findall(duration_str)
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
    reward: Optional[str] = None,
    banner_url: Optional[str] = None,
    picture_url: Optional[str] = None,
    ended: bool = False,
) -> discord.Embed:
    embed = discord.Embed(
        title="🎉 A giveaway is in progress! 🎉" if not ended else "🎉 Giveaway ended 🎉",
        description=prize,
        color=color if not ended else discord.Color.greyple(),
    )
    if reward:
        embed.add_field(name="Reward", value=reward, inline=True)
    embed.add_field(name="Winners", value=str(winners), inline=True)
    if ended:
        embed.add_field(name="Ended", value=f"<t:{end_ts}:R>", inline=True)
    else:
        embed.add_field(name="Time remaining", value=f"<t:{end_ts}:R>", inline=True)
        embed.set_footer(text=f"React with {GIVEAWAY_EMOJI} to enter! • Hosted by {host.display_name}")
    if picture_url:
        embed.set_thumbnail(url=picture_url)
    if banner_url:
        embed.set_image(url=banner_url)
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
    reward: Optional[str] = None,
    banner_url: Optional[str] = None,
    picture_url: Optional[str] = None,
):
    """Waits until end_ts, then picks winners among the people who reacted."""
    remaining = end_ts - int(time.time())
    if remaining > 0:
        await asyncio.sleep(remaining)

    final_ts = int(time.time())
    # What to call the prize in the winner announcement, without repeating the full
    # giveaway text: the short "reward" field if one was set, else the giveaway text itself.
    reward_label = reward if reward else prize

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

    ended_embed = build_giveaway_embed(
        prize, winners_count, final_ts, color, host,
        reward=reward, banner_url=banner_url, picture_url=picture_url, ended=True,
    )

    if not entrants:
        ended_embed.add_field(name="Result", value="No valid entries — no winner could be drawn.", inline=False)
        try:
            await message.edit(embed=ended_embed)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
        await channel.send(f"🎉 The giveaway for **{reward_label}** ended, but nobody entered. No winner this time!")
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
        description=f"{mentions} - you won **{reward_label}**!",
        color=discord.Color.gold(),
    )
    if picture_url:
        congrats_embed.set_thumbnail(url=picture_url)
    congrats_embed.set_footer(text=f"Hosted by {host.display_name}")
    await channel.send(content=mentions, embed=congrats_embed)
    await db.remove_giveaway(message.id)


@bot.tree.command(name="giveaway", description="Start a giveaway")
@app_commands.describe(
    duration="How long the giveaway runs, e.g. 30m, 2h, 1d12h",
    prize="The text to display for the giveaway (what's being won)",
    winners="Number of winners to draw",
    color="Embed color: a name (red, blue, gold, blurple...) or hex code (#ff5733). Optional.",
    reward="Short reward name shown as its own field and reused in the winner announcement "
           "(defaults to the prize text above if left empty)",
    picture="Small image shown in the corner of the embed (optional)",
    banner="Large banner image shown at the bottom of the embed (optional)",
)
async def giveaway(
    interaction: discord.Interaction,
    duration: str,
    prize: str,
    winners: app_commands.Range[int, 1, 50],
    color: Optional[str] = None,
    reward: Optional[str] = None,
    picture: Optional[discord.Attachment] = None,
    banner: Optional[discord.Attachment] = None,
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

    for attachment, label in ((picture, "picture"), (banner, "banner")):
        if attachment and not (attachment.content_type or "").startswith("image/"):
            await interaction.response.send_message(f"❌ The {label} file is not an image.", ephemeral=True)
            return

    # Re-upload the images as attachments on the giveaway message itself (referenced via
    # "attachment://filename") instead of using attachment.url, which is an ephemeral
    # interaction CDN link that can expire before a multi-day giveaway ends.
    files = []
    picture_url = None
    banner_url = None
    if picture:
        picture_filename = f"picture_{picture.filename}"
        files.append(discord.File(io.BytesIO(await picture.read()), filename=picture_filename))
        picture_url = f"attachment://{picture_filename}"
    if banner:
        banner_filename = f"banner_{banner.filename}"
        files.append(discord.File(io.BytesIO(await banner.read()), filename=banner_filename))
        banner_url = f"attachment://{banner_filename}"

    end_ts = int(time.time()) + duration_seconds
    embed = build_giveaway_embed(
        prize, winners, end_ts, embed_color, interaction.user,
        reward=reward, banner_url=banner_url, picture_url=picture_url,
    )

    await interaction.response.send_message(embed=embed, files=files)
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
        reward=reward,
        banner_url=banner_url,
        picture_url=picture_url,
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
            reward=reward,
            banner_url=banner_url,
            picture_url=picture_url,
        )
    )


# --- Auto-responder ---

@bot.tree.command(name="respond", description="Create or update a trigger word auto-responder")
@app_commands.describe(
    word="Trigger word (matched as a whole word in messages, case-insensitive)",
    response="What the bot replies when the word is mentioned (optional if you set a reaction)",
    reaction="Emoji the bot reacts with on the triggering message (optional if you set a response)",
    cooldown="Minimum time between triggers, e.g. 30s, 1m, 1h, 1d (optional, default: no cooldown)",
)
@app_commands.default_permissions(manage_messages=True)
async def respond(
    interaction: discord.Interaction,
    word: str,
    response: Optional[str] = None,
    reaction: Optional[str] = None,
    cooldown: Optional[str] = None,
):
    if not is_admin(interaction.user):
        await interaction.response.send_message(
            "❌ You don't have permission to manage trigger words.", ephemeral=True
        )
        return
 
    word_clean = word.strip()
    if not word_clean:
        await interaction.response.send_message("❌ The trigger word can't be empty.", ephemeral=True)
        return
 
    # At least one of response / reaction is required. An empty response is stored
    # as "" (the DB column is NOT NULL) and is skipped by check_triggers.
    response = (response or "").strip()
    reaction = (reaction or "").strip() or None
    if not response and not reaction:
        await interaction.response.send_message(
            "❌ Give at least a `response` or a `reaction` (or both).", ephemeral=True
        )
        return
 
    cooldown_seconds = 0
    if cooldown:
        try:
            cooldown_seconds = parse_duration(cooldown)
        except ValueError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
 
    await bot.db.upsert_trigger(interaction.guild_id, word_clean, response, reaction, cooldown_seconds)
    bot.trigger_cache.setdefault(interaction.guild_id, {})[word_clean.lower()] = {
        "response": response,
        "reaction": reaction,
        "cooldown_seconds": cooldown_seconds,
    }
 
    cooldown_desc = f"{cooldown_seconds}s cooldown" if cooldown_seconds else "no cooldown"
    if response and reaction:
        action_desc = "message + reaction"
    elif response:
        action_desc = "message only"
    else:
        action_desc = "reaction only"
    await interaction.response.send_message(
        f"✅ Trigger `{word_clean}` saved ({action_desc}, {cooldown_desc}).", ephemeral=True
    )
 
    if reaction:
        try:
            confirmation = await interaction.original_response()
            await confirmation.add_reaction(reaction)
        except discord.HTTPException:
            await interaction.followup.send(
                f"⚠️ I couldn't react with `{reaction}` — make sure it's a valid emoji I have access to "
                "(the trigger was still saved, but the reaction may not work).",
                ephemeral=True,
            )


@bot.tree.command(name="respond-remove", description="Remove an auto-responder trigger word")
@app_commands.describe(word="The trigger word to remove")
async def respond_remove(interaction: discord.Interaction, word: str):
    if not is_admin(interaction.user):
        await interaction.response.send_message(
            "❌ You don't have permission to manage auto-responders.", ephemeral=True
        )
        return

    if not interaction.guild:
        await interaction.response.send_message("❌ This command can only be used in a server.", ephemeral=True)
        return

    word_clean = word.strip().lower()
    await bot.db.remove_trigger(interaction.guild.id, word_clean)
    bot.trigger_cache.get(interaction.guild.id, {}).pop(word_clean, None)

    await interaction.response.send_message(f"Trigger `{word_clean}` removed (if it existed).", ephemeral=True)


async def main():
    if not TOKEN:
        raise SystemExit("❌ La variable d'environnement DISCORD_TOKEN est manquante (voir .env.example).")
    async with bot:
        await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())