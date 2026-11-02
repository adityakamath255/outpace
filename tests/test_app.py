import asyncio
import sqlite3
import sys
from dataclasses import replace
from datetime import date, datetime
from io import StringIO

import pytest
from rich.console import Console
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Footer, Input, Select, Static

from todo.app import TodoApp, main, schedule_status, section_row
from todo.db import Store
from todo.model import Interval, Section, Task, empty_day, points

SCHEDULE = (
    Interval(datetime(2026, 11, 1, 7, 30), datetime(2026, 11, 1, 11)),
    Interval(datetime(2026, 11, 1, 16), datetime(2026, 11, 1, 23)),
)


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


def test_clock_callback_does_not_touch_widgets_after_shutdown(app):
    async def stop_then_tick():
        async with app.run_test():
            assert app.is_running
        app.refresh_clock()

    asyncio.run(stop_then_tick())


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
        intervals=SCHEDULE,
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


@pytest.mark.parametrize(
    "key, field, value", [("e", "#minutes", "35"), ("s", ".interval-end", "10:00")]
)
def test_enter_saves_multi_field_forms(app, key, field, value):
    original = app.day

    async def save_with_enter():
        async with app.run_test(size=(110, 45)) as pilot:
            await pilot.press(key)
            edited = app.screen.query_one(field, Input)
            edited.value = value
            edited.focus()
            await pilot.press("enter")
            assert not isinstance(app.screen, ModalScreen)
            assert app.day != original
            assert app.store.load(original.date) == app.day

    asyncio.run(save_with_enter())


def test_enter_selects_a_section_before_saving_from_an_input(app):
    original = app.day

    async def choose_then_save():
        async with app.run_test(size=(110, 45)) as pilot:
            await pilot.press("e")
            app.screen.query_one(Select).focus()
            await pilot.press("space", "enter")
            assert isinstance(app.screen, ModalScreen)
            assert app.day == original
            new_section = app.screen.query_one("#new-section", Input)
            new_section.value = "New group"
            new_section.focus()
            await pilot.press("enter")
            assert not isinstance(app.screen, ModalScreen)
            assert app.day.sections[-1].title == "New group"
            assert app.day.sections[-1].tasks == original.sections[0].tasks
            assert app.store.load(original.date) == app.day

    asyncio.run(choose_then_save())


@pytest.mark.parametrize("key, field", [("e", "#title"), ("r", "#section-name")])
def test_enter_on_a_closed_dropdown_saves_the_form(app, key, field):
    original = app.day

    async def save_from_dropdown():
        async with app.run_test(size=(110, 45)) as pilot:
            await pilot.press(key)
            app.screen.query_one(field, Input).value = "Changed"
            app.screen.query_one(Select).focus()
            await pilot.press("enter")
            assert not isinstance(app.screen, ModalScreen)
            assert app.day != original
            assert app.store.load(original.date) == app.day

    asyncio.run(save_from_dropdown())


def test_escape_closes_the_dropdown_before_canceling_the_form(app):
    original = app.day

    async def cancel_in_layers():
        async with app.run_test() as pilot:
            await pilot.press("e")
            app.screen.query_one(Select).focus()
            await pilot.press("down", "escape")
            assert isinstance(app.screen, ModalScreen)
            await pilot.press("escape")
            assert not isinstance(app.screen, ModalScreen)
            assert app.day == original

    asyncio.run(cancel_in_layers())


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


def test_today_button_stays_enabled_on_today(app):
    async def open_today():
        async with app.run_test() as pilot:
            await pilot.press("t")
            today = app.day
            assert today.date == date.today()
            assert not app.query_one("#today", Button).disabled
            assert await pilot.click("#today")
            assert app.day == today

    asyncio.run(open_today())


def test_task_changes_save_and_undo(app):
    async def exercise_tasks():
        async with app.run_test(size=(110, 45)) as pilot:
            await pilot.pause()
            assert await pilot.click(footer_action(app, "add"))
            await pilot.press(*"read daily")
            assert app.screen.query_one("#title", Input).value == "read daily"
            await pilot.press("tab")
            assert app.focused.id == "earned"
            await pilot.press("shift+tab")
            assert app.focused.id == "title"
            app.screen.query_one("#available", Input).value = "2.5"
            app.screen.query_one("#earned", Input).value = "0.5"
            app.screen.query_one("#minutes", Input).value = "20"
            await pilot.press("enter")
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
            await pilot.press("enter")
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


def test_oversized_estimate_keeps_form_and_explains_the_limit(app):
    original = app.day
    oversized = str(2**63)

    async def reject_estimate():
        async with app.run_test(size=(110, 45)) as pilot:
            await pilot.press("e")
            field = app.screen.query_one("#minutes", Input)
            field.value = oversized
            await pilot.press("enter")
            assert isinstance(app.screen, ModalScreen)
            assert field.value == oversized
            error = str(app.screen.query_one("#error", Static).content)
            assert "too large" in error
            assert app.day == original
            assert not app.history
            assert Store(app.store.path).load(original.date) == original

    asyncio.run(reject_estimate())


def test_saving_points_keeps_navigation_hints_visible(app):
    async def edit_points():
        async with app.run_test() as pilot:
            hints = str(app.query_one("#status", Static).content)
            await pilot.press("enter")
            app.screen.query_one("#earned", Input).value = "2.5"
            await pilot.press("enter")
            assert next(app.day.tasks())[1].earned == points("2.5")
            assert str(app.query_one("#status", Static).content) == hints

    asyncio.run(edit_points())


def test_dialog_buttons_support_arrows_and_vim_without_changing_input_keys(app):
    async def use_dialog_keys():
        async with app.run_test() as pilot:
            await pilot.press("enter")
            app.screen.query_one("#cancel", Button).focus()
            await pilot.pause()
            for key, target in (
                ("right", "save"),
                ("h", "cancel"),
                ("j", "save"),
                ("up", "cancel"),
                ("l", "save"),
                ("k", "cancel"),
                ("down", "save"),
                ("left", "cancel"),
            ):
                await pilot.press(key)
                assert app.focused.id == target
            await pilot.press("shift+tab")
            field = app.screen.query_one("#earned", Input)
            assert app.focused is field
            field.value = ""
            await pilot.press(*"hjkl", "left", "X")
            assert field.value == "hjkXl"
            await pilot.press("escape")

    asyncio.run(use_dialog_keys())


def test_sections_can_be_renamed_including_empty_duplicate_named_groups(app):
    empty = Section("Tasks")
    original = replace(app.day, sections=app.day.sections + (empty,))
    app.store.save(original, expected=app.day)
    app = TodoApp(app.store, original.date)

    async def rename_sections():
        async with app.run_test(size=(110, 45)) as pilot:
            await pilot.press("r", *"Work", "enter")
            assert app.day.sections[0].title == "Work"
            assert app.day.sections[0].id == original.sections[0].id
            assert app.day.sections[0].tasks == original.sections[0].tasks
            assert app.store.load(original.date) == app.day
            await pilot.press("u")
            assert app.day == original

            await pilot.press("r", "shift+tab", "space", "end", "enter", "tab")
            assert app.screen.query_one("#section", Select).value == empty.id
            assert app.screen.query_one(Input).value == empty.title
            await pilot.press(*"Later")
            assert await pilot.click("#save")
            assert app.day.sections[0] == original.sections[0]
            assert app.day.sections[1] == replace(empty, title="Later")
            assert app.store.load(original.date) == app.day
            await pilot.press("u")
            assert app.day == original
            assert app.store.load(original.date) == original

    asyncio.run(rename_sections())


def test_section_rename_rejects_blank_names_and_preserves_failed_drafts(app):
    original = app.day
    changed = original.rename_section(original.sections[0].id, "Changed elsewhere")

    async def fail_rename():
        async with app.run_test() as pilot:
            await pilot.press("r")
            field = app.screen.query_one("#section-name", Input)
            field.value = " "
            await pilot.press("enter")
            assert isinstance(app.screen, ModalScreen)
            assert app.day == original
            assert str(app.screen.query_one("#error", Static).content)
            assert app.store.load(original.date) == original

            field.value = "Keep this draft"
            Store(app.store.path).save(changed, expected=original)
            await pilot.press("enter")
            assert isinstance(app.screen, ModalScreen)
            assert field.value == "Keep this draft"
            assert "changed" in str(app.screen.query_one("#error", Static).content)
            assert app.day == original
            assert app.store.load(original.date) == changed
            await pilot.press("escape")

    asyncio.run(fail_rename())


def test_section_rename_keeps_focused_actions_reachable_in_a_short_terminal(app):
    async def rename_in_short_terminal():
        async with app.run_test(size=(60, 12)) as pilot:
            await pilot.press("r")
            app.screen.query_one(Input).value = "Work"
            await pilot.press("tab", "tab")
            assert app.focused.id == "save"
            assert await pilot.click("#save")
            assert app.day.sections[0].title == "Work"

    asyncio.run(rename_in_short_terminal())


def test_delete_has_a_two_key_confirmation_and_cancel_stays_the_default(app):
    original = app.day

    async def delete_and_undo():
        async with app.run_test() as pilot:
            await pilot.press("d")
            assert app.focused.id == "cancel"
            await pilot.press("enter")
            assert not isinstance(app.screen, ModalScreen)
            assert app.day == original
            await pilot.press("d", "y")
            assert not isinstance(app.screen, ModalScreen)
            assert not list(app.day.tasks())
            assert app.store.load(original.date) == app.day
            await pilot.press("u")
            assert app.day == original
            assert app.store.load(original.date) == original

    asyncio.run(delete_and_undo())


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
    intervals = tuple(reversed(SCHEDULE))
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
            assert tomorrow.intervals == ()
            assert app.store.load(tomorrow.date) is None
            await pilot.press("a", *"New task")
            await pilot.press("enter")
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
                await pilot.press("a", *title, "enter")
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
                await pilot.press("enter")
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
            await pilot.press("a", *"First task", "enter")
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
            await pilot.press("enter")
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


def test_out_of_range_clock_preserves_draft_and_explains_the_format(app):
    original = app.day

    async def reject_clock():
        async with app.run_test(size=(110, 45)) as pilot:
            await pilot.press("s")
            start = app.screen.query(".interval-start").first(Input)
            start.value = "9:75"
            await pilot.press("enter")
            assert isinstance(app.screen, ModalScreen)
            assert start.value == "9:75"
            assert "24-hour" in str(app.screen.query_one("#error", Static).content)
            assert app.day == original
            assert not app.history
            assert Store(app.store.path).load(original.date) == original

    asyncio.run(reject_clock())


def test_schedule_can_be_cleared_and_an_empty_schedule_edited(app):
    async def clear_schedule():
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("s")
            for row in list(app.screen.query(".interval-row")):
                row.query_one(Button).press()
                await pilot.pause()
            app.screen.query_one("#save", Button).focus()
            await pilot.press("enter")
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
            await pilot.press("enter")
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
            await pilot.press("enter")
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
