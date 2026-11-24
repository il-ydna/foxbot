"""Scheduling operations shared by the CLI and the Discord bot."""
from datetime import date, datetime, timedelta

from . import config, db, ingest, mailer
from .match import Rules, Slot, propose, week_windows


def now_local() -> datetime:
    """Current wall-clock time in the lab's timezone, as a naive datetime (matches stored times)."""
    return datetime.now(config.TZ).replace(tzinfo=None)


def next_week_start(now: datetime) -> date:
    return now.date() + timedelta(days=7 - now.weekday())


def rules() -> Rules:
    return Rules(
        session_minutes=config.SESSION_MINUTES, lab_open=config.LAB_OPEN, lab_close=config.LAB_CLOSE,
        max_per_member=config.MAX_SESSIONS_PER_MEMBER, max_concurrent=config.MAX_CONCURRENT,
        buffer_minutes=config.BUFFER_MINUTES, min_notice_hours=config.MIN_NOTICE_HOURS,
        stale_days=config.STALE_DAYS,
    )


def run_proposal(week_start: date, now: datetime | None = None, save: bool = True) -> dict:
    """Sync the form, match waiting participants for the week, and store the new sessions.

    Safe to run repeatedly: participants and members who already have sessions keep them, and
    only the gaps are filled.
    """
    now = now or now_local()
    synced = ingest.sync()
    active = db.active_sessions()
    busy = {s["participant_id"] for s in active}
    waiting = [p for p in db.participants("status = 'waiting'") if p["id"] not in busy]
    week = week_start.isoformat()
    existing = [
        Slot(s["participant_id"], s["member_id"], datetime.fromisoformat(s["start_at"]),
             datetime.fromisoformat(s["end_at"]))
        for s in active if s["week_start"] == week
    ]
    blocked = frozenset(
        (s["participant_id"], s["member_id"])
        for s in db.sessions("s.week_start = ? AND s.status = 'cancelled'", (week,))
    )
    members = [m for m in db.members() if m["availability"]]
    slots, unmatched = propose(waiting, members, week_start, existing, now, rules(), blocked)
    new_ids = []
    if save:
        for slot in slots:
            new_ids.append(db.add_session(week, slot.participant_id, slot.member_id,
                                          slot.start.isoformat(timespec="minutes"),
                                          slot.end.isoformat(timespec="minutes")))
    return {"synced": synced, "slots": slots, "session_ids": new_ids, "unmatched": unmatched,
            "members": len(members), "waiting": len(waiting)}


def short_when(session: dict) -> str:
    start = datetime.fromisoformat(session["start_at"])
    clock = start.strftime("%I:%M%p").lstrip("0").lower().replace(":00", "")
    return f"{start.strftime('%a')} {start.month}/{start.day} {clock}"


# --- state changes --------------------------------------------------------
# Each returns a short human-readable result, or None if the session was not in a state that
# allows the change (someone else already acted on it).

def member_confirm(sid: int) -> dict | None:
    """Member confirms a proposed session: email the participant. Returns
    {"sent": bool, "subject": str, "body": str} or None."""
    if not config.DRY_RUN and not mailer.can_send():
        raise RuntimeError("Email is not set up (GMAIL_APP_PASSWORD missing), so nothing was sent.")
    stamp = now_local().isoformat(timespec="minutes")
    if not db.transition(sid, ("proposed",), "emailed", emailed_at=stamp):
        return None
    session = db.session(sid)
    subject, body = mailer.invitation(session)
    try:
        sent = mailer.send(session["participant_email"], subject, body)
    except Exception:
        db.transition(sid, ("emailed",), "proposed", emailed_at=None)  # nothing went out; allow a retry
        raise
    if sent:
        ingest.write_status(session["sheet_row"], reached_out=f"emailed {short_when(session)}")
    return {"sent": sent, "subject": subject, "body": body}


def member_cancel(sid: int) -> bool:
    return db.transition(sid, ("proposed",), "cancelled")


def participant_confirm(sid: int) -> bool:
    if not db.transition(sid, ("emailed",), "confirmed"):
        return False
    session = db.session(sid)
    db.set_participant_status(session["participant_id"], "done")
    ingest.write_status(session["sheet_row"],
                        scheduled=f"confirmed - {short_when(session)} ({session['member_name']})")
    return True


def participant_decline(sid: int) -> bool:
    if not db.transition(sid, ("emailed",), "declined"):
        return False
    session = db.session(sid)
    # Their stated availability was wrong for this slot, so don't auto-propose them again.
    db.set_participant_status(session["participant_id"], "followup")
    ingest.write_status(session["sheet_row"], reached_out=f"declined {short_when(session)}; needs follow-up")
    return True


def release_unavailable(member_id: int, now: datetime | None = None) -> tuple[list[dict], list[dict]]:
    """After a runner changes their availability, withdraw their upcoming sessions that no longer
    fit it. Only sessions still waiting on the runner are withdrawn; ones where the participant has
    already been emailed or has confirmed are returned separately for the runner to sort out,
    because that participant is expecting the session.

    Returns (withdrawn sessions, contacted sessions that no longer fit)."""
    now = now or now_local()
    member = db.member(member_id)
    availability = member["availability"] if member else None
    withdrawn, contacted = [], []
    upcoming = db.sessions("s.member_id = ? AND s.status IN (?,?,?) AND s.start_at > ?",
                           (member_id, *db.ACTIVE, now.isoformat(timespec="minutes")))
    for session in upcoming:
        start, end = datetime.fromisoformat(session["start_at"]), datetime.fromisoformat(session["end_at"])
        windows = week_windows(availability, date.fromisoformat(session["week_start"]), rules())
        if any(lo <= start and end <= hi for lo, hi in windows):
            continue
        if session["status"] == "proposed" and db.transition(session["id"], ("proposed",), "withdrawn"):
            withdrawn.append(session)
        elif session["status"] != "proposed":
            contacted.append(session)
    return withdrawn, contacted
