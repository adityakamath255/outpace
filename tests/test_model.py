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
    number,
    parse_clock,
    parse_minutes,
    per_point,
    percentage,
    points,
    schedule,
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


def test_copy_day_preserves_the_plan_and_resets_progress_with_new_identities():
    source = Day(
        DAY,
        schedule([(1380, 60), (90, 120)]),
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
    for original, task in zip(source.tasks(), copied.tasks(), strict=True):
        assert task == replace(original, id=task.id, earned=Fraction(0))
    original_ids = {s.id for s in source.sections} | {t.id for t in source.tasks()}
    copied_ids = {s.id for s in copied.sections} | {t.id for t in copied.tasks()}
    assert original_ids.isdisjoint(copied_ids)
    assert next(source.tasks()).earned == points("0.75")


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
    intervals = schedule([(450, 660), (960, 1380)])
    for hour in (11, 12, 16):
        assert time_progress(intervals, hour * HOUR) == (
            12600,
            37800,
        )
    assert time_progress(intervals, 24 * HOUR) == (37800, 37800)
    assert time_progress(intervals, 6 * HOUR) == (0, 37800)


def test_overnight_schedule_belongs_to_its_original_day():
    (interval,) = schedule([(1380, 60)])
    assert time_progress((interval,), 24 * HOUR + 1800) == (5400, 7200)


@pytest.mark.parametrize(
    ("text", "minutes"),
    [
        (" 7:30 ", 450),
        ("0730", 450),
        ("730", 450),
        ("7", 420),
        ("15", 900),
        ("23:59", 1439),
    ],
)
def test_clock_accepts_times_with_or_without_the_colon(text, minutes):
    assert parse_clock(text) == minutes


@pytest.mark.parametrize("text", ["", "7:3", "7:", "24", "9:75", "12345"])
def test_clock_rejects_other_times(text):
    with pytest.raises(ValueError, match="24-hour time"):
        parse_clock(text)


def test_schedule_continues_past_midnight_in_order():
    intervals = schedule([(960, 1380), (1410, 30), (60, 90)])
    assert intervals == (
        Interval(960, 1380),
        Interval(1410, 1470),
        Interval(1500, 1530),
    )
    assert schedule([]) == ()


def test_schedule_rejects_intervals_out_of_order():
    # the second interval would land on the next day
    with pytest.raises(ValueError, match="24 hours"):
        schedule([(450, 660), (630, 720)])


def test_day_accepts_touching_intervals_and_rejects_partial_overlap():
    first, touching = schedule([(450, 660), (660, 720)])
    day = Day(DAY, (touching, first), (Section("Tasks"),))
    assert time_progress(day.intervals, 12 * HOUR) == (16200, 16200)
    with pytest.raises(ValueError, match="overlap"):
        replace(day, intervals=(first, Interval(10 * 60 + 59, 12 * 60)))


def test_report_matches_the_original_calculations():
    earned, available = Fraction(18), Fraction(42)
    elapsed, total = 12600, 37800
    assert percentage(earned / available) == "42.85"
    assert percentage(Fraction(elapsed, total)) == "33.33"
    assert per_point(total, available) == 15 * 60
    assert per_point(total - elapsed, available - earned) == 17 * 60 + 30
    assert per_point(0, Fraction(0)) is None
    # unscored tasks count toward neither
    practice = Task("Practice", points("4"), points("0.75"))
    assert score((practice, Task("Note", None))) == (Fraction(3, 4), 4)


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
    assert score(changed.remove_task(first.id).tasks()) == (0, 2)
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
