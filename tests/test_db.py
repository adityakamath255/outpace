import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import date, datetime

import pytest

from todo.db import Store, decode
from todo.model import Day, Section, Span, Task, empty_day, points

DAY = date(2026, 11, 1)


def planned(day: date) -> Day:
    return replace(
        empty_day(day), sections=(Section("Tasks", (Task("Practice", points("2")),)),)
    )


def test_list_dates_returns_saved_days_newest_first_and_copies_coexist(tmp_path):
    with closing(Store(tmp_path / "todo.sqlite3")) as store:
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
    task = Task("Practice", points("2.5"), points("0.125"), 24 * 60)
    day = replace(empty_day(DAY), sections=(Section("Tasks", (task,)),))
    with closing(Store(path)) as store:
        store.save(day)
    with closing(Store(path)) as reopened:
        assert reopened.load(DAY) == day


def test_days_stored_with_unscored_done_states_still_load():
    # stored before unscored tasks lost their done state
    stored = (
        '{"intervals": [], "sections": [{"id": "s", "title": "", "tasks": [{"id": '
        '"t", "title": "Sleep", "available": null, "earned": "0", "minutes": null, '
        '"checked": true}]}]}'
    )
    assert decode(DAY, stored).sections[0].tasks == (Task("Sleep", None, id="t"),)


def test_blank_days_are_not_stored(tmp_path):
    day = planned(DAY)
    with closing(Store(tmp_path / "todo.sqlite3")) as store:
        store.save(day)
        store.save(day.remove_task(next(day.tasks()).id))
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
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("PRAGMA user_version = 4")
    with pytest.raises(ValueError, match="version 4"):
        Store(path)
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4


def test_a_version_2_database_keeps_its_days_and_gains_spans(tmp_path):
    path = tmp_path / "todo.sqlite3"
    day = planned(DAY)
    with closing(Store(path)) as store:
        store.save(day)
    # as the previous version left it: days only
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("DROP TABLE spans")
        connection.execute("PRAGMA user_version = 2")
    span = Span(
        "november",
        datetime(2026, 11, 6, 10, 30),
        datetime(2026, 12, 1),
        planned(DAY).sections,
    )
    with closing(Store(path)) as store:
        assert store.load(DAY) == day
        assert store.spans() == ()
        store.save(span)
    with closing(Store(path)) as store:
        assert store.spans() == (span,)
        store.delete_span(span.id)
        assert store.spans() == ()


def test_a_span_keeps_its_days_off_and_older_spans_have_none(tmp_path):
    span = Span(
        "november",
        datetime(2026, 11, 6, 10, 30),
        datetime(2026, 12, 1),
        planned(DAY).sections,
        off=(date(2026, 11, 20),),
    )
    path = tmp_path / "todo.sqlite3"
    with closing(Store(path)) as store:
        store.save(span)
        store.save(replace(span, id="older", off=()))
    # as spans were saved before days off
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "UPDATE spans SET data = json_remove(data, '$.off') WHERE id = 'older'"
        )
    with closing(Store(path)) as store:
        assert set(store.spans()) == {span, replace(span, id="older", off=())}
