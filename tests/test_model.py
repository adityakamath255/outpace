from dataclasses import replace
from datetime import date, datetime, timedelta
from fractions import Fraction

import pytest

from todo.model import (
    Day,
    Interval,
    Section,
    Span,
    Tally,
    Task,
    days_text,
    empty_day,
    number,
    on_pace_at,
    pace_at,
    parse_clock,
    parse_days,
    parse_minutes,
    per_point,
    percentage,
    points,
    schedule,
    score,
    seconds_since,
    tallied,
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
    # the second interval would start on the next day
    with pytest.raises(ValueError, match="24 hours"):
        schedule([(450, 660), (630, 720)])


def test_day_accepts_touching_intervals_and_rejects_overlap_or_over_a_day():
    first, touching = schedule([(450, 660), (660, 720)])
    day = Day(DAY, (touching, first), (Section("Tasks"),))
    assert time_progress(day.intervals, 12 * HOUR) == (16200, 16200)
    with pytest.raises(ValueError, match="overlap"):
        replace(day, intervals=(first, Interval(10 * 60 + 59, 12 * 60)))
    # the limit schedule() keeps, held by every day however it is built
    assert replace(day, intervals=(Interval(0, 24 * 60),)).intervals
    with pytest.raises(ValueError, match="24 hours"):
        replace(day, intervals=(first, Interval(24 * 60, 48 * 60)))


def test_a_span_counts_all_its_time_and_must_end_after_it_starts():
    start = datetime(2026, 11, 6, 10, 30)
    span = Span("november", start, datetime(2026, 12, 1), (Section(""),))
    now = datetime(2026, 11, 7, 10, 30)
    total = (24 * 24 + 13) * HOUR + 30 * 60
    assert time_progress(span.intervals, seconds_since(span.origin, now)) == (
        24 * HOUR,
        total,
    )
    with pytest.raises(ValueError, match="end after it starts"):
        replace(span, end=start)
    # the store keeps minutes, so seconds would not survive a save
    with pytest.raises(ValueError, match="to the minute"):
        replace(span, end=datetime(2026, 12, 1, 0, 0, 30))


def test_days_off_leave_a_span_whole_days_clipped_to_its_ends():
    span = Span(
        "november",
        datetime(2026, 11, 6, 10, 30),
        datetime(2026, 11, 12, 6),
        (Section(""),),
        off=(
            date(2026, 11, 12),
            date(2026, 11, 8),
            date(2026, 11, 9),
            date(2026, 11, 6),
        ),
    )
    assert span.off == (
        date(2026, 11, 6),
        date(2026, 11, 8),
        date(2026, 11, 9),
        date(2026, 11, 12),
    )
    # minutes from 11-06 10:30: 11-07, then 11-10 and 11-11
    assert span.intervals == (
        Interval(13 * 60 + 30, 37 * 60 + 30),
        Interval(85 * 60 + 30, 133 * 60 + 30),
    )
    assert (
        replace(span, off=tuple(date(2026, 11, d) for d in range(6, 13))).intervals
        == ()
    )
    with pytest.raises(ValueError, match="within the span"):
        replace(span, off=(date(2026, 11, 5),))


def test_days_off_read_dates_and_ranges_and_print_runs_as_ranges():
    days = parse_days(" 2026-11-20, 2026-11-25 - 2026-11-27,2026-11-21 ")
    assert days == tuple(date(2026, 11, d) for d in (20, 21, 25, 26, 27))
    assert days_text(days) == "2026-11-20 - 2026-11-21, 2026-11-25 - 2026-11-27"
    assert parse_days(" ") == ()
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        parse_days("2026-11-20, 11-21")
    with pytest.raises(ValueError, match="after it starts"):
        parse_days("2026-11-27 - 2026-11-25")


def test_a_tally_is_kept_only_when_the_points_change():
    day = replace(empty_day(DAY), sections=(Section("", (Task("a", points("2")),)),))
    at = datetime(2026, 11, 1, 9, 0, 0, 500)
    counted = tallied(day, at)
    assert counted.tallies == (Tally(at, Fraction(0), Fraction(2)),)
    assert tallied(counted, at + timedelta(hours=1)) == counted
    # a list without points has nothing to graph
    empty = empty_day(DAY)
    assert tallied(empty, at) == empty


def test_a_clock_set_back_stamps_the_tally_at_the_last_one():
    day = replace(empty_day(DAY), sections=(Section("", (Task("a", points("2")),)),))
    at = datetime(2026, 11, 1, 9)
    counted = tallied(day, at)
    done = replace(counted, sections=(Section("", (next(counted.tasks()).toggle(),)),))
    assert tallied(done, at - timedelta(minutes=5)).tallies[-1] == Tally(
        at, Fraction(2), Fraction(2)
    )


def test_pace_at_a_moment_comes_from_the_last_tally_against_the_schedule():
    tallies = (
        Tally(datetime(2026, 11, 1, 7), Fraction(1), Fraction(4)),
        Tally(datetime(2026, 11, 1, 10), Fraction(3), Fraction(4)),
        Tally(datetime(2026, 11, 1, 11), Fraction(0), Fraction(0)),
    )
    day = Day(DAY, (Interval(8 * 60, 12 * 60),), (Section(""),), tallies)
    hours = [6, 8, 9, 10]
    assert [pace_at(day, datetime(2026, 11, 1, h)) for h in hours] == [
        None,
        HOUR,
        0,
        HOUR,
    ]
    # no points left to earn
    assert pace_at(day, datetime(2026, 11, 1, 11, 30)) is None
    after = replace(day, tallies=tallies[:2])
    assert pace_at(after, datetime(2026, 11, 1, 13)) == -HOUR
    assert pace_at(replace(after, intervals=()), datetime(2026, 11, 1, 9)) is None


def test_on_pace_points_follow_the_total_and_the_schedule():
    tallies = (
        Tally(datetime(2026, 11, 1, 7), Fraction(0), Fraction(4)),
        Tally(datetime(2026, 11, 1, 9), Fraction(1), Fraction(8)),
    )
    # four scheduled hours, with a break from 10:00 to 11:00
    intervals = (Interval(8 * 60, 10 * 60), Interval(11 * 60, 13 * 60))
    day = Day(DAY, intervals, (Section(""),), tallies)
    moments = [(6, 0), (8, 30), (10, 0), (10, 30), (12, 0)]
    assert [on_pace_at(day, datetime(2026, 11, 1, h, m)) for h, m in moments] == [
        None,
        # the total before 9:00, then the latest one, through the break and on past
        # the last tally
        Fraction(1, 2),
        4,
        4,
        6,
    ]


def test_percentages_truncate_and_per_point_needs_points():
    assert percentage(Fraction(18, 42)) == "42.85"
    assert percentage(Fraction(12600, 37800)) == "33.33"
    assert per_point(37800, Fraction(42)) == 15 * 60
    assert per_point(0, Fraction(0)) is None


def test_score_leaves_out_unscored_tasks():
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
