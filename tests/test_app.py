import asyncio
import sqlite3
import sys
from contextlib import closing
from dataclasses import replace
from datetime import date, datetime
from io import StringIO

import pytest
from rich.console import Console
from textual.screen import ModalScreen
from textual.widgets import Footer, Input, Static

from todo.app import (
    COLUMNS,
    SectionRule,
    Sort,
    TodoApp,
    arrange,
    duration,
    main,
)
from todo.db import Store
from todo.model import Interval, Section, Task, empty_day, parse_schedule, points

SCHEDULE = parse_schedule("07:30-11:00, 16:00-23:00")


def stats_text(app):
    output = StringIO()
    Console(file=output, width=60).print(app.query_one("#stats", Static).content)
    return output.getvalue()


async def add_task(pilot, title):
    await pilot.press("o")
    pilot.app.screen.query_one("#title", Input).value = title
    await pilot.press("enter", "escape")


def line_of(text, label):
    """The stats row a grid label starts; labels are padded by two spaces."""
    return next(row for row in text.splitlines() if row.startswith(f"{label}  "))


def footer_action(app, action):
    return next(key for key in app.query_one(Footer).children if key.action == action)


def test_section_share_is_not_truncated_and_is_hidden_at_all_points():
    section = Section("Tasks", (Task("Practice", points("5")),))

    def rule(total):
        output = StringIO()
        Console(file=output, width=44).print(SectionRule(section, total))
        return output.getvalue()

    assert "50.00% of total" in rule(points("10"))
    assert "of total" not in rule(points("5"))


def test_clock_callback_does_not_touch_widgets_after_shutdown(app):
    async def stop_then_tick():
        async with app.run_test():
            assert app.is_running
        app.refresh_stats()

    asyncio.run(stop_then_tick())


@pytest.fixture
def app(tmp_path):
    day = replace(
        empty_day(date(2026, 11, 1)),
        intervals=SCHEDULE,
        sections=(Section("Tasks", (Task("Practice", points("5"), points("2"), 30),)),),
    )
    with closing(Store(tmp_path / "todo.sqlite3")) as store:
        store.save(day)
        yield TodoApp(store, day.date)


def test_browsing_days_does_not_show_notifications(app, monkeypatch):
    messages = []
    monkeypatch.setattr(
        app, "notify", lambda message, **kwargs: messages.append(message)
    )

    async def browse():
        async with app.run_test() as pilot:
            await pilot.press("]", "]", "[", "[")
            assert messages == []

    asyncio.run(browse())


def test_task_changes_save_and_undo(app):
    async def exercise_tasks():
        async with app.run_test(size=(110, 45)) as pilot:
            await pilot.pause()
            assert await pilot.click(footer_action(app, "add(True)"))
            await pilot.press(*"2.5", "tab", *"20", "tab", *"read daily", "enter")
            added = next(task for task in app.day.tasks() if task.title == "read daily")
            assert (added.available, added.earned, added.minutes) == (
                points("2.5"),
                0,
                20,
            )
            # adding stays open with empty fields for the next task
            assert isinstance(app.screen, ModalScreen)
            assert app.screen.query_one("#title", Input).value == ""
            await pilot.press("escape")

            await pilot.press("enter")
            await pilot.press(*"2.5", "enter")
            assert next(
                task for task in app.day.tasks() if task.id == added.id
            ).complete

            before_cancel = app.day
            await pilot.press("e")
            app.screen.query_one("#title", Input).value = "Discarded edit"
            await pilot.press("escape")
            assert app.day == before_cancel
            await pilot.press("e")
            app.screen.query_one("#minutes", Input).value = "35"
            await pilot.press("enter")
            edited = next(task for task in app.day.tasks() if task.id == added.id)
            assert edited.minutes == 35
            assert edited.earned == points("2.5")

            before_move = app.day
            await pilot.press("K")
            assert app.day.sections[0].tasks[0].id == added.id
            await pilot.press("u")
            assert app.day == before_move

            await pilot.press("space")
            assert (
                next(task for task in app.day.tasks() if task.id == added.id).earned
                == 0
            )
            await pilot.press("u")
            assert app.day == before_move

            assert await pilot.click(footer_action(app, "delete"))
            assert all(task.id != added.id for task in app.day.tasks())
            await pilot.press("u")
            assert app.day == before_move

        assert app.store.load(app.day.date) == app.day

    asyncio.run(exercise_tasks())


def test_unscored_tasks_cannot_be_marked_done(app, monkeypatch):
    messages = []
    monkeypatch.setattr(
        app, "notify", lambda message, **kwargs: messages.append(message)
    )

    async def try_to_complete():
        async with app.run_test() as pilot:
            await add_task(pilot, "Remember")
            before = app.day
            await pilot.press("enter", "space", "plus")
            assert app.day == before
            assert not isinstance(app.screen, ModalScreen)
            assert messages == ["Unscored tasks have no done state."] * 3

    asyncio.run(try_to_complete())


@pytest.mark.parametrize("existing", [False, True])
def test_reuse_copies_a_saved_day_and_undo_restores_the_destination(app, existing):
    source = app.day
    destination = empty_day(date(2026, 11, 2))
    if existing:
        destination = destination.insert_task(
            Task("Keep this plan", None), destination.sections[0].id, 0
        )
        app.store.save(destination)

    async def reuse_day():
        async with app.run_test() as pilot:
            await pilot.press("]")
            original = app.day
            await pilot.press("c")
            # the most recent earlier saved day is offered first
            assert app.screen.query_one(Input).value == source.date.isoformat()
            await pilot.press("enter")
            assert not isinstance(app.screen, ModalScreen)
            copied = app.day
            assert copied.date == destination.date
            assert next(copied.tasks()).earned == 0
            assert copied.intervals == source.intervals
            assert app.store.load(source.date) == source
            assert app.store.load(copied.date) == copied
            await pilot.press("u")
            assert app.day == original
            restored = app.store.load(original.date)
            assert (restored == original) if existing else restored.blank
            assert app.store.load(source.date) == source

    asyncio.run(reuse_day())


def test_failed_save_keeps_form_and_does_not_change_history(app, monkeypatch):
    original = app.day

    def fail(day):
        raise sqlite3.OperationalError("disk I/O error")

    async def fail_save():
        async with app.run_test(size=(110, 45)) as pilot:
            await pilot.press("enter")
            app.screen.query_one(Input).value = "2.5"
            with monkeypatch.context() as patch:
                patch.setattr(app.store, "save", fail)
                await pilot.press("enter")
            assert isinstance(app.screen, ModalScreen)
            assert app.screen.query_one(Input).value == "2.5"
            assert "disk I/O" in str(app.screen.query_one("#error", Static).content)
            assert app.day == original
            assert not app.histories.get(app.day.date)
            assert app.store.load(original.date) == original

    asyncio.run(fail_save())


def test_selected_sections_are_renamed_and_blank_means_untitled(app):
    empty = Section("Tasks")
    original = replace(app.day, sections=app.day.sections + (empty,))
    app.store.save(original)
    app = TodoApp(app.store, original.date)

    async def rename_sections():
        async with app.run_test(size=(110, 45)) as pilot:
            # Enter on a rule does nothing; e renames the section
            await pilot.press("g", "enter")
            assert not isinstance(app.screen, ModalScreen)
            await pilot.press("e", "ctrl+u", *"Work", "enter")
            assert app.day.sections[0].title == "Work"
            assert app.day.sections[0].id == original.sections[0].id
            assert app.day.sections[0].tasks == original.sections[0].tasks
            assert app.store.load(original.date) == app.day
            await pilot.press("u")
            assert app.day == original

            # the last row is the empty section's rule; blank means untitled
            await pilot.press("G", "e", "ctrl+u", "enter")
            assert app.day.sections[0] == original.sections[0]
            assert app.day.sections[1] == replace(empty, title="")
            assert app.store.load(original.date) == app.day
            await pilot.press("u")
            assert app.day == original
            assert app.store.load(original.date) == original

    asyncio.run(rename_sections())


def test_x_deletes_without_a_dialog_and_undo_names_the_change(app, monkeypatch):
    original = app.day
    messages = []
    monkeypatch.setattr(
        app, "notify", lambda message, **kwargs: messages.append(message)
    )

    async def delete_and_undo():
        async with app.run_test() as pilot:
            # d is unbound, so a Vim habit of dd deletes nothing
            await pilot.press("d", "d")
            assert app.day == original
            await pilot.press("x")
            assert not isinstance(app.screen, ModalScreen)
            assert not list(app.day.tasks())
            assert app.store.load(original.date) == app.day
            await pilot.press("u")
            assert app.day == original
            assert app.store.load(original.date) == original
            assert messages == ["Undid: deleted Practice"]

            await add_task(pilot, "Second")
            await pilot.press("k")
            practice = app.selected()[1]
            await pilot.press("x")
            assert app.selected()[1].title == "Second"
            await pilot.press("u")
            assert app.selected()[1] == practice

    asyncio.run(delete_and_undo())


def test_plus_and_minus_step_points_and_undo_names_the_change(app, monkeypatch):
    messages = []
    monkeypatch.setattr(
        app, "notify", lambda message, **kwargs: messages.append(message)
    )

    async def step():
        async with app.run_test() as pilot:
            await pilot.press("plus")
            assert app.selected()[1].earned == 3
            await pilot.press("minus", "minus", "minus", "minus")
            assert app.selected()[1].earned == 0
            assert len(app.histories[app.day.date]) == 4
            await pilot.press("u")
            assert app.selected()[1].earned == 1
            assert messages == ["Undid: changed Practice from 1/5 to 0/5"]

    asyncio.run(step())


def test_help_lists_every_key_binding(app):
    from todo.app import HELP, VimList

    documented = {key for _, rows in HELP for keys, _ in rows for key in keys.split()}
    bindings = [*TodoApp.BINDINGS, *VimList.BINDINGS]
    displays = {
        app.get_key_display(binding.with_key(key))
        for binding in bindings
        for key in binding.key.split(",")
    }
    assert displays <= documented

    async def open_and_close():
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("question_mark")
            assert isinstance(app.screen, ModalScreen)
            await pilot.press("o")
            assert not list(app.screen.query("#title"))
            await pilot.press("escape")
            assert not isinstance(app.screen, ModalScreen)

    asyncio.run(open_and_close())


def test_an_ended_schedule_has_no_time_left_per_point(app, monkeypatch):
    class Clock:
        @staticmethod
        def now():
            return datetime(2026, 11, 2, 8)

    monkeypatch.setattr("todo.app.datetime", Clock)

    async def check_ended_schedule():
        async with app.run_test():
            assert "left / pt  -" in stats_text(app)

    asyncio.run(check_ended_schedule())


def test_day_switching_preserves_lists_and_creates_new_days(app):
    original = app.day
    earlier = replace(
        empty_day(date(2026, 10, 31)),
        intervals=parse_schedule("23:00-01:00"),
        sections=(Section("Earlier", (Task("Previous task", points("1")),)),),
    )
    app.store.save(earlier)

    async def switch_days():
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("space")
            edited = app.day
            assert app.histories[app.day.date]
            await pilot.press("[")
            assert app.day == earlier
            assert not app.histories.get(app.day.date)
            assert earlier.date.isoformat() in str(app.query_one("#date").content)
            assert app.store.load(original.date) == edited

            await pilot.press("]")
            assert app.day == edited
            await pilot.press("]")
            tomorrow = app.day
            assert tomorrow.date == date(2026, 11, 2)
            assert not list(tomorrow.tasks())
            assert tomorrow.intervals == ()
            assert app.store.load(tomorrow.date).blank
            await add_task(pilot, "New task")
            assert app.store.load(tomorrow.date) == app.day
            assert next(app.day.tasks()).title == "New task"

            await pilot.press("T", *original.date.isoformat(), "enter")
            assert not isinstance(app.screen, ModalScreen)
            assert app.day == edited
            assert app.focused.id == "tasks"
            assert app.store.load(earlier.date) == earlier
            assert app.store.load(original.date) == edited
            # undo history survives visiting other days
            await pilot.press("u")
            assert app.day == original
            assert app.store.load(original.date) == original

            await pilot.press("t")
            assert app.day.date == date.today()

    asyncio.run(switch_days())


def test_same_day_cancellation_and_invalid_dates_preserve_undo(app):
    async def keep_current_day():
        async with app.run_test(size=(60, 24)) as pilot:
            await pilot.press("space")
            current = app.day
            await pilot.press("T", "enter")
            assert app.day == current
            await pilot.press("T")
            for invalid in ("2026-02-30", "20261102"):
                app.screen.query_one(Input).value = invalid
                await pilot.press("enter")
                assert isinstance(app.screen, ModalScreen)
                assert app.screen.query_one(Input).value == invalid
                assert "YYYY-MM-DD" in str(
                    app.screen.query_one("#error", Static).content
                )
                assert app.day == current
            await pilot.press("escape")
            assert app.day == current

            await pilot.press("e", "[", "]", "t", "T")
            assert app.screen.query_one("#task-dialog")
            assert app.day == current
            await pilot.press("escape")

    asyncio.run(keep_current_day())


def test_failed_day_load_keeps_current_list_history_and_date_draft(app, monkeypatch):
    target = date(2026, 11, 2)

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("Unavailable database")

    async def fail_open():
        async with app.run_test() as pilot:
            await pilot.press("space")
            current = app.day
            await pilot.press("T")
            app.screen.query_one(Input).value = target.isoformat()
            with monkeypatch.context() as patch:
                patch.setattr(app.store, "load", fail)
                await pilot.press("enter")
            assert isinstance(app.screen, ModalScreen)
            assert app.screen.query_one(Input).value == target.isoformat()
            assert str(app.screen.query_one("#error", Static).content)
            assert app.day == current
            assert app.store.load(current.date) == current
            assert app.store.load(target).blank
            await pilot.press("enter")
            assert not isinstance(app.screen, ModalScreen)
            assert app.day.date == target
            assert not app.histories.get(app.day.date)

    asyncio.run(fail_open())


def test_previous_and_next_day_stop_at_supported_date_boundaries(app):
    async def check_boundaries():
        async with app.run_test() as pilot:
            app.switch_day(date.min)
            await pilot.press("[")
            assert app.day.date == date.min
            app.switch_day(date.max)
            await pilot.press("]")
            assert app.day.date == date.max

    asyncio.run(check_boundaries())


def test_schedule_edit_saves_recalculates_and_undoes(app):
    original = app.day

    async def edit_schedule():
        async with app.run_test(size=(110, 45)) as pilot:
            await pilot.press("at")
            field = app.screen.query_one(Input)
            assert field.value == "07:30 - 11:00, 16:00 - 23:00"
            field.value = "09:00-12:00, 23:00-01:00, 01:30-02:00"
            await pilot.press("enter")
            assert app.day.intervals == (
                Interval(9 * 60, 12 * 60),
                Interval(23 * 60, 25 * 60),
                Interval(25 * 60 + 30, 26 * 60),
            )
            assert app.day.sections == original.sections
            assert "5h30m" in stats_text(app)
            assert app.store.load(original.date) == app.day
            await pilot.press("u")
            assert app.day == original
            assert app.store.load(original.date) == original

    asyncio.run(edit_schedule())


def test_schedule_can_be_cleared_and_an_empty_schedule_edited(app):
    async def clear_schedule():
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("at", "ctrl+u", "enter")
            assert app.day.intervals == ()
            assert "schedule  @ set" in stats_text(app)
            assert app.store.load(app.day.date) == app.day
            await pilot.press("at", *"08:00-09:00", "enter")
            assert app.day.intervals == (Interval(8 * 60, 9 * 60),)

    asyncio.run(clear_schedule())


def test_messages_stack_up_to_five_and_drop_the_oldest(app):
    async def undo_repeatedly():
        async with app.run_test(notifications=True) as pilot:
            await pilot.press("plus", "plus", "plus", "u", "u", "u", "u", "u", "u")
            await pilot.pause()
            toasts = [str(toast.render()) for toast in app.screen.query("Toast")]
            assert len(toasts) == 5
            assert "Nothing to undo." in toasts[-1]
            assert all("4/5 to 5/5" not in toast for toast in toasts)

    asyncio.run(undo_repeatedly())


def test_keys_on_a_section_rule_act_on_the_section(app):
    original = app.day

    async def edit_sections():
        async with app.run_test(size=(120, 35)) as pilot:
            # Esc cancels a new section; a blank name makes it untitled
            await pilot.press("number_sign", "escape")
            assert app.day == original
            await pilot.press("number_sign", "enter")
            first, added = app.day.sections
            assert added.title == "" and not added.tasks
            assert app.selected() == (added, None)
            # K on a rule moves the whole section
            await pilot.press("K")
            assert app.day.sections == (added, first)
            # a task moved past the end of its section joins the next one
            await pilot.press("G", "K", "K")
            assert app.day.sections[0].tasks == first.tasks
            # x on a rule deletes the section with its tasks
            await pilot.press("g", "x")
            assert [s.id for s in app.day.sections] == [first.id]
            assert not list(app.day.tasks())
            await pilot.press("u", "u", "u", "u")
            assert app.day == original
            assert app.store.load(original.date) == original

    asyncio.run(edit_sections())


def test_lowering_available_points_caps_earned_points(app):
    async def lower_points():
        async with app.run_test() as pilot:
            await pilot.press("e")
            app.screen.query_one("#available", Input).value = "1"
            await pilot.press("enter")
            task = app.selected()[1]
            assert (task.earned, task.available) == (1, 1)

    asyncio.run(lower_points())


def test_deleting_a_sections_last_task_does_not_select_the_next_section(app):
    async def delete_twice():
        async with app.run_test(size=(110, 45)) as pilot:
            await pilot.press("number_sign", "enter")
            await add_task(pilot, "Next")
            await pilot.press("g", "j", "x")
            section, task = app.selected()
            assert task is None and section.id == app.day.sections[0].id

    asyncio.run(delete_twice())


def titles(day):
    return [[task.title for task in section.tasks] for section in day.sections]


def test_o_and_O_add_next_to_the_selected_row(app):
    async def add_tasks():
        async with app.run_test(size=(110, 45)) as pilot:
            # further tasks in one dialog follow the previous one
            await pilot.press("o")
            for title in ("Below", "Further"):
                app.screen.query_one("#title", Input).value = title
                await pilot.press("enter")
            await pilot.press("escape", "O")
            app.screen.query_one("#title", Input).value = "Above"
            await pilot.press("enter", "escape")
            assert titles(app.day) == [["Practice", "Below", "Above", "Further"]]
            # on a rule, o adds at the top of its section
            await pilot.press("g")
            await add_task(pilot, "First")
            assert titles(app.day)[0][0] == "First"
            assert app.store.load(app.day.date) == app.day

    asyncio.run(add_tasks())


def test_put_copies_the_last_deleted_row_and_resets_progress_on_other_days(app):
    practice = app.day.sections[0].tasks[0]

    def copies(day):
        return [replace(task, id=practice.id) for task in day.tasks()]

    async def put():
        async with app.run_test(size=(110, 45)) as pilot:
            await pilot.press("p")
            assert titles(app.day) == [["Practice"]]
            await pilot.press("x", "p", "P")
            assert copies(app.day) == [practice, practice]
            ids = [task.id for task in app.day.tasks()]
            assert len(set(ids)) == 2 and practice.id not in ids
            await pilot.press("]", "p")
            assert copies(app.day) == [replace(practice, earned=0)]
            # a deleted section comes back with its tasks below the current one
            await pilot.press("[", "number_sign", *"Other", "enter", "g", "x", "p")
            assert titles(app.day) == [[], ["Practice", "Practice"]]
            assert app.store.load(app.day.date) == app.day

    asyncio.run(put())


def test_a_delete_that_changes_nothing_keeps_the_row_to_put(app):
    async def delete_twice():
        async with app.run_test() as pilot:
            # the second x clears a section that is already empty
            await pilot.press("x", "x", "p")
            assert titles(app.day) == [["Practice"]]

    asyncio.run(delete_twice())


def test_braces_jump_between_sections_like_paragraphs(app):
    a, b, c = (
        Section("A", (Task("a1", None), Task("a2", None))),
        Section("B"),
        Section("C", (Task("c1", None),)),
    )
    app.store.save(replace(app.day, sections=(a, b, c)))
    app = TodoApp(app.store, app.day.date)

    async def jump():
        async with app.run_test() as pilot:
            visited = []
            for key in "}}}}{{{{":
                await pilot.press(key)
                section, task = app.selected()
                visited.append((task or section).title)
            assert visited == ["B", "C", "c1", "c1", "C", "B", "A", "A"]
            await pilot.press("G", "{")
            assert app.selected() == (c, None)

    asyncio.run(jump())


def test_the_cursor_never_rests_on_a_subtotal(app):
    a, b = Task("a", points("1")), Task("b", points("2"))
    first, second = Section("A", (a,)), Section("B", (b,))
    app.store.save(replace(app.day, sections=(first, second)))
    app = TodoApp(app.store, app.day.date)

    async def move():
        async with app.run_test() as pilot:
            # the done task vanishes from under the cursor, above A's subtotal
            await pilot.press("f", "space")
            section, task = app.selected()
            assert (section.id, task) == (first.id, None)
            # five rows make a half page two: from B's rule, onto B's subtotal
            await pilot.press("ctrl+d", "ctrl+d")
            assert app.selected()[1] == b

    asyncio.run(move())


def test_a_long_list_ends_above_the_footer(app):
    tasks = tuple(Task(f"task {n}", None) for n in range(60))
    app.store.save(replace(app.day, sections=(Section("Many", tasks),)))
    app = TodoApp(app.store, app.day.date)

    async def scroll_to_end():
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("G")
            # the column names share the pane, so the list cannot take all of it
            assert (
                app.query_one("#tasks").region.bottom <= app.query_one(Footer).region.y
            )

    asyncio.run(scroll_to_end())


def test_ctrl_d_and_ctrl_u_move_half_a_page(app):
    tasks = tuple(Task(f"task {n}", None) for n in range(60))
    app.store.save(replace(app.day, sections=(Section("Many", tasks),)))
    app = TodoApp(app.store, app.day.date)

    async def scroll():
        async with app.run_test(size=(80, 24)) as pilot:
            widget = app.query_one("#tasks")
            half = widget.scrollable_content_region.height // 2
            start = widget.highlighted
            await pilot.press("ctrl+d")
            assert widget.highlighted == start + half
            await pilot.press("ctrl+u", "ctrl+u")
            assert widget.highlighted == 0
            await pilot.press(*["ctrl+d"] * 10)
            assert widget.highlighted == widget.option_count - 1

    asyncio.run(scroll())


def test_durations_drop_zero_parts():
    seconds = (0, 59, 2400, 10800, 13200, 10799, 400, 37800)
    assert [duration(s).plain for s in seconds] == [
        "0s",
        "59s",
        "40m",
        "3h",
        "3h40m",
        "2h59m59s",
        "6m40s",
        "10h30m",
    ]


def test_redo_reapplies_undone_changes_until_a_new_change(app, monkeypatch):
    messages = []
    monkeypatch.setattr(
        app, "notify", lambda message, **kwargs: messages.append(message)
    )

    async def undo_and_redo():
        async with app.run_test() as pilot:
            await pilot.press("plus", "plus")
            changed = app.day
            await pilot.press("u", "u", "ctrl+r", "ctrl+r")
            assert app.day == changed
            assert app.store.load(changed.date) == changed
            assert messages == [
                "Undid: changed Practice from 3/5 to 4/5",
                "Undid: changed Practice from 2/5 to 3/5",
                "Redid: changed Practice from 2/5 to 3/5",
                "Redid: changed Practice from 3/5 to 4/5",
            ]
            await pilot.press("u", "minus", "ctrl+r")
            assert messages[-1] == "Nothing to redo."
            assert app.selected()[1].earned == 2

    asyncio.run(undo_and_redo())


def test_sorting_puts_missing_values_last_and_keeps_ties_in_stored_order():
    pts, est = COLUMNS[1], COLUMNS[2]
    a, b = Task("a", points("2"), minutes=30), Task("b", points("2"), minutes=10)
    c, d = Task("c", None, minutes=10), Task("d", None)
    tasks = (a, b, c, d)
    assert arrange(tasks, None) == tasks
    assert arrange(tasks, Sort(est, False)) == (b, c, a, d)
    assert arrange(tasks, Sort(est, True)) == (a, b, c, d)
    assert arrange(tasks, Sort(pts, True)) == (a, b, c, d)


def test_sorting_lists_tasks_across_sections_and_changes_only_the_view(app):
    a, b = Task("a", points("1"), minutes=60), Task("b", points("4"), minutes=20)
    c = Task("c", None)
    first, second = Section("A", (a,)), Section("B", (b, c))
    day = replace(app.day, sections=(first, second))
    app.store.save(day)
    app = TodoApp(app.store, day.date)

    def shown():
        return [o.id for o in app.query_one("#tasks").options if not o.disabled]

    async def sort():
        async with app.run_test(size=(110, 45)) as pilot:
            # the menu lists stored order, then each column both ways
            await pilot.press("s", *"jjjjj", "enter")
            assert shown() == [b.id, a.id, c.id]
            # what works on stored order or sections is refused while sorted
            await pilot.press("J", "}", "number_sign", "o", "O", "p")
            assert app.day == day
            assert not isinstance(app.screen, ModalScreen)
            # clicks on pts: most first, then fewest first, then stored order
            pts = 1 + 2 + 7
            assert await pilot.click("#columns", offset=(pts, 0))
            assert shown() == [b.id, a.id, c.id]
            assert await pilot.click("#columns", offset=(pts, 0))
            assert shown() == [a.id, b.id, c.id]
            assert await pilot.click("#columns", offset=(pts, 0))
            assert shown() == [first.id, a.id, second.id, b.id, c.id]
            assert app.store.load(day.date) == day

    asyncio.run(sort())


def test_plan_shows_spare_or_over_scheduled_time(app):
    section = app.day.sections[0]

    def line(label):
        return line_of(stats_text(app), label)

    async def plan():
        async with app.run_test():
            assert "07:30 - 11:00" in line("schedule")
            assert "30m" in line("needed")
            assert "10h" in line("spare")
            app.save_change(
                app.day.insert_task(Task("Long", None, minutes=660), section.id, 0),
                "added Long",
            )
            assert "1h" in line("over")

    asyncio.run(plan())


@pytest.mark.parametrize("value", ["20261102", "2026-W45-1"])
def test_command_line_rejects_dates_the_date_prompt_rejects(
    value, tmp_path, monkeypatch
):
    database = tmp_path / "todo.sqlite3"
    monkeypatch.setattr(
        sys, "argv", ["todo", "--database", str(database), "--date", value]
    )
    monkeypatch.setattr(TodoApp, "run", lambda self: None)
    with pytest.raises(SystemExit) as raised:
        main()
    assert raised.value.code == 2


def test_f_hides_done_tasks_in_both_views_until_pressed_again(app):
    d1, p = Task("d1", points("1"), points("1")), Task("p", points("3"), points("1"))
    d2, q = Task("d2", points("2"), points("2")), Task("q", points("1"))
    a, b = Section("A", (d1, p)), Section("B", (d2, q))
    day = replace(app.day, sections=(a, b))
    app.store.save(day)
    app = TodoApp(app.store, day.date)

    def shown():
        return [o.id for o in app.query_one("#tasks").options if not o.disabled]

    async def filter_done():
        async with app.run_test(size=(110, 45)) as pilot:
            await pilot.press("f")
            assert shown() == [a.id, p.id, b.id, q.id]
            assert "2 done hidden" in str(app.query_one("#date").content)
            # a move could swap with a hidden task, so it is refused
            await pilot.press("g", "j", "J")
            assert app.day == day
            # sorted by most points first: as many tasks as sections
            await pilot.press("s", "j", "j", "j", "enter")
            assert shown() == [p.id, q.id]
            assert not app.query_one("#empty").display
            await pilot.press("s", "g", "enter")
            # finishing tasks hides them; with nothing left, say so
            await pilot.press("g", "j", "plus", "plus")
            assert shown() == [a.id, b.id, q.id]
            widget = app.query_one("#tasks")
            widget.highlighted = widget.get_option_index(q.id)
            await pilot.press("space")
            assert shown() == [a.id, b.id]
            assert app.query_one("#empty").display
            await pilot.press("f")
            assert len(shown()) == 6
            assert "hidden" not in str(app.query_one("#date").content)

    asyncio.run(filter_done())
