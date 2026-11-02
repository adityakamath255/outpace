from dataclasses import replace
from datetime import date, datetime
from fractions import Fraction

import pytest

from todo.model import (
    Day,
    Interval,
    Section,
    Task,
    duration,
    empty_day,
    number,
    parse_interval,
    per_point,
    percentage,
    points,
    ratio,
    score,
    time_progress,
)

DAY = date(2026, 11, 1)


def test_points_and_estimates_are_independent():
    task = Task("Practice", points("4"), points("0.75"), minutes=40)
    edited = replace(task, minutes=10)
    assert edited.earned == task.earned
    assert score((edited, Task("Note", None, checked=True))) == (Fraction(3, 4), 4)
    assert per_point(task.minutes * 60, task.available) == "10:00"


def test_decimal_points_round_trip_without_display_loss():
    for text in ("0.000001", "12345678901234567890.125", "2.5"):
        assert number(points(text)) == text
    assert points("0.1") + points("0.2") == points("0.3")


def test_decimal_formatting_preserves_digits_after_fraction_reduction():
    text = "0.00000000000000088817841970012523233890533447265625"
    assert number(points(text)) == text


def test_day_requires_a_section_for_adding_tasks():
    with pytest.raises(ValueError, match="section"):
        Day(DAY, (), ())


@pytest.mark.parametrize("text", ["-1", "nan", "inf", "1/2", ""])
def test_points_reject_nondecimal_inputs(text):
    with pytest.raises(ValueError):
        points(text)


def test_invalid_task_scores_are_rejected_instead_of_disappearing_from_total():
    with pytest.raises(ValueError):
        Task("Practice", points("2"), points("2.5"))
    with pytest.raises(ValueError):
        Task("Practice", points("0"))
    with pytest.raises(ValueError):
        Task("Note", None, points("1"))


def test_schedule_pauses_between_intervals_and_stops_at_end():
    day = empty_day(DAY)
    for hour in (11, 12, 16):
        assert time_progress(day.intervals, datetime(2026, 11, 1, hour)) == (
            12600,
            37800,
        )
    assert time_progress(day.intervals, datetime(2026, 11, 2)) == (37800, 37800)
    assert time_progress(day.intervals, datetime(2026, 11, 1, 6)) == (0, 37800)


def test_overnight_schedule_belongs_to_its_original_day():
    interval = Interval(datetime(2026, 11, 1, 23), datetime(2026, 11, 2, 1))
    assert time_progress((interval,), datetime(2026, 11, 2, 0, 30)) == (5400, 7200)
    with pytest.raises(ValueError, match="overlap"):
        Day(DAY, (interval, interval), (Section("Tasks"),))


def test_interval_parser_accepts_clock_formats_and_outer_whitespace():
    assert parse_interval(DAY, " 7:30 ", "11:00") == Interval(
        datetime(2026, 11, 1, 7, 30), datetime(2026, 11, 1, 11)
    )


@pytest.mark.parametrize("next_day", [False, True])
def test_interval_parser_anchors_overnight_intervals(next_day):
    assert parse_interval(DAY, "23:00", "01:00", next_day=next_day) == Interval(
        datetime(2026, 11, 1 + next_day, 23),
        datetime(2026, 11, 2 + next_day, 1),
    )


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("7:3", "08:00"),
        ("07:30", "bad"),
        ("24:00", "01:00"),
        ("07:60", "09:00"),
        ("07:30", "7:30"),
    ],
)
def test_interval_parser_rejects_invalid_clocks_and_equal_endpoints(start, end):
    with pytest.raises(ValueError):
        parse_interval(DAY, start, end)


@pytest.mark.parametrize(
    ("start", "end", "next_day"),
    [("23:00", "01:00", False), ("07:30", "11:00", True)],
)
def test_interval_parser_reports_unrepresentable_dates(start, end, next_day):
    with pytest.raises(ValueError, match="date range"):
        parse_interval(date.max, start, end, next_day=next_day)


def test_day_accepts_touching_intervals_and_rejects_partial_overlap():
    first = parse_interval(DAY, "07:30", "11:00")
    touching = parse_interval(DAY, "11:00", "12:00")
    day = Day(DAY, (touching, first), (Section("Tasks"),))
    assert time_progress(day.intervals, datetime(2026, 11, 1, 12)) == (16200, 16200)
    with pytest.raises(ValueError, match="overlap"):
        replace(day, intervals=(first, parse_interval(DAY, "10:59", "12:00")))


def test_report_matches_the_original_calculations():
    earned, available = Fraction(18), Fraction(42)
    elapsed, total = 12600, 37800
    assert percentage(ratio(earned, available)) == "42.85"
    assert percentage(ratio(elapsed, total)) == "33.33"
    assert (
        percentage(ratio(earned, available) - ratio(elapsed, total), signed=True)
        == "+9.52"
    )
    assert duration(total) == "10:30:00"
    assert per_point(total, available) == "15:00"
    assert per_point(total - elapsed, available - earned) == "17:30"
    assert percentage(ratio(0, 0)) == "n/a"
    assert per_point(0, Fraction(0)) == "n/a"


def test_edit_move_remove_and_complete_preserve_task_identity():
    first = Task("Same name", points("3"), points("1"))
    second = Task("Same name", points("2"))
    section = Section("One", (first, second))
    destination = Section("Two")
    day = Day(DAY, (), (section, destination))
    moved = day.move_task(first.id, 1)
    assert [task.id for task in moved.sections[0].tasks] == [second.id, first.id]
    completed = first.toggle()
    assert completed.complete and completed.id == first.id
    changed = day.put_task(completed, destination.id)
    assert changed.sections[0].tasks == (second,)
    assert changed.sections[1].tasks == (completed,)
    assert score(task for _, task in changed.remove_task(first.id).tasks()) == (0, 2)
    assert first.earned == 1


def test_unscored_completion_does_not_award_points():
    task = Task("Remember", None).toggle()
    assert task.complete
    assert score((task,)) == (0, 0)
