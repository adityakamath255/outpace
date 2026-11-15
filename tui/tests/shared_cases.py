"""Writes spec/cases.json: inputs chosen here, outputs computed by this model, for
other implementations to check against. Run from tui/ with
`python -m tests.shared_cases`; `--check` exits 1 when the file is stale.

The file's conventions:
- Each group maps to a list of {"input": ..., "output": ...}. An output of
  {"error": message} means the input is rejected with that message.
- Fractions are strings like "5" and "5/2"; seconds and minutes are integers.
- Local times are ISO 8601 without an offset; tally times carry one. "zone" names the
  time zone that converts between them.
- "plans" holds plans by name, and cases refer to them by name. A plan is
  {"date": ..., "data": ...} for a day or {"id": ..., "data": ...} for a span, data
  being the JSON the database stores for it.
"""

import json
import os
import sys
import time
from collections.abc import Callable
from datetime import date, datetime
from fractions import Fraction
from pathlib import Path
from typing import Any

ZONE = "Asia/Kolkata"
# the generator converts local times in this process's zone
os.environ["TZ"] = ZONE
time.tzset()

from todo.db import decode, decode_span, encode, encode_span  # noqa: E402
from todo.model import (  # noqa: E402
    Day,
    Figures,
    Interval,
    Section,
    Span,
    Tally,
    Task,
    completion,
    days_text,
    duration_per_point,
    figures,
    interval_text,
    minutes_text,
    number,
    on_pace_at,
    pace_at,
    parse_clock,
    parse_date,
    parse_days,
    parse_minutes,
    percentage,
    points,
    schedule,
    seconds_since,
    tallied,
    tally_at,
)

PATH = Path(__file__).resolve().parents[2] / "spec" / "cases.json"


def fraction(value: Fraction | None) -> str | None:
    return None if value is None else str(value)


def instant(day: date, hour: int, minute: int) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute).astimezone()


def plan_json(plan: Day | Span) -> dict[str, Any]:
    if isinstance(plan, Day):
        return {"date": plan.date.isoformat(), "data": json.loads(encode(plan))}
    return {"id": plan.id, "data": json.loads(encode_span(plan))}


def plan_from(value: dict[str, Any]) -> Day | Span:
    text = json.dumps(value["data"])
    if "date" in value:
        return decode(date.fromisoformat(value["date"]), text)
    return decode_span(value["id"], text)


def task_json(task: Task) -> dict[str, Any]:
    return {
        "id": task.id,
        "title": task.title,
        "available": fraction(task.available),
        "earned": str(task.earned),
        "minutes": task.minutes,
    }


def tally_json(tally: Tally | None) -> list[str] | None:
    if tally is None:
        return None
    return [tally.at.isoformat(), str(tally.earned), str(tally.available)]


def figures_json(totals: Figures) -> dict[str, Any]:
    return {
        "earned": str(totals.earned),
        "available": str(totals.available),
        "worked": str(totals.worked),
        "needed": str(totals.needed),
        "timed_available": str(totals.timed_available),
        "timed_needed": str(totals.timed_needed),
        "undurated": totals.undurated,
        "elapsed": totals.elapsed,
        "scheduled": totals.scheduled,
        "progress": totals.progress,
        "pace": totals.pace,
        "free": fraction(totals.free),
        "schedule_per_point": totals.schedule_per_point,
        "duration_per_point": totals.duration_per_point,
    }


def cases(inputs: list[Any], compute: Callable[[Any], Any]) -> list[dict[str, Any]]:
    found = []
    for value in inputs:
        try:
            output = compute(value)
        except ValueError as error:
            output = {"error": str(error)}
        found.append({"input": value, "output": output})
    return found


def task(name: str, available: str | None, earned: str = "0", minutes=None) -> Task:
    return Task(
        name.capitalize(),
        None if available is None else points(available),
        points(earned),
        minutes,
        id=f"task-{name}",
    )


# a split day with every kind of task: done, partial, untouched, unscored, untimed
DAY = Day(
    date(2026, 11, 11),
    schedule([(450, 900), (990, 1380)]),
    (
        Section(
            "Work",
            (
                task("report", "6", "6", 120),
                task("review", "4", "1.5", 90),
                task("email", "1.5"),
            ),
            id="section-work",
        ),
        Section(
            "",
            (task("walk", None, minutes=30), task("read", "2", "0.5", 45)),
            id="section-untitled",
        ),
    ),
    (
        Tally(instant(date(2026, 11, 11), 7, 10), Fraction(0), Fraction(12)),
        Tally(instant(date(2026, 11, 11), 9, 45), Fraction(3), Fraction(12)),
        Tally(instant(date(2026, 11, 11), 12, 0), Fraction(6), Fraction(12)),
        Tally(instant(date(2026, 11, 11), 12, 0), Fraction(6), Fraction(27, 2)),
        Tally(instant(date(2026, 11, 11), 17, 30), Fraction(9, 1), Fraction(27, 2)),
    ),
)
# a schedule past midnight, with no tallies yet
NIGHT = Day(
    date(2026, 11, 12),
    schedule([(1260, 120)]),
    (Section("", (task("study", "3", "1", 180),), id="section-night"),),
)
# a week with two days off, one of them a range ending on the last day
SPAN = Span(
    "week",
    datetime(2026, 11, 9, 10, 30),
    datetime(2026, 11, 16, 18, 0),
    (
        Section(
            "", (task("thesis", "10", "4", 600), task("gym", "3")), id="section-span"
        ),
    ),
    "span-week",
    (date(2026, 11, 11), date(2026, 11, 15), date(2026, 11, 16)),
    (Tally(instant(date(2026, 11, 10), 20, 0), Fraction(4), Fraction(13)),),
)
EMPTY = Day(date(2026, 11, 13), (), (Section("", (), id="section-empty"),))
PLANS = {
    "day": plan_json(DAY),
    "night": plan_json(NIGHT),
    "span": plan_json(SPAN),
    "empty": plan_json(EMPTY),
}

MOMENTS = [
    "2026-11-11T07:00:00",
    "2026-11-11T07:30:00",
    "2026-11-11T09:45:00",
    "2026-11-11T12:00:00",
    "2026-11-11T15:45:00",
    "2026-11-11T18:20:30",
    "2026-11-12T01:00:00",
    "2026-11-12T23:30:00",
    "2026-11-13T00:30:00",
    "2026-11-15T12:00:00",
    "2026-11-17T00:00:00",
]


def figures_at(value: dict[str, Any]) -> dict[str, Any]:
    plan = plan_from(PLANS[value["plan"]])
    now = seconds_since(plan.origin, datetime.fromisoformat(value["now"]))
    return figures_json(figures(plan.tasks(), plan.intervals, now))


def at(value: dict[str, Any]) -> dict[str, Any]:
    plan = plan_from(PLANS[value["plan"]])
    moment = datetime.fromisoformat(value["moment"])
    return {
        "tally": tally_json(tally_at(plan, moment)),
        "pace": pace_at(plan, moment),
        "on_pace": fraction(on_pace_at(plan, moment)),
    }


def tallied_at(value: dict[str, Any]) -> list[list[str] | None]:
    plan = plan_from(PLANS[value["plan"]])
    if value["toggle"]:
        first = next(plan.tasks())
        plan = plan.replace_task(first.toggle())
    after = tallied(plan, datetime.fromisoformat(value["now"]))
    return [tally_json(tally) for tally in after.tallies]


def stored_task(value: dict[str, Any]) -> Task:
    data = {"intervals": [], "sections": [{"id": "s", "title": "", "tasks": [value]}]}
    (section,) = plan_from({"date": "2026-11-11", "data": data}).sections
    (task,) = section.tasks
    return task


def task_change(value: dict[str, Any]) -> dict[str, Any]:
    before = stored_task(value["task"])
    match value["change"]:
        case "toggle":
            after = before.toggle()
        case "step":
            after = before.step(value["delta"])
        case "edit":
            available = value["available"]
            after = before.edit(
                value["title"],
                None if available is None else Fraction(available),
                value["minutes"],
            )
        case change:
            raise AssertionError(change)
    return task_json(after)


def task_values(value: dict[str, Any]) -> dict[str, Any]:
    stored = stored_task(value)
    return {
        "complete": stored.complete,
        "completion": fraction(completion(stored)),
        "duration_per_point": fraction(duration_per_point(stored)),
    }


TASKS = [task_json(t) for t in (*DAY.sections[0].tasks, *DAY.sections[1].tasks)]
# a day stored before tallies, with the done state older days kept
OLD_DAY = {
    "date": "2026-11-01",
    "data": {
        "intervals": [[480, 1020]],
        "sections": [
            {
                "id": "section-old",
                "title": "",
                "tasks": [{**TASKS[0], "earned": "0", "checked": True}],
            }
        ],
    },
}


def generate() -> dict[str, Any]:
    return {
        "zone": ZONE,
        "plans": PLANS,
        "points": cases(
            ["2", "2.5", " 3 ", "0", "007.50", "1/3", "-1", "", "2.", ".5", "abc"],
            lambda value: str(points(value)),
        ),
        "minutes": cases(
            ["90", "1h30m", "1h", "45m", " 2H ", "1h 30m", "24h", "0", "", "1.5h", "h"],
            parse_minutes,
        ),
        "minutes_text": cases([1, 45, 60, 90, 1439, 1440], minutes_text),
        "clock": cases(
            ["07:30", "7:30", "0730", "730", "7", "23:59", "24:00", "12:60", "", "7:5"],
            parse_clock,
        ),
        "schedule": cases(
            [
                [[450, 900], [990, 1380]],
                [[1260, 120]],
                [[1320, 60], [120, 300]],
                [[600, 600]],
                [[990, 1380], [450, 900]],
                [],
            ],
            lambda clocks: [[i.start, i.end] for i in schedule(map(tuple, clocks))],
        ),
        "interval_text": cases(
            [[450, 900], [1260, 1560], [1500, 1620]],
            lambda value: interval_text(Interval(*value)),
        ),
        "date": cases(
            ["2026-11-20", " 2026-11-20 ", "20261120", "2026-11-31", "2026-2-1"],
            lambda value: parse_date(value).isoformat(),
        ),
        "days": cases(
            [
                "2026-11-20",
                "2026-11-25 - 2026-11-27, 2026-11-20",
                "2026-11-20, 2026-11-20 - 2026-11-21",
                "2026-12-31 - 2027-01-01",
                " , ",
                "2026-11-27 - 2026-11-25",
                "2026-11-20 -2026-11-21",
            ],
            lambda value: [day.isoformat() for day in parse_days(value)],
        ),
        "days_text": cases(
            [
                ["2026-11-20"],
                ["2026-11-20", "2026-11-21", "2026-11-22", "2026-11-25"],
                ["2026-12-31", "2027-01-01"],
                [],
            ],
            lambda value: days_text(tuple(map(date.fromisoformat, value))),
        ),
        "number": cases(
            ["5", "5/2", "1/8", "123456789/1000", "0"],
            lambda value: number(Fraction(value)),
        ),
        "percentage": cases(
            ["1", "0", "1/3", "2/3", "-1/3", "27/2", "1/100000"],
            lambda value: percentage(Fraction(value)),
        ),
        "span_intervals": cases(
            ["span"],
            lambda name: [[i.start, i.end] for i in plan_from(PLANS[name]).intervals],
        ),
        "figures": cases(
            [
                {"plan": plan, "now": now}
                for plan, now in [
                    ("day", "2026-11-11T07:00:00"),
                    ("day", "2026-11-11T12:00:00"),
                    ("day", "2026-11-11T15:45:00"),
                    ("day", "2026-11-11T18:20:30"),
                    ("day", "2026-11-12T01:00:00"),
                    ("night", "2026-11-12T23:30:00"),
                    ("night", "2026-11-13T00:30:00"),
                    ("span", "2026-11-11T12:00:00"),
                    ("span", "2026-11-15T12:00:00"),
                    ("empty", "2026-11-13T12:00:00"),
                ]
            ],
            figures_at,
        ),
        "at": cases(
            [
                {"plan": plan, "moment": moment}
                for plan in ("day", "night", "span")
                for moment in MOMENTS
            ],
            at,
        ),
        "tallied": cases(
            [
                # a change after the last tally, one with a clock set back, and none
                {"plan": "day", "now": "2026-11-11T19:00:00", "toggle": True},
                {"plan": "day", "now": "2026-11-11T17:00:00", "toggle": True},
                {"plan": "day", "now": "2026-11-11T19:00:00", "toggle": False},
                {"plan": "night", "now": "2026-11-12T21:05:00", "toggle": True},
                {"plan": "night", "now": "2026-11-12T21:05:00", "toggle": False},
                {"plan": "empty", "now": "2026-11-13T09:00:00", "toggle": False},
            ],
            tallied_at,
        ),
        "task_change": cases(
            [
                {"task": TASKS[0], "change": "toggle"},
                {"task": TASKS[1], "change": "toggle"},
                {"task": TASKS[3], "change": "toggle"},
                {"task": TASKS[1], "change": "step", "delta": 1},
                {"task": TASKS[1], "change": "step", "delta": 5},
                {"task": TASKS[1], "change": "step", "delta": -2},
                {"task": TASKS[3], "change": "step", "delta": 1},
                *(
                    {"task": TASKS[n], "change": "edit", **edit}
                    for n, edit in [
                        (0, {"title": " Report ", "available": "8", "minutes": 60}),
                        (1, {"title": "Review", "available": "1", "minutes": 90}),
                        (1, {"title": "Review", "available": None, "minutes": 90}),
                        (3, {"title": "Walk", "available": "2", "minutes": None}),
                        (2, {"title": " ", "available": "1.5", "minutes": None}),
                        (2, {"title": "Email", "available": "0", "minutes": None}),
                        (2, {"title": "Email", "available": "1", "minutes": 1441}),
                        (2, {"title": "Email", "available": "1", "minutes": 0}),
                    ]
                ),
            ],
            task_change,
        ),
        "task_values": cases(TASKS, task_values),
        # stored data each plan decodes from and encodes back to, compared as JSON
        "storage": cases(
            [*PLANS.values(), OLD_DAY],
            lambda value: plan_json(plan_from(value))["data"],
        ),
    }


def text() -> str:
    return json.dumps(generate(), indent=1, ensure_ascii=False) + "\n"


if __name__ == "__main__":
    if sys.argv[1:] == ["--check"]:
        sys.exit(PATH.read_text() != text())
    PATH.parent.mkdir(exist_ok=True)
    PATH.write_text(text())
