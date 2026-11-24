"""State transitions end to end against a temporary database, with no network."""
import asyncio
from datetime import date, datetime

import pytest

from foxbot import config, db, ingest, mailer, service

WEEK = date(2026, 11, 30)
NOW = datetime(2026, 11, 27, 10, 0)


def av(*recurring):
    return {"recurring": [{"day": d, "start": s, "end": e} for d, s, e in recurring], "dated": [],
            "confidence": "high", "notes": ""}


@pytest.fixture
def lab(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config, "DRY_RUN", True)
    monkeypatch.setattr(ingest, "sync", lambda: {"source": "test", "added": 0, "parsed": 0})
    sent = []
    monkeypatch.setattr(mailer, "send", lambda to, subject, body: sent.append((to, subject, body)) or True)
    db.init()
    db.upsert_participant(2, "2026-11-05T10:00:00", "Pat Example", "pat@example.com", "pe1", "Tue 1-6", False)
    db.upsert_participant(3, "2026-11-06T10:00:00", "Sam Example", "sam@example.com", "se1", "Tue 1-6", False)
    for p in db.participants():
        db.set_participant_availability(p["id"], av(("tue", "13:00", "18:00")))
    db.set_member(111, "Runner One", "tue afternoons", av(("tue", "12:00", "18:00")))
    db.set_member(222, "Runner Two", "tue afternoons", av(("tue", "12:00", "21:00")))
    return sent


def test_propose_is_idempotent(lab):
    first = service.run_proposal(WEEK, NOW)
    assert len(first["session_ids"]) == 2
    again = service.run_proposal(WEEK, NOW)
    assert again["session_ids"] == [] and again["waiting"] == 0


def test_no_email_until_member_confirms_and_never_twice(lab):
    sid = service.run_proposal(WEEK, NOW)["session_ids"][0]
    assert lab == []
    result = service.member_confirm(sid)
    assert result["sent"] and len(lab) == 1 and lab[0][0] == "pat@example.com"
    assert "Tuesday, December 1" in result["body"]
    assert service.member_confirm(sid) is None and len(lab) == 1  # double click sends nothing more
    assert db.session(sid)["status"] == "emailed"


def test_confirm_then_participant_confirms(lab):
    sid = service.run_proposal(WEEK, NOW)["session_ids"][0]
    assert not service.participant_confirm(sid)  # cannot confirm before the email went out
    service.member_confirm(sid)
    assert service.participant_confirm(sid)
    assert db.session(sid)["status"] == "confirmed"
    assert db.participant(db.session(sid)["participant_id"])["status"] == "done"


def test_decline_parks_participant_for_follow_up(lab):
    sid = service.run_proposal(WEEK, NOW)["session_ids"][0]
    service.member_confirm(sid)
    assert service.participant_decline(sid)
    pid = db.session(sid)["participant_id"]
    assert db.participant(pid)["status"] == "followup"
    assert pid not in [s.participant_id for s in service.run_proposal(WEEK, NOW)["slots"]]


def test_member_cancel_moves_participant_to_someone_else(lab):
    ids = service.run_proposal(WEEK, NOW)["session_ids"]
    target = db.session(ids[0])
    assert service.member_cancel(ids[0])
    again = service.run_proposal(WEEK, NOW)
    assert len(again["slots"]) <= 1
    for slot in again["slots"]:
        assert slot.participant_id == target["participant_id"] and slot.member_id != target["member_id"]


def test_failed_send_rolls_back(lab, monkeypatch):
    sid = service.run_proposal(WEEK, NOW)["session_ids"][0]

    def boom(*args):
        raise OSError("smtp down")
    monkeypatch.setattr(mailer, "send", boom)
    with pytest.raises(OSError):
        service.member_confirm(sid)
    assert db.session(sid)["status"] == "proposed"


def test_live_mode_without_email_credentials_refuses(lab, monkeypatch):
    sid = service.run_proposal(WEEK, NOW)["session_ids"][0]
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(config, "GMAIL_APP_PASSWORD", "")
    with pytest.raises(RuntimeError):
        service.member_confirm(sid)
    assert db.session(sid)["status"] == "proposed"


def test_discord_rendering(lab):
    from foxbot import bot

    async def go():
        sid = service.run_proposal(WEEK, NOW)["session_ids"][0]
        text, card, view = bot.render(db.session(sid))
        assert text in ("<@111>", "<@222>")
        assert card.title == "Pat Example (pat@example.com)" and card.author.name.endswith("Proposed session")
        start = datetime.fromisoformat(db.session(sid)["start_at"]).replace(tzinfo=config.TZ)
        assert start.hour == 13 and start.utcoffset().total_seconds() == -5 * 3600  # 1 pm Eastern
        assert [(f.name, f.value) for f in card.fields] == [
            ("Scheduled for", f"<t:{int(start.timestamp())}:F>"), ("Duration", "2 hours")]
        assert [c.custom_id for c in view.children] == [f"foxbot:confirm:{sid}", f"foxbot:cancel:{sid}"]
        service.member_confirm(sid)
        _, card, view = bot.render(db.session(sid))
        assert [c.custom_id for c in view.children] == [f"foxbot:pyes:{sid}", f"foxbot:pno:{sid}"]
        service.participant_confirm(sid)
        _, card, view = bot.render(db.session(sid))
        assert view.children == [] and card.author.name.endswith("Session confirmed")
        client = bot.FoxBot()
        assert {c.name for c in (bot.availability, bot.schedule)} == {"availability", "schedule"}
        await client.close()
    asyncio.run(go())


def test_changing_availability_withdraws_only_unconfirmed_sessions(lab):
    ids = service.run_proposal(WEEK, NOW)["session_ids"]
    by_member = {db.session(i)["member_id"]: i for i in ids}
    assert set(by_member) == {111, 222}
    service.member_confirm(by_member[222])  # runner two's participant has been emailed
    # Both runners move to Wednesdays only.
    db.set_member(111, "Runner One", "wed", av(("wed", "12:00", "18:00")))
    db.set_member(222, "Runner Two", "wed", av(("wed", "12:00", "18:00")))
    withdrawn, contacted = service.release_unavailable(111, NOW)
    assert [s["id"] for s in withdrawn] == [by_member[111]] and contacted == []
    assert db.session(by_member[111])["status"] == "withdrawn"
    withdrawn, contacted = service.release_unavailable(222, NOW)
    assert withdrawn == [] and [s["id"] for s in contacted] == [by_member[222]]
    assert db.session(by_member[222])["status"] == "emailed"  # participant is expecting it
    # The withdrawn participant is back in the pool and may go to the same runner at a new time.
    assert db.session(by_member[111])["participant_id"] in {p["id"] for p, _ in service.run_proposal(WEEK, NOW)["unmatched"]}


def test_sessions_that_still_fit_are_kept(lab):
    ids = service.run_proposal(WEEK, NOW)["session_ids"]
    db.set_member(111, "Runner One", "tue all day", av(("tue", "09:00", "21:00")))
    assert service.release_unavailable(111, NOW) == ([], [])
    db.set_member(111, "Runner One", "", None)  # cleared availability releases everything unconfirmed
    withdrawn, _ = service.release_unavailable(111, NOW)
    assert len(withdrawn) == 1 and withdrawn[0]["id"] in ids
