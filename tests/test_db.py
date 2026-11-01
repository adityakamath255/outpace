import sqlite3
from dataclasses import replace
from datetime import date

import pytest

from todo.db import Store
from todo.model import Day, Section, Task, empty_day, points

DAY = date(2026, 11, 1)


def test_save_reopen_edit_and_restore_snapshot(tmp_path):
    path = tmp_path / "nested" / "todo.sqlite3"
    store = Store(path)
    task = Task("Practice", points("2.5"), points("0.125"), 20)
    original = replace(empty_day(DAY), sections=(Section("Tasks", (task,)),))
    store.save(original)
    assert Store(path).load(DAY) == original
    edited = original.put_task(
        replace(task, earned=points("1.25")), original.sections[0].id
    )
    store.save(edited, expected=original)
    assert Store(path).load(DAY) == edited
    store.save(original, expected=edited)
    assert store.load(DAY) == original


def test_failed_write_rolls_back_without_touching_other_days(tmp_path):
    store = Store(tmp_path / "todo.sqlite3")
    original = replace(
        empty_day(DAY), sections=(Section("Tasks", (Task("Practice", points("2")),)),)
    )
    tomorrow = empty_day(date(2026, 11, 2))
    store.save(original)
    store.save(tomorrow)
    # a conflicting identity fails after the old day's rows have been deleted
    conflict = Day(DAY, (), (replace(tomorrow.sections[0], title="Conflict"),))
    with pytest.raises(sqlite3.IntegrityError):
        store.save(conflict, expected=original)
    assert store.load(DAY) == original
    assert store.load(tomorrow.date) == tomorrow


def test_import_cannot_replace_an_existing_day(tmp_path):
    store = Store(tmp_path / "todo.sqlite3")
    original = empty_day(DAY)
    store.save(original)
    imported = replace(original, sections=(Section("Imported"),))
    with pytest.raises(ValueError, match="already exists"):
        store.save(imported)
    assert store.load(DAY) == original


def test_newer_database_is_not_modified(tmp_path):
    path = tmp_path / "todo.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version = 2")
    with pytest.raises(ValueError, match="version 2"):
        Store(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2


def test_stale_save_cannot_overwrite_newer_edits(tmp_path):
    path = tmp_path / "todo.sqlite3"
    store = Store(path)
    task = Task("Practice", points("2"))
    original = replace(empty_day(DAY), sections=(Section("Tasks", (task,)),))
    store.save(original)
    current = original.put_task(
        replace(task, earned=points("1")), original.sections[0].id
    )
    stale = original.put_task(replace(task, minutes=30), original.sections[0].id)
    Store(path).save(current, expected=original)
    with pytest.raises(ValueError, match="changed"):
        store.save(stale, expected=original)
    assert store.load(DAY) == current
