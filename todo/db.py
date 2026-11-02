import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import date, datetime
from fractions import Fraction
from pathlib import Path

from .model import Day, Interval, Section, Task

SCHEMA = """
BEGIN;
CREATE TABLE days (date TEXT PRIMARY KEY);
CREATE TABLE intervals (
    day TEXT NOT NULL REFERENCES days(date) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    start TEXT NOT NULL,
    end TEXT NOT NULL,
    PRIMARY KEY (day, position)
);
CREATE TABLE sections (
    id TEXT PRIMARY KEY,
    day TEXT NOT NULL REFERENCES days(date) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    title TEXT NOT NULL
);
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    section TEXT NOT NULL REFERENCES sections(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    title TEXT NOT NULL,
    available TEXT,
    earned TEXT NOT NULL,
    minutes INTEGER CHECK (minutes > 0),
    checked INTEGER NOT NULL CHECK (checked IN (0, 1))
);
PRAGMA user_version = 1;
COMMIT;
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                connection.executescript(SCHEMA)
            elif version != 1:
                raise ValueError(f"Unsupported database version {version}; expected 1.")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            yield connection

    def load(self, day: date) -> Day | None:
        with self.connect() as connection, connection:
            connection.execute("BEGIN")
            return self._load(connection, day)

    def _load(self, connection: sqlite3.Connection, day: date) -> Day | None:
        if not connection.execute(
            "SELECT 1 FROM days WHERE date = ?", (day.isoformat(),)
        ).fetchone():
            return None
        intervals = tuple(
            Interval(datetime.fromisoformat(start), datetime.fromisoformat(end))
            for start, end in connection.execute(
                "SELECT start, end FROM intervals WHERE day = ? ORDER BY position",
                (day.isoformat(),),
            )
        )
        sections = []
        for section_id, title in connection.execute(
            "SELECT id, title FROM sections WHERE day = ? ORDER BY position",
            (day.isoformat(),),
        ):
            tasks = tuple(
                Task(
                    title=title,
                    available=Fraction(available) if available is not None else None,
                    earned=Fraction(earned),
                    minutes=minutes,
                    checked=bool(checked),
                    id=task_id,
                )
                for task_id, title, available, earned, minutes, checked in connection.execute(
                    "SELECT id, title, available, earned, minutes, checked "
                    "FROM tasks WHERE section = ? ORDER BY position",
                    (section_id,),
                )
            )
            sections.append(Section(title, tasks, section_id))
        return Day(day, intervals, tuple(sections))

    def _expect_day(
        self, connection: sqlite3.Connection, list_date: date, expected: Day | None
    ) -> Day | None:
        current = self._load(connection, list_date)
        if current != expected:
            detail = (
                f"The list for {list_date} already exists. "
                if expected is None
                else "This list changed in another session. "
            )
            raise ValueError(detail + "Switch to another day and back to reload it.")
        return current

    def save(self, day: Day, *, expected: Day | None = None) -> None:
        # expected=None requires absence; otherwise the stored snapshot must match
        with self.connect() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._expect_day(connection, day.date, expected)
            if current is None:
                connection.execute(
                    "INSERT INTO days VALUES (?)", (day.date.isoformat(),)
                )
            connection.execute(
                "DELETE FROM intervals WHERE day = ?", (day.date.isoformat(),)
            )
            connection.execute(
                "DELETE FROM sections WHERE day = ?", (day.date.isoformat(),)
            )
            connection.executemany(
                "INSERT INTO intervals VALUES (?, ?, ?, ?)",
                (
                    (
                        day.date.isoformat(),
                        index,
                        interval.start.isoformat(),
                        interval.end.isoformat(),
                    )
                    for index, interval in enumerate(day.intervals)
                ),
            )
            for index, section in enumerate(day.sections):
                connection.execute(
                    "INSERT INTO sections VALUES (?, ?, ?, ?)",
                    (section.id, day.date.isoformat(), index, section.title),
                )
                connection.executemany(
                    "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        (
                            task.id,
                            section.id,
                            position,
                            task.title,
                            str(task.available) if task.available is not None else None,
                            str(task.earned),
                            task.minutes,
                            int(task.checked),
                        )
                        for position, task in enumerate(section.tasks)
                    ),
                )

    def delete(self, expected: Day) -> None:
        with self.connect() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expect_day(connection, expected.date, expected)
            connection.execute(
                "DELETE FROM days WHERE date = ?", (expected.date.isoformat(),)
            )
