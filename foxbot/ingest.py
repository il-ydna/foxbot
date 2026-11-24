"""Read interest-form responses from the Google Sheet (or a local CSV snapshot) into the database."""
import csv
from datetime import datetime

from . import config, db
from .parse import parse_availability

# Column positions in the responses sheet.
COL_TIMESTAMP, COL_NAME, COL_EMAIL, COL_NETID, COL_AVAIL, COL_REACHED, COL_SCHEDULED = range(7)


def _worksheet():
    import gspread

    client = gspread.service_account(filename=config.GOOGLE_SERVICE_ACCOUNT_FILE)
    return client.open_by_key(config.SHEET_ID).worksheet(config.SHEET_TAB)


def _read_rows() -> tuple[list[list[str]], str]:
    """Rows including the header, plus a label for where they came from."""
    if config.GOOGLE_SERVICE_ACCOUNT_FILE:
        return _worksheet().get_all_values(), "google sheet"
    if config.RESPONSES_CSV.exists():
        with open(config.RESPONSES_CSV, newline="", encoding="utf-8") as f:
            return list(csv.reader(f)), "local csv snapshot"
    return [], "no source configured"


def _timestamp(value: str) -> str | None:
    for fmt in ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M"):
        try:
            return datetime.strptime(value.strip(), fmt).isoformat(timespec="seconds")
        except ValueError:
            continue
    return None


def sync() -> dict:
    """Add new responses and parse any that have not been parsed. Returns counts."""
    db.init()
    rows, source = _read_rows()
    added = 0
    for index, row in enumerate(rows[1:], start=2):  # sheet rows are 1-based; row 1 is the header
        row = row + [""] * (7 - len(row))
        submitted = _timestamp(row[COL_TIMESTAMP])
        email = row[COL_EMAIL].strip()
        # The sheet has free-form notes below the responses; a real response has both of these.
        if not submitted or "@" not in email:
            continue
        done = bool(row[COL_SCHEDULED].strip() or row[COL_REACHED].strip())
        if db.upsert_participant(index, submitted, row[COL_NAME].strip(), email.lower(),
                                 row[COL_NETID].strip(), row[COL_AVAIL].strip(), done):
            added += 1
    parsed = 0
    for p in db.participants("status = 'waiting' AND availability_json IS NULL"):
        reference = datetime.fromisoformat(p["submitted_at"]).date()
        db.set_participant_availability(p["id"], parse_availability(p["availability_raw"], reference))
        parsed += 1
    return {"source": source, "added": added, "parsed": parsed}


def write_status(sheet_row: int | None, reached_out: str | None = None, scheduled: str | None = None) -> bool:
    """Mirror a status into the sheet's 'reached out' / 'scheduled' columns. No-op in dry-run or
    without a service account. Returns True if the sheet was written."""
    if config.DRY_RUN or not config.GOOGLE_SERVICE_ACCOUNT_FILE or not sheet_row:
        return False
    ws = _worksheet()
    if reached_out is not None:
        ws.update_cell(sheet_row, COL_REACHED + 1, reached_out)
    if scheduled is not None:
        ws.update_cell(sheet_row, COL_SCHEDULED + 1, scheduled)
    return True
