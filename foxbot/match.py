"""Pair waiting participants with lab members for one week. Pure functions, no I/O."""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from .parse import DAYS


@dataclass(frozen=True)
class Rules:
    session_minutes: int = 120
    lab_open: time = time(9, 0)
    lab_close: time = time(21, 0)
    max_per_member: int = 3
    max_concurrent: int = 1
    buffer_minutes: int = 15
    min_notice_hours: int = 36
    stale_days: int = 60


@dataclass(frozen=True)
class Slot:
    participant_id: int
    member_id: int
    start: datetime
    end: datetime


def _t(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


def week_windows(availability: dict | None, week_start: date, rules: Rules) -> list[tuple[datetime, datetime]]:
    """Concrete (start, end) windows inside the given Monday-to-Sunday week, clipped to lab hours
    and merged. Times are naive local datetimes."""
    if not availability:
        return []
    raw = []
    for w in availability.get("recurring", []):
        day = week_start + timedelta(days=DAYS.index(w["day"]))
        raw.append((day, _t(w["start"]), _t(w["end"])))
    for w in availability.get("dated", []):
        day = date.fromisoformat(w["date"])
        if week_start <= day < week_start + timedelta(days=7):
            raw.append((day, _t(w["start"]), _t(w["end"])))
    spans = []
    for day, start, end in raw:
        start, end = max(start, rules.lab_open), min(end, rules.lab_close)
        if start < end:
            spans.append((datetime.combine(day, start), datetime.combine(day, end)))
    spans.sort()
    merged: list[tuple[datetime, datetime]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _ceil_half_hour(dt: datetime) -> datetime:
    dt = dt.replace(second=0, microsecond=0)
    extra = dt.minute % 30
    return dt if extra == 0 else dt + timedelta(minutes=30 - extra)


def _overlaps(a_start, a_end, b_start, b_end) -> bool:
    return a_start < b_end and b_start < a_end


def propose(participants: list[dict], members: list[dict], week_start: date,
            existing: list[Slot], now: datetime, rules: Rules,
            blocked: frozenset[tuple[int, int]] = frozenset()) -> tuple[list[Slot], list[tuple[dict, str]]]:
    """Greedy first-come-first-served matching.

    participants: dicts with id, submitted_at (ISO), availability; already filtered to those
        waiting and without an active session. Processed in submission order.
    members: dicts with discord_id, availability.
    existing: sessions already active in this week; they occupy members and the room.
    blocked: (participant_id, member_id) pairs not to propose again, because that member already
        said they can't run that participant this week.
    Returns (new slots, [(participant, reason they were not scheduled)]).
    """
    length = timedelta(minutes=rules.session_minutes)
    buffer = timedelta(minutes=rules.buffer_minutes)
    earliest = now + timedelta(hours=rules.min_notice_hours)
    member_windows = {m["discord_id"]: week_windows(m.get("availability"), week_start, rules) for m in members}
    taken = list(existing)
    new: list[Slot] = []
    unmatched: list[tuple[dict, str]] = []

    def load(member_id):
        return sum(1 for s in taken if s.member_id == member_id)

    def feasible(member_id, start, end):
        if start < earliest or load(member_id) >= rules.max_per_member:
            return False
        if any(s.member_id == member_id and _overlaps(start - buffer, end + buffer, s.start, s.end) for s in taken):
            return False
        concurrent = sum(1 for s in taken if _overlaps(start - buffer, end + buffer, s.start, s.end))
        return concurrent < rules.max_concurrent

    for p in sorted(participants, key=lambda p: p["submitted_at"]):
        submitted = datetime.fromisoformat(p["submitted_at"])
        if (now - submitted).days > rules.stale_days:
            unmatched.append((p, f"signed up more than {rules.stale_days} days ago; re-confirm availability first"))
            continue
        windows = week_windows(p.get("availability"), week_start, rules)
        if not windows:
            av = p.get("availability") or {}
            if av.get("dated") and not av.get("recurring"):
                unmatched.append((p, "only gave specific dates, none in this week"))
            else:
                unmatched.append((p, "no usable availability; needs a human to read their answer"))
            continue
        best = None
        overlap_exists = False
        for member_id, m_windows in member_windows.items():
            if (p["id"], member_id) in blocked:
                continue
            for p_start, p_end in windows:
                for m_start, m_end in m_windows:
                    lo, hi = max(p_start, m_start), min(p_end, m_end)
                    start = _ceil_half_hour(lo)
                    overlap_exists = overlap_exists or start + length <= hi
                    while start + length <= hi:
                        if feasible(member_id, start, start + length):
                            key = (load(member_id), start, member_id)
                            if best is None or key < best[0]:
                                best = (key, Slot(p["id"], member_id, start, start + length))
                            break  # earliest feasible start in this overlap is enough
                        start += timedelta(minutes=30)
        if best is None:
            unmatched.append((p, "members who overlap are already full this week" if overlap_exists
                              else "no overlap with any member's open time this week"))
        else:
            taken.append(best[1])
            new.append(best[1])
    return new, unmatched
