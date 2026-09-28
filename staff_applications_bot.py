"""
Staff Applications Bot  (discord.py 2.4+)

Flow:
  1. An admin runs /staffpanel in the applications channel -> panel with an Apply button.
  2. A member clicks Apply -> a form (modal) opens.
  3. The submission is posted in the review channel with Accept / Interview / Deny buttons.
  4. The decision is saved on the review message and the applicant gets a DM.
"""

import logging
import os
import time
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

# ============================== CONFIG ==============================
TOKEN = os.getenv("DISCORD_TOKEN")

GUILD_ID = 1410440666747633707             # your server ID
PENDING_CHANNEL_ID = 1513904283337101364   # new applications arrive here
ACCEPTED_CHANNEL_ID = 1513904281198137475  # accepted applications are posted here
DENIED_CHANNEL_ID = 1513904282267811920    # denied applications are posted here
APPLY_CHANNEL_ID = 1513904279847702699     # channel where the Apply panel lives
STAFF_ROLE_ID = 0        # role given when an application is accepted (0 = give no role)
REVIEWER_ROLE_ID = 0     # role allowed to Accept / Deny (0 = only Manage Server / Admin)

SERVER_NAME = "ELT | ELITE LEADERS COMMUNITY"
BANNER_URL = ""          # direct image link for the panel banner (leave "" for none)
ACCENT = 0x8B3DFF        # embed colour

INTERVIEW_INVITE_URL = "https://discord.gg/VE3Yaje9ED"  # sent in the interview DM

MIN_DAYS_IN_SERVER = 7   # member must have been in the server this long
COOLDOWN_HOURS = 72      # wait time before re-applying after a submission
# ====================================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("staffapps")

state = {"open": True}
pending: set[int] = set()
handled: set[int] = set()
submitting: set[int] = set()
cooldowns: dict[int, float] = {}

intents = discord.Intents.default()
intents.members = True  # enable "Server Members Intent" in the developer portal
bot = commands.Bot(command_prefix="!", intents=intents)


# ------------------------------ helpers ------------------------------
def is_reviewer(member: discord.abc.User) -> bool:
    if not isinstance(member, discord.Member):
        return False
    perms = member.guild_permissions
    if perms.administrator or perms.manage_guild:
        return True
    return bool(REVIEWER_ROLE_ID and member.get_role(REVIEWER_ROLE_ID))


def clip(text: str, limit: int = 1000) -> str:
    text = (text or "").strip() or "—"
    return text if len(text) <= limit else text[: limit - 1] + "…"


def quote(text: str, limit: int = 1000) -> str:
    """Clip an answer and show it as a quote block (also stops user text from making headings)."""
    body = clip(text, limit).replace("\n", "\n> ")
    body = body if len(body) <= 1200 else body[:1199] + "…"
    return "> " + body


def panel_embed() -> discord.Embed:
    status = "🟢 Applications are **OPEN**" if state["open"] else "🔴 Applications are **CLOSED**"
    e = discord.Embed(
        title=f"✦ Join the {SERVER_NAME} Staff Team",
        description=(
            "Want to help shape this community? We're looking for people who are "
            "reliable, fair and genuinely invested in the server.\n\n"
            f"{status}"
        ),
        color=ACCENT if state["open"] else 0xE74C3C,
    )
    e.add_field(
        name="🎯 What we look for",
        value=(
            "• Maturity and good judgement under pressure\n"
            "• Clear communication and teamwork\n"
            "• Consistent activity in the community\n"
            "• A calm, fair approach with members"
        ),
        inline=False,
    )
    e.add_field(
        name="📌 Requirements",
        value=(
            f"• In the server for **{MIN_DAYS_IN_SERVER}+ days**\n"
            "• No staff experience needed\n"
            "• Honest, thoughtful answers"
        ),
        inline=True,
    )
    e.add_field(
        name="⏱ How it works",
        value=(
            "1. Press **Apply**\n"
            "2. Fill in the short form\n"
            "3. We review it and reply by DM\n"
            "*(keep your DMs open)*"
        ),
        inline=True,
    )
    if BANNER_URL:
        e.set_image(url=BANNER_URL)
    e.set_footer(text=f"{SERVER_NAME} • Staff Recruitment")
    return e


# ------------------------------ Apply flow ------------------------------
async def has_open_application(uid: int) -> bool:
    """True if this user still has an application waiting in the pending channel.
    Reads the channel itself, so it keeps working after restarts and manual deletions."""
    channel = bot.get_channel(PENDING_CHANNEL_ID)
    if channel is None:
        return False
    footer = f"User ID: {uid}"
    try:
        async for m in channel.history(limit=100):
            if (
                m.author.id == bot.user.id
                and m.embeds
                and m.embeds[0].footer.text == footer
                and m.components  # buttons still present = not decided yet
            ):
                return True
    except discord.HTTPException:
        return False
    return False

class ApplicationModal(discord.ui.Modal, title="Staff Application"):
    age = discord.ui.TextInput(
        label="Your age",
        placeholder="e.g. 19",
        min_length=1,
        max_length=3,
        required=True,
    )
    activity = discord.ui.TextInput(
        label="Timezone and daily activity",
        placeholder="e.g. GMT+1, about 4 hours a day",
        max_length=100,
        required=True,
    )
    why = discord.ui.TextInput(
        label="Why do you want to be staff?",
        style=discord.TextStyle.paragraph,
        min_length=30,
        max_length=1000,
        required=True,
    )
    experience = discord.ui.TextInput(
        label="Previous experience (optional)",
        style=discord.TextStyle.paragraph,
        max_length=1000,
        required=False,
    )
    scenario = discord.ui.TextInput(
        label="Two members are arguing. What do you do?",
        style=discord.TextStyle.paragraph,
        min_length=30,
        max_length=1000,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        age_text = self.age.value.strip()
        if not (age_text.isascii() and age_text.isdigit() and 13 <= int(age_text) <= 99):
            await interaction.response.send_message(
                "⚠️ Please enter your real age as a number (13 or older). Press **Apply** to try again.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        uid = interaction.user.id
        if uid in submitting or await has_open_application(uid):
            await interaction.followup.send("⏳ You already have an application under review.", ephemeral=True)
            return
        submitting.add(uid)

        channel = interaction.client.get_channel(PENDING_CHANNEL_ID)
        if channel is None:
            submitting.discard(uid)
            log.error("Pending channel %s not found", PENDING_CHANNEL_ID)
            await interaction.followup.send(
                "⚠️ Applications are temporarily unavailable. Please tell an admin.",
                ephemeral=True,
            )
            return

        user = interaction.user
        joined = (
            discord.utils.format_dt(user.joined_at, "R")
            if isinstance(user, discord.Member) and user.joined_at
            else "—"
        )
        e = discord.Embed(
            description=(
                "# ✦ STAFF APPLICATION\n"
                f"**Applicant:** {user.mention} (`{user.id}`)\n"
                f"**Age:** {clip(self.age.value, 10)}  •  "
                f"**Timezone / Activity:** {clip(self.activity.value, 100)}  •  "
                f"**Joined:** {joined}\n\n"
                f"## Why do you want to be staff?\n{quote(self.why.value)}\n\n"
                f"## Previous experience\n{quote(self.experience.value)}\n\n"
                f"## How would you handle an argument?\n{quote(self.scenario.value)}"
            ),
            color=ACCENT,
            timestamp=datetime.now(timezone.utc),
        )
        e.set_thumbnail(url=user.display_avatar.url)
        e.add_field(name="Status", value="🕓 Pending review", inline=False)
        e.set_footer(text=f"User ID: {user.id}")

        try:
            await channel.send(embed=e, view=review_view(user.id, interview=True))
        except discord.HTTPException:
            log.exception("Could not post application")
            submitting.discard(user.id)
            await interaction.followup.send(
                "⚠️ Something went wrong sending your application. Please try again later.",
                ephemeral=True,
            )
            return

        pending.add(user.id)
        submitting.discard(user.id)
        cooldowns[user.id] = time.time() + COOLDOWN_HOURS * 3600
        await interaction.followup.send(
            "✅ Your application was sent! We'll reply by DM — keep your DMs open.",
            ephemeral=True,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        log.exception("Application modal error", exc_info=error)
        pending.discard(interaction.user.id)
        submitting.discard(interaction.user.id)
        msg = "⚠️ Something went wrong. Please try again."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


class ApplyView(discord.ui.View):
    def __init__(self, closed: bool = False):
        super().__init__(timeout=None)
        if closed:
            self.apply.label = "Applications Closed"
            self.apply.style = discord.ButtonStyle.secondary
            self.apply.disabled = True

    @discord.ui.button(label="Apply", emoji="📝", style=discord.ButtonStyle.primary, custom_id="staffapp:apply")
    async def apply(self, interaction: discord.Interaction, button: discord.ui.Button):
        user = interaction.user

        if not state["open"]:
            return await interaction.response.send_message("🔴 Applications are currently closed.", ephemeral=True)

        if await has_open_application(user.id):
            return await interaction.response.send_message(
                "⏳ You already have an application under review.", ephemeral=True
            )

        if STAFF_ROLE_ID and isinstance(user, discord.Member) and user.get_role(STAFF_ROLE_ID):
            return await interaction.response.send_message("You're already part of the staff team!", ephemeral=True)

        wait_until = cooldowns.get(user.id, 0)
        if wait_until > time.time():
            return await interaction.response.send_message(
                f"⏳ You can apply again {discord.utils.format_dt(datetime.fromtimestamp(wait_until, timezone.utc), 'R')}.",
                ephemeral=True,
            )

        if isinstance(user, discord.Member) and user.joined_at:
            days = (datetime.now(timezone.utc) - user.joined_at).days
            if days < MIN_DAYS_IN_SERVER:
                return await interaction.response.send_message(
                    f"You need to be in the server for at least **{MIN_DAYS_IN_SERVER} days** to apply "
                    f"(you're at {days}).",
                    ephemeral=True,
                )

        await interaction.response.send_modal(ApplicationModal())


# ------------------------------ Review flow ------------------------------
class DenyModal(discord.ui.Modal, title="Deny Application"):
    reason = discord.ui.TextInput(
        label="Reason (sent to the applicant)",
        style=discord.TextStyle.paragraph,
        max_length=500,
        required=True,
    )

    def __init__(self, uid: int, message: discord.Message):
        super().__init__()
        self.uid = uid
        self.message = message

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await finalize(
            interaction, self.message, self.uid, "deny", reason=self.reason.value
        )


async def finalize(interaction, message: discord.Message, uid: int, action: str, reason: str = ""):
    guild = interaction.guild
    reviewer = interaction.user
    notes = []

    if action != "interview":
        if message.id in handled:
            await interaction.followup.send("This application was already handled.", ephemeral=True)
            return
        handled.add(message.id)

    embed = message.embeds[0].copy() if message.embeds else discord.Embed(title="Application")
    # replace the Status field
    status_index = next((i for i, f in enumerate(embed.fields) if f.name == "Status"), None)

    if action == "accept":
        color, status = 0x2ECC71, f"✅ Accepted by {reviewer.mention}"
        member = guild.get_member(uid)
        if member is None:
            try:
                member = await guild.fetch_member(uid)
            except discord.HTTPException:
                member = None
        if member is None:
            notes.append("⚠️ The applicant is no longer in the server.")
        elif STAFF_ROLE_ID:
            role = guild.get_role(STAFF_ROLE_ID)
            if role is None:
                notes.append("⚠️ Staff role not found — check STAFF_ROLE_ID.")
            else:
                try:
                    await member.add_roles(role, reason=f"Staff application accepted by {reviewer}")
                except discord.Forbidden:
                    notes.append("⚠️ I can't give that role — move my bot role above it.")
        dm = discord.Embed(
            title="🎉 Application accepted!",
            description=f"Congratulations! Your staff application in **{guild.name}** was accepted. "
                        "A team member will contact you soon.",
            color=0x2ECC71,
        )
        pending.discard(uid)
        cooldowns.pop(uid, None)

    elif action == "interview":
        color, status = 0xF1C40F, f"🎤 Interview requested by {reviewer.mention}"
        dm = discord.Embed(
            title="Interview request",
            description=f"Your staff application in **{guild.name}** looks promising, "
                        "join this server for Interview:\n"
                        f"{INTERVIEW_INVITE_URL}",
            color=0xF1C40F,
        )

    else:  # deny
        color, status = 0xE74C3C, f"❌ Denied by {reviewer.mention}\n**Reason:** {clip(reason, 500)}"
        dm = discord.Embed(
            title="Application update",
            description=f"Unfortunately your staff application in **{guild.name}** was not accepted this time.\n\n"
                        f"**Reason:** {clip(reason, 500)}\n\nYou're welcome to apply again in the future.",
            color=0xE74C3C,
        )
        pending.discard(uid)

    embed.color = color
    if status_index is not None:
        embed.set_field_at(status_index, name="Status", value=status, inline=False)
    else:
        embed.add_field(name="Status", value=status, inline=False)

    if action == "interview":
        try:
            await message.edit(embed=embed, view=review_view(uid, interview=False))
        except discord.HTTPException:
            notes.append("⚠️ Couldn't update the application message.")
    else:
        dest = interaction.client.get_channel(ACCEPTED_CHANNEL_ID if action == "accept" else DENIED_CHANNEL_ID)
        posted = False
        if dest is None:
            notes.append("⚠️ Result channel not found - check the channel IDs.")
        else:
            try:
                await dest.send(embed=embed)
                posted = True
            except discord.HTTPException:
                notes.append("⚠️ I couldn't post in the result channel - check my permissions there.")
        try:
            if posted:
                await message.delete()  # moved out of pending
            else:
                await message.edit(embed=embed, view=None)  # keep the record in pending
        except discord.HTTPException:
            notes.append("⚠️ Couldn't clean up the pending message.")

    # DM the applicant
    target = guild.get_member(uid)
    try:
        user = target or await interaction.client.fetch_user(uid)
        await user.send(embed=dm)
    except (discord.Forbidden, discord.HTTPException):
        notes.append("⚠️ Couldn't DM the applicant (their DMs are closed).")

    await interaction.followup.send("Done. " + " ".join(notes) if notes else "Done ✅", ephemeral=True)


class ReviewButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"staffapp:(?P<action>accept|interview|deny):(?P<uid>[0-9]+)",
):
    STYLES = {
        "accept": ("Accept", "✅", discord.ButtonStyle.success),
        "interview": ("Interview", None, discord.ButtonStyle.secondary),  # grey, no emoji
        "deny": ("Deny", "❌", discord.ButtonStyle.danger),
    }

    def __init__(self, action: str, uid: int):
        label, emoji, style = self.STYLES[action]
        super().__init__(
            discord.ui.Button(label=label, emoji=emoji, style=style, custom_id=f"staffapp:{action}:{uid}")
        )
        self.action = action
        self.uid = uid

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match, /):
        return cls(match["action"], int(match["uid"]))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not is_reviewer(interaction.user):
            await interaction.response.send_message("You're not allowed to review applications.", ephemeral=True)
            return False
        return True

    async def callback(self, interaction: discord.Interaction):
        if self.action == "deny":
            await interaction.response.send_modal(DenyModal(self.uid, interaction.message))
            return
        await interaction.response.defer(ephemeral=True)
        await finalize(interaction, interaction.message, self.uid, self.action)


def review_view(uid: int, interview: bool = True) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(ReviewButton("accept", uid))
    if interview:
        view.add_item(ReviewButton("interview", uid))
    view.add_item(ReviewButton("deny", uid))
    return view


# ------------------------------ Commands ------------------------------
@bot.tree.command(name="staffpanel", description="Post the staff application panel in this channel")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def staffpanel(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("Admins only.", ephemeral=True)
    await interaction.response.send_message("Panel posted ✅", ephemeral=True)
    await interaction.channel.send(embed=panel_embed(), view=ApplyView(closed=not state["open"]))


async def find_panel(guild: discord.Guild):
    """Finds the bot's Apply panel message in the applications channel."""
    channel = guild.get_channel(APPLY_CHANNEL_ID)
    if channel is None:
        return None
    async for msg in channel.history(limit=100):
        if msg.author.id == bot.user.id and msg.embeds and (msg.embeds[0].title or "").startswith("✦ Join the"):
            return msg
    return None


@bot.tree.command(name="staffapps", description="Open or close staff applications")
@app_commands.describe(mode="Open or close applications")
@app_commands.choices(mode=[
    app_commands.Choice(name="Open", value="open"),
    app_commands.Choice(name="Close", value="close"),
])
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def staffapps(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("Admins only.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    state["open"] = mode.value == "open"
    note = ""
    try:
        panel = await find_panel(interaction.guild)
        if panel:
            await panel.edit(embed=panel_embed(), view=ApplyView(closed=not state["open"]))
        else:
            note = " I couldn't find the panel - run /staffpanel again."
    except discord.HTTPException:
        note = " I couldn't update the panel message - check my permissions in that channel."
    await interaction.followup.send(
        f"Applications are now **{'OPEN' if state['open'] else 'CLOSED'}**.{note}",
        ephemeral=True,
    )


# ------------------------------ Startup ------------------------------
@bot.event
async def setup_hook():
    bot.add_view(ApplyView())
    bot.add_dynamic_items(ReviewButton)
    if GUILD_ID:
        guild = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
    else:
        await bot.tree.sync()


@bot.event
async def on_ready():
    log.info("Logged in as %s (%s)", bot.user, bot.user.id)
    guild = bot.get_guild(GUILD_ID)
    if guild:
        try:
            panel = await find_panel(guild)
            if panel and "CLOSED" in (panel.embeds[0].description or ""):
                state["open"] = False
                log.info("Panel is marked CLOSED - keeping applications closed")
        except discord.HTTPException:
            log.warning("Couldn't read the panel to restore open/closed state")


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN environment variable is missing.")
    bot.run(TOKEN)
