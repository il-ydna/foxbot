"""Command line: python -m foxbot <command>

  run                      start the Discord bot (always-on process)
  sync                     pull new form responses and parse their availability
  parsed                   show how each waiting participant's availability was parsed
  preview [YYYY-MM-DD]     show what would be proposed for a week, without saving anything
"""
import sys
from datetime import date, datetime

from . import config, db, ingest, service
from .parse import describe


def main(argv):
    command = argv[1] if len(argv) > 1 else "help"
    db.init()
    if command == "run":
        from .bot import run
        run()
    elif command == "sync":
        print(ingest.sync())
    elif command == "parsed":
        for p in db.participants("status = 'waiting'"):
            av = p["availability"] or {}
            print(f"\n#{p['id']} {p['name']} (submitted {p['submitted_at'][:10]})"
                  f" [{av.get('confidence', 'unparsed')}] {av.get('notes', '')}")
            print("  said:   " + (p["availability_raw"] or "").replace("\n", " / "))
            print("  parsed: " + describe(av).replace("\n", "; "))
    elif command == "preview":
        now = service.now_local()
        week = date.fromisoformat(argv[2]) if len(argv) > 2 else service.next_week_start(now)
        result = service.run_proposal(week, now, save=False)
        names = {p["id"]: p["name"] for p in db.participants()}
        members = {m["discord_id"]: m["name"] for m in db.members()}
        print(f"Week of {week}: {result['waiting']} waiting, {result['members']} members with availability")
        for s in result["slots"]:
            print(f"  {s.start:%a %m/%d %H:%M}-{s.end:%H:%M}  {members[s.member_id]:<12} runs {names[s.participant_id]}")
        print("Not scheduled:")
        for p, reason in result["unmatched"]:
            print(f"  {p['name']}: {reason}")
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv)
