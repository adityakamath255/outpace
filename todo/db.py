import json
import sqlite3
from datetime import date
from fractions import Fraction
from pathlib import Path

from .model import Day, Interval, Section, Task, empty_day

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
                        # days stored earlier also carry "checked", the done
                        # state unscored tasks no longer have; it is dropped
                        id=task["id"],
                    )
                    for task in section["tasks"]
                ),
                section["id"],
            )
            for section in data["sections"]
        ),
    )


class Store:
    """One JSON document per day, keyed by its ISO date.

    The store holds the database exclusively until closed, so no other session
    can change a day underneath this one. A blank day is never stored, so a
    blank day and a missing one are the same."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=0)
        try:
            # exclusive locking mode keeps the lock from the first transaction
            # until the connection closes
            self.connection.execute("PRAGMA locking_mode = EXCLUSIVE")
            try:
                self.connection.execute("BEGIN EXCLUSIVE")
            except sqlite3.OperationalError as error:
                if error.sqlite_errorcode == sqlite3.SQLITE_BUSY:
                    raise ValueError(
                        "The database is open in another todo session."
                    ) from error
                raise
            with self.connection:
                version = self.connection.execute("PRAGMA user_version").fetchone()[0]
                if version == 0:
                    self.connection.execute(
                        "CREATE TABLE days (date TEXT PRIMARY KEY, data TEXT NOT NULL)"
                    )
                elif version != VERSION:
                    raise ValueError(
                        f"Unsupported database version {version}; expected {VERSION}."
                    )
                self.connection.execute(f"PRAGMA user_version = {VERSION}")
        except BaseException:
            self.connection.close()
            raise

    def close(self) -> None:
        self.connection.close()

    def load(self, day: date) -> Day:
        row = self.connection.execute(
            "SELECT data FROM days WHERE date = ?", (day.isoformat(),)
        ).fetchone()
        return empty_day(day) if row is None else decode(day, row[0])

    def list_dates(self) -> tuple[date, ...]:
        return tuple(
            date.fromisoformat(value)
            for (value,) in self.connection.execute(
                "SELECT date FROM days ORDER BY date DESC"
            )
        )

    def save(self, day: Day) -> None:
        with self.connection:
            if day.blank:
                self.connection.execute(
                    "DELETE FROM days WHERE date = ?", (day.date.isoformat(),)
                )
            else:
                self.connection.execute(
                    "INSERT OR REPLACE INTO days VALUES (?, ?)",
                    (day.date.isoformat(), encode(day)),
                )
