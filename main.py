"""Discord bot: a "sticky" message that always stays at the bottom of the channel.

Principle: every time a member posts a new message in a channel with an
active sticky, a counter is incremented. Once the threshold is reached, the
old sticky message is deleted and a new one is sent (so it's always at the
bottom of the conversation), then the counter is reset to 0.
"""
import asyncio
import logging
import os
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from core.db import Database

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("sticky-bot")

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")
DATA_DIR = os.getenv("DATA_DIR", "/data")
DB_PATH = os.path.join(DATA_DIR, "sticky.db")
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


class StickyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=INTENTS)
        self.db: Optional[Database] = None

    async def setup_hook(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        self.db = Database(DB_PATH)
        await self.db.connect()
        log.info("Base de données prête (%s)", DB_PATH)

        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Commandes synchronisées sur le serveur de test %s", GUILD_ID)
        else:
            await self.tree.sync()
            log.info("Commandes synchronisées globalement (jusqu'à 1h pour apparaître partout)")

    async def close(self):
        if self.db:
            await self.db.close()
        await super().close()

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


bot = StickyBot()

LOG_SOURCE_CHANNEL_ID = 1533559386629472486
LOG_TARGET_CHANNEL_ID = 1545497664894935181


async def forward_to_log(message: discord.Message):
    target = bot.get_channel(LOG_TARGET_CHANNEL_ID)
    if target is None:
        log.warning("Salon de log %s introuvable (cache).", LOG_TARGET_CHANNEL_ID)
        return
 
    embed = discord.Embed(
        description=message.content or "*(aucun contenu texte)*",
        color=discord.Color.blurple(),
        timestamp=message.created_at,
    )
    embed.set_author(name=f"{message.author} ({message.author.id})", icon_url=message.author.display_avatar.url)
    embed.set_footer(text=f"#{message.channel.name}")
 
    files = []
    for attachment in message.attachments:
        try:
            files.append(await attachment.to_file())
        except discord.HTTPException as e:
            log.warning("Impossible de récupérer la pièce jointe %s : %s", attachment.filename, e)
 
    try:
        await target.send(embed=embed, files=files or None)
    except discord.Forbidden:
        log.warning("Permissions manquantes pour écrire dans le salon de log %s", LOG_TARGET_CHANNEL_ID)
    except discord.HTTPException as e:
        log.warning("Échec de l'envoi du log : %s", e)

# ---------------------------------------------------------------------------
# Listens to every message to trigger the sticky repost
# ---------------------------------------------------------------------------
@bot.event
async def on_message(message: discord.Message):
    if message.author.id == bot.user.id or message.guild is None:
        return

    if message.channel.id == LOG_SOURCE_CHANNEL_ID:
        await forward_to_log(message)

    cfg = await bot.db.get_sticky(message.channel.id)
    if not cfg:
        return

    counter = await bot.db.increment_counter(message.channel.id)
    if counter < cfg["threshold"]:
        return

    # Threshold reached: delete the old sticky and send a new one
    if cfg["last_message_id"]:
        try:
            old = await message.channel.fetch_message(cfg["last_message_id"])
            await old.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    try:
        new_msg = await message.channel.send(cfg["text"])
        await bot.db.reset_counter_and_message(message.channel.id, new_msg.id)
    except discord.Forbidden:
        log.warning("Missing permissions in %s", message.channel.id)


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------
@bot.tree.command(name="stick", description="Stick a message for this channel")
@app_commands.describe(
    text="Message content",
    threshold="Number of messages before repost (default 1)",
)
@app_commands.default_permissions(manage_messages=True)
async def stick(interaction: discord.Interaction, text: str, threshold: app_commands.Range[int, 1, 50] = DEFAULT_THRESHOLD):
    perms = interaction.channel.permissions_for(interaction.guild.me)
    if not (perms.send_messages and perms.manage_messages):
        await interaction.response.send_message(
            "❌ Missing permissions: **Send messages** and **Manage messages** in this channel.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)
    msg = await interaction.channel.send(text)
    await bot.db.set_sticky(interaction.channel.id, interaction.guild_id, text, threshold, msg.id)
    await interaction.followup.send(
        f"📌 Sticky enabled in {interaction.channel.mention} (repost every {threshold} messages).",
        ephemeral=True,
    )


@bot.tree.command(name="stickstop", description="Disable the sticky post in this channel")
@app_commands.default_permissions(manage_messages=True)
async def stickstop(interaction: discord.Interaction):
    cfg = await bot.db.remove_sticky(interaction.channel.id)
    if not cfg:
        await interaction.response.send_message("There is no active sticky message in this channel.", ephemeral=True)
        return

    if cfg["last_message_id"]:
        try:
            old = await interaction.channel.fetch_message(cfg["last_message_id"])
            await old.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    await interaction.response.send_message("🛑 Sticky disabled for this channel.", ephemeral=True)


@bot.tree.command(name="stickstatus", description="View the sticky post settings for this channel")
async def stickstatus(interaction: discord.Interaction):
    cfg = await bot.db.get_sticky(interaction.channel.id)
    if not cfg:
        await interaction.response.send_message("There is no active sticky message in this channel.", ephemeral=True)
        return

    embed = discord.Embed(title="📌 Active Sticky", color=discord.Color.blurple())
    embed.add_field(name="Text", value=cfg["text"][:1000], inline=False)
    embed.add_field(name="Repost Threshold", value=f"{cfg['threshold']} messages", inline=True)
    embed.add_field(name="Current Counter", value=f"{cfg['counter']}/{cfg['threshold']}", inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


# --- People/role allowed to change the bot's name/avatar ---
# Comma-separated Discord IDs, e.g. "111111111111111111,222222222222222222"
BOT_ADMIN_IDS = {
    int(x) for x in os.getenv("BOT_ADMIN_IDS", "").split(",") if x.strip().isdigit()
}
# Allowed role ID (optional), e.g. "333333333333333333"
_role_env = os.getenv("BOT_ADMIN_ROLE_ID", "").strip()
BOT_ADMIN_ROLE_ID = int(_role_env) if _role_env.isdigit() else None


def is_bot_admin(member: discord.Member) -> bool:
    """Checks whether this member is allowed to change the bot's name/avatar."""
    if member.id in BOT_ADMIN_IDS:
        return True
    if BOT_ADMIN_ROLE_ID is not None:
        return any(role.id == BOT_ADMIN_ROLE_ID for role in member.roles)
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
    if not is_bot_admin(interaction.user):
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


async def main():
    if not TOKEN:
        raise SystemExit("❌ La variable d'environnement DISCORD_TOKEN est manquante (voir .env.example).")
    async with bot:
        await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())