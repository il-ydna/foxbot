from datetime import date, datetime, time

from foxbot.match import Rules, Slot, propose, week_windows

WEEK = date(2026, 11, 30)  # a Monday
NOW = datetime(2026, 11, 27, 10, 0)  # the Friday before
RULES = Rules()


def av(recurring=(), dated=()):
    return {
        "recurring": [{"day": d, "start": s, "end": e} for d, s, e in recurring],
        "dated": [{"date": d, "start": s, "end": e} for d, s, e in dated],
        "confidence": "high", "notes": "",
    }


def person(pid, availability, submitted="2026-11-05T10:00:00"):
    return {"id": pid, "submitted_at": submitted, "availability": availability}


def member(mid, availability):
    return {"discord_id": mid, "availability": availability}


def test_week_windows_clips_to_lab_hours_and_merges():
    windows = week_windows(av([("mon", "00:00", "23:59"), ("mon", "20:00", "22:00")]), WEEK, RULES)
    assert windows == [(datetime(2026, 11, 30, 9, 0), datetime(2026, 11, 30, 21, 0))]


def test_dated_windows_outside_week_are_ignored():
    assert week_windows(av(dated=[("2026-11-10", "12:00", "20:00")]), WEEK, RULES) == []
    assert len(week_windows(av(dated=[("2026-12-02", "12:00", "20:00")]), WEEK, RULES)) == 1


def test_basic_match_uses_overlap_and_half_hour_grid():
    slots, unmatched = propose([person(1, av([("tue", "13:15", "18:00")]))],
                               [member(10, av([("tue", "12:00", "16:00")]))], WEEK, [], NOW, RULES)
    assert unmatched == []
    assert slots == [Slot(1, 10, datetime(2026, 12, 1, 13, 30), datetime(2026, 12, 1, 15, 30))]


def test_overlap_shorter_than_session_is_not_a_match():
    slots, unmatched = propose([person(1, av([("tue", "13:00", "14:30")]))],
                               [member(10, av([("tue", "12:00", "18:00")]))], WEEK, [], NOW, RULES)
    assert slots == [] and "no overlap" in unmatched[0][1]


def test_first_come_first_served_and_one_room():
    people = [person(2, av([("mon", "14:00", "16:00")]), "2026-11-06T09:00:00"),
              person(1, av([("mon", "14:00", "16:00")]), "2026-11-05T09:00:00")]
    members = [member(10, av([("mon", "09:00", "21:00")])), member(11, av([("mon", "09:00", "21:00")]))]
    slots, unmatched = propose(people, members, WEEK, [], NOW, RULES)
    assert [s.participant_id for s in slots] == [1]  # earlier sign-up wins the only slot
    assert unmatched[0][0]["id"] == 2
    # With two rooms both can run at once, with different members.
    slots, _ = propose(people, members, WEEK, [], NOW, Rules(max_concurrent=2))
    assert {s.participant_id for s in slots} == {1, 2} and len({s.member_id for s in slots}) == 2


def test_member_weekly_cap_and_buffer():
    people = [person(i, av([("mon", "09:00", "21:00")]), f"2026-11-05T0{i}:00:00") for i in range(1, 5)]
    slots, unmatched = propose(people, [member(10, av([("mon", "09:00", "21:00")]))], WEEK, [], NOW,
                               Rules(max_per_member=3))
    assert len(slots) == 3 and len(unmatched) == 1
    starts = sorted(s.start for s in slots)
    assert starts[1] - starts[0] >= __import__("datetime").timedelta(minutes=120 + 15)


def test_work_is_balanced_across_members():
    people = [person(i, av([("mon", "09:00", "21:00"), ("tue", "09:00", "21:00")]), f"2026-11-05T0{i}:00:00")
              for i in range(1, 5)]
    members = [member(10, av([("mon", "09:00", "21:00"), ("tue", "09:00", "21:00")])),
               member(11, av([("mon", "09:00", "21:00"), ("tue", "09:00", "21:00")]))]
    slots, _ = propose(people, members, WEEK, [], NOW, RULES)
    loads = [sum(1 for s in slots if s.member_id == m) for m in (10, 11)]
    assert loads == [2, 2]


def test_stale_and_expired_participants_are_reported_not_scheduled():
    members = [member(10, av([("mon", "09:00", "21:00")]))]
    stale = person(1, av([("mon", "19:00", "21:00")]), "2026-04-09T15:59:48")
    expired = person(2, av(dated=[("2026-11-10", "12:00", "20:00")]))
    slots, unmatched = propose([stale, expired], members, WEEK, [], NOW, RULES)
    assert slots == []
    reasons = {p["id"]: r for p, r in unmatched}
    assert "days ago" in reasons[1] and "specific dates" in reasons[2]


def test_min_notice_existing_sessions_and_blocked_pairs():
    p = person(1, av([("mon", "09:00", "13:00")]))
    m = member(10, av([("mon", "09:00", "13:00")]))
    late = datetime(2026, 11, 29, 20, 0)  # Sunday night: Monday 9am is under 36h away
    assert propose([p], [m], WEEK, [], late, RULES)[0] == []
    held = [Slot(99, 10, datetime(2026, 11, 30, 9, 0), datetime(2026, 11, 30, 11, 0))]
    assert propose([p], [m], WEEK, held, NOW, RULES)[0] == []  # 11:15 + 2h does not fit before 13:00
    assert propose([p], [m], WEEK, [], NOW, RULES, frozenset({(1, 10)}))[0] == []
    assert len(propose([p], [m], WEEK, [], NOW, RULES)[0]) == 1
