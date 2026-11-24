"""Turn free-text availability into structured windows with Claude.

Only the availability text and a reference date are sent to the API: no names, emails or NetIDs.
"""
from datetime import date
from typing import Literal

import anthropic
from pydantic import BaseModel

from . import config

Day = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class RecurringWindow(BaseModel):
    day: Day
    start: str  # "HH:MM", 24-hour
    end: str


class DatedWindow(BaseModel):
    date: str  # "YYYY-MM-DD"
    start: str
    end: str


class Availability(BaseModel):
    recurring: list[RecurringWindow]
    dated: list[DatedWindow]
    confidence: Literal["high", "medium", "low"]
    notes: str


SYSTEM = """You convert a person's free-text availability into structured time windows for \
scheduling a lab session of {minutes} minutes.

Output two lists:
- recurring: windows that repeat every week ("Mondays after 5", "weekends", "MWF 2-5").
- dated: windows tied to specific calendar dates ("4/15 after 4pm", "next week", "tomorrow"). \
Resolve relative dates and dates without a year against the reference date, which is the day the \
text was written. "Next week" means the Monday-to-Sunday week after the reference date.

Rules:
- Times are 24-hour "HH:MM". A window is the span during which the person is free; the session \
must fit inside it.
- "All day", "anytime" or a bare day name means 00:00 to 23:59. "After X" means X to 23:59. \
"Before X" means 00:00 to X. The scheduler clips windows to lab hours itself.
- If the person says they can START within a range, extend the end by the session length.
- A single time with no range ("Wednesday 12:30") is a window starting then and lasting the \
session length.
- Times without am/pm: assume the reading that falls in normal daytime hours (1-7 means pm).
- If a day name appears without a date and nothing suggests a one-off, treat it as recurring. \
If the text mixes specific dates with weekday names for those same dates, use dated only.
- Do not invent availability. Leave out anything too vague to schedule against ("most days", \
"weekends are likely best" with no times is fine as full-day weekend windows; "sometimes" is not).

confidence: "high" when the text is unambiguous, "medium" when you made a reasonable assumption, \
"low" when a human should check before anyone is contacted. Put the assumption or the problem in \
notes, in one short sentence. Leave notes empty when confidence is high."""


def parse_availability(text: str, reference: date) -> dict:
    """Return an Availability dict. Never raises for model-side problems: a failed parse comes
    back as empty, low-confidence availability so a human reviews it."""
    text = (text or "").strip()
    if not text:
        return _empty("No availability given.")
    client = anthropic.Anthropic()
    try:
        response = client.messages.parse(
            model=config.MODEL,
            max_tokens=16000,
            system=SYSTEM.format(minutes=config.SESSION_MINUTES),
            messages=[{
                "role": "user",
                "content": f"Reference date: {reference.isoformat()} ({reference.strftime('%A')})\n\n"
                           f"Availability text:\n{text}",
            }],
            output_config={"effort": "low"},
            output_format=Availability,
        )
    except anthropic.RateLimitError:
        raise
    except anthropic.APIStatusError as e:
        if e.status_code >= 500:
            raise
        return _empty(f"Parser request rejected ({e.status_code}).")
    if response.stop_reason != "end_turn" or response.parsed_output is None:
        return _empty(f"Parser stopped early ({response.stop_reason}).")
    return _clean(response.parsed_output.model_dump())


def _empty(note: str) -> dict:
    return {"recurring": [], "dated": [], "confidence": "low", "notes": note}


def _valid_time(value: str) -> bool:
    try:
        h, m = value.split(":")
        return 0 <= int(h) <= 23 and 0 <= int(m) <= 59
    except (ValueError, AttributeError):
        return False


def _clean(av: dict) -> dict:
    """Drop windows the scheduler could not use; mark the result low-confidence if any were dropped."""
    dropped = 0
    keep_r, keep_d = [], []
    for w in av["recurring"]:
        if _valid_time(w["start"]) and _valid_time(w["end"]) and w["start"] < w["end"]:
            keep_r.append(w)
        else:
            dropped += 1
    for w in av["dated"]:
        try:
            date.fromisoformat(w["date"])
            ok = _valid_time(w["start"]) and _valid_time(w["end"]) and w["start"] < w["end"]
        except ValueError:
            ok = False
        if ok:
            keep_d.append(w)
        else:
            dropped += 1
    av["recurring"], av["dated"] = keep_r, keep_d
    if dropped:
        av["confidence"] = "low"
        av["notes"] = (av["notes"] + f" {dropped} unusable window(s) dropped.").strip()
    return av


def describe(av: dict | None) -> str:
    """Human-readable summary of parsed availability, for Discord."""
    if not av or (not av["recurring"] and not av["dated"]):
        return "nothing set"
    lines = []
    for day in DAYS:
        spans = [f"{w['start']}–{w['end']}" for w in av["recurring"] if w["day"] == day]
        if spans:
            lines.append(f"{day.capitalize()}: {', '.join(spans)}")
    for w in sorted(av["dated"], key=lambda w: (w["date"], w["start"])):
        lines.append(f"{w['date']}: {w['start']}–{w['end']}")
    return "\n".join(lines)
