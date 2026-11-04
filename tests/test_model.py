from dataclasses import replace
from datetime import date
from fractions import Fraction

import pytest

from todo.model import (
    Day,
    Interval,
    Section,
    Task,
    empty_day,
    format_schedule,
    number,
    parse_minutes,
    parse_schedule,
    per_point,
    percentage,
    points,
    ratio,
    score,
    time_progress,
)

DAY = date(2026, 11, 1)
HOUR = 3600


@pytest.mark.parametrize(
    ("text", "minutes"),
    [
        (" \t ", None),
        (" 40 ", 40),
        ("1h30m", 90),
        ("1H 30M", 90),
        ("2h", 120),
        ("45m", 45),
    ],
)
def test_duration_parser_accepts_minutes_or_hours_and_minutes(text, minutes):
    assert parse_minutes(text) == minutes


@pytest.mark.parametrize("text", ["1.5", "-1", "h", "1m30h", "1h30"])
def test_duration_parser_rejects_other_formats(text):
    with pytest.raises(
        ValueError,
        match=r"^Use minutes, like 90, or hours and minutes, like 1h30m\.$",
    ):
        parse_minutes(text)


def test_tasks_reject_nonpositive_durations():
    with pytest.raises(ValueError, match=r"^A duration must be positive\.$"):
        Task("Practice", None, minutes=0)


def test_durations_are_capped_at_a_day():
    assert Task("Practice", None, minutes=24 * 60).minutes == 24 * 60
    with pytest.raises(ValueError, match=r"^A duration cannot exceed 24 hours\.$"):
        Task("Practice", None, minutes=24 * 60 + 1)


def test_new_days_have_no_schedule():
    day = empty_day(DAY)
    assert day.intervals == ()
    assert time_progress(day.intervals, 12 * HOUR) == (0, 0)


def test_copy_day_preserves_the_plan_and_resets_progress_with_new_identities():
    source = Day(
        DAY,
        parse_schedule("23:00-01:00, 01:30-02:00"),
        (
            Section(
                "Tasks",
                (
                    Task("Practice", points("2.5"), points("0.75"), 20),
                    Task("Finished", points("1"), points("1")),
                    Task("Remember", None),
                ),
            ),
            Section("Empty"),
        ),
    )

    copied = source.copy_to(date(2026, 11, 3))

    assert copied.date == date(2026, 11, 3)
    assert copied.intervals == source.intervals
    assert [section.title for section in copied.sections] == ["Tasks", "Empty"]
    assert not copied.sections[1].tasks
    for (_, original), (_, task) in zip(source.tasks(), copied.tasks(), strict=True):
        assert task == replace(original, id=task.id, earned=Fraction(0))
    original_ids = {s.id for s in source.sections} | {t.id for _, t in source.tasks()}
    copied_ids = {s.id for s in copied.sections} | {t.id for _, t in copied.tasks()}
    assert original_ids.isdisjoint(copied_ids)
    assert next(source.tasks())[1].earned == points("0.75")


def test_rename_section_preserves_other_day_data_and_allows_duplicate_titles():
    first = Section("One", (Task("Practice", points("2"), points("1")),))
    second = Section("Two", (Task("Note", None),))
    day = replace(empty_day(DAY), sections=(first, second))

    renamed = day.rename_section(first.id, " Two ")

    assert renamed == replace(day, sections=(replace(first, title="Two"), second))
    assert day.sections == (first, second)


def test_blank_section_titles_mean_untitled_and_unknown_ids_are_rejected():
    day = empty_day(DAY)
    assert day.sections[0].title == ""
    named = day.rename_section(day.sections[0].id, "Work")
    assert named.rename_section(day.sections[0].id, " \t ").sections[0].title == ""
    with pytest.raises(ValueError, match="section"):
        day.rename_section("missing", "Work")


def test_points_and_estimates_are_independent():
    task = Task("Practice", points("4"), points("0.75"), minutes=40)
    edited = replace(task, minutes=10)
    assert edited.earned == task.earned
    assert score((edited, Task("Note", None))) == (Fraction(3, 4), 4)
    assert per_point(task.minutes * 60, task.available) == 600


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
    intervals = parse_schedule("07:30-11:00, 16:00-23:00")
    for hour in (11, 12, 16):
        assert time_progress(intervals, hour * HOUR) == (
            12600,
            37800,
        )
    assert time_progress(intervals, 24 * HOUR) == (37800, 37800)
    assert time_progress(intervals, 6 * HOUR) == (0, 37800)


def test_overnight_schedule_belongs_to_its_original_day():
    (interval,) = parse_schedule("23:00-01:00")
    assert time_progress((interval,), 24 * HOUR + 1800) == (5400, 7200)
    with pytest.raises(ValueError, match="overlap"):
        Day(DAY, (interval, interval), (Section("Tasks"),))


def test_schedule_parser_accepts_short_clocks_and_whitespace():
    assert parse_schedule(" 7:30 - 11:00 ,") == (Interval(450, 660),)
    assert parse_schedule("  ") == ()


def test_schedule_continues_past_midnight_in_order():
    text = "16:00 - 23:00, 23:30 - 00:30, 01:00 - 01:30"
    intervals = parse_schedule(text)
    assert intervals == (
        Interval(960, 1380),
        Interval(1410, 1470),
        Interval(1500, 1530),
    )
    assert format_schedule(intervals) == text


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("7:3-08:00", "24-hour time"),
        ("07:30-bad", "24-hour time"),
        ("24:00-01:00", "24-hour time"),
        ("9:75-10:00", "24-hour time"),
        ("07:30", "start - end"),
        ("07:30-7:30", "identical"),
        # out of order, so the second interval would land on the next day
        ("07:30-11:00, 10:30-12:00", "24 hours"),
    ],
)
def test_schedule_parser_explains_invalid_schedules(text, message):
    with pytest.raises(ValueError, match=message):
        parse_schedule(text)


def test_day_accepts_touching_intervals_and_rejects_partial_overlap():
    first, touching = parse_schedule("07:30-11:00, 11:00-12:00")
    day = Day(DAY, (touching, first), (Section("Tasks"),))
    assert time_progress(day.intervals, 12 * HOUR) == (16200, 16200)
    with pytest.raises(ValueError, match="overlap"):
        replace(day, intervals=(first, Interval(10 * 60 + 59, 12 * 60)))


def test_report_matches_the_original_calculations():
    earned, available = Fraction(18), Fraction(42)
    elapsed, total = 12600, 37800
    assert percentage(ratio(earned, available)) == "42.85"
    assert percentage(ratio(elapsed, total)) == "33.33"
    assert percentage(ratio(earned, available) - ratio(elapsed, total)) == "9.52"
    assert per_point(total, available) == 15 * 60
    assert per_point(total - elapsed, available - earned) == 17 * 60 + 30
    assert per_point(0, Fraction(0)) is None


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
    changed = day.replace_task(completed)
    assert changed.sections[0].tasks == (completed, second)
    assert score(task for _, task in changed.remove_task(first.id).tasks()) == (0, 2)
    assert first.earned == 1


def test_unscored_tasks_have_no_done_state():
    task = Task("Remember", None)
    assert not task.complete
    for change in (task.toggle, lambda: task.step(1)):
        with pytest.raises(ValueError, match=r"^Unscored tasks have no done state\.$"):
            change()


def test_step_changes_earned_points_by_one_within_bounds():
    task = Task("Read", points("2.5"), points("2"))
    assert task.step(1).earned == points("2.5")
    assert task.step(1).step(1).earned == points("2.5")
    assert task.step(-1).earned == 1
    assert Task("Read", points("3")).step(-1).earned == 0


def test_tasks_move_across_section_boundaries():
    a, b, c = (Task(name, None) for name in "abc")
    first, second = Section("", (a, b)), Section("", (c,))
    day = Day(DAY, (), (first, second))

    def titles(day):
        return [[task.title for task in section.tasks] for section in day.sections]

    assert titles(day.move_task(b.id, 1)) == [["a"], ["b", "c"]]
    assert titles(day.move_task(c.id, -1)) == [["a", "b", "c"], []]
    assert day.move_task(a.id, -1) == day
    assert day.move_task(c.id, 1) == day


def test_sections_insert_move_and_remove():
    task = Task("Practice", points("2"))
    first, second = Section("One", (task,)), Section("Two")
    day = Day(DAY, (), (first, second))
    new = Section("")
    assert day.insert_section(new, 1).sections == (first, new, second)
    assert day.move_section(first.id, 1).sections == (second, first)
    assert day.move_section(first.id, -1) == day
    assert day.remove_section(first.id).sections == (second,)
    only = Day(DAY, (), (first,))
    assert only.remove_section(first.id).sections == (replace(first, tasks=()),)


def test_editing_a_task_keeps_completion_and_caps_partial_progress():
    partial = Task("Read", points("4"), points("3"))
    assert partial.edit("Read", points("2"), None).earned == 2
    assert partial.edit("Read", points("5"), None).earned == 3
    done = Task("Read", points("2"), points("2"))
    assert done.edit("Read", points("5"), 30).complete
    assert Task("Remember", None).edit("Remember", points("3"), None).earned == 0
    assert partial.edit("Read", None, None) == replace(
        partial, available=None, earned=Fraction(0)
    )


def test_titles_are_trimmed_by_the_model():
    assert Task("  Read ", None).title == "Read"
    assert Section(" Work ").title == "Work"
