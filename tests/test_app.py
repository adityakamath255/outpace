import asyncio
import sqlite3
import sys
from dataclasses import replace
from datetime import date, datetime
from io import StringIO

import pytest
from rich.console import Console
from textual.screen import ModalScreen
from textual.widgets import Footer, Input, Static

from todo.app import SectionRule, TodoApp, main, schedule_status
from todo.db import Store
from todo.model import Interval, Section, Task, empty_day, parse_schedule, points

HOUR = 3600
SCHEDULE = parse_schedule("07:30-11:00, 16:00-23:00")


def stats_text(app):
    output = StringIO()
    Console(file=output, width=60).print(app.query_one("#stats", Static).content)
    return output.getvalue()


async def add_task(pilot, title, *, close=True):
    await pilot.press("a")
    pilot.app.screen.query_one("#title", Input).value = title
    if close:
        await pilot.press("enter", "escape")


def footer_action(app, action):
    return next(key for key in app.query_one(Footer).children if key.action == action)


def test_section_share_is_not_truncated():
    section = Section("Tasks", (Task("Practice", points("5")),))
    output = StringIO()
    Console(file=output, width=44).print(SectionRule(section, points("5")))
    assert "100.00% of total" in output.getvalue()


def test_clock_callback_does_not_touch_widgets_after_shutdown(app):
    async def stop_then_tick():
        async with app.run_test():
            assert app.is_running
        app.refresh_stats()

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
            assert await pilot.click(footer_action(app, "add"))
            await pilot.press(*"2.5", "tab", *"20", "tab", *"read daily", "enter")
            added = next(
                task for _, task in app.day.tasks() if task.title == "read daily"
            )
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
            assert all(task.id != added.id for _, task in app.day.tasks())
            await pilot.press("u")
            assert app.day == before_move

        assert Store(app.store.path).load(app.day.date) == app.day

    asyncio.run(exercise_tasks())


def test_enter_completes_an_unscored_task_without_a_dialog(app):
    async def complete_unscored_task():
        async with app.run_test() as pilot:
            await add_task(pilot, "Remember")
            await pilot.press("enter")
            assert app.selected()[1].checked
            assert not isinstance(app.screen, ModalScreen)

    asyncio.run(complete_unscored_task())


@pytest.mark.parametrize("existing", [False, True])
def test_reuse_copies_a_saved_day_and_undo_restores_the_destination(app, existing):
    source = app.day
    destination = empty_day(date(2026, 11, 2))
    if existing:
        destination = destination.add_task(
            Task("Keep this plan", None, checked=True), destination.sections[0].id
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
            assert next(copied.tasks())[1].earned == 0
            assert copied.intervals == source.intervals
            assert app.store.load(source.date) == source
            assert app.store.load(copied.date) == copied
            await pilot.press("u")
            assert app.day == original
            assert app.store.load(original.date) == (original if existing else None)
            assert app.store.load(source.date) == source

    asyncio.run(reuse_day())


def test_total_estimate_uses_hours_and_minutes(app):
    section, _ = next(app.day.tasks())

    async def read_estimate():
        async with app.run_test():
            app.save_change(
                app.day.add_task(Task("Read", None, minutes=190), section.id),
                "added Read",
            )
            assert "3h40m estimated across 2 tasks" in stats_text(app)

    asyncio.run(read_estimate())


def test_failed_save_keeps_form_and_does_not_change_history(app):
    original = app.day
    _, task = next(original.tasks())
    concurrent = original.replace_task(replace(task, title="Changed elsewhere"))

    async def fail_save():
        async with app.run_test(size=(110, 45)) as pilot:
            await pilot.press("enter")
            app.screen.query_one(Input).value = "2.5"
            Store(app.store.path).save(concurrent, expected=original)
            await pilot.press("enter")
            assert isinstance(app.screen, ModalScreen)
            assert app.screen.query_one(Input).value == "2.5"
            assert "changed" in str(app.screen.query_one("#error", Static).content)
            assert app.day == original
            assert not app.history
            assert Store(app.store.path).load(original.date) == concurrent

    asyncio.run(fail_save())


def test_selected_sections_are_renamed_and_blank_means_untitled(app):
    empty = Section("Tasks")
    original = replace(app.day, sections=app.day.sections + (empty,))
    app.store.save(original, expected=app.day)
    app = TodoApp(app.store, original.date)

    async def rename_sections():
        async with app.run_test(size=(110, 45)) as pilot:
            # r on a task renames the task's section
            await pilot.press("r", "ctrl+u", *"Work", "enter")
            assert app.day.sections[0].title == "Work"
            assert app.day.sections[0].id == original.sections[0].id
            assert app.day.sections[0].tasks == original.sections[0].tasks
            assert app.store.load(original.date) == app.day
            await pilot.press("u")
            assert app.day == original

            # the last row is the empty section's rule; Enter on a rule renames it
            await pilot.press("G", "enter", "ctrl+u", "enter")
            assert app.day.sections[0] == original.sections[0]
            assert app.day.sections[1] == replace(empty, title="")
            assert app.store.load(original.date) == app.day
            await pilot.press("u")
            assert app.day == original
            assert app.store.load(original.date) == original

    asyncio.run(rename_sections())


def test_dd_deletes_without_a_dialog_and_undo_names_the_change(app, monkeypatch):
    original = app.day
    messages = []
    monkeypatch.setattr(
        app, "notify", lambda message, **kwargs: messages.append(message)
    )

    async def delete_and_undo():
        async with app.run_test() as pilot:
            await pilot.press("d", "G", "d")
            assert app.day == original
            await pilot.press("d", "d")
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
            await pilot.press("d", "d")
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
            assert len(app.history) == 4
            await pilot.press("u")
            assert app.selected()[1].earned == 1
            assert messages == ["Undid: changed Practice from 1/5 to 0/5"]

    asyncio.run(step())


def test_help_lists_every_key_binding(app):
    from todo.app import HELP, TaskList

    documented = {key for _, rows in HELP for keys, _ in rows for key in keys.split()}
    bindings = [*TodoApp.BINDINGS, *TaskList.BINDINGS]
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
            await pilot.press("a")
            assert not list(app.screen.query("#title"))
            await pilot.press("escape")
            assert not isinstance(app.screen, ModalScreen)

    asyncio.run(open_and_close())


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
            stats = stats_text(app)
            assert "schedule ended" in stats
            assert "remaining / point  ended" in stats

    asyncio.run(check_ended_schedule())


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (7 * HOUR, "Not started"),
        (7 * HOUR + 1800, "In progress"),
        (11 * HOUR, "Between intervals"),
        (23 * HOUR, "Schedule ended"),
    ],
)
def test_schedule_status_follows_the_intervals(now, expected):
    intervals = tuple(reversed(SCHEDULE))
    assert schedule_status(intervals, now) == expected


def test_overnight_and_unscheduled_lists_have_distinct_statuses():
    now = 24 * HOUR + 1800
    overnight = parse_schedule("23:00-01:00")
    assert schedule_status(overnight, now) == "In progress"
    assert schedule_status((), now) == "No schedule"


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
            assert app.history
            await pilot.press("[")
            assert app.day == earlier
            assert not app.history
            assert earlier.date.isoformat() in str(app.query_one("#date").content)
            assert app.store.load(original.date) == edited

            await pilot.press("]")
            assert app.day == edited
            await pilot.press("]")
            tomorrow = app.day
            assert tomorrow.date == date(2026, 11, 2)
            assert not list(tomorrow.tasks())
            assert tomorrow.intervals == ()
            assert app.store.load(tomorrow.date) is None
            await add_task(pilot, "New task")
            assert app.store.load(tomorrow.date) == app.day
            assert next(app.day.tasks())[1].title == "New task"

            await pilot.press("ctrl+d", *original.date.isoformat(), "enter")
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
            await pilot.press("ctrl+d", "enter")
            assert app.day == current
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
            await pilot.press("escape")
            assert app.day == current

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
            current = app.day
            await pilot.press("ctrl+d")
            app.screen.query_one(Input).value = target.isoformat()
            with monkeypatch.context() as patch:
                patch.setattr(app.store, "load", fail)
                await pilot.press("enter")
            assert isinstance(app.screen, ModalScreen)
            assert app.screen.query_one(Input).value == target.isoformat()
            assert str(app.screen.query_one("#error", Static).content)
            assert app.day == current
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
                await add_task(pilot, title)
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
            await add_task(pilot, "Keep my draft", close=False)
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
            await add_task(pilot, "First task")
            saved = app.day
            changed = replace(
                saved, sections=(replace(saved.sections[0], title="Changed elsewhere"),)
            )
            Store(store.path).save(changed, expected=saved)
            await pilot.press("u")
            assert app.day == saved
            assert store.load(saved.date) == changed
            # nothing in the history can apply once the stored day moved on
            assert not app.history

    asyncio.run(reject_undo())


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
            await pilot.press("s")
            field = app.screen.query_one(Input)
            assert field.value == "07:30-11:00, 16:00-23:00"
            field.value = "09:00-12:00, 23:00-01:00, 01:30-02:00"
            await pilot.press("enter")
            assert app.day.intervals == (
                Interval(9 * 60, 12 * 60),
                Interval(23 * 60, 25 * 60),
                Interval(25 * 60 + 30, 26 * 60),
            )
            assert app.day.sections == original.sections
            assert "5h30m" in stats_text(app)
            assert Store(app.store.path).load(original.date) == app.day
            await pilot.press("u")
            assert app.day == original
            assert Store(app.store.path).load(original.date) == original

    asyncio.run(edit_schedule())


def test_schedule_can_be_cleared_and_an_empty_schedule_edited(app):
    async def clear_schedule():
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("s", "ctrl+u", "enter")
            assert app.day.intervals == ()
            assert "no schedule" in stats_text(app)
            assert Store(app.store.path).load(app.day.date) == app.day
            await pilot.press("s", *"08:00-09:00", "enter")
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
            # o opens an untitled section below and selects its rule
            await pilot.press("o")
            first, added = app.day.sections
            assert added.title == "" and not added.tasks
            assert app.selected() == (added, None)
            # K on a rule moves the whole section
            await pilot.press("K")
            assert app.day.sections == (added, first)
            # a task moved past the end of its section joins the next one
            await pilot.press("G", "K", "K")
            assert app.day.sections[0].tasks == first.tasks
            # dd on a rule deletes the section with its tasks
            await pilot.press("g", "d", "d")
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


def test_undo_after_another_session_changed_the_day_keeps_their_change(app):
    async def undo_stale():
        async with app.run_test() as pilot:
            await pilot.press("plus", "]")
            elsewhere = Store(app.store.path)
            current = elsewhere.load(date(2026, 11, 1))
            other = current.rename_section(current.sections[0].id, "Other session")
            elsewhere.save(other, expected=current)
            await pilot.press("[")
            assert app.day == other
            await pilot.press("u")
            assert elsewhere.load(other.date) == other
            assert app.day == other

    asyncio.run(undo_stale())


def test_deleting_a_sections_last_task_does_not_select_the_next_section(app):
    async def delete_twice():
        async with app.run_test(size=(110, 45)) as pilot:
            await pilot.press("o")
            await add_task(pilot, "Next")
            await pilot.press("g", "j", "d", "d")
            section, task = app.selected()
            assert task is None and section.id == app.day.sections[0].id

    asyncio.run(delete_twice())
