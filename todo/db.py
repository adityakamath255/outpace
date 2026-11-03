import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import date
from fractions import Fraction
from pathlib import Path

from .model import Day, Interval, Section, Task

VERSION = 2


def encode(day: Day) -> str:
    return json.dumps(
        {
            "intervals": [[interval.start, interval.end] for interval in day.intervals],
            "sections": [
                {
                    "id": section.id,
                    "title": section.title,
                    "tasks": [
                        {
                            "id": task.id,
                            "title": task.title,
                            "available": None
                            if task.available is None
                            else str(task.available),
                            "earned": str(task.earned),
                            "minutes": task.minutes,
                            "checked": task.checked,
                        }
                        for task in section.tasks
                    ],
                }
                for section in day.sections
            ],
        },
        ensure_ascii=False,
    )


def decode(list_date: date, text: str) -> Day:
    data = json.loads(text)
    return Day(
        list_date,
        tuple(Interval(start, end) for start, end in data["intervals"]),
        tuple(
            Section(
                section["title"],
                tuple(
                    Task(
                        task["title"],
                        None
                        if task["available"] is None
                        else Fraction(task["available"]),
                        Fraction(task["earned"]),
                        task["minutes"],
                        task["checked"],
                        task["id"],
                    )
                    for task in section["tasks"]
                ),
                section["id"],
            )
            for section in data["sections"]
        ),
    )


class Conflict(ValueError):
    """The stored day is not the one the caller expected."""


class Store:
    """One JSON document per day, keyed by its ISO date."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.transaction() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                connection.execute(
                    "CREATE TABLE days (date TEXT PRIMARY KEY, data TEXT NOT NULL)"
                )
            elif version != VERSION:
                raise ValueError(
                    f"Unsupported database version {version}; expected {VERSION}."
                )
            connection.execute(f"PRAGMA user_version = {VERSION}")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Commits when the block finishes and rolls back if it raises."""
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            yield connection

    def load(self, day: date) -> Day | None:
        with closing(sqlite3.connect(self.path)) as connection:
            return self._load(connection, day)

    def list_dates(self) -> tuple[date, ...]:
        with closing(sqlite3.connect(self.path)) as connection:
            return tuple(
                date.fromisoformat(value)
                for (value,) in connection.execute(
                    "SELECT date FROM days ORDER BY date DESC"
                )
            )

    def _load(self, connection: sqlite3.Connection, day: date) -> Day | None:
        row = connection.execute(
            "SELECT data FROM days WHERE date = ?", (day.isoformat(),)
        ).fetchone()
        return None if row is None else decode(day, row[0])

    def _expect_day(
        self, connection: sqlite3.Connection, list_date: date, expected: Day | None
    ) -> None:
        # compares parsed days, so stored text need not match encode exactly
        if self._load(connection, list_date) != expected:
            detail = (
                f"The list for {list_date} already exists. "
                if expected is None
                else "This list changed in another session. "
            )
            raise Conflict(detail + "Switch to another day and back to reload it.")

    def save(self, day: Day, *, expected: Day | None = None) -> None:
        # expected=None requires absence; otherwise the stored day must match
        with self.transaction() as connection:
            self._expect_day(connection, day.date, expected)
            connection.execute(
                "INSERT OR REPLACE INTO days VALUES (?, ?)",
                (day.date.isoformat(), encode(day)),
            )

    def delete(self, expected: Day) -> None:
        with self.transaction() as connection:
            self._expect_day(connection, expected.date, expected)
            connection.execute(
                "DELETE FROM days WHERE date = ?", (expected.date.isoformat(),)
            )
