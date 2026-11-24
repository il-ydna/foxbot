"""Discord bot: member availability, weekly schedule posts, confirmations and reminders."""
import asyncio
import logging
from collections import Counter
from datetime import date, datetime, timedelta

import discord
from discord import app_commands
from discord.ext import tasks

from . import config, db, mailer, service
from .parse import describe, parse_availability

log = logging.getLogger("foxbot")
GUILD = discord.Object(id=config.DISCORD_GUILD_ID)

STATUS_TEXT = {
    "proposed": "waiting for the runner to confirm",
    "emailed": "participant emailed, waiting for their reply",
    "confirmed": "confirmed by participant",
    "declined": "participant declined; needs follow-up by hand",
    "cancelled": "runner can't make it; slot released",
    "withdrawn": "withdrawn: the runner's availability changed",
}
# Header line and side-bar colour of the session card, per state.
CARD = {
    "proposed": ("🗓️  Proposed session", discord.Colour.gold()),
    "emailed": ("✉️  Participant emailed", discord.Colour.blue()),
    "confirmed": ("✅  Session confirmed", discord.Colour.green()),
    "declined": ("❌  Participant declined", discord.Colour.red()),
    "cancelled": ("⊗  Runner can't make it", discord.Colour.light_grey()),
    "withdrawn": ("⊗  Session unscheduled", discord.Colour.light_grey()),
}
BUTTONS = {
    "confirm": ("Confirm and email participant", discord.ButtonStyle.success),
    "cancel": ("I can't run this", discord.ButtonStyle.secondary),
    "pyes": ("Participant confirmed", discord.ButtonStyle.success),
    "pno": ("Participant declined", discord.ButtonStyle.danger),
}


def _duration(minutes: int) -> str:
    hours = minutes / 60
    return f"{hours:g} hour{'s' if hours != 1 else ''}" if minutes % 30 == 0 else f"{minutes} minutes"


def render(session: dict) -> tuple[str, discord.Embed, discord.ui.View]:
    """Message for a session in its current state: a line that pings the runner, a card, buttons."""
    start = datetime.fromisoformat(session["start_at"]).replace(tzinfo=config.TZ)
    end = datetime.fromisoformat(session["end_at"]).replace(tzinfo=config.TZ)
    header, colour = CARD[session["status"]]
    card = discord.Embed(title=f"{session['participant_name']} ({session['participant_email']})", colour=colour)
    card.set_author(name=header)
    # <t:...:F> is shown by Discord as a full date and time in each reader's own timezone.
    card.add_field(name="Scheduled for", value=f"<t:{int(start.timestamp())}:F>", inline=True)
    card.add_field(name="Duration", value=_duration(int((end - start).total_seconds() // 60)), inline=True)
    view = discord.ui.View(timeout=None)
    actions = {"proposed": ("confirm", "cancel"), "emailed": ("pyes", "pno")}.get(session["status"], ())
    for action in actions:
        view.add_item(SessionButton(action, session["id"]))
    return f"<@{session['member_id']}>", card, view


class SessionButton(discord.ui.DynamicItem[discord.ui.Button], template=r"foxbot:(?P<action>[a-z]+):(?P<sid>\d+)"):
    """Buttons carry the session id in their custom_id, so they keep working across restarts."""

    def __init__(self, action: str, sid: int):
        label, style = BUTTONS[action]
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=f"foxbot:{action}:{sid}"))
        self.action, self.sid = action, sid

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["action"], int(match["sid"]))

    async def callback(self, interaction: discord.Interaction):
        session = await asyncio.to_thread(db.session, self.sid)
        if session is None:
            return await interaction.response.send_message("That session no longer exists.", ephemeral=True)
        is_runner = interaction.user.id == session["member_id"]
        is_admin = getattr(interaction.user, "guild_permissions", None) and interaction.user.guild_permissions.manage_guild
        if not (is_runner or is_admin):
            return await interaction.response.send_message(
                f"Only {session['member_name']} can act on this session.", ephemeral=True)

        await interaction.response.defer()  # sending email can take longer than Discord's 3 seconds
        note = None
        try:
            if self.action == "confirm":
                result = await asyncio.to_thread(service.member_confirm, self.sid)
                if result is None:
                    note = "Someone already acted on this session."
                elif result["sent"]:
                    note = f"Emailed {session['participant_name']}. I'll post here when they reply."
                else:
                    note = ("**Dry run: no email was sent.** This is what would go to "
                            f"{session['participant_name']}:\n>>> **{result['subject']}**\n{result['body']}")
            elif self.action == "cancel":
                ok = await asyncio.to_thread(service.member_cancel, self.sid)
                note = ("Released. Run `/schedule propose` to try to place this participant with someone else."
                        if ok else "Someone already acted on this session.")
            elif self.action == "pyes":
                ok = await asyncio.to_thread(service.participant_confirm, self.sid)
                note = "Marked confirmed." if ok else "This session is not waiting on a participant reply."
            elif self.action == "pno":
                ok = await asyncio.to_thread(service.participant_decline, self.sid)
                note = ("Marked declined. They won't be auto-scheduled again until someone follows up."
                        if ok else "This session is not waiting on a participant reply.")
        except Exception as error:  # noqa: BLE001 - report any failure to the person who clicked
            log.exception("button %s failed for session %s", self.action, self.sid)
            note = f"That failed and nothing was changed: `{type(error).__name__}: {error}`"
        text, card, view = render(await asyncio.to_thread(db.session, self.sid))
        await interaction.edit_original_response(content=text, embed=card, view=view)
        if note:
            await interaction.followup.send(note[:2000], ephemeral=True)


availability = app_commands.Group(name="availability", description="When you can run participants")
schedule = app_commands.Group(name="schedule", description="Participant running schedule")


async def release_after_change(interaction: discord.Interaction) -> str:
    """Withdraw the caller's unconfirmed sessions that no longer fit their availability, update
    those messages, and return text to add to the reply."""
    withdrawn, contacted = await asyncio.to_thread(service.release_unavailable, interaction.user.id)
    for session in withdrawn:
        await interaction.client.refresh(session["id"])
    text = ""
    if withdrawn:
        times = ", ".join(service.short_when(s) for s in withdrawn)
        text += (f"\n\nUnscheduled {len(withdrawn)} session(s) you're no longer free for ({times}). "
                 "Run `/schedule propose` to place those participants again.")
    if contacted:
        lines = "\n".join(f"- {mailer.fmt_when(s['start_at'], s['end_at'])}: {s['participant_name']} "
                          f"({STATUS_TEXT[s['status']]})" for s in contacted)
        text += ("\n\n⚠️ These are outside your new availability but the participant has already been "
                 f"contacted, so I left them in place. Reschedule with them or find cover:\n{lines}")
    return text


@availability.command(name="set", description="Describe when you can run sessions, in plain words")
@app_commands.describe(when='e.g. "Mon and Wed 1-6pm, Fri mornings. Not available Dec 3."')
async def availability_set(interaction: discord.Interaction, when: str):
    await interaction.response.defer(ephemeral=True)
    today = service.now_local().date()
    parsed = await asyncio.to_thread(parse_availability, when, today)
    await asyncio.to_thread(db.set_member, interaction.user.id, interaction.user.display_name, when, parsed)
    reply = f"Saved. This replaces anything you set before.\n```\n{describe(parsed)}\n```"
    if parsed["notes"]:
        reply += f"\nNote: {parsed['notes']}"
    reply += "\nIf that's wrong, run `/availability set` again with clearer wording."
    reply += await release_after_change(interaction)
    await interaction.followup.send(reply[:2000], ephemeral=True)


@availability.command(name="show", description="Show the availability I have for you")
async def availability_show(interaction: discord.Interaction):
    member = await asyncio.to_thread(db.member, interaction.user.id)
    if not member or not member["availability"]:
        return await interaction.response.send_message("Nothing set. Use `/availability set`.", ephemeral=True)
    await interaction.response.send_message(
        f"You said: “{member['availability_raw']}”\n```\n{describe(member['availability'])}\n```", ephemeral=True)


@availability.command(name="clear", description="Remove your availability so you get no new sessions")
async def availability_clear(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await asyncio.to_thread(db.set_member, interaction.user.id, interaction.user.display_name, "", None)
    reply = "Cleared. You won't be given new sessions." + await release_after_change(interaction)
    await interaction.followup.send(reply[:2000], ephemeral=True)


@schedule.command(name="propose", description="Match waiting participants to members and post the sessions")
@app_commands.describe(week="Any date in the week to schedule, YYYY-MM-DD. Defaults to next week.")
async def schedule_propose(interaction: discord.Interaction, week: str | None = None):
    try:
        day = date.fromisoformat(week) if week else None
    except ValueError:
        return await interaction.response.send_message("Use a date like 2026-12-01.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    week_start = day - timedelta(days=day.weekday()) if day else service.next_week_start(service.now_local())
    result = await interaction.client.post_proposal(week_start)
    new, total = len(result["session_ids"]), result["total"]
    reply = f"Week of {week_start}: {new} new session(s), {total - new} already scheduled. All {total} are listed in the channel."
    if total == 0:
        reply = f"Week of {week_start}: nothing could be scheduled."
    if result["unmatched"]:
        tally = Counter(reason for _, reason in result["unmatched"])
        reply += f"\n{len(result['unmatched'])} still unscheduled:\n" + "\n".join(
            f"- {n}: {reason}" for reason, n in tally.most_common())
    await interaction.followup.send(reply[:2000], ephemeral=True)


@schedule.command(name="unscheduled", description="See who is still unscheduled for a week, and why (only you see this)")
@app_commands.describe(week="Any date in the week, YYYY-MM-DD. Defaults to next week.")
async def schedule_unscheduled(interaction: discord.Interaction, week: str | None = None):
    try:
        day = date.fromisoformat(week) if week else None
    except ValueError:
        return await interaction.response.send_message("Use a date like 2026-12-01.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    week_start = day - timedelta(days=day.weekday()) if day else service.next_week_start(service.now_local())
    # A trial run that saves nothing: whoever it cannot place is who is unscheduled.
    result = await asyncio.to_thread(service.run_proposal, week_start, None, False)
    lines = [f"- {p['name']}: {reason}" for p, reason in result["unmatched"]]
    header = f"**Unscheduled for the week of {week_start:%b} {week_start.day}: {len(lines)}**"
    if result["slots"]:
        header += (f"\n{len(result['slots'])} more could be placed right now; "
                   "run `/schedule propose` to post them.")
    if not lines and not result["slots"]:
        header = f"Everyone waiting has a session for the week of {week_start:%b} {week_start.day}."
    chunk = header
    for line in lines:
        if len(chunk) + len(line) + 1 > 1900:
            await interaction.followup.send(chunk, ephemeral=True)
            chunk = ""
        chunk += "\n" + line
    await interaction.followup.send(chunk.strip(), ephemeral=True)


@schedule.command(name="show", description="List sessions that are proposed, awaiting reply, or confirmed")
async def schedule_show(interaction: discord.Interaction):
    sessions = await asyncio.to_thread(db.active_sessions)
    if not sessions:
        return await interaction.response.send_message("No active sessions.", ephemeral=True)
    lines = [f"{mailer.fmt_when(s['start_at'], s['end_at'])}: {s['member_name']} runs "
             f"{s['participant_name']} ({STATUS_TEXT[s['status']]})" for s in sessions]
    await interaction.response.send_message("\n".join(lines)[:2000], ephemeral=True)


class FoxBot(discord.Client):
    def __init__(self):
        super().__init__(
            intents=discord.Intents.default(),
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True),
        )
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        self.add_dynamic_items(SessionButton)
        self.tree.add_command(availability, guild=GUILD)
        self.tree.add_command(schedule, guild=GUILD)
        try:
            await self.tree.sync(guild=GUILD)
        except discord.Forbidden:
            log.error("Cannot register slash commands. Is the bot in the server, and was it invited "
                      "with the applications.commands scope?")
        self.tick.start()

    async def on_ready(self):
        mode = "DRY RUN (no email, no sheet writes)" if config.DRY_RUN else "LIVE"
        log.info("Logged in as %s. Mode: %s", self.user, mode)

    async def channel(self) -> discord.abc.Messageable:
        return self.get_channel(config.DISCORD_CHANNEL_ID) or await self.fetch_channel(config.DISCORD_CHANNEL_ID)

    async def post_proposal(self, week_start: date) -> dict:
        """Match the week, then list every active session for it in the channel: the new ones and
        the ones proposed earlier. Earlier messages are replaced so each session appears once."""
        result = await asyncio.to_thread(service.run_proposal, week_start)
        channel = await self.channel()
        marks = ",".join("?" for _ in db.ACTIVE)
        week_sessions = await asyncio.to_thread(
            db.sessions, f"s.week_start = ? AND s.status IN ({marks})", (week_start.isoformat(), *db.ACTIVE))
        result["total"] = len(week_sessions)
        if week_sessions:
            await channel.send(
                f"## Sessions for the week of {week_start:%B} {week_start.day}"
                + ("\n*Dry-run mode: confirming shows the email but does not send it.*" if config.DRY_RUN else ""))
        for session in week_sessions:
            if session["message_id"]:
                try:
                    await channel.get_partial_message(session["message_id"]).delete()
                except discord.HTTPException:
                    pass  # already gone, or too old to remove; the fresh copy below is the live one
            text, card, view = render(session)
            message = await channel.send(text, embed=card, view=view)
            await asyncio.to_thread(db.update_session, session["id"], message_id=message.id)
        if result["members"] == 0:
            await channel.send("No one has availability set, so nothing could be scheduled. "
                               "Use `/availability set`.")
        elif result["unmatched"]:
            count = len(result["unmatched"])
            await channel.send(
                f"{count} participant{'s are' if count != 1 else ' is'} still unscheduled for the week of "
                f"{week_start:%b} {week_start.day}. Use `/schedule unscheduled` to see who and why.")
        return result

    async def refresh(self, sid: int):
        """Re-draw a session's message after its state changed outside a button click."""
        session = await asyncio.to_thread(db.session, sid)
        if not session["message_id"]:
            return
        try:
            channel = await self.channel()
            message = await channel.fetch_message(session["message_id"])
            text, card, view = render(session)
            await message.edit(content=text, embed=card, view=view)
        except discord.HTTPException:
            log.warning("could not refresh message for session %s", sid)

    @tasks.loop(minutes=5)
    async def tick(self):
        """Everything time-driven. Each job records that it ran, so restarts never repeat one."""
        for job in (self.weekly_jobs, self.check_replies, self.send_reminders, self.send_nudges):
            try:
                await job()
            except Exception:  # noqa: BLE001 - one failing job must not stop the others
                log.exception("%s failed", job.__name__)

    @tick.before_loop
    async def before_tick(self):
        await self.wait_until_ready()

    async def weekly_jobs(self):
        now = service.now_local()
        week_start = service.next_week_start(now)
        ask_day = (config.PROPOSE_WEEKDAY - 1) % 7
        if now.weekday() == ask_day and now.time() >= config.PROPOSE_TIME:
            if await asyncio.to_thread(db.claim, f"asked:{week_start}"):
                await (await self.channel()).send(
                    f"Running participants next week (week of {week_start:%b} {week_start.day})? "
                    "Update your availability with `/availability set` today. "
                    "The schedule is proposed tomorrow morning.")
        if now.weekday() == config.PROPOSE_WEEKDAY and now.time() >= config.PROPOSE_TIME:
            if await asyncio.to_thread(db.claim, f"proposed:{week_start}"):
                await self.post_proposal(week_start)

    async def check_replies(self):
        waiting = await asyncio.to_thread(db.sessions, "s.status = 'emailed'")
        by_address = {s["participant_email"]: s for s in waiting}
        replies = await asyncio.to_thread(mailer.fetch_replies, set(by_address))
        for address, text in replies:
            session = by_address[address]
            when = mailer.fmt_when(session["start_at"], session["end_at"])
            verdict = await asyncio.to_thread(mailer.classify_reply, text, when)
            channel = await self.channel()
            if verdict == "confirmed" and await asyncio.to_thread(service.participant_confirm, session["id"]):
                await channel.send(f"<@{session['member_id']}> {session['participant_name']} confirmed {when}.")
            elif verdict == "declined" and await asyncio.to_thread(service.participant_decline, session["id"]):
                await channel.send(f"<@{session['member_id']}> {session['participant_name']} can't make {when}. "
                                   "Their reply is in the lab inbox; they need rescheduling by hand.")
            else:
                await channel.send(f"<@{session['member_id']}> {session['participant_name']} replied about {when} "
                                   "but I couldn't tell if it's a yes. Read it in the lab inbox, then press "
                                   "the matching button on the session message.")
            await self.refresh(session["id"])

    async def send_reminders(self):
        now = service.now_local()
        soon = (now + timedelta(hours=24)).isoformat(timespec="minutes")
        due = await asyncio.to_thread(
            db.sessions, "s.status = 'confirmed' AND s.reminded = 0 AND s.start_at <= ? AND s.start_at > ?",
            (soon, now.isoformat(timespec="minutes")))
        for session in due:
            await asyncio.to_thread(db.update_session, session["id"], reminded=1)
            when = mailer.fmt_when(session["start_at"], session["end_at"])
            emailed = await asyncio.to_thread(mailer.send, session["participant_email"], *mailer.reminder(session))
            await (await self.channel()).send(
                f"<@{session['member_id']}> reminder: you're running {session['participant_name']} "
                f"{when}." + (" They've been sent a reminder email." if emailed else ""))

    async def send_nudges(self):
        cutoff = (service.now_local() - timedelta(hours=48)).isoformat(timespec="minutes")
        stale = await asyncio.to_thread(
            db.sessions, "s.status = 'emailed' AND s.nudged = 0 AND s.emailed_at <= ?", (cutoff,))
        for session in stale:
            await asyncio.to_thread(db.update_session, session["id"], nudged=1)
            await (await self.channel()).send(
                f"<@{session['member_id']}> no reply from {session['participant_name']} in 48 hours about "
                f"{mailer.fmt_when(session['start_at'], session['end_at'])}. Worth a follow-up.")


def run():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not config.DISCORD_TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set in .env")
    db.init()
    FoxBot().run(config.DISCORD_TOKEN, log_handler=None)
