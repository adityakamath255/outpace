import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal, localcontext
from fractions import Fraction
from itertools import pairwise
from uuid import uuid4

# sqlite stores integers in at most 64 signed bits
MAX_MINUTES = 2**63 - 1
_MINUTES_ERROR = (
    f"Estimated minutes must be a positive whole number up to {MAX_MINUTES}."
)
_CLOCK_ERROR = "Use a 24-hour time in H:MM or HH:MM format."


def identifier() -> str:
    return uuid4().hex


def points(value: str) -> Fraction:
    if not re.fullmatch(r"\d+(?:\.\d+)?", value.strip()):
        raise ValueError("Use a nonnegative decimal for points, such as 2.5.")
    return Fraction(value.strip())


def parse_minutes(value: str) -> int | None:
    value = value.strip()
    if not value:
        return None
    try:
        minutes = int(value)
    except ValueError as error:
        raise ValueError(_MINUTES_ERROR) from error
    if not value.isdecimal() or not 0 < minutes <= MAX_MINUTES:
        raise ValueError(_MINUTES_ERROR)
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
        if not self.title.strip():
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
        if self.minutes is not None and not 0 < self.minutes <= MAX_MINUTES:
            raise ValueError(_MINUTES_ERROR)

    @property
    def complete(self) -> bool:
        return self.checked if self.available is None else self.earned == self.available

    def toggle(self) -> Task:
        if self.available is None:
            return replace(self, checked=not self.checked)
        return replace(self, earned=Fraction(0) if self.complete else self.available)


@dataclass(frozen=True)
class Section:
    title: str
    tasks: tuple[Task, ...] = ()
    id: str = field(default_factory=identifier)


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

    def rename_section(self, section_id: str, title: str) -> Day:
        title = title.strip()
        if not title:
            raise ValueError("Give the section a title.")
        if not any(section.id == section_id for section in self.sections):
            raise ValueError("Choose a section.")
        return replace(
            self,
            sections=tuple(
                replace(section, title=title) if section.id == section_id else section
                for section in self.sections
            ),
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

    def move_task(self, task_id: str, offset: int) -> Day:
        for section in self.sections:
            for index, task in enumerate(section.tasks):
                if task.id == task_id:
                    destination = index + offset
                    if not 0 <= destination < len(section.tasks):
                        return self
                    tasks = list(section.tasks)
                    tasks[index], tasks[destination] = tasks[destination], tasks[index]
                    return replace(
                        self,
                        sections=tuple(
                            replace(s, tasks=tuple(tasks)) if s.id == section.id else s
                            for s in self.sections
                        ),
                    )
        return self


def empty_day(day: date) -> Day:
    return Day(
        day,
        (
            Interval(
                datetime.combine(day, time(7, 30)), datetime.combine(day, time(11))
            ),
            Interval(datetime.combine(day, time(16)), datetime.combine(day, time(23))),
        ),
        (Section("Tasks"),),
    )


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
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def per_point(seconds: int, available: Fraction) -> str:
    if not available:
        return "n/a"
    seconds = int(Fraction(seconds) / available)
    return f"{seconds // 60:02d}:{seconds % 60:02d}"
