import asyncio
from dataclasses import replace
from datetime import date

import pytest
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from todo.app import TodoApp
from todo.db import Store
from todo.model import Section, Task, empty_day, points


def test_app_starts(tmp_path):
    app = TodoApp(Store(tmp_path / "todo.sqlite3"), empty_day(date(2026, 11, 1)))

    async def start():
        async with app.run_test():
            assert app.is_running

    asyncio.run(start())


@pytest.fixture
def app(tmp_path):
    day = replace(
        empty_day(date(2026, 11, 1)),
        sections=(Section("Tasks", (Task("Practice", points("5"), points("2"), 30),)),),
    )
    store = Store(tmp_path / "todo.sqlite3")
    store.save(day)
    return TodoApp(store, day)


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
            assert await pilot.click("#add-task")
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

            assert await pilot.click("#delete-task")
            assert await pilot.click("#delete")
            assert all(task.id != added.id for _, task in app.day.tasks())
            await pilot.press("u")
            assert app.day == before_move

        assert Store(app.store.path).load(app.day.date) == app.day

    asyncio.run(exercise_tasks())


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
