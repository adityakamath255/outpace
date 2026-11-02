import asyncio
import sqlite3
import sys
from dataclasses import replace
from datetime import date, datetime
from io import StringIO

import pytest
from rich.console import Console
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Footer, Input, Static

from todo.app import TodoApp, main, schedule_status, section_row
from todo.db import Store
from todo.model import Interval, Section, Task, empty_day, points


def footer_action(app, action):
    return next(key for key in app.query_one(Footer).children if key.action == action)


def test_section_share_is_not_truncated():
    section = Section("Tasks", (Task("Practice", points("5")),))
    output = StringIO()
    Console(file=output, width=44).print(section_row(section, points("5")))
    assert "100.00% of total" in output.getvalue()


def test_app_starts(tmp_path):
    app = TodoApp(Store(tmp_path / "todo.sqlite3"), date(2026, 11, 1))

    async def start():
        async with app.run_test():
            assert app.is_running

    asyncio.run(start())


def test_startup_does_not_save_an_unedited_day(tmp_path, monkeypatch):
    path = tmp_path / "todo.sqlite3"
    monkeypatch.setattr(
        sys, "argv", ["todo", "--database", str(path), "--date", "2026-11-01"]
    )
    monkeypatch.setattr(TodoApp, "run", lambda self: None)
    main()
    assert Store(path).load(date(2026, 11, 1)) is None


@pytest.fixture
def app(tmp_path):
    day = replace(
        empty_day(date(2026, 11, 1)),
        sections=(Section("Tasks", (Task("Practice", points("5"), points("2"), 30),)),),
    )
    store = Store(tmp_path / "todo.sqlite3")
    store.save(day)
    return TodoApp(store, day.date)


@pytest.mark.parametrize("key", ("a", "e", "enter", "d"))
def test_task_dialogs_open(app, key):
    async def open_dialog():
        async with app.run_test(size=(110, 45)) as pilot:
            await pilot.press(key)
            assert isinstance(app.screen, ModalScreen)

    asyncio.run(open_dialog())


def test_task_changes_save_and_undo(app):
    async def exercise_tasks():
        async with app.run_test(size=(110, 45)) as pilot:
            await pilot.pause()
            assert await pilot.click(footer_action(app, "add"))
            await pilot.press(*"read daily")
            assert app.screen.query_one("#title", Input).value == "read daily"
            await pilot.press("tab")
            assert app.focused.id == "available"
            await pilot.press("shift+tab")
            assert app.focused.id == "title"
            app.screen.query_one("#available", Input).value = "2.5"
            app.screen.query_one("#earned", Input).value = "0.5"
            app.screen.query_one("#minutes", Input).value = "20"
            await pilot.press("ctrl+enter")
            added = next(
                task for _, task in app.day.tasks() if task.title == "read daily"
            )
            assert (added.available, added.earned, added.minutes) == (
                points("2.5"),
                points("0.5"),
                20,
            )

            await pilot.press("h", "enter")
            await pilot.press(*"2.5", "enter")
            assert next(
                task for _, task in app.day.tasks() if task.id == added.id
            ).complete

            before_cancel = app.day
            await pilot.press("e")
            app.screen.query_one("#title", Input).value = "Discarded edit"
            await pilot.press("escape")
            assert app.day == before_cancel
            await pilot.press("e")
            app.screen.query_one("#minutes", Input).value = "35"
            await pilot.press("ctrl+enter")
            edited = next(task for _, task in app.day.tasks() if task.id == added.id)
            assert edited.minutes == 35
            assert edited.earned == points("2.5")

            before_move = app.day
            await pilot.press("K")
            assert app.day.sections[0].tasks[0].id == added.id
            await pilot.press("u")
            assert app.day == before_move

            await pilot.press("space")
            assert (
                next(task for _, task in app.day.tasks() if task.id == added.id).earned
                == 0
            )
            await pilot.press("u")
            assert app.day == before_move

            assert await pilot.click(footer_action(app, "delete"))
            assert await pilot.click("#delete")
            assert all(task.id != added.id for _, task in app.day.tasks())
            await pilot.press("u")
            assert app.day == before_move

        assert Store(app.store.path).load(app.day.date) == app.day

    asyncio.run(exercise_tasks())


def test_panes_keep_selection_and_edits_work_after_resize(app):
    async def switch_panes():
        async with app.run_test(size=(80, 24)) as pilot:
            selected = app.selected()
            await pilot.press("l")
            assert app.focused.id == "sidebar"
            await pilot.resize_terminal(60, 24)
            assert not app.query_one("#task-pane").display
            assert app.query_one("#sidebar").display
            assert app.selected() == selected
            await pilot.press("e")
            assert isinstance(app.screen, ModalScreen)
            assert app.screen.query_one("#title", Input).value == selected[1].title
            await pilot.press("escape")
            assert await pilot.click("#toggle-view")
            assert app.focused.id == "tasks"
            assert app.query_one("#task-pane").display
            assert not app.query_one("#sidebar").display
            await pilot.press("right")
            assert app.focused.id == "sidebar"
            await pilot.resize_terminal(80, 24)
            assert app.query_one("#task-pane").display
            assert app.query_one("#sidebar").display
            await pilot.press("left")
            assert app.focused.id == "tasks"
            assert app.selected() == selected

    asyncio.run(switch_panes())


def test_failed_save_keeps_form_and_does_not_change_history(app):
    original = app.day
    section, task = next(original.tasks())
    concurrent = original.put_task(replace(task, title="Changed elsewhere"), section.id)

    async def fail_save():
        async with app.run_test(size=(110, 45)) as pilot:
            await pilot.press("enter")
            app.screen.query_one("#earned", Input).value = "2.5"
            Store(app.store.path).save(concurrent, expected=original)
            await pilot.press("enter")
            assert isinstance(app.screen, ModalScreen)
            assert app.screen.query_one("#earned", Input).value == "2.5"
            assert "changed" in str(app.screen.query_one("#error", Static).content)
            assert app.day == original
            assert not app.history
            assert Store(app.store.path).load(original.date) == concurrent

    asyncio.run(fail_save())


def test_ended_schedule_is_explained_instead_of_showing_zero_remaining(
    app, monkeypatch
):
    class Clock:
        @staticmethod
        def now():
            return datetime(2026, 11, 2, 8)

    monkeypatch.setattr("todo.app.datetime", Clock)

    async def check_ended_schedule():
        async with app.run_test():
            assert "Schedule ended" in str(app.query_one("#time", Static).content)
            assert "Remaining / point Ended" in str(
                app.query_one("#pace", Static).content
            )

    asyncio.run(check_ended_schedule())


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2026, 11, 1, 7), "Not started"),
        (datetime(2026, 11, 1, 7, 30), "In progress"),
        (datetime(2026, 11, 1, 11), "Between intervals"),
        (datetime(2026, 11, 1, 23), "Schedule ended"),
    ],
)
def test_schedule_status_uses_intervals_instead_of_the_list_date(now, expected):
    intervals = tuple(reversed(empty_day(date(2026, 11, 1)).intervals))
    assert schedule_status(intervals, now) == expected


def test_overnight_and_unscheduled_lists_have_distinct_statuses():
    now = datetime(2026, 11, 2, 0, 30)
    overnight = (Interval(datetime(2026, 11, 1, 23), datetime(2026, 11, 2, 1)),)
    assert schedule_status(overnight, now) == "In progress"
    assert schedule_status((), now) == "No schedule"


def test_day_switching_preserves_lists_and_creates_new_days(app):
    original = app.day
    earlier = replace(
        empty_day(date(2026, 10, 31)),
        intervals=(Interval(datetime(2026, 10, 31, 23), datetime(2026, 11, 1, 1)),),
        sections=(Section("Earlier", (Task("Previous task", points("1")),)),),
    )
    app.store.save(earlier)

    async def switch_days():
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("space")
            edited = app.day
            assert app.history
            assert await pilot.click("#previous-day")
            assert app.day == earlier
            assert not app.history
            assert earlier.date.isoformat() in str(
                app.query_one("#choose-day", Button).label
            )
            assert app.store.load(original.date) == edited

            await pilot.press("]")
            assert app.day == edited
            await pilot.press("]")
            tomorrow = app.day
            assert tomorrow.date == date(2026, 11, 2)
            assert not list(tomorrow.tasks())
            assert all(
                interval.start.date() == tomorrow.date
                for interval in tomorrow.intervals
            )
            assert app.store.load(tomorrow.date) is None
            await pilot.press("a", *"New task")
            await pilot.press("ctrl+enter")
            assert app.store.load(tomorrow.date) == app.day
            assert next(app.day.tasks())[1].title == "New task"

            await pilot.press("ctrl+d", *original.date.isoformat())
            assert await pilot.click("#open")
            assert not isinstance(app.screen, ModalScreen)
            assert app.day == edited
            assert not app.history
            assert app.focused.id == "tasks"
            assert app.store.load(earlier.date) == earlier
            assert app.store.load(original.date) == edited
            await pilot.press("u")
            assert app.day == edited
            assert app.store.load(original.date) == edited

            await pilot.press("t")
            assert app.day.date == date.today()
            await pilot.press("[")
            assert await pilot.click("#today")
            assert app.day.date == date.today()

    asyncio.run(switch_days())


def test_same_day_cancellation_and_invalid_dates_preserve_undo(app):
    async def keep_current_day():
        async with app.run_test(size=(60, 24)) as pilot:
            await pilot.press("space")
            current, history = app.day, list(app.history)
            assert await pilot.click("#choose-day")
            await pilot.press("enter")
            assert app.day == current
            assert app.history == history
            await pilot.press("ctrl+d")
            for invalid in ("2026-02-30", "20261102"):
                app.screen.query_one(Input).value = invalid
                await pilot.press("enter")
                assert isinstance(app.screen, ModalScreen)
                assert app.screen.query_one(Input).value == invalid
                assert "YYYY-MM-DD" in str(
                    app.screen.query_one("#error", Static).content
                )
                assert app.day == current
                assert app.history == history
            await pilot.press("escape")
            assert app.day == current
            assert app.history == history

            await pilot.press("e", "[", "]", "t", "ctrl+d")
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
            current, history = app.day, list(app.history)
            await pilot.press("ctrl+d")
            app.screen.query_one(Input).value = target.isoformat()
            with monkeypatch.context() as patch:
                patch.setattr(app.store, "load", fail)
                await pilot.press("enter")
            assert isinstance(app.screen, ModalScreen)
            assert app.screen.query_one(Input).value == target.isoformat()
            assert str(app.screen.query_one("#error", Static).content)
            assert app.day == current
            assert app.history == history
            assert app.store.load(current.date) == current
            assert app.store.load(target) is None
            await pilot.press("enter")
            assert not isinstance(app.screen, ModalScreen)
            assert app.day.date == target
            assert not app.history

    asyncio.run(fail_open())


def test_first_edit_saves_and_undo_restores_an_unsaved_day(tmp_path):
    app = TodoApp(Store(tmp_path / "todo.sqlite3"), date(2026, 11, 1))

    async def edit_new_day():
        async with app.run_test(size=(110, 45)) as pilot:
            original = app.day
            for title in ("First task", "Try again"):
                assert app.store.load(original.date) is None
                await pilot.press("a", *title, "ctrl+enter")
                assert app.store.load(original.date) == app.day
                assert next(app.day.tasks())[1].title == title
                await pilot.press("u")
                assert app.day == original
                assert app.store.load(original.date) is None

    asyncio.run(edit_new_day())


@pytest.mark.parametrize("concurrent", [False, True])
def test_failed_first_edit_preserves_the_draft_and_saved_data(
    tmp_path, monkeypatch, concurrent
):
    store = Store(tmp_path / "todo.sqlite3")
    app = TodoApp(store, date(2026, 11, 1))
    original = app.day
    winner = replace(empty_day(original.date), sections=(Section("Created elsewhere"),))

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("Unavailable database")

    async def fail_first_edit():
        async with app.run_test(size=(110, 45)) as pilot:
            await pilot.press("a", *"Keep my draft")
            with monkeypatch.context() as patch:
                if concurrent:
                    Store(store.path).save(winner)
                else:
                    patch.setattr(store, "save", fail)
                await pilot.press("ctrl+enter")
            assert isinstance(app.screen, ModalScreen)
            assert app.screen.query_one("#title", Input).value == "Keep my draft"
            assert str(app.screen.query_one("#error", Static).content)
            assert app.day == original
            assert not app.history
            assert store.load(original.date) == (winner if concurrent else None)

    asyncio.run(fail_first_edit())


def test_undo_first_edit_cannot_delete_another_sessions_changes(tmp_path):
    store = Store(tmp_path / "todo.sqlite3")
    app = TodoApp(store, date(2026, 11, 1))

    async def reject_undo():
        async with app.run_test(size=(110, 45)) as pilot:
            await pilot.press("a", *"First task", "ctrl+enter")
            saved, history = app.day, list(app.history)
            changed = replace(
                saved, sections=(replace(saved.sections[0], title="Changed elsewhere"),)
            )
            Store(store.path).save(changed, expected=saved)
            await pilot.press("u")
            assert app.day == saved
            assert app.history == history
            assert store.load(saved.date) == changed

    asyncio.run(reject_undo())


def test_previous_and_next_day_stop_at_supported_date_boundaries(app):
    async def check_boundaries():
        async with app.run_test() as pilot:
            app.switch_day(date.min)
            await pilot.press("[")
            assert app.day.date == date.min
            assert app.query_one("#previous-day", Button).disabled
            app.switch_day(date.max)
            await pilot.press("]")
            assert app.day.date == date.max
            assert app.query_one("#next-day", Button).disabled

    asyncio.run(check_boundaries())


def test_schedule_edit_saves_recalculates_and_undoes(app):
    original = app.day

    async def edit_schedule():
        async with app.run_test(size=(110, 45)) as pilot:
            assert await pilot.click("#edit-schedule")
            rows = list(app.screen.query(".interval-row"))
            assert await pilot.click(rows[1].query_one(Button))
            await pilot.press("ctrl+n")
            assert await pilot.click("#add-interval")
            rows = list(app.screen.query(".interval-row"))
            for row, start, end, next_day in (
                (rows[0], "09:00", "12:00", False),
                (rows[1], "23:00", "01:00", False),
                (rows[2], "01:30", "02:00", True),
            ):
                row.query_one(".interval-start", Input).value = start
                row.query_one(".interval-end", Input).value = end
                row.query_one(Checkbox).value = next_day
            assert app.day == original
            assert await pilot.click("#save")
            assert app.day.intervals == (
                Interval(datetime(2026, 11, 1, 9), datetime(2026, 11, 1, 12)),
                Interval(datetime(2026, 11, 1, 23), datetime(2026, 11, 2, 1)),
                Interval(datetime(2026, 11, 2, 1, 30), datetime(2026, 11, 2, 2)),
            )
            assert app.day.sections == original.sections
            assert "05:30:00" in str(app.query_one("#time", Static).content)
            assert Store(app.store.path).load(original.date) == app.day
            await pilot.press("u")
            assert app.day == original
            assert Store(app.store.path).load(original.date) == original

    asyncio.run(edit_schedule())


def test_schedule_overlap_preserves_draft_and_cancel_discards_it(app):
    original = app.day

    async def reject_overlap():
        async with app.run_test(size=(110, 45)) as pilot:
            await pilot.press("s")
            end = app.screen.query(".interval-end").first(Input)
            end.value = "18:00"
            await pilot.press("ctrl+enter")
            assert isinstance(app.screen, ModalScreen)
            assert end.value == "18:00"
            assert "overlap" in str(app.screen.query_one("#error", Static).content)
            assert app.day == original
            assert not app.history
            assert Store(app.store.path).load(original.date) == original
            await pilot.press("escape")
            assert not isinstance(app.screen, ModalScreen)
            assert app.day == original

    asyncio.run(reject_overlap())


def test_schedule_can_be_cleared_and_an_empty_schedule_edited(app):
    async def clear_schedule():
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("s")
            for row in list(app.screen.query(".interval-row")):
                row.query_one(Button).press()
                await pilot.pause()
            await pilot.press("ctrl+enter")
            assert app.day.intervals == ()
            assert "Remaining / point n/a" in str(
                app.query_one("#pace", Static).content
            )
            assert Store(app.store.path).load(app.day.date) == app.day
            await pilot.press("s")
            assert app.focused.id == "add-interval"
            await pilot.press("ctrl+n")
            assert app.focused.has_class("interval-start")
            app.screen.query_one(".interval-start", Input).value = "08:00"
            app.screen.query_one(".interval-end", Input).value = "09:00"
            await pilot.press("ctrl+enter")
            assert app.day.intervals == (
                Interval(datetime(2026, 11, 1, 8), datetime(2026, 11, 1, 9)),
            )

    asyncio.run(clear_schedule())


def test_schedule_save_conflict_preserves_draft_and_other_changes(app):
    original = app.day
    section, task = next(original.tasks())
    concurrent = original.put_task(replace(task, title="Changed elsewhere"), section.id)

    async def reject_conflict():
        async with app.run_test(size=(110, 45)) as pilot:
            await pilot.press("s")
            start = app.screen.query(".interval-start").first(Input)
            start.value = "08:00"
            Store(app.store.path).save(concurrent, expected=original)
            await pilot.press("ctrl+enter")
            assert isinstance(app.screen, ModalScreen)
            assert start.value == "08:00"
            assert "changed" in str(app.screen.query_one("#error", Static).content)
            assert app.day == original
            assert not app.history
            assert Store(app.store.path).load(original.date) == concurrent

    asyncio.run(reject_conflict())


@pytest.mark.parametrize("size", [(80, 24), (60, 24)])
def test_schedule_controls_fit_their_labels(app, size):
    async def check_labels():
        async with app.run_test(size=size) as pilot:
            await pilot.press("s")
            row = app.screen.query(".interval-row").first()
            for control in (row.query_one(Checkbox), row.query_one(Button)):
                needed = control.get_content_width(row.size, app.size)
                assert control.content_size.width >= needed
                assert control.region.right <= row.content_region.right
            for clock in row.query(Input):
                assert clock.content_size.width >= len(clock.value) + 1

    asyncio.run(check_labels())
