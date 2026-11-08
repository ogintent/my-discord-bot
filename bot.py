import os, io, json, copy, time, random, asyncio
from datetime import timedelta
from typing import Optional
from collections import defaultdict, deque
import discord
from discord import app_commands
from discord.ext import commands
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
    check = (lambda m: m.author.id == member.id) if member else (lambda m: True)
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
bot.run(os.getenv('DISCORD_TOKEN'))