import os, io, json, copy, random, asyncio
from datetime import timedelta
from typing import Optional
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
STAFF_ROLE_ID = int(os.getenv("STAFF_ROLE_ID"))
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID"))
REVIEW_CHANNEL_ID = int(os.getenv("REVIEW_CHANNEL_ID"))
CATEGORY_ID = int(os.getenv("TICKET_CATEGORY_ID", "0"))
ESCALATE_ROLE_ID = int(os.getenv("ESCALATE_ROLE_ID", "0"))
GUILD_ID = int(os.getenv("GUILD_ID", "0"))

intents = discord.Intents.default()


# ---------- Ticket panel text (edit these any time) ----------
PANEL_TITLE = "◆ Frontline Support"
PANEL_LINES = [
    "TO PURCHASE MANUALLY, PRESS PURCHASE.",
    "DO NOT ASK TO ASK. SIMPLY ASK YOUR QUESTION.",
    'FOR MEDIA APPLICATIONS. PRESS "MEDIA" BUTTON.',
    "FOR AUTOMATIC DELIVERY, USE THE WEBSITE.",
]
PAYMENT_METHODS = [
    "💰 CRYPTO CURRENCIES",
    "💰 PAYPAL (FAMILY & FRIENDS) (MUST PAY ON DISCORD)",
    "💰 CASHAPP",
    "💰 CREDIT CARDS (MUST PAY ON DISCORD)",
    "💰 ZELLE (MUST PAY ON DISCORD)",
    "💰 VENMO (MUST PAY ON DISCORD)",
]
PANEL_COLOR = 0xF1C40F  # gold bar on the left


# ---------- Saved settings (website, image, on/off, warnings) ----------
DATA_FILE = "data.json"
DEFAULTS = {"website": "https://example.com", "image": "",
            "tickets_enabled": True, "warnings": {}}


def load_data():
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        d = {}
    for k, v in DEFAULTS.items():
        d.setdefault(k, copy.deepcopy(v))
    return d


def save_data():
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


data = load_data()


class MyBot(commands.Bot):
    async def setup_hook(self):
        self.add_view(OpenView())    # keeps the panel dropdown working after restarts
        self.add_view(CloseView())
        if GUILD_ID:
            # Instant sync to your server only
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            # clear old global copies so commands don't show up twice
            self.tree.clear_commands(guild=None)
            await self.tree.sync()
        else:
            synced = await self.tree.sync()
        print(f"Synced {len(synced)} commands")


bot = MyBot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")


@bot.tree.error
async def on_app_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.CheckFailure):
        msg = "You don't have permission to use that."
    else:
        print(error)
        msg = f"Something went wrong: {error}"
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


# ---------- Helpers ----------
def is_staff(member):
    return (any(r.id == STAFF_ROLE_ID for r in member.roles)
            or member.guild_permissions.administrator)


def staff_only():
    async def predicate(interaction: discord.Interaction):
        return is_staff(interaction.user)
    return app_commands.check(predicate)


def is_ticket(channel):
    return (isinstance(channel, discord.TextChannel)
            and channel.topic is not None and channel.topic.isdigit())


async def get_owner(guild, channel):
    if not is_ticket(channel):
        return None
    uid = int(channel.topic)
    member = guild.get_member(uid)
    if member:
        return member
    try:
        return await guild.fetch_member(uid)
    except discord.HTTPException:
        return None


async def check_target(interaction, member):
    guild = interaction.guild
    if member == interaction.user:
        msg = "You can't do that to yourself."
    elif member.id == guild.owner_id or member.top_role >= guild.me.top_role:
        msg = "My role isn't high enough to do that to this member."
    elif interaction.user.id != guild.owner_id and member.top_role >= interaction.user.top_role:
        msg = "That member's role is equal to or higher than yours."
    else:
        return True
    await interaction.response.send_message(msg, ephemeral=True)
    return False


async def build_transcript(channel):
    lines = []
    async for m in channel.history(limit=None, oldest_first=True):
        text = m.content or ""
        for a in m.attachments:
            text += f" [attachment: {a.url}]"
        if m.embeds and not text:
            text = "[embed]"
        lines.append(f"[{m.created_at:%Y-%m-%d %H:%M}] {m.author}: {text}")
    return "\n".join(lines)


def transcript_file(text, name):
    return discord.File(io.BytesIO(text.encode("utf-8")), filename=f"{name}.txt")


# ---------- Reviews ----------
class ReviewModal(discord.ui.Modal, title="Leave a review"):
    comment = discord.ui.TextInput(
        label="Comments", style=discord.TextStyle.paragraph,
        required=False, max_length=500)

    def __init__(self, stars):
        super().__init__()
        self.stars = stars

    async def on_submit(self, interaction: discord.Interaction):
        ch = bot.get_channel(REVIEW_CHANNEL_ID)
        e = discord.Embed(title="New Review",
                          description=self.comment.value or "No comment",
                          color=discord.Color.gold())
        e.add_field(name="Rating", value="⭐" * self.stars)
        e.set_author(name=str(interaction.user),
                     icon_url=interaction.user.display_avatar.url)
        await ch.send(embed=e)
        await interaction.response.send_message("Thanks for your review!")


class ReviewView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=86400)

    @discord.ui.select(
        placeholder="Rate your support",
        options=[discord.SelectOption(label=f"{i} star{'s' if i > 1 else ''}", value=str(i))
                 for i in range(1, 6)])
    async def rate(self, interaction: discord.Interaction, select: discord.ui.Select):
        await interaction.response.send_modal(ReviewModal(int(select.values[0])))


# ---------- Tickets ----------
# key: (name shown inside the ticket, emoji)
TICKET_TYPES = {
    "support":  ("Support", "⚙️"),
    "purchase": ("Purchase", "💰"),
    "media":    ("Media Application", "📷"),
    "reseller": ("Reseller", "💳"),
    "product":  ("Product Support", "🛒"),
    "general":  ("General Support", "💬"),
    "other":    ("Other", "❓"),
}

# The dropdown menu. Order here = order in Discord. Delete a line to remove an option.
# (key, label shown in the dropdown, emoji)
DROPDOWN_OPTIONS = [
    ("support",  "Support",         "⚙️"),
    ("purchase", "Purchase Now",    "💰"),
    ("media",    "Media",           "📷"),
    ("reseller", "Resell",          "💳"),
    ("website",  "Website",         "🌐"),   # replies with the link instead of making a ticket
    ("product",  "Product Support", "🛒"),
    ("general",  "General Support", "💬"),
    ("other",    "Other",           "❓"),
]


async def create_ticket(guild, user, kind):
    """Returns (channel, None) on success or (None, existing_channel)."""
    existing = discord.utils.get(guild.text_channels, topic=str(user.id))
    if existing:
        return None, existing

    label, emoji = TICKET_TYPES[kind]
    staff = guild.get_role(STAFF_ROLE_ID)
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        user: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, attach_files=True),
        staff: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, manage_channels=True),
    }
    category = guild.get_channel(CATEGORY_ID) if CATEGORY_ID else None
    ch = await guild.create_text_channel(
        f"{kind}-{user.name}", overwrites=overwrites,
        category=category, topic=str(user.id))

    e = discord.Embed(
        title=f"{emoji} {label}",
        description="Describe your issue and staff will be with you shortly.",
        color=discord.Color.blurple())
    e.add_field(name="Opened by", value=user.mention)
    e.add_field(name="Type", value=label)
    await ch.send(f"{user.mention} {staff.mention}", embed=e, view=CloseView())
    return ch, None


class CloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.danger,
                       custom_id="ticket:close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.channel
        if not (is_staff(interaction.user) or str(interaction.user.id) == channel.topic):
            return await interaction.response.send_message("Not allowed.", ephemeral=True)

        await interaction.response.send_message("Closing in 5 seconds, saving transcript...")
        text = await build_transcript(channel)

        log = bot.get_channel(LOG_CHANNEL_ID)
        await log.send(f"Transcript for **{channel.name}**",
                       file=transcript_file(text, channel.name))

        owner = await get_owner(interaction.guild, channel)
        if owner:
            try:
                await owner.send("Your ticket was closed. Here's the transcript, "
                                 "and we'd love a review:",
                                 file=transcript_file(text, channel.name),
                                 view=ReviewView())
            except discord.Forbidden:
                pass  # user has DMs closed

        await asyncio.sleep(5)
        await channel.delete()


async def handle_open(interaction: discord.Interaction, kind: str):
    if not data["tickets_enabled"]:
        return await interaction.response.send_message(
            "Tickets are currently closed. Please try again later.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    ch, existing = await create_ticket(interaction.guild, interaction.user, kind)
    if existing:
        await interaction.followup.send(
            f"You already have a ticket: {existing.mention}", ephemeral=True)
    else:
        await interaction.followup.send(f"Ticket created: {ch.mention}", ephemeral=True)


class OpenView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.select(
        custom_id="ticket:type",
        placeholder="◆ Choose ticket type...",
        options=[discord.SelectOption(label=f"◆ {label}", value=key, emoji=emoji)
                 for key, label, emoji in DROPDOWN_OPTIONS])
    async def choose(self, interaction: discord.Interaction, select: discord.ui.Select):
        choice = select.values[0]
        if choice == "website":
            await interaction.response.send_message(
                f"🌐 **Our website:** {data['website']}", ephemeral=True)
        else:
            await handle_open(interaction, choice)
        try:
            await interaction.message.edit(view=OpenView())  # reset dropdown so it can be reused
        except discord.HTTPException:
            pass


@bot.tree.command(name="ticketpanel", description="Post the ticket panel here")
@app_commands.checks.has_permissions(administrator=True)
async def ticketpanel(interaction: discord.Interaction):
    rules = "\n\n".join(f"🟢 `{line}`" for line in PANEL_LINES)
    payments = "\n".join(PAYMENT_METHODS)
    description = (f"{rules}\n\n"
                   f"# ACCEPTED PAYMENT METHODS:\n"
                   f"```\n{payments}\n```\n"
                   f"[**OFFICIAL FRONTLINE WEBSITE**]({data['website']})")
    e = discord.Embed(title=PANEL_TITLE, description=description, color=PANEL_COLOR)
    if data["image"]:
        e.set_image(url=data["image"])
    e.set_footer(text="Frontline Support")
    await interaction.channel.send(embed=e, view=OpenView())
    await interaction.response.send_message("Panel posted.", ephemeral=True)


@bot.tree.command(name="on", description="Turn the ticket system on (staff)")
@staff_only()
async def tickets_on(interaction: discord.Interaction):
    data["tickets_enabled"] = True
    save_data()
    await interaction.response.send_message("✅ Tickets are now **ON**.", ephemeral=True)


@bot.tree.command(name="off", description="Turn the ticket system off (staff)")
@staff_only()
async def tickets_off(interaction: discord.Interaction):
    data["tickets_enabled"] = False
    save_data()
    await interaction.response.send_message("⛔ Tickets are now **OFF**.", ephemeral=True)


@bot.tree.command(name="hidetoggle", description="Hide/show this ticket from its owner (staff)")
@staff_only()
async def hidetoggle(interaction: discord.Interaction):
    owner = await get_owner(interaction.guild, interaction.channel)
    if not owner:
        return await interaction.response.send_message("Use this inside a ticket.", ephemeral=True)
    hidden = interaction.channel.overwrites_for(owner).view_channel is False
    await interaction.channel.set_permissions(owner, view_channel=hidden,
                                              send_messages=True, attach_files=True)
    await interaction.response.send_message(
        "👁️ Ticket is now **visible** to the user." if hidden
        else "🙈 Ticket is now **hidden** from the user.")


@bot.tree.command(name="unhide", description="Make this ticket visible to its owner again (staff)")
@staff_only()
async def unhide(interaction: discord.Interaction):
    owner = await get_owner(interaction.guild, interaction.channel)
    if not owner:
        return await interaction.response.send_message("Use this inside a ticket.", ephemeral=True)
    await interaction.channel.set_permissions(owner, view_channel=True,
                                              send_messages=True, attach_files=True)
    await interaction.response.send_message("👁️ Ticket is now **visible** to the user.")


@bot.tree.command(name="addticket", description="Add a member to this ticket (staff)")
@staff_only()
async def addticket(interaction: discord.Interaction, member: discord.Member):
    if not is_ticket(interaction.channel):
        return await interaction.response.send_message("Use this inside a ticket.", ephemeral=True)
    await interaction.channel.set_permissions(member, view_channel=True,
                                              send_messages=True, attach_files=True)
    await interaction.response.send_message(f"✅ Added {member.mention} to this ticket.")


@bot.tree.command(name="reseller-ticket", description="Open a reseller ticket for a member (staff)")
@staff_only()
async def reseller_ticket(interaction: discord.Interaction, member: discord.Member):
    await interaction.response.defer(ephemeral=True)
    ch, existing = await create_ticket(interaction.guild, member, "reseller")
    if existing:
        await interaction.followup.send(
            f"{member.mention} already has a ticket: {existing.mention}", ephemeral=True)
    else:
        await interaction.followup.send(f"Reseller ticket created: {ch.mention}", ephemeral=True)


@bot.tree.command(name="escalate", description="Escalate this ticket (staff)")
@staff_only()
async def escalate(interaction: discord.Interaction, reason: str = "No reason given"):
    channel = interaction.channel
    if not is_ticket(channel):
        return await interaction.response.send_message("Use this inside a ticket.", ephemeral=True)
    role = interaction.guild.get_role(ESCALATE_ROLE_ID) if ESCALATE_ROLE_ID else None
    e = discord.Embed(title="🚨 Ticket escalated", description=reason,
                      color=discord.Color.red())
    e.set_footer(text=f"Escalated by {interaction.user}")
    await interaction.response.send_message(
        role.mention if role else None, embed=e,
        allowed_mentions=discord.AllowedMentions(roles=True))
    if role:
        await channel.set_permissions(role, view_channel=True, send_messages=True)
    if not channel.name.startswith("escalated-"):
        await channel.edit(name=f"escalated-{channel.name}"[:100])


# ---------- Website & image (changeable any time) ----------
@bot.tree.command(name="website", description="Get our website link")
async def website(interaction: discord.Interaction):
    e = discord.Embed(title="🌐 Our Website", description=data["website"],
                      color=discord.Color.blurple())
    if data["image"]:
        e.set_image(url=data["image"])
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Visit Website", url=data["website"]))
    await interaction.response.send_message(embed=e, view=view)


@bot.tree.command(name="setwebsite", description="Change the website link (admin)")
@app_commands.checks.has_permissions(administrator=True)
async def setwebsite(interaction: discord.Interaction, url: str):
    if not url.startswith(("http://", "https://")):
        return await interaction.response.send_message(
            "The link must start with http:// or https://", ephemeral=True)
    data["website"] = url
    save_data()
    await interaction.response.send_message(
        f"✅ Website updated to {url}\n"
        "The dropdown's Website option and `/website` use it right away. "
        "Run `/ticketpanel` again to update the link inside the panel text.",
        ephemeral=True)


@bot.tree.command(name="setimage", description="Set the banner image, or type 'none' to remove (admin)")
@app_commands.checks.has_permissions(administrator=True)
async def setimage(interaction: discord.Interaction, url: str):
    if url.lower() == "none":
        data["image"] = ""
        msg = "✅ Banner image removed."
    elif url.startswith(("http://", "https://")):
        data["image"] = url
        msg = "✅ Banner image updated. Run `/ticketpanel` again to see it on the panel."
    else:
        msg = "Send a direct image link starting with http:// or https://, or type `none`."
    save_data()
    await interaction.response.send_message(msg, ephemeral=True)


# ---------- Moderation ----------
@bot.tree.command(name="kick", description="Kick a member")
@app_commands.checks.has_permissions(kick_members=True)
async def kick(interaction: discord.Interaction, member: discord.Member,
               reason: str = "No reason given"):
    if not await check_target(interaction, member):
        return
    await member.kick(reason=f"{interaction.user}: {reason}")
    await interaction.response.send_message(f"👢 Kicked **{member}** — {reason}")


@bot.tree.command(name="ban", description="Ban a member")
@app_commands.checks.has_permissions(ban_members=True)
async def ban(interaction: discord.Interaction, member: discord.Member,
              reason: str = "No reason given"):
    if not await check_target(interaction, member):
        return
    await member.ban(reason=f"{interaction.user}: {reason}")
    await interaction.response.send_message(f"🔨 Banned **{member}** — {reason}")


@bot.tree.command(name="unban", description="Unban a user by their ID")
@app_commands.checks.has_permissions(ban_members=True)
async def unban(interaction: discord.Interaction, user_id: str):
    try:
        await interaction.guild.unban(discord.Object(id=int(user_id)))
    except (ValueError, discord.NotFound):
        return await interaction.response.send_message(
            "Couldn't find a ban for that ID.", ephemeral=True)
    await interaction.response.send_message(f"✅ Unbanned `{user_id}`")


@bot.tree.command(name="timeout", description="Timeout a member")
@app_commands.checks.has_permissions(moderate_members=True)
async def timeout(interaction: discord.Interaction, member: discord.Member,
                  minutes: app_commands.Range[int, 1, 40320],
                  reason: str = "No reason given"):
    if not await check_target(interaction, member):
        return
    await member.timeout(timedelta(minutes=minutes), reason=f"{interaction.user}: {reason}")
    await interaction.response.send_message(
        f"🔇 Timed out **{member}** for {minutes} min — {reason}")


@bot.tree.command(name="untimeout", description="Remove a member's timeout")
@app_commands.checks.has_permissions(moderate_members=True)
async def untimeout(interaction: discord.Interaction, member: discord.Member):
    await member.timeout(None)
    await interaction.response.send_message(f"🔊 Removed timeout from **{member}**")


@bot.tree.command(name="warn", description="Warn a member")
@app_commands.checks.has_permissions(moderate_members=True)
async def warn(interaction: discord.Interaction, member: discord.Member, reason: str):
    if not await check_target(interaction, member):
        return
    data["warnings"].setdefault(str(member.id), []).append({
        "reason": reason, "mod": str(interaction.user),
        "time": discord.utils.utcnow().strftime("%Y-%m-%d")})
    save_data()
    count = len(data["warnings"][str(member.id)])
    try:
        await member.send(f"⚠️ You were warned in **{interaction.guild.name}**: {reason}")
    except discord.Forbidden:
        pass
    await interaction.response.send_message(
        f"⚠️ Warned **{member}** — {reason} (total warnings: {count})")


@bot.tree.command(name="warnings", description="View a member's warnings")
@app_commands.checks.has_permissions(moderate_members=True)
async def warnings(interaction: discord.Interaction, member: discord.Member):
    items = data["warnings"].get(str(member.id), [])
    if not items:
        return await interaction.response.send_message(
            f"{member} has no warnings.", ephemeral=True)
    lines = [f"**{i}.** {w['reason']} — by {w['mod']} on {w['time']}"
             for i, w in enumerate(items[-10:], start=1)]
    e = discord.Embed(title=f"Warnings for {member}", description="\n".join(lines),
                      color=discord.Color.orange())
    await interaction.response.send_message(embed=e, ephemeral=True)


@bot.tree.command(name="clearwarnings", description="Clear all warnings for a member")
@app_commands.checks.has_permissions(moderate_members=True)
async def clearwarnings(interaction: discord.Interaction, member: discord.Member):
    data["warnings"].pop(str(member.id), None)
    save_data()
    await interaction.response.send_message(f"🧹 Cleared warnings for **{member}**")


@bot.tree.command(name="purge", description="Delete recent messages")
@app_commands.checks.has_permissions(manage_messages=True)
async def purge(interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100]):
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=amount)
    await interaction.followup.send(f"🧹 Deleted {len(deleted)} messages.", ephemeral=True)


@bot.tree.command(name="slowmode", description="Set slowmode in this channel (0 turns it off)")
@app_commands.checks.has_permissions(manage_channels=True)
async def slowmode(interaction: discord.Interaction, seconds: app_commands.Range[int, 0, 21600]):
    await interaction.channel.edit(slowmode_delay=seconds)
    await interaction.response.send_message(
        "Slowmode turned off." if seconds == 0 else f"🐌 Slowmode set to {seconds}s.")


@bot.tree.command(name="lock", description="Lock this channel")
@app_commands.checks.has_permissions(manage_channels=True)
async def lock(interaction: discord.Interaction):
    await interaction.channel.set_permissions(interaction.guild.default_role,
                                              send_messages=False)
    await interaction.response.send_message("🔒 Channel locked.")


@bot.tree.command(name="unlock", description="Unlock this channel")
@app_commands.checks.has_permissions(manage_channels=True)
async def unlock(interaction: discord.Interaction):
    await interaction.channel.set_permissions(interaction.guild.default_role,
                                              send_messages=None)
    await interaction.response.send_message("🔓 Channel unlocked.")


# ---------- Info & fun ----------
@bot.tree.command(name="help", description="Show all commands")
async def help_cmd(interaction: discord.Interaction):
    e = discord.Embed(title="◆ Commands", color=discord.Color.blurple())
    e.add_field(name="◆ Tickets",
                value="/on · /off · /unhide · /hidetoggle · /reseller-ticket · "
                      "/escalate · /addticket (staff)", inline=False)
    e.add_field(name="◆ Moderation",
                value="/kick · /ban · /unban · /timeout · /untimeout · /warn · "
                      "/warnings · /clearwarnings · /purge · /slowmode · /lock · /unlock",
                inline=False)
    e.add_field(name="◆ Server",
                value="/website · /setwebsite · /setimage · /embed · /ticketpanel · /say (staff)",
                inline=False)
    e.add_field(name="◆ Info & Fun",
                value="/ping · /serverinfo · /userinfo · /avatar · /poll · "
                      "/8ball · /coinflip · /roll", inline=False)
    await interaction.response.send_message(embed=e)


@bot.tree.command(name="ping", description="Check the bot's latency")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"🏓 Pong! {round(bot.latency * 1000)}ms")


@bot.tree.command(name="serverinfo", description="Show info about this server")
async def serverinfo(interaction: discord.Interaction):
    g = interaction.guild
    e = discord.Embed(title=g.name, color=discord.Color.blurple())
    if g.icon:
        e.set_thumbnail(url=g.icon.url)
    e.add_field(name="Owner", value=f"<@{g.owner_id}>")
    e.add_field(name="Members", value=g.member_count)
    e.add_field(name="Channels", value=len(g.channels))
    e.add_field(name="Roles", value=len(g.roles))
    e.add_field(name="Boosts", value=g.premium_subscription_count)
    e.add_field(name="Created", value=discord.utils.format_dt(g.created_at, "D"))
    await interaction.response.send_message(embed=e)


@bot.tree.command(name="userinfo", description="Show info about a member")
async def userinfo(interaction: discord.Interaction, member: Optional[discord.Member] = None):
    member = member or interaction.user
    e = discord.Embed(title=str(member), color=member.color)
    e.set_thumbnail(url=member.display_avatar.url)
    e.add_field(name="ID", value=member.id)
    e.add_field(name="Top role", value=member.top_role.mention)
    e.add_field(name="Joined", value=discord.utils.format_dt(member.joined_at, "D"))
    e.add_field(name="Account created", value=discord.utils.format_dt(member.created_at, "D"))
    await interaction.response.send_message(embed=e)


@bot.tree.command(name="avatar", description="Show a member's avatar")
async def avatar(interaction: discord.Interaction, member: Optional[discord.Member] = None):
    member = member or interaction.user
    e = discord.Embed(title=f"{member}'s avatar", color=discord.Color.blurple())
    e.set_image(url=member.display_avatar.url)
    await interaction.response.send_message(embed=e)


@bot.tree.command(name="say", description="Make the bot say something (staff)")
@staff_only()
async def say(interaction: discord.Interaction, message: str):
    await interaction.channel.send(message, allowed_mentions=discord.AllowedMentions.none())
    await interaction.response.send_message("Sent.", ephemeral=True)


@bot.tree.command(name="poll", description="Create a poll (2-4 options)")
async def poll(interaction: discord.Interaction, question: str, option1: str, option2: str,
               option3: Optional[str] = None, option4: Optional[str] = None):
    opts = [o for o in (option1, option2, option3, option4) if o]
    emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣"]
    e = discord.Embed(title=f"📊 {question}",
                      description="\n".join(f"{emojis[i]} {o}" for i, o in enumerate(opts)),
                      color=discord.Color.blurple())
    e.set_footer(text=f"Poll by {interaction.user}")
    await interaction.response.send_message(embed=e)
    msg = await interaction.original_response()
    for i in range(len(opts)):
        await msg.add_reaction(emojis[i])


@bot.tree.command(name="8ball", description="Ask the magic 8-ball")
async def eightball(interaction: discord.Interaction, question: str):
    answers = ["Yes.", "No.", "Definitely.", "Not a chance.", "Ask again later.",
               "Most likely.", "I wouldn't count on it.", "Signs point to yes."]
    await interaction.response.send_message(f"🎱 **{question}**\n{random.choice(answers)}")


@bot.tree.command(name="coinflip", description="Flip a coin")
async def coinflip(interaction: discord.Interaction):
    await interaction.response.send_message(f"🪙 {random.choice(['Heads', 'Tails'])}!")


@bot.tree.command(name="roll", description="Roll a dice")
async def roll(interaction: discord.Interaction, sides: app_commands.Range[int, 2, 1000] = 6):
    await interaction.response.send_message(f"🎲 You rolled a **{random.randint(1, sides)}** (d{sides})")


# ---------- Embed generator ----------
class EmbedModal(discord.ui.Modal, title="Embed Generator"):
    embed_title = discord.ui.TextInput(label="Title", max_length=256)
    embed_desc = discord.ui.TextInput(label="Description",
                                      style=discord.TextStyle.paragraph, max_length=4000)
    embed_color = discord.ui.TextInput(label="Hex color (e.g. 5865F2)",
                                       required=False, max_length=6)
    embed_image = discord.ui.TextInput(label="Image URL", required=False)
    embed_footer = discord.ui.TextInput(label="Footer", required=False, max_length=200)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            color = discord.Color(int(self.embed_color.value, 16)) if self.embed_color.value \
                else discord.Color.blurple()
        except ValueError:
            color = discord.Color.blurple()
        e = discord.Embed(title=self.embed_title.value,
                          description=self.embed_desc.value, color=color)
        if self.embed_image.value:
            e.set_image(url=self.embed_image.value)
        if self.embed_footer.value:
            e.set_footer(text=self.embed_footer.value)
        await interaction.channel.send(embed=e)
        await interaction.response.send_message("Embed sent.", ephemeral=True)


@bot.tree.command(name="embed", description="Create and post a custom embed")
@app_commands.checks.has_permissions(manage_guild=True)
async def embed(interaction: discord.Interaction):
    await interaction.response.send_modal(EmbedModal())


bot.run(os.getenv("DISCORD_TOKEN"))