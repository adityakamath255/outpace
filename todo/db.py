import json
import sqlite3
from datetime import date, datetime
from fractions import Fraction
from pathlib import Path
from typing import Any

from .model import Day, Interval, Section, Span, Tally, Task, empty_day

# version 3 added the spans table; version 2 databases get it on open
VERSION = 3
SPANS_TABLE = "CREATE TABLE spans (id TEXT PRIMARY KEY, data TEXT NOT NULL)"


def encode_sections(sections: tuple[Section, ...]) -> list[dict[str, Any]]:
    return [
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
        for section in sections
    ]


def decode_sections(data: list[dict[str, Any]]) -> tuple[Section, ...]:
    return tuple(
        Section(
            section["title"],
            tuple(
                Task(
                    task["title"],
                    None if task["available"] is None else Fraction(task["available"]),
                    Fraction(task["earned"]),
                    task["minutes"],
                    # older days also have a "checked" field, which is ignored
                    id=task["id"],
                )
                for task in section["tasks"]
            ),
            section["id"],
        )
        for section in data
    )


def encode_tallies(tallies: tuple[Tally, ...]) -> list[list[str]]:
    return [
        [tally.at.isoformat(), str(tally.earned), str(tally.available)]
        for tally in tallies
    ]


def decode_tallies(data: list[list[str]]) -> tuple[Tally, ...]:
    return tuple(
        Tally(datetime.fromisoformat(at), Fraction(earned), Fraction(available))
        for at, earned, available in data
    )


def encode(day: Day) -> str:
    return json.dumps(
        {
            "intervals": [[interval.start, interval.end] for interval in day.intervals],
            "sections": encode_sections(day.sections),
            "tallies": encode_tallies(day.tallies),
        },
        ensure_ascii=False,
    )


def decode(list_date: date, text: str) -> Day:
    data = json.loads(text)
    return Day(
        list_date,
        tuple(Interval(start, end) for start, end in data["intervals"]),
        decode_sections(data["sections"]),
        # days saved before tallies have none
        decode_tallies(data.get("tallies", [])),
    )


def encode_span(span: Span) -> str:
    return json.dumps(
        {
            "title": span.title,
            "start": span.start.isoformat(timespec="minutes"),
            "end": span.end.isoformat(timespec="minutes"),
            "off": [day.isoformat() for day in span.off],
            "sections": encode_sections(span.sections),
            "tallies": encode_tallies(span.tallies),
        },
        ensure_ascii=False,
    )


def decode_span(span_id: str, text: str) -> Span:
    data = json.loads(text)
    return Span(
        data["title"],
        datetime.fromisoformat(data["start"]),
        datetime.fromisoformat(data["end"]),
        decode_sections(data["sections"]),
        span_id,
        # spans saved before days off or tallies have neither
        tuple(date.fromisoformat(day) for day in data.get("off", [])),
        decode_tallies(data.get("tallies", [])),
    )


class Store:
    """One JSON document per day, keyed by ISO date, and one per span, keyed by id.
    Holds the database exclusively until closed. Blank days aren't stored, so loading
    a missing day gives a blank one. Spans stay until deleted."""

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
                    self.connection.execute(SPANS_TABLE)
                elif version == 2:
                    self.connection.execute(SPANS_TABLE)
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

    def spans(self) -> tuple[Span, ...]:
        """All spans, sorted by start then end. There are few enough to load at once."""
        spans = (
            decode_span(span_id, data)
            for span_id, data in self.connection.execute("SELECT id, data FROM spans")
        )
        return tuple(sorted(spans, key=lambda span: (span.start, span.end)))

    def save(self, plan: Day | Span) -> None:
        with self.connection:
            match plan:
                case Day() if plan.blank:
                    self.connection.execute(
                        "DELETE FROM days WHERE date = ?", (plan.date.isoformat(),)
                    )
                case Day():
                    self.connection.execute(
                        "INSERT OR REPLACE INTO days VALUES (?, ?)",
                        (plan.date.isoformat(), encode(plan)),
                    )
                case Span():
                    self.connection.execute(
                        "INSERT OR REPLACE INTO spans VALUES (?, ?)",
                        (plan.id, encode_span(plan)),
                    )

    def delete_span(self, span_id: str) -> None:
        with self.connection:
            self.connection.execute("DELETE FROM spans WHERE id = ?", (span_id,))
