# foxbot

Scheduling bot for FoxLab participant sessions.

1. Reads the study interest form (Google Sheet) and parses each free-text availability answer.
2. Lab members tell it when they can run sessions, in Discord.
3. Each week it proposes participant/member pairings in the lab Discord channel and pings the runners.
4. When a runner presses **Confirm**, the participant is emailed from the lab Gmail account.
   Nobody is emailed before that.
5. It tracks the participant's reply, reminds the runner the day before, and nudges after 48 hours of silence.

## Run

```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python scripts/check_creds.py   # verifies .env without printing secrets
.venv/bin/python -m foxbot run            # the always-on process
```

Other commands: `python -m foxbot sync | parsed | preview [YYYY-MM-DD]`. Tests: `python -m pytest`.

## Discord commands

- `/availability set when:"Mon and Wed 1-6pm, Fri mornings"`, `/availability show`, `/availability clear`
- `/schedule propose [week]` posts sessions for a week (also runs automatically, Fridays 10:00)
- `/schedule unscheduled [week]` shows who couldn't be placed and why, visible only to you
- `/schedule show` lists active sessions

## Configuration (`.env`, never committed)

| Variable | Purpose |
|---|---|
| `DISCORD_TOKEN`, `DISCORD_GUILD_ID`, `DISCORD_CHANNEL_ID` | Bot login, server, and the channel schedules are posted in |
| `ANTHROPIC_API_KEY` | Parsing availability text and classifying email replies |
| `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD` | Sending participant email and reading replies |
| `SHEET_ID`, `GOOGLE_SERVICE_ACCOUNT_FILE` | Live read/write of the responses sheet. Without the key file, `data/responses.csv` is read instead |
| `DRY_RUN` | `1` (default): no email is sent and the sheet is not written. `0` to go live |
| `LAB_LOCATION` | Shown in participant emails |

Scheduling rules (`SESSION_MINUTES`, `LAB_OPEN`, `LAB_CLOSE`, `MAX_SESSIONS_PER_MEMBER`, `MAX_CONCURRENT`,
`BUFFER_MINUTES`, `MIN_NOTICE_HOURS`, `STALE_DAYS`, `PROPOSE_WEEKDAY`, `PROPOSE_TIME`) have defaults in
`foxbot/config.py` and can be overridden in `.env`.

## Privacy

Participant names, emails and NetIDs stay in `data/` (gitignored) and the sheet. Only the availability
text and, later, the text of a participant's reply are sent to the Claude API.
