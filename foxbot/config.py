"""Settings, read from .env. Nothing here should ever be printed wholesale."""
import os
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _hhmm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


DISCORD_TOKEN = _get("DISCORD_TOKEN")
DISCORD_GUILD_ID = int(_get("DISCORD_GUILD_ID", "0") or 0)
DISCORD_CHANNEL_ID = int(_get("DISCORD_CHANNEL_ID", "0") or 0)

GMAIL_ADDRESS = _get("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = _get("GMAIL_APP_PASSWORD").replace(" ", "")
FROM_NAME = _get("FROM_NAME", "FoxLab NYU")

SHEET_ID = _get("SHEET_ID")
SHEET_TAB = _get("SHEET_TAB", "Form Responses 1")
GOOGLE_SERVICE_ACCOUNT_FILE = _get("GOOGLE_SERVICE_ACCOUNT_FILE")
RESPONSES_CSV = ROOT / "data" / "responses.csv"
DB_PATH = ROOT / "data" / "foxbot.db"

# DRY_RUN=1 means: never send email, never write to the sheet.
DRY_RUN = _get("DRY_RUN", "1") != "0"

MODEL = _get("FOXBOT_MODEL", "claude-opus-5-5")
TZ = ZoneInfo(_get("TZ_NAME", "America/New_York"))

STUDY_NAME = _get("STUDY_NAME", "FoxLab chess study")
LAB_LOCATION = _get("LAB_LOCATION", "TBD")
SESSION_MINUTES = int(_get("SESSION_MINUTES", "120"))
LAB_OPEN = _hhmm(_get("LAB_OPEN", "09:00"))
LAB_CLOSE = _hhmm(_get("LAB_CLOSE", "21:00"))
# Sessions a single member will be given in one week.
MAX_SESSIONS_PER_MEMBER = int(_get("MAX_SESSIONS_PER_MEMBER", "3"))
# Sessions that may run at the same time (1 = one testing room).
MAX_CONCURRENT = int(_get("MAX_CONCURRENT", "1"))
BUFFER_MINUTES = int(_get("BUFFER_MINUTES", "15"))
MIN_NOTICE_HOURS = int(_get("MIN_NOTICE_HOURS", "36"))
# Form responses older than this are not scheduled until re-confirmed.
STALE_DAYS = int(_get("STALE_DAYS", "60"))

# Weekly post: weekday 0=Mon .. 6=Sun, local time.
PROPOSE_WEEKDAY = int(_get("PROPOSE_WEEKDAY", "4"))
PROPOSE_TIME = _hhmm(_get("PROPOSE_TIME", "10:00"))
REMINDER_TIME = _hhmm(_get("REMINDER_TIME", "09:00"))
