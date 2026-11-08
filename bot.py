import os, io, json, copy, time, random, asyncio, re
from datetime import timedelta, datetime, timezone
from typing import Optional
from collections import defaultdict, deque
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv
load_dotenv()
STAFF_ROLE_ID = int(os.getenv('STAFF_ROLE_ID'))
LOG_CHANNEL_ID = int(os.getenv('LOG_CHANNEL_ID'))
REVIEW_CHANNEL_ID = int(os.getenv('REVIEW_CHANNEL_ID'))
CATEGORY_ID = int(os.getenv('TICKET_CATEGORY_ID', '0'))
ESCALATE_ROLE_ID = int(os.getenv('ESCALATE_ROLE_ID', '0'))
GUILD_ID = int(os.getenv('GUILD_ID', '0'))
VOUCH_ROLE_ID = int(os.getenv('VOUCH_ROLE_ID', '0'))
VOUCH_CHANNEL_ID = int(os.getenv('VOUCH_CHANNEL_ID', '0'))
MOD_LOG_CHANNEL_ID = int(os.getenv('MOD_LOG_CHANNEL_ID', '0'))
BUSINESS_NAME = 'Frontline'
VOUCH_COOLDOWN_SECONDS = 30
intents = discord.Intents.default()
intents.message_content = os.getenv('MESSAGE_CONTENT') == '1'
PANEL_TITLE = '◆ Frontline Support'
PANEL_LINES = ['TO PURCHASE MANUALLY, PRESS PURCHASE.', 'DO NOT ASK TO ASK. SIMPLY ASK YOUR QUESTION.', 'FOR MEDIA APPLICATIONS. PRESS "MEDIA" BUTTON.', 'FOR AUTOMATIC DELIVERY, USE THE WEBSITE.']
PAYMENT_METHODS = ['💰 CRYPTO CURRENCIES', '💰 PAYPAL (FAMILY & FRIENDS) (MUST PAY ON DISCORD)', '💰 CASHAPP', '💰 CREDIT CARDS (MUST PAY ON DISCORD)', '💰 ZELLE (MUST PAY ON DISCORD)', '💰 VENMO (MUST PAY ON DISCORD)']
PANEL_COLOR = 15844367
DATA_FILE = os.getenv('DATA_FILE', 'data.json')
DEFAULTS = {'website': 'https://example.com', 'image': '', 'tickets_enabled': True, 'warnings': {}, 'vouch_count': 0, 'vouch_sum': 0}

def load_data():
    try:
        with open(DATA_FILE, 'r', encoding='utf-8') as f:
            d = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        d = {}
    for k, v in DEFAULTS.items():
        d.setdefault(k, copy.deepcopy(v))
    return d

def save_data():
    os.makedirs(os.path.dirname(os.path.abspath(DATA_FILE)), exist_ok=True)
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)
data = load_data()

class MyBot(commands.Bot):

    async def setup_hook(self):
        self.add_view(OpenView())
        self.add_view(CloseView())
        self.data, self.save_data, self.is_staff, self.mod_log = (data, save_data, is_staff, mod_log)
        await self.add_cog(AntiSpam(self))
        await self.add_cog(AntiNuke(self))
        await self.add_cog(Giveaway(self))
        await self.add_cog(StaffApps(self))
        await self.add_cog(Community(self))
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            self.tree.clear_commands(guild=None)
            await self.tree.sync()
        else:
            synced = await self.tree.sync()
        print(f'Synced {len(synced)} commands')
bot = MyBot(command_prefix='!', intents=intents)

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}')

@bot.tree.error
async def on_app_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.CheckFailure):
        msg = "You don't have permission to use that."
    else:
        print(error)
        msg = f'Something went wrong: {error}'
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)

def is_staff(member):
    return any((r.id == STAFF_ROLE_ID for r in member.roles)) or member.guild_permissions.administrator

def staff_only():

    async def predicate(interaction: discord.Interaction):
        return is_staff(interaction.user)
    return app_commands.check(predicate)

def is_ticket(channel):
    return isinstance(channel, discord.TextChannel) and channel.topic is not None and channel.topic.isdigit()

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

async def mod_log(guild, action, moderator, target, reason=None, color=discord.Color.orange()):
    """Posts an embed to the mod log channel (if MOD_LOG_CHANNEL_ID is set)."""
    if not MOD_LOG_CHANNEL_ID:
        return
    ch = guild.get_channel(MOD_LOG_CHANNEL_ID)
    if not ch:
        return
    e = discord.Embed(title=action, color=color, timestamp=discord.utils.utcnow())
    if hasattr(target, 'id'):
        e.add_field(name='Target', value=f'{target} (`{target.id}`)')
    else:
        e.add_field(name='Target', value=str(target))
    e.add_field(name='Moderator', value=moderator.mention)
    if reason:
        e.add_field(name='Reason', value=reason[:1000], inline=False)
    try:
        await ch.send(embed=e)
    except discord.HTTPException:
        pass

async def build_transcript(channel):
    lines = []
    async for m in channel.history(limit=None, oldest_first=True):
        text = m.content or ''
        for a in m.attachments:
            text += f' [attachment: {a.url}]'
        if m.embeds and (not text):
            text = '[embed]'
        lines.append(f'[{m.created_at:%Y-%m-%d %H:%M}] {m.author}: {text}')
    return '\n'.join(lines)

def transcript_file(text, name):
    return discord.File(io.BytesIO(text.encode('utf-8')), filename=f'{name}.txt')

class ReviewModal(discord.ui.Modal, title='Leave a review'):
    comment = discord.ui.TextInput(label='Comments', style=discord.TextStyle.paragraph, required=False, max_length=500)

    def __init__(self, stars):
        super().__init__()
        self.stars = stars

    async def on_submit(self, interaction: discord.Interaction):
        ch = bot.get_channel(REVIEW_CHANNEL_ID)
        e = discord.Embed(title='New Review', description=self.comment.value or 'No comment', color=discord.Color.gold())
        e.add_field(name='Rating', value='⭐' * self.stars)
        e.set_author(name=str(interaction.user), icon_url=interaction.user.display_avatar.url)
        await ch.send(embed=e)
        await interaction.response.send_message('Thanks for your review!')

class ReviewView(discord.ui.View):

    def __init__(self):
        super().__init__(timeout=86400)

    @discord.ui.select(placeholder='Rate your support', options=[discord.SelectOption(label=f"{i} star{('s' if i > 1 else '')}", value=str(i)) for i in range(1, 6)])
    async def rate(self, interaction: discord.Interaction, select: discord.ui.Select):
        await interaction.response.send_modal(ReviewModal(int(select.values[0])))
last_vouch = {}
STAR_COLORS = {5: 3066993, 4: 10216804, 3: 15844367, 2: 15105570, 1: 15158332}

@bot.tree.command(name='vouch', description='Leave a vouch for the shop')
@app_commands.describe(stars='Your rating', comment='How was your experience?', product='What you bought (optional)', proof='A screenshot (optional)')
@app_commands.choices(stars=[app_commands.Choice(name='⭐ 1 star', value=1), app_commands.Choice(name='⭐⭐ 2 stars', value=2), app_commands.Choice(name='⭐⭐⭐ 3 stars', value=3), app_commands.Choice(name='⭐⭐⭐⭐ 4 stars', value=4), app_commands.Choice(name='⭐⭐⭐⭐⭐ 5 stars', value=5)])
async def vouch(interaction: discord.Interaction, stars: app_commands.Choice[int], comment: app_commands.Range[str, 3, 500], product: Optional[app_commands.Range[str, 1, 100]]=None, proof: Optional[discord.Attachment]=None):
    if not VOUCH_ROLE_ID:
        return await interaction.response.send_message("Vouches aren't set up yet. An admin needs to add VOUCH_ROLE_ID to the .env file.", ephemeral=True)
    role = interaction.guild.get_role(VOUCH_ROLE_ID)
    if role is None or role not in interaction.user.roles:
        who = role.mention if role else 'the customer role'
        return await interaction.response.send_message(f'You need {who} to leave a vouch.', ephemeral=True)
    now = time.time()
    wait = VOUCH_COOLDOWN_SECONDS - (now - last_vouch.get(interaction.user.id, 0))
    if wait > 0:
        return await interaction.response.send_message(f'Slow down! Try again in {int(wait) + 1}s.', ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    n = stars.value
    number = data['vouch_count'] + 1
    avatar = interaction.user.display_avatar.url
    e = discord.Embed(title='✅ New Vouch', description=f'>>> {comment}', color=STAR_COLORS[n], timestamp=discord.utils.utcnow())
    e.set_author(name=interaction.user.display_name, icon_url=avatar)
    e.set_thumbnail(url=avatar)
    e.add_field(name='Rating', value=f"{'★' * n}{'☆' * (5 - n)}  **{n}/5**", inline=False)
    if product:
        e.add_field(name='Product', value=product)
    e.add_field(name='Vouched by', value=interaction.user.mention)
    e.set_footer(text=f'Vouch #{number} • {BUSINESS_NAME}')
    file = None
    if proof and proof.content_type and proof.content_type.startswith('image/'):
        file = await proof.to_file()
        e.set_image(url=f'attachment://{file.filename}')
    ch = bot.get_channel(VOUCH_CHANNEL_ID) or bot.get_channel(REVIEW_CHANNEL_ID) or interaction.channel
    if file:
        await ch.send(embed=e, file=file)
    else:
        await ch.send(embed=e)
    last_vouch[interaction.user.id] = now
    data['vouch_count'] = number
    data['vouch_sum'] += n
    save_data()
    await interaction.followup.send(f'✅ Your vouch was posted in {ch.mention}. Thank you!', ephemeral=True)

@bot.tree.command(name='vouchstats', description='See the total vouches and average rating')
async def vouchstats(interaction: discord.Interaction):
    count = data['vouch_count']
    if not count:
        return await interaction.response.send_message('No vouches yet.', ephemeral=True)
    avg = data['vouch_sum'] / count
    e = discord.Embed(title=f'📊 {BUSINESS_NAME} Vouches', color=discord.Color.green())
    e.add_field(name='Total vouches', value=str(count))
    e.add_field(name='Average rating', value=f'{avg:.2f} / 5 ⭐')
    await interaction.response.send_message(embed=e)
TICKET_TYPES = {'support': ('Support', '⚙️'), 'purchase': ('Purchase', '💰'), 'media': ('Media Application', '📷'), 'reseller': ('Reseller', '💳'), 'product': ('Product Support', '🛒'), 'general': ('General Support', '💬'), 'other': ('Other', '❓')}
DROPDOWN_OPTIONS = [('support', 'Support', '⚙️'), ('purchase', 'Purchase Now', '💰'), ('media', 'Media', '📷'), ('reseller', 'Resell', '💳'), ('website', 'Website', '🌐'), ('product', 'Product Support', '🛒'), ('general', 'General Support', '💬'), ('other', 'Other', '❓')]

async def create_ticket(guild, user, kind):
    """Returns (channel, None) on success or (None, existing_channel)."""
    existing = discord.utils.get(guild.text_channels, topic=str(user.id))
    if existing:
        return (None, existing)
    label, emoji = TICKET_TYPES[kind]
    staff = guild.get_role(STAFF_ROLE_ID)
    overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False), user: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True), staff: discord.PermissionOverwrite(view_channel=True, send_messages=True), guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True)}
    category = guild.get_channel(CATEGORY_ID) if CATEGORY_ID else None
    ch = await guild.create_text_channel(f'{kind}-{user.name}', overwrites=overwrites, category=category, topic=str(user.id))
    e = discord.Embed(title=f'{emoji} {label}', description='Describe your issue and staff will be with you shortly.', color=discord.Color.blurple())
    e.add_field(name='Opened by', value=user.mention)
    e.add_field(name='Type', value=label)
    await ch.send(f'{user.mention} {staff.mention}', embed=e, view=CloseView())
    return (ch, None)

class CloseView(discord.ui.View):

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label='Close Ticket', style=discord.ButtonStyle.danger, custom_id='ticket:close')
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.channel
        if not (is_staff(interaction.user) or str(interaction.user.id) == channel.topic):
            return await interaction.response.send_message('Not allowed.', ephemeral=True)
        await interaction.response.send_message('Closing in 5 seconds, saving transcript...')
        text = await build_transcript(channel)
        log = bot.get_channel(LOG_CHANNEL_ID)
        await log.send(f'Transcript for **{channel.name}**', file=transcript_file(text, channel.name))
        owner = await get_owner(interaction.guild, channel)
        if owner:
            try:
                await owner.send("Your ticket was closed. Here's the transcript, and we'd love a review:", file=transcript_file(text, channel.name), view=ReviewView())
            except discord.Forbidden:
                pass
        await asyncio.sleep(5)
        await channel.delete()

async def handle_open(interaction: discord.Interaction, kind: str):
    if not data['tickets_enabled']:
        return await interaction.response.send_message('Tickets are currently closed. Please try again later.', ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    ch, existing = await create_ticket(interaction.guild, interaction.user, kind)
    if existing:
        await interaction.followup.send(f'You already have a ticket: {existing.mention}', ephemeral=True)
    else:
        await interaction.followup.send(f'Ticket created: {ch.mention}', ephemeral=True)

class OpenView(discord.ui.View):

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.select(custom_id='ticket:type', placeholder='◆ Choose ticket type...', options=[discord.SelectOption(label=f'◆ {label}', value=key, emoji=emoji) for key, label, emoji in DROPDOWN_OPTIONS])
    async def choose(self, interaction: discord.Interaction, select: discord.ui.Select):
        choice = select.values[0]
        if choice == 'website':
            await interaction.response.send_message(f"🌐 **Our website:** {data['website']}", ephemeral=True)
        else:
            await handle_open(interaction, choice)
        try:
            await interaction.message.edit(view=OpenView())
        except discord.HTTPException:
            pass

@bot.tree.command(name='ticketpanel', description='Post the ticket panel here')
@app_commands.checks.has_permissions(administrator=True)
async def ticketpanel(interaction: discord.Interaction):
    rules = '\n\n'.join((f'🟢 `{line}`' for line in PANEL_LINES))
    payments = '\n'.join(PAYMENT_METHODS)
    description = f"{rules}\n\n# ACCEPTED PAYMENT METHODS:\n```\n{payments}\n```\n[**OFFICIAL FRONTLINE WEBSITE**]({data['website']})"
    e = discord.Embed(title=PANEL_TITLE, description=description, color=PANEL_COLOR)
    if data['image']:
        e.set_image(url=data['image'])
    e.set_footer(text='Frontline Support')
    await interaction.channel.send(embed=e, view=OpenView())
    await interaction.response.send_message('Panel posted.', ephemeral=True)

@bot.tree.command(name='on', description='Turn the ticket system on (staff)')
@staff_only()
async def tickets_on(interaction: discord.Interaction):
    data['tickets_enabled'] = True
    save_data()
    await interaction.response.send_message('✅ Tickets are now **ON**.', ephemeral=True)

@bot.tree.command(name='off', description='Turn the ticket system off (staff)')
@staff_only()
async def tickets_off(interaction: discord.Interaction):
    data['tickets_enabled'] = False
    save_data()
    await interaction.response.send_message('⛔ Tickets are now **OFF**.', ephemeral=True)

@bot.tree.command(name='hidetoggle', description='Hide/show this ticket from its owner (staff)')
@staff_only()
async def hidetoggle(interaction: discord.Interaction):
    owner = await get_owner(interaction.guild, interaction.channel)
    if not owner:
        return await interaction.response.send_message('Use this inside a ticket.', ephemeral=True)
    hidden = interaction.channel.overwrites_for(owner).view_channel is False
    await interaction.channel.set_permissions(owner, view_channel=hidden, send_messages=True, attach_files=True)
    await interaction.response.send_message('👁️ Ticket is now **visible** to the user.' if hidden else '🙈 Ticket is now **hidden** from the user.')

@bot.tree.command(name='unhide', description='Make this ticket visible to its owner again (staff)')
@staff_only()
async def unhide(interaction: discord.Interaction):
    owner = await get_owner(interaction.guild, interaction.channel)
    if not owner:
        return await interaction.response.send_message('Use this inside a ticket.', ephemeral=True)
    await interaction.channel.set_permissions(owner, view_channel=True, send_messages=True, attach_files=True)
    await interaction.response.send_message('👁️ Ticket is now **visible** to the user.')

@bot.tree.command(name='addticket', description='Add a member to this ticket (staff)')
@staff_only()
async def addticket(interaction: discord.Interaction, member: discord.Member):
    if not is_ticket(interaction.channel):
        return await interaction.response.send_message('Use this inside a ticket.', ephemeral=True)
    await interaction.channel.set_permissions(member, view_channel=True, send_messages=True, attach_files=True)
    await interaction.response.send_message(f'✅ Added {member.mention} to this ticket.')

@bot.tree.command(name='reseller-ticket', description='Open a reseller ticket for a member (staff)')
@staff_only()
async def reseller_ticket(interaction: discord.Interaction, member: discord.Member):
    await interaction.response.defer(ephemeral=True)
    ch, existing = await create_ticket(interaction.guild, member, 'reseller')
    if existing:
        await interaction.followup.send(f'{member.mention} already has a ticket: {existing.mention}', ephemeral=True)
    else:
        await interaction.followup.send(f'Reseller ticket created: {ch.mention}', ephemeral=True)

@bot.tree.command(name='escalate', description='Escalate this ticket (staff)')
@staff_only()
async def escalate(interaction: discord.Interaction, reason: str='No reason given'):
    channel = interaction.channel
    if not is_ticket(channel):
        return await interaction.response.send_message('Use this inside a ticket.', ephemeral=True)
    role = interaction.guild.get_role(ESCALATE_ROLE_ID) if ESCALATE_ROLE_ID else None
    e = discord.Embed(title='🚨 Ticket escalated', description=reason, color=discord.Color.red())
    e.set_footer(text=f'Escalated by {interaction.user}')
    await interaction.response.send_message(role.mention if role else None, embed=e, allowed_mentions=discord.AllowedMentions(roles=True))
    if role:
        await channel.set_permissions(role, view_channel=True, send_messages=True)
    if not channel.name.startswith('escalated-'):
        await channel.edit(name=f'escalated-{channel.name}'[:100])

class PurgeTicketsView(discord.ui.View):

    def __init__(self, invoker, save_transcripts):
        super().__init__(timeout=30)
        self.invoker = invoker
        self.save_transcripts = save_transcripts

    async def interaction_check(self, interaction: discord.Interaction):
        if interaction.user.id != self.invoker.id:
            await interaction.response.send_message("This isn't your confirmation.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label='Delete all tickets', style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        tickets = [c for c in guild.text_channels if is_ticket(c)]
        await interaction.response.edit_message(content=f'🗑️ Deleting {len(tickets)} ticket(s)...', view=None)
        log = bot.get_channel(LOG_CHANNEL_ID)
        deleted = 0
        for c in tickets:
            try:
                if self.save_transcripts and log:
                    text = await build_transcript(c)
                    await log.send(f'Transcript for **{c.name}** (purged)', file=transcript_file(text, c.name))
                await c.delete(reason=f'Ticket purge by {interaction.user}')
                deleted += 1
            except discord.HTTPException:
                pass
            await asyncio.sleep(1)
        await mod_log(guild, '🗑️ Tickets purged', interaction.user, f'{deleted} ticket channel(s)', color=discord.Color.red())
        try:
            await interaction.edit_original_response(content=f'✅ Deleted **{deleted}** of {len(tickets)} ticket(s).')
        except discord.HTTPException:
            pass
        self.stop()

    @discord.ui.button(label='Cancel', style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content='Cancelled.', view=None)
        self.stop()

@bot.tree.command(name='purgetickets', description='Delete ALL open ticket channels (admin)')
@app_commands.describe(save_transcripts='Save a transcript of each ticket to the log channel first')
@app_commands.checks.has_permissions(administrator=True)
async def purgetickets(interaction: discord.Interaction, save_transcripts: bool=False):
    if is_ticket(interaction.channel):
        return await interaction.response.send_message('Run this in a normal channel, not inside a ticket.', ephemeral=True)
    count = len([c for c in interaction.guild.text_channels if is_ticket(c)])
    if count == 0:
        return await interaction.response.send_message('There are no open tickets.', ephemeral=True)
    note = ' Transcripts will be saved first.' if save_transcripts else ' Transcripts will NOT be saved.'
    await interaction.response.send_message(f'⚠️ This will permanently delete **{count}** ticket channel(s).{note}\nAre you sure?', view=PurgeTicketsView(interaction.user, save_transcripts), ephemeral=True)

@bot.tree.command(name='website', description='Get our website link')
async def website(interaction: discord.Interaction):
    e = discord.Embed(title='🌐 Our Website', description=data['website'], color=discord.Color.blurple())
    if data['image']:
        e.set_image(url=data['image'])
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label='Visit Website', url=data['website']))
    await interaction.response.send_message(embed=e, view=view)

@bot.tree.command(name='setwebsite', description='Change the website link (admin)')
@app_commands.checks.has_permissions(administrator=True)
async def setwebsite(interaction: discord.Interaction, url: str):
    if not url.startswith(('http://', 'https://')):
        return await interaction.response.send_message('The link must start with http:// or https://', ephemeral=True)
    data['website'] = url
    save_data()
    await interaction.response.send_message(f"✅ Website updated to {url}\nThe dropdown's Website option and `/website` use it right away. Run `/ticketpanel` again to update the link inside the panel text.", ephemeral=True)

@bot.tree.command(name='setimage', description="Set the banner image, or type 'none' to remove (admin)")
@app_commands.checks.has_permissions(administrator=True)
async def setimage(interaction: discord.Interaction, url: str):
    if url.lower() == 'none':
        data['image'] = ''
        msg = '✅ Banner image removed.'
    elif url.startswith(('http://', 'https://')):
        data['image'] = url
        msg = '✅ Banner image updated. Run `/ticketpanel` again to see it on the panel.'
    else:
        msg = 'Send a direct image link starting with http:// or https://, or type `none`.'
    save_data()
    await interaction.response.send_message(msg, ephemeral=True)

@bot.tree.command(name='kick', description='Kick a member')
@app_commands.checks.has_permissions(kick_members=True)
async def kick(interaction: discord.Interaction, member: discord.Member, reason: str='No reason given'):
    if not await check_target(interaction, member):
        return
    await member.kick(reason=f'{interaction.user}: {reason}')
    await interaction.response.send_message(f'👢 Kicked **{member}** — {reason}')
    await mod_log(interaction.guild, '👢 Member kicked', interaction.user, member, reason)

@bot.tree.command(name='ban', description='Ban a member')
@app_commands.checks.has_permissions(ban_members=True)
async def ban(interaction: discord.Interaction, member: discord.Member, reason: str='No reason given'):
    if not await check_target(interaction, member):
        return
    await member.ban(reason=f'{interaction.user}: {reason}')
    await interaction.response.send_message(f'🔨 Banned **{member}** — {reason}')
    await mod_log(interaction.guild, '🔨 Member banned', interaction.user, member, reason, discord.Color.red())

@bot.tree.command(name='idban', description="Ban a user by ID, even if they're not in the server")
@app_commands.checks.has_permissions(ban_members=True)
async def idban(interaction: discord.Interaction, user_id: str, reason: str='No reason given'):
    try:
        uid = int(user_id)
    except ValueError:
        return await interaction.response.send_message("That isn't a valid ID.", ephemeral=True)
    member = interaction.guild.get_member(uid)
    if member and (not await check_target(interaction, member)):
        return
    try:
        await interaction.guild.ban(discord.Object(id=uid), reason=f'{interaction.user}: {reason}')
    except discord.NotFound:
        return await interaction.response.send_message('No user found with that ID.', ephemeral=True)
    except discord.Forbidden:
        return await interaction.response.send_message("I don't have permission to ban that user.", ephemeral=True)
    await interaction.response.send_message(f'🔨 Banned ID `{uid}` — {reason}')
    await mod_log(interaction.guild, '🔨 ID banned', interaction.user, f'`{uid}`', reason, discord.Color.red())

@bot.tree.command(name='softban', description='Ban then unban a member to delete their recent messages')
@app_commands.checks.has_permissions(ban_members=True)
async def softban(interaction: discord.Interaction, member: discord.Member, days: app_commands.Range[int, 1, 7]=1, reason: str='No reason given'):
    if not await check_target(interaction, member):
        return
    await interaction.response.defer()
    await member.ban(reason=f'Softban by {interaction.user}: {reason}', delete_message_seconds=days * 86400)
    await interaction.guild.unban(member, reason='Softban')
    await interaction.followup.send(f'🧹 Softbanned **{member}** (deleted {days} day(s) of messages) — {reason}')
    await mod_log(interaction.guild, '🧹 Member softbanned', interaction.user, member, reason)

@bot.tree.command(name='unban', description='Unban a user by their ID')
@app_commands.checks.has_permissions(ban_members=True)
async def unban(interaction: discord.Interaction, user_id: str):
    try:
        await interaction.guild.unban(discord.Object(id=int(user_id)))
    except (ValueError, discord.NotFound):
        return await interaction.response.send_message("Couldn't find a ban for that ID.", ephemeral=True)
    await interaction.response.send_message(f'✅ Unbanned `{user_id}`')
    await mod_log(interaction.guild, '✅ User unbanned', interaction.user, f'`{user_id}`', color=discord.Color.green())

@bot.tree.command(name='timeout', description='Timeout a member')
@app_commands.checks.has_permissions(moderate_members=True)
async def timeout(interaction: discord.Interaction, member: discord.Member, minutes: app_commands.Range[int, 1, 40320], reason: str='No reason given'):
    if not await check_target(interaction, member):
        return
    await member.timeout(timedelta(minutes=minutes), reason=f'{interaction.user}: {reason}')
    await interaction.response.send_message(f'🔇 Timed out **{member}** for {minutes} min — {reason}')
    await mod_log(interaction.guild, f'🔇 Timeout ({minutes} min)', interaction.user, member, reason)

@bot.tree.command(name='untimeout', description="Remove a member's timeout")
@app_commands.checks.has_permissions(moderate_members=True)
async def untimeout(interaction: discord.Interaction, member: discord.Member):
    await member.timeout(None)
    await interaction.response.send_message(f'🔊 Removed timeout from **{member}**')
    await mod_log(interaction.guild, '🔊 Timeout removed', interaction.user, member, color=discord.Color.green())

@bot.tree.command(name='warn', description='Warn a member')
@app_commands.checks.has_permissions(moderate_members=True)
async def warn(interaction: discord.Interaction, member: discord.Member, reason: str):
    if not await check_target(interaction, member):
        return
    data['warnings'].setdefault(str(member.id), []).append({'reason': reason, 'mod': str(interaction.user), 'time': discord.utils.utcnow().strftime('%Y-%m-%d')})
    save_data()
    count = len(data['warnings'][str(member.id)])
    try:
        await member.send(f'⚠️ You were warned in **{interaction.guild.name}**: {reason}')
    except discord.Forbidden:
        pass
    await interaction.response.send_message(f'⚠️ Warned **{member}** — {reason} (total warnings: {count})')
    await mod_log(interaction.guild, f'⚠️ Warning #{count}', interaction.user, member, reason)

@bot.tree.command(name='warnings', description="View a member's warnings")
@app_commands.checks.has_permissions(moderate_members=True)
async def warnings(interaction: discord.Interaction, member: discord.Member):
    items = data['warnings'].get(str(member.id), [])
    if not items:
        return await interaction.response.send_message(f'{member} has no warnings.', ephemeral=True)
    start = max(0, len(items) - 10)
    lines = [f"**{i}.** {w['reason']} — by {w['mod']} on {w['time']}" for i, w in enumerate(items[start:], start=start + 1)]
    e = discord.Embed(title=f'Warnings for {member} ({len(items)} total)', description='\n'.join(lines), color=discord.Color.orange())
    e.set_footer(text='Remove one with /delwarn')
    await interaction.response.send_message(embed=e, ephemeral=True)

@bot.tree.command(name='delwarn', description='Remove a single warning by its number')
@app_commands.checks.has_permissions(moderate_members=True)
async def delwarn(interaction: discord.Interaction, member: discord.Member, number: int):
    items = data['warnings'].get(str(member.id), [])
    if not 1 <= number <= len(items):
        return await interaction.response.send_message("That member doesn't have a warning with that number.", ephemeral=True)
    removed = items.pop(number - 1)
    if not items:
        data['warnings'].pop(str(member.id), None)
    save_data()
    await interaction.response.send_message(f"🗑️ Removed warning #{number} from **{member}**: {removed['reason']}")
    await mod_log(interaction.guild, '🗑️ Warning removed', interaction.user, member, removed['reason'], discord.Color.green())

@bot.tree.command(name='clearwarnings', description='Clear all warnings for a member')
@app_commands.checks.has_permissions(moderate_members=True)
async def clearwarnings(interaction: discord.Interaction, member: discord.Member):
    data['warnings'].pop(str(member.id), None)
    save_data()
    await interaction.response.send_message(f'🧹 Cleared warnings for **{member}**')
    await mod_log(interaction.guild, '🧹 Warnings cleared', interaction.user, member, color=discord.Color.green())

@bot.tree.command(name='nick', description="Change or reset a member's nickname")
@app_commands.checks.has_permissions(manage_nicknames=True)
async def nick(interaction: discord.Interaction, member: discord.Member, nickname: Optional[app_commands.Range[str, 1, 32]]=None):
    if not await check_target(interaction, member):
        return
    await member.edit(nick=nickname, reason=f'Changed by {interaction.user}')
    await interaction.response.send_message(f'✏️ Nickname for **{member}** set to `{nickname}`.' if nickname else f'✏️ Reset nickname for **{member}**.')
    await mod_log(interaction.guild, '✏️ Nickname changed', interaction.user, member, nickname or 'Reset')

async def check_role(interaction, role):
    guild = interaction.guild
    if role.is_default() or role.managed:
        msg = "I can't give or remove that role."
    elif role >= guild.me.top_role:
        msg = 'That role is higher than (or equal to) my top role.'
    elif interaction.user.id != guild.owner_id and role >= interaction.user.top_role:
        msg = 'That role is higher than (or equal to) your top role.'
    else:
        return True
    await interaction.response.send_message(msg, ephemeral=True)
    return False

@bot.tree.command(name='addrole', description='Give a role to a member')
@app_commands.checks.has_permissions(manage_roles=True)
async def addrole(interaction: discord.Interaction, member: discord.Member, role: discord.Role):
    if not await check_role(interaction, role):
        return
    await member.add_roles(role, reason=f'By {interaction.user}')
    await interaction.response.send_message(f'✅ Gave {role.mention} to {member.mention}', allowed_mentions=discord.AllowedMentions.none())
    await mod_log(interaction.guild, '➕ Role added', interaction.user, member, role.name, discord.Color.green())

@bot.tree.command(name='removerole', description='Remove a role from a member')
@app_commands.checks.has_permissions(manage_roles=True)
async def removerole(interaction: discord.Interaction, member: discord.Member, role: discord.Role):
    if not await check_role(interaction, role):
        return
    await member.remove_roles(role, reason=f'By {interaction.user}')
    await interaction.response.send_message(f'✅ Removed {role.mention} from {member.mention}', allowed_mentions=discord.AllowedMentions.none())
    await mod_log(interaction.guild, '➖ Role removed', interaction.user, member, role.name)

@bot.tree.command(name='purge', description='Delete recent messages (optionally only from one member)')
@app_commands.checks.has_permissions(manage_messages=True)
async def purge(interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100], member: Optional[discord.Member]=None):
    await interaction.response.defer(ephemeral=True)
    check = (lambda m: m.author.id == member.id) if member else lambda m: True
    deleted = await interaction.channel.purge(limit=amount, check=check)
    await interaction.followup.send(f'🧹 Deleted {len(deleted)} messages.', ephemeral=True)
    await mod_log(interaction.guild, '🧹 Messages purged', interaction.user, member or interaction.channel, f'{len(deleted)} messages in {interaction.channel.mention}')

@bot.tree.command(name='slowmode', description='Set slowmode in this channel (0 turns it off)')
@app_commands.checks.has_permissions(manage_channels=True)
async def slowmode(interaction: discord.Interaction, seconds: app_commands.Range[int, 0, 21600]):
    await interaction.channel.edit(slowmode_delay=seconds)
    await interaction.response.send_message('Slowmode turned off.' if seconds == 0 else f'🐌 Slowmode set to {seconds}s.')

@bot.tree.command(name='lock', description='Lock this channel')
@app_commands.checks.has_permissions(manage_channels=True)
async def lock(interaction: discord.Interaction):
    await interaction.channel.set_permissions(interaction.guild.default_role, send_messages=False)
    await interaction.response.send_message('🔒 Channel locked.')
    await mod_log(interaction.guild, '🔒 Channel locked', interaction.user, interaction.channel)

@bot.tree.command(name='unlock', description='Unlock this channel')
@app_commands.checks.has_permissions(manage_channels=True)
async def unlock(interaction: discord.Interaction):
    await interaction.channel.set_permissions(interaction.guild.default_role, send_messages=None)
    await interaction.response.send_message('🔓 Channel unlocked.')
    await mod_log(interaction.guild, '🔓 Channel unlocked', interaction.user, interaction.channel, color=discord.Color.green())

@bot.tree.command(name='help', description='Show all commands')
async def help_cmd(interaction: discord.Interaction):
    e = discord.Embed(title='◆ Commands', color=discord.Color.blurple())
    e.add_field(name='◆ Tickets', value='/on · /off · /unhide · /hidetoggle · /reseller-ticket · /escalate · /addticket · /purgetickets (staff)', inline=False)
    e.add_field(name='◆ Vouches', value='/vouch · /vouchstats', inline=False)
    e.add_field(name='◆ Moderation', value='/kick · /ban · /idban · /softban · /unban · /timeout · /untimeout · /warn · /warnings · /delwarn · /clearwarnings · /nick · /addrole · /removerole · /purge · /slowmode · /lock · /unlock', inline=False)
    e.add_field(name='◆ Server', value='/website · /setwebsite · /setimage · /embed · /ticketpanel · /say (staff)', inline=False)
    e.add_field(name='◆ Protection', value='/antispam · /antinuke (server owner)', inline=False)
    e.add_field(name='◆ Community', value='/giveaway · /staffapps · /verifypanel · /boosterpanel', inline=False)
    e.add_field(name='◆ Info & Fun', value='/ping · /serverinfo · /userinfo · /avatar · /poll · /8ball · /coinflip · /roll', inline=False)
    await interaction.response.send_message(embed=e)

@bot.tree.command(name='ping', description="Check the bot's latency")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f'🏓 Pong! {round(bot.latency * 1000)}ms')

@bot.tree.command(name='serverinfo', description='Show info about this server')
async def serverinfo(interaction: discord.Interaction):
    g = interaction.guild
    e = discord.Embed(title=g.name, color=discord.Color.blurple())
    if g.icon:
        e.set_thumbnail(url=g.icon.url)
    e.add_field(name='Owner', value=f'<@{g.owner_id}>')
    e.add_field(name='Members', value=g.member_count)
    e.add_field(name='Channels', value=len(g.channels))
    e.add_field(name='Roles', value=len(g.roles))
    e.add_field(name='Boosts', value=g.premium_subscription_count)
    e.add_field(name='Created', value=discord.utils.format_dt(g.created_at, 'D'))
    await interaction.response.send_message(embed=e)

@bot.tree.command(name='userinfo', description='Show info about a member')
async def userinfo(interaction: discord.Interaction, member: Optional[discord.Member]=None):
    member = member or interaction.user
    e = discord.Embed(title=str(member), color=member.color)
    e.set_thumbnail(url=member.display_avatar.url)
    e.add_field(name='ID', value=member.id)
    e.add_field(name='Top role', value=member.top_role.mention)
    e.add_field(name='Joined', value=discord.utils.format_dt(member.joined_at, 'D'))
    e.add_field(name='Account created', value=discord.utils.format_dt(member.created_at, 'D'))
    await interaction.response.send_message(embed=e)

@bot.tree.command(name='avatar', description="Show a member's avatar")
async def avatar(interaction: discord.Interaction, member: Optional[discord.Member]=None):
    member = member or interaction.user
    e = discord.Embed(title=f"{member}'s avatar", color=discord.Color.blurple())
    e.set_image(url=member.display_avatar.url)
    await interaction.response.send_message(embed=e)

@bot.tree.command(name='say', description='Make the bot say something (staff)')
@staff_only()
async def say(interaction: discord.Interaction, message: str):
    await interaction.channel.send(message, allowed_mentions=discord.AllowedMentions.none())
    await interaction.response.send_message('Sent.', ephemeral=True)

@bot.tree.command(name='poll', description='Create a poll (2-4 options)')
async def poll(interaction: discord.Interaction, question: str, option1: str, option2: str, option3: Optional[str]=None, option4: Optional[str]=None):
    opts = [o for o in (option1, option2, option3, option4) if o]
    emojis = ['1️⃣', '2️⃣', '3️⃣', '4️⃣']
    e = discord.Embed(title=f'📊 {question}', description='\n'.join((f'{emojis[i]} {o}' for i, o in enumerate(opts))), color=discord.Color.blurple())
    e.set_footer(text=f'Poll by {interaction.user}')
    await interaction.response.send_message(embed=e)
    msg = await interaction.original_response()
    for i in range(len(opts)):
        await msg.add_reaction(emojis[i])

@bot.tree.command(name='8ball', description='Ask the magic 8-ball')
async def eightball(interaction: discord.Interaction, question: str):
    answers = ['Yes.', 'No.', 'Definitely.', 'Not a chance.', 'Ask again later.', 'Most likely.', "I wouldn't count on it.", 'Signs point to yes.']
    await interaction.response.send_message(f'🎱 **{question}**\n{random.choice(answers)}')

@bot.tree.command(name='coinflip', description='Flip a coin')
async def coinflip(interaction: discord.Interaction):
    await interaction.response.send_message(f"🪙 {random.choice(['Heads', 'Tails'])}!")

@bot.tree.command(name='roll', description='Roll a dice')
async def roll(interaction: discord.Interaction, sides: app_commands.Range[int, 2, 1000]=6):
    await interaction.response.send_message(f'🎲 You rolled a **{random.randint(1, sides)}** (d{sides})')

class EmbedModal(discord.ui.Modal, title='Embed Generator'):
    embed_title = discord.ui.TextInput(label='Title', max_length=256)
    embed_desc = discord.ui.TextInput(label='Description', style=discord.TextStyle.paragraph, max_length=4000)
    embed_color = discord.ui.TextInput(label='Hex color (e.g. 5865F2)', required=False, max_length=6)
    embed_image = discord.ui.TextInput(label='Image URL', required=False)
    embed_footer = discord.ui.TextInput(label='Footer', required=False, max_length=200)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            color = discord.Color(int(self.embed_color.value, 16)) if self.embed_color.value else discord.Color.blurple()
        except ValueError:
            color = discord.Color.blurple()
        e = discord.Embed(title=self.embed_title.value, description=self.embed_desc.value, color=color)
        if self.embed_image.value:
            e.set_image(url=self.embed_image.value)
        if self.embed_footer.value:
            e.set_footer(text=self.embed_footer.value)
        await interaction.channel.send(embed=e)
        await interaction.response.send_message('Embed sent.', ephemeral=True)

@bot.tree.command(name='embed', description='Create and post a custom embed')
@app_commands.checks.has_permissions(manage_guild=True)
async def embed(interaction: discord.Interaction):
    await interaction.response.send_modal(EmbedModal())
DURATIONS = [('60 seconds', 60), ('5 minutes', 300), ('10 minutes', 600), ('30 minutes', 1800), ('1 hour', 3600), ('6 hours', 21600), ('1 day', 86400), ('1 week', 604800)]
LEVELS = {'low': (7, 5, 8, 5), 'medium': (5, 5, 5, 4), 'high': (4, 4, 4, 3)}
DUP_WINDOW = 20

def fmt(seconds):
    for label, value in DURATIONS:
        if value == seconds:
            return label
    return f'{seconds} seconds'

class AntiSpam(commands.GroupCog, name='antispam'):

    def __init__(self, bot):
        super().__init__()
        self.bot = bot
        self.history = defaultdict(deque)
        self.busy = set()

    def cfg(self):
        return self.bot.data.setdefault('antispam', {'enabled': False, 'timeout': 600, 'level': 'medium', 'messages': 5, 'seconds': 5, 'mentions': 5, 'duplicates': 4})

    @app_commands.command(name='enable', description='Turn anti-spam on and choose the timeout length')
    @app_commands.describe(timeout='How long spammers are timed out', sensitivity='How strict it is (default: medium)')
    @app_commands.choices(timeout=[app_commands.Choice(name=n, value=v) for n, v in DURATIONS], sensitivity=[app_commands.Choice(name='Low (relaxed)', value='low'), app_commands.Choice(name='Medium', value='medium'), app_commands.Choice(name='High (strict)', value='high')])
    @app_commands.checks.has_permissions(manage_guild=True)
    async def enable(self, interaction: discord.Interaction, timeout: app_commands.Choice[int], sensitivity: Optional[app_commands.Choice[str]]=None):
        level = sensitivity.value if sensitivity else 'medium'
        m, s, men, dup = LEVELS[level]
        c = self.cfg()
        c.update(enabled=True, timeout=timeout.value, level=level, messages=m, seconds=s, mentions=men, duplicates=dup)
        self.bot.save_data()
        note = ''
        if not interaction.guild.me.guild_permissions.moderate_members:
            note += "\n⚠️ I don't have the **Moderate Members** permission, so I can't time anyone out yet."
        if not interaction.guild.me.guild_permissions.manage_messages:
            note += "\n⚠️ I don't have **Manage Messages**, so I can't delete spam."
        await interaction.response.send_message(f'🛡️ Anti-spam is **ON**. Spammers get a **{timeout.name}** timeout (sensitivity: **{level}**).{note}', ephemeral=True)

    @app_commands.command(name='disable', description='Turn anti-spam off')
    @app_commands.checks.has_permissions(manage_guild=True)
    async def disable(self, interaction: discord.Interaction):
        self.cfg()['enabled'] = False
        self.bot.save_data()
        self.history.clear()
        await interaction.response.send_message('🛡️ Anti-spam is now **OFF**.', ephemeral=True)

    @app_commands.command(name='status', description='See the current anti-spam settings')
    @app_commands.checks.has_permissions(manage_guild=True)
    async def status(self, interaction: discord.Interaction):
        c = self.cfg()
        dup_ok = self.bot.intents.message_content
        e = discord.Embed(title='🛡️ Anti-spam', color=discord.Color.green() if c['enabled'] else discord.Color.red())
        e.add_field(name='Status', value='ON' if c['enabled'] else 'OFF')
        e.add_field(name='Timeout', value=fmt(c['timeout']))
        e.add_field(name='Sensitivity', value=c['level'].title())
        e.add_field(name='Triggers', inline=False, value=f"• {c['messages']}+ messages in {c['seconds']}s\n• {c['mentions']}+ mentions in one message\n" + (f"• {c['duplicates']}+ identical messages in {DUP_WINDOW}s" if dup_ok else '• Repeated-message detection is off (needs Message Content Intent)'))
        e.set_footer(text='Staff, admins, people with Manage Messages and ticket channels are ignored.')
        await interaction.response.send_message(embed=e, ephemeral=True)

    def sweep(self, now):
        for key in [k for k, d in self.history.items() if not d or now - d[-1][0] > 60]:
            del self.history[key]

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        c = self.cfg()
        if not c['enabled'] or message.guild is None or message.author.bot:
            return
        member = message.author
        if not isinstance(member, discord.Member):
            return
        if self.bot.is_staff(member) or member.guild_permissions.manage_messages:
            return
        ch = message.channel
        if isinstance(ch, discord.TextChannel) and ch.topic and ch.topic.isdigit():
            return
        key = (message.guild.id, member.id)
        if key in self.busy:
            return
        now = time.monotonic()
        if len(self.history) > 500:
            self.sweep(now)
        hist = self.history[key]
        hist.append((now, message))
        while hist and now - hist[0][0] > max(c['seconds'], DUP_WINDOW):
            hist.popleft()
        reason = None
        recent = [m for t, m in hist if now - t <= c['seconds']]
        if len(recent) >= c['messages']:
            reason = f"sending {len(recent)} messages in {c['seconds']} seconds"
        elif len(message.mentions) + len(message.role_mentions) >= c['mentions']:
            reason = 'mass mentioning'
        elif message.content:
            same = [m for t, m in hist if m.content == message.content]
            if len(same) >= c['duplicates']:
                reason = 'repeating the same message'
        if not reason:
            return
        msgs = [m for _, m in hist]
        self.history.pop(key, None)
        self.busy.add(key)
        try:
            await self.punish(message, member, reason, msgs)
        finally:
            self.busy.discard(key)

    async def punish(self, message, member, reason, msgs):
        duration = self.cfg()['timeout']
        timed_out = False
        try:
            await member.timeout(timedelta(seconds=duration), reason=f'Anti-spam: {reason}')
            timed_out = True
        except discord.HTTPException:
            pass
        for m in msgs:
            try:
                await m.delete()
            except discord.HTTPException:
                pass
        text = f'🚫 {member.mention} was timed out for **{fmt(duration)}** for {reason}.' if timed_out else f'⚠️ {member.mention}, please stop spamming ({reason}).'
        try:
            await message.channel.send(text, delete_after=10, allowed_mentions=discord.AllowedMentions(users=[member]))
        except discord.HTTPException:
            pass
        await self.bot.mod_log(message.guild, '🛡️ Anti-spam', message.guild.me, member, f'{reason} • timeout {fmt(duration)}' if timed_out else f'{reason} • could NOT time out (check my permissions and role position)', discord.Color.red())
NUKE_WINDOW = 10
NUKE_ACTIONS = {discord.AuditLogAction.channel_delete: 'deleting channels', discord.AuditLogAction.channel_create: 'creating channels', discord.AuditLogAction.role_delete: 'deleting roles', discord.AuditLogAction.role_create: 'creating roles', discord.AuditLogAction.ban: 'banning members', discord.AuditLogAction.kick: 'kicking members', discord.AuditLogAction.webhook_create: 'creating webhooks'}
NUKE_LIMITS = {'low': 5, 'medium': 3, 'high': 2}
NUKE_PUNISHMENTS = {'ban': 'Ban', 'kick': 'Kick', 'strip': 'Strip all roles'}

def owner_only():

    async def predicate(interaction: discord.Interaction):
        return interaction.guild is not None and interaction.user.id == interaction.guild.owner_id
    return app_commands.check(predicate)

class AntiNuke(commands.GroupCog, name='antinuke'):

    def __init__(self, bot):
        super().__init__()
        self.bot = bot
        self.events = defaultdict(deque)
        self.handled = {}

    def cfg(self):
        return self.bot.data.setdefault('antinuke', {'enabled': False, 'punishment': 'ban', 'level': 'medium', 'whitelist': []})

    @app_commands.command(name='enable', description='Turn anti-nuke on (server owner only)')
    @app_commands.describe(punishment='What happens to someone who starts nuking', sensitivity='How quickly it reacts (default: medium)')
    @app_commands.choices(punishment=[app_commands.Choice(name='Ban them', value='ban'), app_commands.Choice(name='Kick them', value='kick'), app_commands.Choice(name='Strip all their roles', value='strip')], sensitivity=[app_commands.Choice(name='Low (5 actions in 10s)', value='low'), app_commands.Choice(name='Medium (3 actions in 10s)', value='medium'), app_commands.Choice(name='High (2 actions in 10s)', value='high')])
    @owner_only()
    async def enable(self, interaction: discord.Interaction, punishment: app_commands.Choice[str], sensitivity: Optional[app_commands.Choice[str]]=None):
        level = sensitivity.value if sensitivity else 'medium'
        self.cfg().update(enabled=True, punishment=punishment.value, level=level)
        self.bot.save_data()
        perms = interaction.guild.me.guild_permissions
        need = {'ban': ('ban_members', 'Ban Members'), 'kick': ('kick_members', 'Kick Members'), 'strip': ('manage_roles', 'Manage Roles')}[punishment.value]
        note = ''
        if not perms.view_audit_log:
            note += "\n⚠️ I need the **View Audit Log** permission or anti-nuke can't see anything."
        if not getattr(perms, need[0]):
            note += f'\n⚠️ I need **{need[1]}** to carry out the punishment.'
        await interaction.response.send_message(f'🛡️ Anti-nuke is **ON**. Punishment: **{punishment.name}**. Sensitivity: **{level}** ({NUKE_LIMITS[level]} of the same action in {NUKE_WINDOW}s).{note}', ephemeral=True)

    @app_commands.command(name='disable', description='Turn anti-nuke off (server owner only)')
    @owner_only()
    async def disable(self, interaction: discord.Interaction):
        self.cfg()['enabled'] = False
        self.bot.save_data()
        self.events.clear()
        await interaction.response.send_message('🛡️ Anti-nuke is now **OFF**.', ephemeral=True)

    @app_commands.command(name='status', description='See the anti-nuke settings (server owner only)')
    @owner_only()
    async def status(self, interaction: discord.Interaction):
        c = self.cfg()
        perms = interaction.guild.me.guild_permissions
        e = discord.Embed(title='🛡️ Anti-nuke', color=discord.Color.green() if c['enabled'] else discord.Color.red())
        e.add_field(name='Status', value='ON' if c['enabled'] else 'OFF')
        e.add_field(name='Punishment', value=NUKE_PUNISHMENTS[c['punishment']])
        e.add_field(name='Sensitivity', value=f"{c['level'].title()} ({NUKE_LIMITS[c['level']]} in {NUKE_WINDOW}s)")
        e.add_field(name='Watching for', inline=False, value='• ' + '\n• '.join(NUKE_ACTIONS.values()) + '\n• unapproved bots being added')
        e.add_field(name='Trusted (ignored)', inline=False, value=', '.join((f'<@{u}>' for u in c['whitelist'])) or 'Nobody (you, the owner, are always trusted)')
        e.add_field(name='My permissions', inline=False, value=f"View Audit Log: {('✅' if perms.view_audit_log else '❌')}  Ban: {('✅' if perms.ban_members else '❌')}  Kick: {('✅' if perms.kick_members else '❌')}  Manage Roles: {('✅' if perms.manage_roles else '❌')}")
        await interaction.response.send_message(embed=e, ephemeral=True)

    @app_commands.command(name='whitelist', description='Trust a member or bot so anti-nuke ignores them')
    @owner_only()
    async def whitelist(self, interaction: discord.Interaction, member: discord.Member):
        wl = self.cfg()['whitelist']
        if member.id not in wl:
            wl.append(member.id)
            self.bot.save_data()
        await interaction.response.send_message(f'✅ {member.mention} is now trusted.', ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name='unwhitelist', description='Stop trusting a member or bot')
    @owner_only()
    async def unwhitelist(self, interaction: discord.Interaction, member: discord.Member):
        wl = self.cfg()['whitelist']
        if member.id in wl:
            wl.remove(member.id)
            self.bot.save_data()
        await interaction.response.send_message(f'✅ {member.mention} is no longer trusted.', ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry):
        c = self.cfg()
        if not c['enabled']:
            return
        guild = entry.guild
        uid = entry.user_id
        if uid is None or uid == guild.owner_id or uid == self.bot.user.id or (uid in c['whitelist']):
            return
        now = time.monotonic()
        if now - self.handled.get((guild.id, uid), -999) < 30:
            return
        target = getattr(entry.target, 'id', None) or getattr(entry, 'target_id', None)
        if entry.action == discord.AuditLogAction.bot_add:
            if target is not None and target not in c['whitelist']:
                await self.block_bot(guild, uid, target)
            return
        what = NUKE_ACTIONS.get(entry.action)
        if what is None:
            return
        dq = self.events[guild.id, uid, entry.action]
        dq.append((now, target))
        while dq and now - dq[0][0] > NUKE_WINDOW:
            dq.popleft()
        if len(dq) >= NUKE_LIMITS[c['level']]:
            await self.punish(guild, uid, what, len(dq))

    async def block_bot(self, guild, adder_id, bot_id):
        try:
            await guild.kick(discord.Object(id=bot_id), reason='Anti-nuke: bot added by an untrusted user')
            result = 'The bot was removed.'
        except discord.HTTPException:
            result = 'I could NOT remove it (check my permissions and role position).'
        await self.alert(guild, '🤖 Unapproved bot added', adder_id, f'<@{adder_id}> added <@{bot_id}>. {result}\nUse `/antinuke whitelist` on a bot to allow it.')

    async def punish(self, guild, uid, what, count):
        c = self.cfg()
        self.handled[guild.id, uid] = time.monotonic()
        banned = [t for _, t in self.events.get((guild.id, uid, discord.AuditLogAction.ban), []) if t]
        for key in [k for k in self.events if k[0] == guild.id and k[1] == uid]:
            del self.events[key]
        member = guild.get_member(uid)
        if member is None:
            try:
                member = await guild.fetch_member(uid)
            except discord.HTTPException:
                member = None
        mode = c['punishment']
        if mode == 'strip' and (member is None or member.bot):
            mode = 'kick'
        reason = f'Anti-nuke: {what}'
        try:
            if mode == 'ban':
                await guild.ban(discord.Object(id=uid), reason=reason, delete_message_seconds=0)
            elif mode == 'kick':
                await guild.kick(discord.Object(id=uid), reason=reason)
            else:
                keep = [r for r in member.roles if r.is_default() or r.managed or r >= guild.me.top_role]
                await member.edit(roles=keep, reason=reason)
            done = {'ban': 'banned', 'kick': 'kicked', 'strip': 'stripped of all roles'}[mode]
        except discord.HTTPException:
            done = None
        undone = 0
        for tid in banned:
            try:
                await guild.unban(discord.Object(id=tid), reason='Anti-nuke: undoing mass ban')
                undone += 1
            except discord.HTTPException:
                pass
        detail = f'<@{uid}> did {count}+ actions of **{what}** within {NUKE_WINDOW} seconds.\n'
        detail += f'They were **{done}**.' if done else 'I could **NOT** punish them. Check my permissions and move my role higher.'
        if undone:
            detail += f'\nUnbanned {undone} member(s) they banned.'
        detail += "\nDeleted channels and roles can't be restored automatically."
        await self.alert(guild, '🚨 Anti-nuke triggered', uid, detail)

    async def alert(self, guild, title, uid, detail):
        member = guild.get_member(uid)
        await self.bot.mod_log(guild, title, guild.me, member or f'<@{uid}> (`{uid}`)', detail, discord.Color.red())
        try:
            owner = guild.owner or await guild.fetch_member(guild.owner_id)
            await owner.send(f'**{title}** in **{guild.name}**\n{detail}')
        except (discord.HTTPException, AttributeError):
            pass
GW_MIN, GW_MAX = (10, 30 * 86400)
GW_KEEP = 7 * 86400
GW_COLOR = 15844367
GW_ENDED_COLOR = 9807270

def parse_duration(text):
    """'30m', '2h', '1d12h', '2h 30m' -> seconds. Returns 0 if it can't be read."""
    units = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400, 'w': 604800}
    text = text.lower()
    if re.sub('\\d+\\s*[smhdw]|\\s', '', text):
        return 0
    return sum((int(n) * units[u] for n, u in re.findall('(\\d+)\\s*([smhdw])', text)))

def giveaway_embed(gw, guild):
    end = int(gw['end'])
    e = discord.Embed(title=f"🎉 {gw['prize']}", color=GW_ENDED_COLOR if gw['ended'] else GW_COLOR)
    lines = [gw['desc'], ''] if gw.get('desc') else []
    if gw['ended']:
        winners = ' '.join((f'<@{w}>' for w in gw['winner_ids'])) or 'No valid entries'
        lines += ['**This giveaway has ended.**', '', f'**Winners:** {winners}', f"**Hosted by:** <@{gw['host']}>", f"**Entries:** {len(gw['entries'])}", f'**Ended:** <t:{end}:R>']
        e.set_footer(text='Giveaway ended')
    else:
        lines += ['Click the **🎉 Enter** button below to join!', '', f'**Ends:** <t:{end}:R> (<t:{end}:f>)', f"**Hosted by:** <@{gw['host']}>", f"**Winners:** {gw['winners']}", f"**Entries:** {len(gw['entries'])}"]
        if gw.get('role'):
            lines.append(f"**Requires:** <@&{gw['role']}>")
        e.set_footer(text=f"{gw['winners']} winner{('s' if gw['winners'] != 1 else '')} • Ends at")
        e.timestamp = datetime.fromtimestamp(gw['end'], tz=timezone.utc)
    e.description = '\n'.join(lines)
    if guild is not None and guild.icon:
        e.set_thumbnail(url=guild.icon.url)
    return e

def host_only():

    async def predicate(interaction: discord.Interaction):
        return interaction.guild is not None and (interaction.client.is_staff(interaction.user) or interaction.user.guild_permissions.manage_guild)
    return app_commands.check(predicate)

class LeaveView(discord.ui.View):

    def __init__(self, cog, mid):
        super().__init__(timeout=60)
        self.cog, self.mid = (cog, mid)

    @discord.ui.button(label='Leave giveaway', style=discord.ButtonStyle.danger)
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        gw = self.cog.gws().get(self.mid)
        if gw and (not gw['ended']) and (interaction.user.id in gw['entries']):
            gw['entries'].remove(interaction.user.id)
            self.cog.bot.save_data()
            self.cog.dirty.add(self.mid)
            text = 'You left the giveaway.'
        else:
            text = 'This giveaway has already ended.'
        await interaction.response.edit_message(content=text, view=None)

class GiveawayView(discord.ui.View):

    def __init__(self, ended=False):
        super().__init__(timeout=None)
        self.enter.disabled = ended

    @discord.ui.button(label='Enter', emoji='🎉', style=discord.ButtonStyle.primary, custom_id='giveaway:enter')
    async def enter(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog('giveaway')
        mid = str(interaction.message.id)
        gw = cog.gws().get(mid)
        if gw is None or gw['ended'] or gw['end'] <= time.time():
            return await interaction.response.send_message('This giveaway has ended.', ephemeral=True)
        if gw.get('role') and (not any((r.id == gw['role'] for r in interaction.user.roles))):
            return await interaction.response.send_message(f"You need <@&{gw['role']}> to enter this giveaway.", ephemeral=True)
        uid = interaction.user.id
        if uid in gw['entries']:
            return await interaction.response.send_message("You're already entered in this giveaway. 🎉", view=LeaveView(cog, mid), ephemeral=True)
        gw['entries'].append(uid)
        cog.bot.save_data()
        cog.dirty.add(mid)
        await interaction.response.send_message(f"✅ You've entered the giveaway for **{gw['prize']}**. Good luck! 🍀", ephemeral=True)

class Giveaway(commands.GroupCog, name='giveaway'):

    def __init__(self, bot):
        super().__init__()
        self.bot = bot
        self.dirty = set()

    def gws(self):
        return self.bot.data.setdefault('giveaways', {})

    async def cog_load(self):
        self.bot.add_view(GiveawayView())
        self.tick.start()

    async def cog_unload(self):
        self.tick.cancel()

    @app_commands.command(name='start', description='Start a giveaway')
    @app_commands.describe(prize="What you're giving away", duration='How long it runs, e.g. 30m, 2h, 1d or 1d12h', winners='How many winners (default 1)', channel='Where to post it (default: this channel)', required_role='Only members with this role can enter', description='Extra text to show in the giveaway')
    @host_only()
    async def gw_start(self, interaction: discord.Interaction, prize: app_commands.Range[str, 1, 200], duration: str, winners: app_commands.Range[int, 1, 20]=1, channel: Optional[discord.TextChannel]=None, required_role: Optional[discord.Role]=None, description: Optional[app_commands.Range[str, 1, 500]]=None):
        secs = parse_duration(duration)
        if not GW_MIN <= secs <= GW_MAX:
            return await interaction.response.send_message('Duration must be between 10 seconds and 30 days, like `30m`, `2h`, `1d` or `1d12h`.', ephemeral=True)
        ch = channel or interaction.channel
        await interaction.response.defer(ephemeral=True)
        gw = {'guild_id': interaction.guild.id, 'channel_id': ch.id, 'prize': prize, 'host': interaction.user.id, 'winners': winners, 'end': time.time() + secs, 'entries': [], 'role': required_role.id if required_role else None, 'desc': description, 'ended': False, 'winner_ids': []}
        try:
            msg = await ch.send(embed=giveaway_embed(gw, interaction.guild), view=GiveawayView())
        except discord.HTTPException:
            return await interaction.followup.send(f"I can't post in {ch.mention}. Give me **Send Messages** and **Embed Links** there.", ephemeral=True)
        self.gws()[str(msg.id)] = gw
        self.bot.save_data()
        await interaction.followup.send(f'🎉 Giveaway started in {ch.mention}!', ephemeral=True)

    @app_commands.command(name='end', description='End a giveaway now and pick the winners')
    @app_commands.describe(giveaway='Pick the giveaway')
    @host_only()
    async def gw_end(self, interaction: discord.Interaction, giveaway: str):
        gw = self.lookup(interaction, giveaway)
        if gw is None:
            return await interaction.response.send_message("I couldn't find that giveaway.", ephemeral=True)
        if gw['ended']:
            return await interaction.response.send_message('That giveaway already ended. Use `/giveaway reroll` to pick new winners.', ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        await self.finish(giveaway)
        await interaction.followup.send('✅ Giveaway ended.', ephemeral=True)

    @app_commands.command(name='reroll', description='Pick new winners for an ended giveaway')
    @app_commands.describe(giveaway='Pick the giveaway', winners='How many new winners (default 1)')
    @host_only()
    async def gw_reroll(self, interaction: discord.Interaction, giveaway: str, winners: app_commands.Range[int, 1, 20]=1):
        gw = self.lookup(interaction, giveaway)
        if gw is None:
            return await interaction.response.send_message("I couldn't find that giveaway.", ephemeral=True)
        if not gw['ended']:
            return await interaction.response.send_message('That giveaway is still running. End it first.', ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        new = await self.pick(guild, gw, winners, gw['winner_ids'])
        if not new:
            return await interaction.followup.send('There are no more valid entries to pick from.', ephemeral=True)
        gw['winner_ids'] = gw['winner_ids'] + new
        self.bot.save_data()
        channel = guild.get_channel(gw['channel_id'])
        if channel:
            try:
                await channel.get_partial_message(int(giveaway)).edit(embed=giveaway_embed(gw, guild))
            except discord.HTTPException:
                pass
            await self.announce(channel, gw, giveaway, new, reroll=True)
        await interaction.followup.send('✅ Rerolled.', ephemeral=True)

    @app_commands.command(name='cancel', description='Cancel a giveaway without picking winners')
    @app_commands.describe(giveaway='Pick the giveaway')
    @host_only()
    async def gw_cancel(self, interaction: discord.Interaction, giveaway: str):
        gw = self.lookup(interaction, giveaway)
        if gw is None:
            return await interaction.response.send_message("I couldn't find that giveaway.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        channel = interaction.guild.get_channel(gw['channel_id'])
        if channel:
            try:
                await channel.get_partial_message(int(giveaway)).delete()
            except discord.HTTPException:
                pass
        self.gws().pop(giveaway, None)
        self.dirty.discard(giveaway)
        self.bot.save_data()
        await interaction.followup.send('🗑️ Giveaway cancelled and removed.', ephemeral=True)

    @app_commands.command(name='list', description='Show the giveaways that are running')
    @host_only()
    async def gw_list(self, interaction: discord.Interaction):
        rows = [(mid, gw) for mid, gw in self.gws().items() if gw['guild_id'] == interaction.guild_id and (not gw['ended'])]
        if not rows:
            return await interaction.response.send_message('No giveaways are running.', ephemeral=True)
        rows.sort(key=lambda r: r[1]['end'])
        lines = [f"**{gw['prize'][:60]}** • ends <t:{int(gw['end'])}:R> • {len(gw['entries'])} entries • [jump](https://discord.com/channels/{gw['guild_id']}/{gw['channel_id']}/{mid})" for mid, gw in rows[:20]]
        await interaction.response.send_message(embed=discord.Embed(title='🎉 Active giveaways', description='\n'.join(lines), color=GW_COLOR), ephemeral=True)

    @gw_end.autocomplete('giveaway')
    @gw_reroll.autocomplete('giveaway')
    @gw_cancel.autocomplete('giveaway')
    async def giveaway_choices(self, interaction: discord.Interaction, current: str):
        out = []
        for mid, gw in sorted(self.gws().items(), key=lambda r: -r[1]['end']):
            if gw['guild_id'] == interaction.guild_id and current.lower() in gw['prize'].lower():
                tag = 'ended' if gw['ended'] else 'running'
                out.append(app_commands.Choice(name=f"{gw['prize'][:70]} ({tag})", value=mid))
        return out[:25]

    def lookup(self, interaction, mid):
        gw = self.gws().get(mid)
        return gw if gw and gw['guild_id'] == interaction.guild_id else None

    async def pick(self, guild, gw, count, exclude):
        pool = [u for u in gw['entries'] if u not in exclude]
        random.shuffle(pool)
        role = guild.get_role(gw['role']) if gw.get('role') else None
        chosen = []
        for uid in pool:
            if len(chosen) >= count:
                break
            member = guild.get_member(uid)
            if member is None:
                try:
                    member = await guild.fetch_member(uid)
                except discord.HTTPException:
                    continue
            if member.bot or (role and role not in member.roles):
                continue
            chosen.append(uid)
        return chosen

    async def finish(self, mid):
        gw = self.gws().get(mid)
        if gw is None or gw['ended']:
            return
        gw['ended'] = True
        gw['end'] = min(gw['end'], time.time())
        self.dirty.discard(mid)
        guild = self.bot.get_guild(gw['guild_id'])
        gw['winner_ids'] = await self.pick(guild, gw, gw['winners'], []) if guild else []
        self.bot.save_data()
        channel = guild.get_channel(gw['channel_id']) if guild else None
        if channel is None:
            return
        try:
            await channel.get_partial_message(int(mid)).edit(embed=giveaway_embed(gw, guild), view=GiveawayView(ended=True))
        except discord.HTTPException:
            pass
        await self.announce(channel, gw, mid, gw['winner_ids'])

    async def announce(self, channel, gw, mid, winners, reroll=False):
        jump = f"https://discord.com/channels/{gw['guild_id']}/{gw['channel_id']}/{mid}"
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label='Jump to giveaway', url=jump))
        if winners:
            mentions = ' '.join((f'<@{w}>' for w in winners))
            text = f"🔄 New winner{('s' if len(winners) > 1 else '')}: {mentions}! You won **{gw['prize']}**!" if reroll else f"🎉 Congratulations {mentions}! You won **{gw['prize']}**!"
        else:
            text = f"😢 There were no valid entries, so nobody won **{gw['prize']}**."
        try:
            await channel.send(text, view=view, allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=w) for w in winners]))
        except discord.HTTPException:
            pass
        for w in winners:
            try:
                user = await self.bot.fetch_user(w)
                await user.send(f"🎉 You won **{gw['prize']}** in **{channel.guild.name}**!\n{jump}")
            except discord.HTTPException:
                pass

    @tasks.loop(seconds=5)
    async def tick(self):
        now = time.time()
        for mid, gw in list(self.gws().items()):
            try:
                if not gw['ended'] and gw['end'] <= now:
                    await self.finish(mid)
                elif gw['ended'] and now - gw['end'] > GW_KEEP:
                    self.gws().pop(mid, None)
                    self.bot.save_data()
            except Exception as e:
                print(f'Giveaway error ({mid}): {e}')
        for mid in list(self.dirty):
            self.dirty.discard(mid)
            gw = self.gws().get(mid)
            if not gw or gw['ended']:
                continue
            try:
                guild = self.bot.get_guild(gw['guild_id'])
                channel = guild.get_channel(gw['channel_id']) if guild else None
                if channel:
                    await channel.get_partial_message(int(mid)).edit(embed=giveaway_embed(gw, guild))
            except Exception as e:
                print(f'Giveaway refresh error ({mid}): {e}')

    @tick.before_loop
    async def before_tick(self):
        await self.bot.wait_until_ready()
APP_COLOR = 15844367
APP_COOLDOWN = 7 * 86400
APP_QUESTIONS = [('Age & Date of Birth', 'Example: 19 - 04/12/2006'), ('Timezone & availability', 'Your timezone, which days you can be active, and about how many hours per day.'), ('Have you ever been staff for ANY OTHER cheating service?', 'If yes: which ones, your role, and why you left. If no, just say "No".'), ('Have you ever cheated with any of our cheats/products?', 'Be honest. If yes, which product(s) and roughly when.'), ('Do you have any previous reseller, support or moderation experience?', 'Where, what you did, and for how long.'), ('Have you ever been banned from, scammed, leaked from, or caused a chargeback with a cheat provider or community?', 'If yes, explain what happened.'), ('Why do you want to join our team?', 'A few sentences is fine.'), ('Why should we pick you over other applicants?', 'What makes you a good fit?'), ('A customer says one of our products got them banned and demands a refund. What do you do?', "Walk us through how you'd handle it."), ('A customer asks you to "go outside the ticket" or pay a way we don\'t accept. How do you respond?', 'What would you say or do?'), ('You notice another staff member breaking rules or leaking information. What do you do?', 'Be specific.'), ('Do you understand payment methods like crypto, PayPal and CashApp, and the risks of scams and chargebacks?', 'Explain what you know and any experience you have.'), ('Are you comfortable keeping all customer and company information private?', "Tell us how you'd handle sensitive information."), ('Anything else we should know?', 'Optional. Type "none" to skip.')]
APP_QUICK = {0: 'Age & DOB', 2: 'Staff for another cheating service?', 3: 'Cheated with our products?'}
APP_PANEL_DESC = "Want to join the **Frontline** team? Click **Apply** below and I'll send you the application in your DMs.\n\n**Before you apply**\n• Your DMs must be open\n• Be honest. We check everything\n• Be active, professional and mature\n\n**How it works**\n1️⃣ Click **Apply**\n2️⃣ Answer the questions in your DMs\n3️⃣ Our team reviews it and DMs you the result"
VERIFY_DESC = 'Welcome to **Frontline**! 👋\n\nClick the button below to verify yourself and unlock the rest of the server.'
BOOSTER_DISCOUNT = '20%'
BOOSTER_PERKS = [f'💸 **{BOOSTER_DISCOUNT} off all orders**', '⚡ **Priority support** in your tickets', '🎨 An exclusive **Booster** role', '🎁 **Early access** to new products and restocks', '🎉 Access to **booster-only giveaways**']

def manager_only():

    async def predicate(interaction: discord.Interaction):
        return interaction.guild is not None and interaction.user.guild_permissions.manage_guild
    return app_commands.check(predicate)

def answer_embeds(answers):
    """Split the answers into embeds that stay under Discord's 6000-character limit."""
    embeds, cur, size = ([], discord.Embed(color=2829617), 0)
    for i, (q, _) in enumerate(APP_QUESTIONS):
        name, value = (f'{i + 1}. {q}'[:256], answers[i])
        if size + len(name) + len(value) > 5000 or len(cur.fields) >= 20:
            embeds.append(cur)
            cur, size = (discord.Embed(color=2829617), 0)
        cur.add_field(name=name, value=value, inline=False)
        size += len(name) + len(value)
    embeds.append(cur)
    return embeds

class ConfirmView(discord.ui.View):

    def __init__(self, user_id):
        super().__init__(timeout=300)
        self.user_id, self.value = (user_id, None)

    async def interaction_check(self, interaction: discord.Interaction):
        return interaction.user.id == self.user_id

    @discord.ui.button(label='Submit application', emoji='✅', style=discord.ButtonStyle.success)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = True
        await interaction.response.edit_message(view=None)
        self.stop()

    @discord.ui.button(label='Cancel', style=discord.ButtonStyle.danger)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = False
        await interaction.response.edit_message(view=None)
        self.stop()

class ApplyView(discord.ui.View):

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label='Apply', emoji='📝', style=discord.ButtonStyle.success, custom_id='apps:apply')
    async def apply(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.client.get_cog('staffapps').begin(interaction)

class DecisionModal(discord.ui.Modal):
    note = discord.ui.TextInput(label='Note to the applicant (optional)', style=discord.TextStyle.paragraph, required=False, max_length=500)

    def __init__(self, cog, message, accept):
        super().__init__(title='Accept application' if accept else 'Deny application')
        self.cog, self.message, self.accept = (cog, message, accept)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.decide(interaction, self.message, self.accept, self.note.value)

class AppReviewView(discord.ui.View):

    def __init__(self):
        super().__init__(timeout=None)

    async def start(self, interaction, accept):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message('Only members with **Manage Server** can review applications.', ephemeral=True)
        cog = interaction.client.get_cog('staffapps')
        app = cog.cfg()['list'].get(str(interaction.message.id))
        if app is None or app['status'] != 'pending':
            return await interaction.response.send_message('That application was already handled.', ephemeral=True)
        await interaction.response.send_modal(DecisionModal(cog, interaction.message, accept))

    @discord.ui.button(label='Accept', emoji='✅', style=discord.ButtonStyle.success, custom_id='apps:accept')
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.start(interaction, True)

    @discord.ui.button(label='Deny', emoji='❌', style=discord.ButtonStyle.danger, custom_id='apps:deny')
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.start(interaction, False)

class StaffApps(commands.GroupCog, name='staffapps'):

    def __init__(self, bot):
        super().__init__()
        self.bot = bot
        self.active = set()
        self.jobs = set()

    def cfg(self):
        c = self.bot.data.setdefault('apps', {})
        for k, v in (('channel', 0), ('role', 0), ('open', True), ('next', 1)):
            c.setdefault(k, v)
        c.setdefault('list', {})
        return c

    async def cog_load(self):
        self.bot.add_view(ApplyView())
        self.bot.add_view(AppReviewView())

    @app_commands.command(name='setup', description='Choose where applications are sent for review')
    @app_commands.describe(channel='Channel where finished applications are posted', role='Role to give automatically when you accept someone (optional)')
    @manager_only()
    async def setup_apps(self, interaction: discord.Interaction, channel: discord.TextChannel, role: Optional[discord.Role]=None):
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            return await interaction.response.send_message(f'I need **View Channel**, **Send Messages** and **Embed Links** in {channel.mention}.', ephemeral=True)
        c = self.cfg()
        c['channel'] = channel.id
        c['role'] = role.id if role else 0
        self.bot.save_data()
        await interaction.response.send_message(f'✅ Applications will be posted in {channel.mention}.' + (f' Accepted applicants get {role.mention}.' if role else ''), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name='panel', description='Post the staff application panel here')
    @manager_only()
    async def panel(self, interaction: discord.Interaction):
        e = discord.Embed(title='📝 Staff Applications', description=APP_PANEL_DESC, color=APP_COLOR)
        if interaction.guild.icon:
            e.set_thumbnail(url=interaction.guild.icon.url)
        e.set_footer(text='Frontline • Staff Team')
        await interaction.channel.send(embed=e, view=ApplyView())
        note = '' if self.cfg()['channel'] else "\n⚠️ You haven't chosen a review channel yet. Run `/staffapps setup`."
        await interaction.response.send_message('Panel posted.' + note, ephemeral=True)

    @app_commands.command(name='toggle', description='Open or close staff applications')
    @app_commands.choices(state=[app_commands.Choice(name='Open', value='open'), app_commands.Choice(name='Closed', value='closed')])
    @manager_only()
    async def toggle(self, interaction: discord.Interaction, state: app_commands.Choice[str]):
        self.cfg()['open'] = state.value == 'open'
        self.bot.save_data()
        await interaction.response.send_message(f'Applications are now **{state.name.upper()}**.', ephemeral=True)

    @app_commands.command(name='pending', description='List applications waiting for review')
    @manager_only()
    async def pending(self, interaction: discord.Interaction):
        c = self.cfg()
        rows = [(mid, a) for mid, a in c['list'].items() if a['status'] == 'pending']
        if not rows:
            return await interaction.response.send_message('No applications are waiting.', ephemeral=True)
        lines = [f"**#{a['n']}** <@{a['user']}> • <t:{int(a['ts'])}:R> • [open](https://discord.com/channels/{interaction.guild.id}/{c['channel']}/{mid})" for mid, a in rows[:20]]
        await interaction.response.send_message(embed=discord.Embed(title='📝 Pending applications', description='\n'.join(lines), color=APP_COLOR), ephemeral=True)

    async def begin(self, interaction):
        c, user = (self.cfg(), interaction.user)
        guild = interaction.guild
        if not c['open']:
            return await interaction.response.send_message('Staff applications are **closed** right now. Check back later!', ephemeral=True)
        if not c['channel'] or guild.get_channel(c['channel']) is None:
            return await interaction.response.send_message("Applications aren't set up yet. Please tell an admin.", ephemeral=True)
        if user.id in self.active:
            return await interaction.response.send_message("You're already filling in an application. Check your DMs!", ephemeral=True)
        for app in c['list'].values():
            if app['user'] != user.id:
                continue
            if app['status'] == 'pending':
                return await interaction.response.send_message('You already have an application waiting for review.', ephemeral=True)
            if app['status'] == 'accepted':
                return await interaction.response.send_message('Your application was already accepted. 🎉', ephemeral=True)
            if app['status'] == 'denied' and time.time() - app['ts'] < APP_COOLDOWN:
                return await interaction.response.send_message(f"You can apply again <t:{int(app['ts'] + APP_COOLDOWN)}:R>.", ephemeral=True)
        intro = discord.Embed(title='📝 Frontline Staff Application', description=f"Thanks for applying! There are **{len(APP_QUESTIONS)} questions**.\n\n• Reply to each question **in this chat**\n• Max **1000 characters** per answer\n• You have 15 minutes per question\n• Type `cancel` at any time to stop\n\nBe honest. We verify everything. Let's begin!", color=APP_COLOR)
        try:
            await user.send(embed=intro)
        except discord.HTTPException:
            return await interaction.response.send_message("❌ I couldn't DM you. Turn on **Direct Messages** for this server (server name → Privacy Settings), then click Apply again.", ephemeral=True)
        await interaction.response.send_message("📬 Check your DMs. I've sent you the application!", ephemeral=True)
        self.active.add(user.id)
        job = asyncio.create_task(self.run(user, guild))
        self.jobs.add(job)
        job.add_done_callback(self.jobs.discard)

    async def run(self, user, guild):
        total = len(APP_QUESTIONS)
        try:
            answers = []
            for i, (question, hint) in enumerate(APP_QUESTIONS, 1):
                await user.send(embed=discord.Embed(title=f'Question {i}/{total}', description=f'**{question}**\n\n*{hint}*', color=APP_COLOR))
                while True:
                    msg = await self.bot.wait_for('message', timeout=900, check=lambda m: m.author.id == user.id and isinstance(m.channel, discord.DMChannel))
                    text = msg.content.strip()
                    if text.lower() == 'cancel':
                        return await user.send('❌ Application cancelled. You can apply again any time.')
                    if not text:
                        await user.send('Please answer with text.')
                    elif len(text) > 1000:
                        await user.send(f'That answer is {len(text)} characters. Please shorten it to 1000 or less and send it again.')
                    else:
                        answers.append(text)
                        break
            view = ConfirmView(user.id)
            await user.send(embed=discord.Embed(title='✅ All done!', description='Submit your application, or cancel to throw it away.', color=discord.Color.green()), view=view)
            await view.wait()
            if not view.value:
                return await user.send('❌ Application cancelled. You can apply again any time.')
            await self.submit(user, guild, answers)
            await user.send(embed=discord.Embed(title='📨 Application sent!', description='Our team will review it and DM you with the result.', color=discord.Color.green()))
        except asyncio.TimeoutError:
            try:
                await user.send("⌛ Your application timed out. Click **Apply** again whenever you're ready.")
            except discord.HTTPException:
                pass
        except discord.HTTPException:
            pass
        finally:
            self.active.discard(user.id)

    async def submit(self, user, guild, answers):
        c = self.cfg()
        channel = guild.get_channel(c['channel'])
        n = c['next']
        c['next'] += 1
        member = guild.get_member(user.id)
        if member is None:
            try:
                member = await guild.fetch_member(user.id)
            except discord.HTTPException:
                member = None
        e = discord.Embed(title=f'📝 Staff Application #{n}', color=APP_COLOR, timestamp=discord.utils.utcnow())
        e.set_author(name=str(user), icon_url=user.display_avatar.url)
        e.set_thumbnail(url=user.display_avatar.url)
        e.add_field(name='Applicant', value=f'{user.mention}\n`{user.id}`')
        e.add_field(name='Account created', value=f'<t:{int(user.created_at.timestamp())}:R>')
        if member is not None and member.joined_at:
            e.add_field(name='Joined server', value=f'<t:{int(member.joined_at.timestamp())}:R>')
        for i, label in APP_QUICK.items():
            e.add_field(name=label, value=answers[i][:300], inline=False)
        e.set_footer(text='Status: Pending • full answers are in the thread below')
        msg = await channel.send(embed=e, view=AppReviewView())
        c['list'][str(msg.id)] = {'user': user.id, 'n': n, 'status': 'pending', 'ts': time.time()}
        self.bot.save_data()
        try:
            target = await msg.create_thread(name=f'Application #{n} - {user.name}'[:100], auto_archive_duration=1440)
        except discord.HTTPException:
            target = channel
        for emb in answer_embeds(answers):
            await target.send(embed=emb)

    async def decide(self, interaction, message, accept, note):
        await interaction.response.defer(ephemeral=True)
        c = self.cfg()
        app = c['list'].get(str(message.id))
        if app is None or app['status'] != 'pending':
            return await interaction.followup.send('That application was already handled.', ephemeral=True)
        app['status'] = 'accepted' if accept else 'denied'
        app['ts'] = time.time()
        self.bot.save_data()
        word = 'Accepted' if accept else 'Denied'
        emb = message.embeds[0].copy()
        emb.color = discord.Color.green() if accept else discord.Color.red()
        emb.add_field(name=f"{('✅' if accept else '❌')} {word}", value=f'By {interaction.user.mention}' + (f'\n{note}' if note else ''), inline=False)
        emb.set_footer(text=f'Status: {word}')
        try:
            await message.edit(embed=emb, view=None)
        except discord.HTTPException:
            pass
        guild = interaction.guild
        member = guild.get_member(app['user'])
        if member is None:
            try:
                member = await guild.fetch_member(app['user'])
            except discord.HTTPException:
                member = None
        role_note = ''
        if accept and c['role'] and (member is not None):
            role = guild.get_role(c['role'])
            try:
                if role:
                    await member.add_roles(role, reason=f"Staff application #{app['n']} accepted")
            except discord.HTTPException:
                role_note = " (I couldn't give them the role. Check my permissions and role position.)"
        try:
            user = member or await self.bot.fetch_user(app['user'])
            dm = discord.Embed(title='🎉 Your staff application was accepted!' if accept else "Your staff application wasn't successful", description=(note + '\n\n' if note else '') + ('Welcome to the team! A staff member will be in touch with next steps.' if accept else 'Thank you for applying. You can apply again in 7 days.'), color=discord.Color.green() if accept else discord.Color.red())
            await user.send(embed=dm)
            dm_note = ''
        except discord.HTTPException:
            dm_note = " (Their DMs are closed, so I couldn't message them.)"
        await interaction.followup.send(f"{word} application #{app['n']}.{role_note}{dm_note}", ephemeral=True)

class VerifyView(discord.ui.View):

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label='Verify', emoji='✅', style=discord.ButtonStyle.success, custom_id='verify:button')
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = interaction.client.data.get('verify', {})
        guild = interaction.guild
        role = guild.get_role(cfg.get('role', 0))
        if role is None:
            return await interaction.response.send_message("Verification isn't set up yet. Please tell an admin.", ephemeral=True)
        if role in interaction.user.roles:
            return await interaction.response.send_message("You're already verified. ✅", ephemeral=True)
        try:
            await interaction.user.add_roles(role, reason='Verified')
            remove = guild.get_role(cfg.get('remove', 0))
            if remove and remove in interaction.user.roles:
                await interaction.user.remove_roles(remove, reason='Verified')
        except discord.HTTPException:
            return await interaction.response.send_message("I couldn't give you the role. Please tell an admin.", ephemeral=True)
        await interaction.response.send_message(f"✅ You're verified! Welcome to **{guild.name}**.", ephemeral=True)

class BoosterView(discord.ui.View):

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label='Claim Perks', emoji='💎', style=discord.ButtonStyle.primary, custom_id='booster:claim')
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.premium_since is None:
            return await interaction.response.send_message("💎 You're not boosting this server yet. Boost it to unlock these perks!", ephemeral=True)
        text = f'💎 Thank you for boosting! To use your **{BOOSTER_DISCOUNT} discount**, open a **Purchase** ticket and our team will apply it to your order.'
        role = interaction.guild.get_role(interaction.client.data.get('booster', {}).get('role', 0))
        if role is not None and role not in interaction.user.roles:
            try:
                await interaction.user.add_roles(role, reason='Server booster perks')
                text += f'\nYou also got the {role.mention} role.'
            except discord.HTTPException:
                pass
        await interaction.response.send_message(text, ephemeral=True)

class Community(commands.Cog):

    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        self.bot.add_view(VerifyView())
        self.bot.add_view(BoosterView())

    @app_commands.command(name='verifypanel', description='Post the verification panel here (admin)')
    @app_commands.describe(role='Role members get when they verify', remove_role='Role to take away when they verify, like Unverified (optional)')
    @app_commands.checks.has_permissions(administrator=True)
    async def verifypanel(self, interaction: discord.Interaction, role: discord.Role, remove_role: Optional[discord.Role]=None):
        guild = interaction.guild
        if role.managed or role.is_default() or role >= guild.me.top_role:
            return await interaction.response.send_message(f"I can't hand out {role.mention}. Move my role **above** it in Server Settings → Roles.", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        self.bot.data['verify'] = {'role': role.id, 'remove': remove_role.id if remove_role else 0}
        self.bot.save_data()
        e = discord.Embed(title='✅ Verification', description=VERIFY_DESC, color=discord.Color.green())
        if guild.icon:
            e.set_thumbnail(url=guild.icon.url)
        e.set_footer(text=f'{guild.name} • Verification')
        await interaction.channel.send(embed=e, view=VerifyView())
        await interaction.response.send_message('Verification panel posted.', ephemeral=True)

    @app_commands.command(name='boosterpanel', description='Post the server booster perks panel here (admin)')
    @app_commands.describe(role='Role booster perks members get when they claim (optional)')
    @app_commands.checks.has_permissions(administrator=True)
    async def boosterpanel(self, interaction: discord.Interaction, role: Optional[discord.Role]=None):
        guild = interaction.guild
        if role is not None and (role.managed or role.is_default() or role >= guild.me.top_role):
            return await interaction.response.send_message(f"I can't hand out {role.mention}. Move my role **above** it in Server Settings → Roles.", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        self.bot.data['booster'] = {'role': role.id if role else 0}
        self.bot.save_data()
        e = discord.Embed(title='💎 Server Booster Perks', description=f'Boost **{guild.name}** and unlock exclusive rewards as our way of saying thank you!', color=16023551)
        e.add_field(name='🎁 Your perks', value='\n'.join(BOOSTER_PERKS), inline=False)
        e.add_field(name='📋 How to claim', value='1️⃣ Boost this server\n2️⃣ Click **Claim Perks** below\n3️⃣ Open a **Purchase** ticket. Staff will apply your discount', inline=False)
        if guild.icon:
            e.set_thumbnail(url=guild.icon.url)
        e.set_footer(text='Perks last as long as your boost is active')
        await interaction.channel.send(embed=e, view=BoosterView())
        await interaction.response.send_message('Booster panel posted.', ephemeral=True)
bot.run(os.getenv('DISCORD_TOKEN'))
