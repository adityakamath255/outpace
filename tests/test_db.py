import sqlite3
from dataclasses import replace
from datetime import date

import pytest

from todo.db import Store
from todo.model import Day, Section, Task, empty_day, points

DAY = date(2026, 11, 1)


def planned(day: date) -> Day:
    return replace(
        empty_day(day), sections=(Section("Tasks", (Task("Practice", points("2")),)),)
    )


def test_list_dates_returns_saved_days_newest_first_and_copies_coexist(tmp_path):
    store = Store(tmp_path / "todo.sqlite3")
    assert store.list_dates() == ()
    source = planned(DAY)
    copied = source.copy_to(date(2026, 11, 3))
    store.save(copied)
    store.save(source)

    assert store.list_dates() == (copied.date, source.date)
    assert store.load(source.date) == source
    assert store.load(copied.date) == copied
    assert store.load(date(2026, 11, 2)).blank


def test_days_survive_reopening(tmp_path):
    path = tmp_path / "nested" / "todo.sqlite3"
    store = Store(path)
    task = Task("Practice", points("2.5"), points("0.125"), 24 * 60)
    day = replace(empty_day(DAY), sections=(Section("Tasks", (task,)),))
    store.save(day)
    store.close()
    assert Store(path).load(DAY) == day


def test_blank_days_are_not_stored(tmp_path):
    store = Store(tmp_path / "todo.sqlite3")
    day = planned(DAY)
    store.save(day)
    store.save(day.remove_task(next(day.tasks())[1].id))
    assert store.list_dates() == (DAY,)
    store.save(empty_day(DAY))
    assert store.list_dates() == ()


def test_a_second_session_cannot_open_the_database(tmp_path):
    path = tmp_path / "todo.sqlite3"
    store = Store(path)
    with pytest.raises(ValueError, match="another todo session"):
        Store(path)
    store.close()
    Store(path).close()


def test_newer_database_is_not_modified(tmp_path):
    path = tmp_path / "todo.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version = 3")
    with pytest.raises(ValueError, match="version 3"):
        Store(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
