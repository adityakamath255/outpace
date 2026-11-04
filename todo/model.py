import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time
from decimal import Decimal, localcontext
from fractions import Fraction
from itertools import pairwise
from typing import Literal
from uuid import uuid4

_DURATION_ERROR = "Use minutes, like 90, or hours and minutes, like 1h30m."
_UNSCORED_ERROR = "Unscored tasks have no done state."
_CLOCK_ERROR = "Use a 24-hour time in H:MM or HH:MM format."
DAY_MINUTES = 24 * 60


def identifier() -> str:
    return uuid4().hex


def points(value: str) -> Fraction:
    value = value.strip()
    if not re.fullmatch(r"\d+(?:\.\d+)?", value):
        raise ValueError("Use a nonnegative decimal for points, such as 2.5.")
    return Fraction(value)


def parse_minutes(value: str) -> int | None:
    """Reads a duration in minutes, from 90 or 1h30m; Task decides which
    durations are allowed."""
    value = value.strip().lower()
    if not value:
        return None
    if re.fullmatch(r"[0-9]+", value):
        return int(value)
    match = re.fullmatch(r"(?:([0-9]+)h)?\s*(?:([0-9]+)m)?", value)
    if not match or not any(match.groups()):
        raise ValueError(_DURATION_ERROR)
    hours, minutes = (int(part or 0) for part in match.groups())
    return hours * 60 + minutes


@dataclass(frozen=True)
class Task:
    title: str
    # None makes an unscored task: a plain list item, never done or not done
    available: Fraction | None
    earned: Fraction = Fraction(0)
    minutes: int | None = None
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
        elif self.earned:
            raise ValueError("Unscored tasks cannot have earned points.")
        if self.minutes is not None:
            if self.minutes <= 0:
                raise ValueError("A duration must be positive.")
            # a list's schedule spans at most a day, so a longer task cannot fit it
            if self.minutes > DAY_MINUTES:
                raise ValueError("A duration cannot exceed 24 hours.")

    @property
    def complete(self) -> bool:
        return self.available is not None and self.earned == self.available

    def toggle(self) -> Task:
        if self.available is None:
            raise ValueError(_UNSCORED_ERROR)
        return replace(self, earned=Fraction(0) if self.complete else self.available)

    def edit(self, title: str, available: Fraction | None, minutes: int | None) -> Task:
        """Change a task's plan: a done task stays done, and partial progress is
        capped at the new available points."""
        if available is None:
            earned = Fraction(0)
        elif self.complete:
            earned = available
        else:
            earned = min(self.earned, available)
        return replace(
            self, title=title, available=available, earned=earned, minutes=minutes
        )

    def duplicate(self, *, keep_progress: bool) -> Task:
        """The same plan under a new identity, with or without its progress."""
        if keep_progress:
            return replace(self, id=identifier())
        return replace(self, id=identifier(), earned=Fraction(0))

    def step(self, delta: int) -> Task:
        if self.available is None:
            raise ValueError(_UNSCORED_ERROR)
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

    def duplicate(self, *, keep_progress: bool) -> Section:
        return Section(
            self.title,
            tuple(task.duplicate(keep_progress=keep_progress) for task in self.tasks),
        )


@dataclass(frozen=True)
class Interval:
    """Minutes from the start of the list's day; from DAY_MINUTES on is the
    next day, so a list's schedule can run past midnight."""

    start: int
    end: int

    def __post_init__(self) -> None:
        if self.start < 0:
            raise ValueError("An interval cannot start before its day.")
        if self.end <= self.start:
            raise ValueError("An interval must end after it starts.")

    @property
    def seconds(self) -> int:
        return (self.end - self.start) * 60


def _clock(text: str) -> int:
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", text.strip())
    if match is None or int(match[1]) > 23 or int(match[2]) > 59:
        raise ValueError(_CLOCK_ERROR)
    return int(match[1]) * 60 + int(match[2])


def parse_schedule(text: str) -> tuple[Interval, ...]:
    """Reads intervals written in order, like 07:30 - 11:00, 16:00 - 23:00.

    The first starts on the list's day. Each later one starts at the first time
    with its clock reading at or after the previous end, so writing past
    midnight continues into the next day; blank means no schedule."""
    intervals: list[Interval] = []
    for part in filter(None, (part.strip() for part in text.split(","))):
        start_text, dash, end_text = part.partition("-")
        if not dash:
            raise ValueError("Write each interval as start - end, like 07:30 - 11:00.")
        start, end = _clock(start_text), _clock(end_text)
        if start == end:
            raise ValueError("An interval cannot have identical start and end times.")
        previous_end = intervals[-1].end if intervals else 0
        while start < previous_end:
            start += DAY_MINUTES
        intervals.append(Interval(start, start + (end - start) % DAY_MINUTES))
    # an interval written out of order would otherwise roll silently into tomorrow
    if intervals and intervals[-1].end - intervals[0].start > DAY_MINUTES:
        raise ValueError(
            "The schedule spans more than 24 hours; write its intervals in order."
        )
    return tuple(intervals)


def format_schedule(intervals: tuple[Interval, ...]) -> str:
    return ", ".join(interval_text(i) for i in intervals)


def interval_text(interval: Interval) -> str:
    return f"{clock(interval.start)} - {clock(interval.end)}"


def parse_date(value: str) -> date:
    value = value.strip()
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        parsed = None
    # fromisoformat also accepts forms like 20261102
    if parsed is None or value != parsed.isoformat():
        raise ValueError("Use a date in YYYY-MM-DD format.")
    return parsed


def clock(minutes: int) -> str:
    return f"{minutes // 60 % 24:02d}:{minutes % 60:02d}"


def seconds_into(list_date: date, now: datetime) -> int:
    """Seconds from the start of the list's day, comparable with its intervals."""
    return int((now - datetime.combine(list_date, time())).total_seconds())


@dataclass(frozen=True)
class Day:
    date: date
    # stored in start order whatever order they are given in
    intervals: tuple[Interval, ...]
    sections: tuple[Section, ...]

    def __post_init__(self) -> None:
        if not self.sections:
            raise ValueError("A list needs at least one section.")
        ordered = tuple(sorted(self.intervals, key=lambda interval: interval.start))
        object.__setattr__(self, "intervals", ordered)
        if any(a.end > b.start for a, b in pairwise(ordered)):
            raise ValueError("Scheduled intervals cannot overlap.")
        ids = [section.id for section in self.sections]
        ids += [task.id for _, task in self.tasks()]
        if len(ids) != len(set(ids)):
            raise ValueError("Task and section identities must be unique.")

    @property
    def blank(self) -> bool:
        """Nothing planned: what a day without a saved list looks like."""
        return (
            not self.intervals
            and len(self.sections) == 1
            and not self.sections[0].title
            and not self.sections[0].tasks
        )

    def tasks(self) -> Iterator[tuple[Section, Task]]:
        return ((section, task) for section in self.sections for task in section.tasks)

    def copy_to(self, list_date: date) -> Day:
        sections = tuple(s.duplicate(keep_progress=False) for s in self.sections)
        return Day(list_date, self.intervals, sections)

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

    def insert_section(self, section: Section, index: int) -> Day:
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

    def insert_task(self, task: Task, section_id: str, index: int) -> Day:
        s = self._section_index(section_id)
        sections = list(self.sections)
        tasks = sections[s].tasks
        sections[s] = replace(
            sections[s], tasks=tasks[:index] + (task,) + tasks[index:]
        )
        return replace(self, sections=tuple(sections))

    def replace_task(self, task: Task) -> Day:
        """Replaces the task with the same id, wherever it is."""
        return replace(
            self,
            sections=tuple(
                replace(s, tasks=tuple(task if t.id == task.id else t for t in s.tasks))
                for s in self.sections
            ),
        )

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


def time_progress(intervals: tuple[Interval, ...], now: int) -> tuple[int, int]:
    """Elapsed and total scheduled seconds, now being seconds_into the day."""
    return (
        sum(min(max(now - i.start * 60, 0), i.seconds) for i in intervals),
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


def percentage(value: Fraction | None) -> str:
    if value is None:
        return "n/a"
    hundredths = int(value * 10_000)
    sign = "-" if hundredths < 0 else ""
    whole, decimal = divmod(abs(hundredths), 100)
    return f"{sign}{whole}.{decimal:02d}"


def per_point(seconds: int, available: Fraction) -> int | None:
    """Whole seconds per point, or None without points."""
    return int(Fraction(seconds) / available) if available else None
