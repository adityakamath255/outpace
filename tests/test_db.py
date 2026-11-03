import sqlite3
from dataclasses import replace
from datetime import date

import pytest

from todo.db import Store
from todo.model import Section, Task, empty_day, points

DAY = date(2026, 11, 1)


def test_list_dates_returns_saved_days_newest_first_and_copies_coexist(tmp_path):
    store = Store(tmp_path / "todo.sqlite3")
    assert store.list_dates() == ()
    source = replace(
        empty_day(DAY), sections=(Section("Tasks", (Task("Practice", points("2")),)),)
    )
    copied = source.copy_to(date(2026, 11, 3))
    store.save(copied)
    store.save(source)

    assert store.list_dates() == (copied.date, source.date)
    assert store.load(source.date) == source
    assert store.load(copied.date) == copied
    assert store.load(date(2026, 11, 2)) is None


def test_save_reopen_edit_and_restore_snapshot(tmp_path):
    path = tmp_path / "nested" / "todo.sqlite3"
    store = Store(path)
    task = Task("Practice", points("2.5"), points("0.125"), 20)
    original = replace(empty_day(DAY), sections=(Section("Tasks", (task,)),))
    store.save(original)
    assert Store(path).load(DAY) == original
    edited = original.replace_task(replace(task, earned=points("1.25")))
    store.save(edited, expected=original)
    assert Store(path).load(DAY) == edited
    store.save(original, expected=edited)
    assert store.load(DAY) == original


def test_largest_allowed_estimate_round_trips(tmp_path):
    store = Store(tmp_path / "todo.sqlite3")
    task = Task("Practice", None, minutes=2**63 - 1)
    day = replace(empty_day(DAY), sections=(Section("Tasks", (task,)),))
    store.save(day)
    assert store.load(DAY) == day


def test_creation_cannot_replace_an_existing_day(tmp_path):
    store = Store(tmp_path / "todo.sqlite3")
    original = empty_day(DAY)
    store.save(original)
    replacement = replace(original, sections=(Section("Replacement"),))
    with pytest.raises(ValueError, match="already exists"):
        store.save(replacement)
    assert store.load(DAY) == original


def test_newer_database_is_not_modified(tmp_path):
    path = tmp_path / "todo.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version = 3")
    with pytest.raises(ValueError, match="version 3"):
        Store(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3


def test_stale_save_cannot_overwrite_newer_edits(tmp_path):
    path = tmp_path / "todo.sqlite3"
    store = Store(path)
    task = Task("Practice", points("2"))
    original = replace(empty_day(DAY), sections=(Section("Tasks", (task,)),))
    store.save(original)
    current = original.replace_task(replace(task, earned=points("1")))
    stale = original.replace_task(replace(task, minutes=30))
    Store(path).save(current, expected=original)
    with pytest.raises(ValueError, match="changed"):
        store.save(stale, expected=original)
    assert store.load(DAY) == current


def test_delete_requires_matching_snapshot_and_preserves_other_days(tmp_path):
    store = Store(tmp_path / "todo.sqlite3")
    original = replace(
        empty_day(DAY), sections=(Section("Tasks", (Task("Practice", points("2")),)),)
    )
    other = empty_day(date(2026, 11, 2))
    store.save(original)
    store.save(other)
    changed = replace(
        original, sections=(replace(original.sections[0], title="Changed"),)
    )
    store.save(changed, expected=original)
    with pytest.raises(ValueError, match="changed"):
        store.delete(original)
    assert store.load(DAY) == changed
    store.delete(changed)
    assert store.load(DAY) is None
    assert store.load(other.date) == other
    store.save(original)
    assert store.load(DAY) == original
