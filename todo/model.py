import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal, localcontext
from fractions import Fraction
from itertools import pairwise
from typing import Literal
from uuid import uuid4

# sqlite stores integers in at most 64 signed bits
MAX_MINUTES = 2**63 - 1
_MINUTES_ERROR = "Use a positive whole number of minutes."
_MINUTES_TOO_LARGE = "Estimate too large."
_CLOCK_ERROR = "Use a 24-hour time in H:MM or HH:MM format."


def identifier() -> str:
    return uuid4().hex


def points(value: str) -> Fraction:
    if not re.fullmatch(r"\d+(?:\.\d+)?", value.strip()):
        raise ValueError("Use a nonnegative decimal for points, such as 2.5.")
    return Fraction(value.strip())


def _check_minutes(minutes: int) -> None:
    if not minutes > 0:
        raise ValueError(_MINUTES_ERROR)
    if minutes > MAX_MINUTES:
        raise ValueError(_MINUTES_TOO_LARGE)


def parse_minutes(value: str) -> int | None:
    value = value.strip()
    if not value:
        return None
    if not value.isdecimal():
        raise ValueError(_MINUTES_ERROR)
    try:
        minutes = int(value)
    except ValueError as error:
        raise ValueError(_MINUTES_TOO_LARGE) from error
    _check_minutes(minutes)
    return minutes


@dataclass(frozen=True)
class Task:
    title: str
    available: Fraction | None
    earned: Fraction = Fraction(0)
    minutes: int | None = None
    checked: bool = False
    id: str = field(default_factory=identifier)

    def __post_init__(self) -> None:
        object.__setattr__(self, "title", self.title.strip())
        if not self.title:
            raise ValueError("Give the task a title.")
        if self.available is not None:
            if self.available <= 0 or not 0 <= self.earned <= self.available:
                raise ValueError(
                    "Available points must be positive; earned points must be "
                    "between zero and available points."
                )
            if self.checked:
                raise ValueError("Scored tasks derive completion from their points.")
        elif self.earned:
            raise ValueError("Unscored tasks cannot have earned points.")
        if self.minutes is not None:
            _check_minutes(self.minutes)

    @property
    def complete(self) -> bool:
        return self.checked if self.available is None else self.earned == self.available

    def toggle(self) -> Task:
        if self.available is None:
            return replace(self, checked=not self.checked)
        return replace(self, earned=Fraction(0) if self.complete else self.available)

    def edit(self, title: str, available: Fraction | None, minutes: int | None) -> Task:
        """Change a task's plan: a done task stays done, and partial progress is
        capped at the new available points."""
        if available is None:
            return replace(
                self,
                title=title,
                available=None,
                earned=Fraction(0),
                minutes=minutes,
                checked=self.complete,
            )
        return replace(
            self,
            title=title,
            available=available,
            earned=available if self.complete else min(self.earned, available),
            minutes=minutes,
            checked=False,
        )

    def step(self, delta: int) -> Task:
        if self.available is None:
            return replace(self, checked=delta > 0)
        return replace(
            self, earned=min(max(self.earned + delta, Fraction(0)), self.available)
        )


@dataclass(frozen=True)
class Section:
    # an empty title is an untitled group, like a blank line in Markdown
    title: str
    tasks: tuple[Task, ...] = ()
    id: str = field(default_factory=identifier)

    def __post_init__(self) -> None:
        object.__setattr__(self, "title", self.title.strip())


@dataclass(frozen=True)
class Interval:
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValueError("An interval must end after it starts.")

    @property
    def seconds(self) -> int:
        return int((self.end - self.start).total_seconds())


def parse_interval(
    day: date, start: str, end: str, *, next_day: bool = False
) -> Interval:
    clocks = (start.strip(), end.strip())
    if any(re.fullmatch(r"\d{1,2}:\d{2}", clock) is None for clock in clocks):
        raise ValueError(_CLOCK_ERROR)
    try:
        start_clock, end_clock = (time.strptime(clock, "%H:%M") for clock in clocks)
    except ValueError as error:
        raise ValueError(_CLOCK_ERROR) from error
    if start_clock == end_clock:
        raise ValueError("An interval cannot have identical start and end times.")
    try:
        start_day = day + timedelta(days=1) if next_day else day
        start_at = datetime.combine(start_day, start_clock)
        end_at = datetime.combine(start_day, end_clock)
        if end_clock < start_clock:
            end_at += timedelta(days=1)
    except OverflowError as error:
        raise ValueError(
            "Scheduled interval exceeds the supported date range."
        ) from error
    return Interval(start_at, end_at)


@dataclass(frozen=True)
class Day:
    date: date
    intervals: tuple[Interval, ...]
    sections: tuple[Section, ...]

    def __post_init__(self) -> None:
        if not self.sections:
            raise ValueError("A list needs at least one section.")
        ordered = sorted(self.intervals, key=lambda interval: interval.start)
        if any(a.end > b.start for a, b in pairwise(ordered)):
            raise ValueError("Scheduled intervals cannot overlap.")
        ids = [section.id for section in self.sections]
        ids += [task.id for _, task in self.tasks()]
        if len(ids) != len(set(ids)):
            raise ValueError("Task and section identities must be unique.")

    def tasks(self) -> Iterator[tuple[Section, Task]]:
        return ((section, task) for section in self.sections for task in section.tasks)

    def copy_to(self, list_date: date) -> Day:
        offset = list_date - self.date
        try:
            intervals = tuple(
                Interval(interval.start + offset, interval.end + offset)
                for interval in self.intervals
            )
        except OverflowError as error:
            raise ValueError(
                "Scheduled interval exceeds the supported date range."
            ) from error
        sections = tuple(
            Section(
                section.title,
                tuple(
                    replace(task, id=identifier(), earned=Fraction(0), checked=False)
                    for task in section.tasks
                ),
            )
            for section in self.sections
        )
        return Day(list_date, intervals, sections)

    def _section_index(self, section_id: str) -> int:
        for index, section in enumerate(self.sections):
            if section.id == section_id:
                return index
        raise ValueError("Choose a section.")

    def rename_section(self, section_id: str, title: str) -> Day:
        index = self._section_index(section_id)
        sections = list(self.sections)
        sections[index] = replace(sections[index], title=title)
        return replace(self, sections=tuple(sections))

    def insert_section(self, section: Section, after: str) -> Day:
        index = self._section_index(after) + 1
        return replace(
            self,
            sections=self.sections[:index] + (section,) + self.sections[index:],
        )

    def move_section(self, section_id: str, offset: int) -> Day:
        index = self._section_index(section_id)
        destination = index + offset
        if not 0 <= destination < len(self.sections):
            return self
        sections = list(self.sections)
        sections[index], sections[destination] = sections[destination], sections[index]
        return replace(self, sections=tuple(sections))

    def remove_section(self, section_id: str) -> Day:
        index = self._section_index(section_id)
        if len(self.sections) == 1:
            # a day keeps at least one section, so removing the last one empties it
            return replace(self, sections=(replace(self.sections[0], tasks=()),))
        return replace(
            self, sections=self.sections[:index] + self.sections[index + 1 :]
        )

    def put_task(self, task: Task, section_id: str) -> Day:
        if not any(section.id == section_id for section in self.sections):
            raise ValueError("Choose a section.")
        sections = []
        for section in self.sections:
            if section.id == section_id:
                tasks = tuple(task if t.id == task.id else t for t in section.tasks)
                if all(t.id != task.id for t in section.tasks):
                    tasks += (task,)
            else:
                tasks = tuple(t for t in section.tasks if t.id != task.id)
            sections.append(replace(section, tasks=tasks))
        return replace(self, sections=tuple(sections))

    def remove_task(self, task_id: str) -> Day:
        return replace(
            self,
            sections=tuple(
                replace(s, tasks=tuple(t for t in s.tasks if t.id != task_id))
                for s in self.sections
            ),
        )

    def _task_position(self, task_id: str) -> tuple[int, int] | None:
        for s, section in enumerate(self.sections):
            for index, task in enumerate(section.tasks):
                if task.id == task_id:
                    return s, index
        return None

    def move_task(self, task_id: str, offset: Literal[-1, 1]) -> Day:
        """Move a task one place; past either end of its section it joins the
        neighbouring section, as a line moves past a blank line in Markdown."""
        position = self._task_position(task_id)
        if position is None:
            return self
        s, index = position
        sections = [list(section.tasks) for section in self.sections]
        destination = index + offset
        if 0 <= destination < len(sections[s]):
            tasks = sections[s]
            tasks[index], tasks[destination] = tasks[destination], tasks[index]
        elif 0 <= s + offset < len(sections):
            task = sections[s].pop(index)
            if offset > 0:
                sections[s + offset].insert(0, task)
            else:
                sections[s + offset].append(task)
        else:
            return self
        return replace(
            self,
            sections=tuple(
                replace(section, tasks=tuple(tasks))
                for section, tasks in zip(self.sections, sections, strict=True)
            ),
        )


def empty_day(day: date) -> Day:
    return Day(day, (), (Section(""),))


def score(tasks: Iterable[Task]) -> tuple[Fraction, Fraction]:
    earned = Fraction(0)
    available = Fraction(0)
    for task in tasks:
        if task.available is not None:
            earned += task.earned
            available += task.available
    return earned, available


def time_progress(intervals: tuple[Interval, ...], now: datetime) -> tuple[int, int]:
    return (
        sum(
            min(max(int((now - i.start).total_seconds()), 0), i.seconds)
            for i in intervals
        ),
        sum(i.seconds for i in intervals),
    )


def ratio(numerator: int | Fraction, denominator: int | Fraction) -> Fraction | None:
    return Fraction(numerator, denominator) if denominator else None


def number(value: Fraction) -> str:
    if value.denominator == 1:
        return str(value.numerator)
    # terminating fractions need at most numerator digits + denominator bits of precision
    with localcontext() as context:
        context.prec = len(str(value.numerator)) + value.denominator.bit_length()
        return (
            format(Decimal(value.numerator) / Decimal(value.denominator), "f")
            .rstrip("0")
            .rstrip(".")
        )


def percentage(value: Fraction | None, *, signed: bool = False) -> str:
    if value is None:
        return "n/a"
    hundredths = int(value * 10_000)
    sign = "-" if hundredths < 0 else "+" if signed else ""
    whole, decimal = divmod(abs(hundredths), 100)
    return f"{sign}{whole}.{decimal:02d}"


def duration(seconds: int) -> str:
    parts = zip((seconds // 3600, seconds // 60 % 60, seconds % 60), "hms", strict=True)
    return "".join(f"{value}{unit}" for value, unit in parts if value) or "0s"


def per_point(seconds: int, available: Fraction) -> str:
    if not available:
        return "n/a"
    return duration(int(Fraction(seconds) / available))
