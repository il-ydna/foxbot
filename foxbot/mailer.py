"""Participant email: compose, send over Gmail SMTP, and read replies over IMAP.

Sending and reading both need GMAIL_APP_PASSWORD. With DRY_RUN on, nothing is sent.
"""
import email
import email.policy
import imaplib
import smtplib
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from typing import Literal

import anthropic
from pydantic import BaseModel

from . import config


def fmt_when(start_iso: str, end_iso: str) -> str:
    start, end = datetime.fromisoformat(start_iso), datetime.fromisoformat(end_iso)
    clock = lambda d: d.strftime("%I:%M %p").lstrip("0")  # noqa: E731
    return f"{start.strftime('%A, %B')} {start.day}, {clock(start)} – {clock(end)}"


def _first(name: str) -> str:
    return name.strip().split()[0] if name.strip() else "there"


def invitation(session: dict) -> tuple[str, str]:
    """Subject and body of the email asking a participant to confirm a proposed time."""
    start = datetime.fromisoformat(session["start_at"])
    subject = f"{config.STUDY_NAME}: can you come in {start.strftime('%A')} {start.month}/{start.day}?"
    hours = config.SESSION_MINUTES / 60
    body = (
        f"Hi {_first(session['participant_name'])},\n\n"
        f"Thanks for signing up for the {config.STUDY_NAME}. Based on the availability you gave us, "
        f"we'd like to schedule your session for:\n\n"
        f"    {fmt_when(session['start_at'], session['end_at'])}\n"
        f"    Location: {config.LAB_LOCATION}\n\n"
        f"The session takes about {hours:g} hours. Please reply to this email to confirm, or let us "
        f"know if that time no longer works and we'll find another.\n\n"
        f"Thanks,\n{_first(session['member_name'])}\nFoxLab, NYU\n"
    )
    return subject, body


def reminder(session: dict) -> tuple[str, str]:
    subject = f"Reminder: {config.STUDY_NAME} tomorrow"
    body = (
        f"Hi {_first(session['participant_name'])},\n\n"
        f"A reminder that your {config.STUDY_NAME} session is tomorrow:\n\n"
        f"    {fmt_when(session['start_at'], session['end_at'])}\n"
        f"    Location: {config.LAB_LOCATION}\n\n"
        f"If something has come up, reply to this email and we'll reschedule.\n\n"
        f"Thanks,\nFoxLab, NYU\n"
    )
    return subject, body


def can_send() -> bool:
    return bool(not config.DRY_RUN and config.GMAIL_ADDRESS and config.GMAIL_APP_PASSWORD)


def send(to: str, subject: str, body: str) -> bool:
    """Send one email. Returns False (and sends nothing) in dry-run or without credentials."""
    if not can_send():
        return False
    msg = EmailMessage()
    msg["From"] = formataddr((config.FROM_NAME, config.GMAIL_ADDRESS))
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(config.GMAIL_ADDRESS, config.GMAIL_APP_PASSWORD)
        smtp.send_message(msg)
    return True


def _text_of(msg) -> str:
    part = msg.get_body(preferencelist=("plain",)) if hasattr(msg, "get_body") else None
    if part is None:
        return ""
    text = part.get_content()
    # Keep only what the participant wrote, not the quoted invitation underneath.
    kept = []
    for line in text.splitlines():
        if line.startswith(">") or (line.startswith("On ") and line.rstrip().endswith("wrote:")):
            break
        kept.append(line)
    return "\n".join(kept).strip()


def fetch_replies(addresses: set[str]) -> list[tuple[str, str]]:
    """Unread inbox messages from the given addresses, as (address, text). Marks them read."""
    if not (config.GMAIL_ADDRESS and config.GMAIL_APP_PASSWORD) or not addresses:
        return []
    found = []
    imap = imaplib.IMAP4_SSL("imap.gmail.com", timeout=30)
    try:
        imap.login(config.GMAIL_ADDRESS, config.GMAIL_APP_PASSWORD)
        imap.select("INBOX")
        _, data = imap.search(None, "UNSEEN")
        for num in data[0].split():
            _, header = imap.fetch(num, "(BODY.PEEK[HEADER.FIELDS (FROM)])")
            sender = parseaddr(email.message_from_bytes(header[0][1]).get("From", ""))[1].lower()
            if sender not in addresses:
                continue  # leave unrelated mail unread for the humans who use this inbox
            _, full = imap.fetch(num, "(RFC822)")  # fetching the body marks it read
            msg = email.message_from_bytes(full[0][1], policy=email.policy.default)
            found.append((sender, _text_of(msg)))
    finally:
        try:
            imap.logout()
        except Exception:  # noqa: BLE001 - connection may already be gone
            pass
    return found


class ReplyVerdict(BaseModel):
    verdict: Literal["confirmed", "declined", "unclear"]


def classify_reply(text: str, when: str) -> str:
    """Is this reply a yes, a no, or something a human should read? Only the reply text is sent."""
    if not text.strip():
        return "unclear"
    try:
        response = anthropic.Anthropic().messages.parse(
            model=config.MODEL,
            max_tokens=16000,
            system=(
                "A study participant was asked by email to confirm a session at a proposed time. "
                "Classify their reply. 'confirmed' only if they clearly accept that exact time. "
                "'declined' only if they clearly cannot make it or withdraw. Anything else, "
                "including a counter-proposal, a question, or a conditional yes, is 'unclear'."
            ),
            messages=[{"role": "user", "content": f"Proposed time: {when}\n\nReply:\n{text}"}],
            output_config={"effort": "low"},
            output_format=ReplyVerdict,
        )
    except anthropic.APIError:
        return "unclear"
    if response.stop_reason != "end_turn" or response.parsed_output is None:
        return "unclear"
    return response.parsed_output.verdict
