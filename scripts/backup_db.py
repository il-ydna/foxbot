"""Copy the database to data/backups/ with today's date, keeping the newest 14 copies."""
import sqlite3
from datetime import date
from pathlib import Path

data = Path(__file__).resolve().parent.parent / "data"
backups = data / "backups"
backups.mkdir(mode=0o700, exist_ok=True)
target = backups / f"foxbot-{date.today().isoformat()}.db"
source = sqlite3.connect(data / "foxbot.db")
dest = sqlite3.connect(target)
with dest:
    source.backup(dest)  # consistent snapshot even while the bot is writing
dest.close()
source.close()
target.chmod(0o600)
for old in sorted(backups.glob("foxbot-*.db"))[:-14]:
    old.unlink()
print(f"backed up to {target}")
