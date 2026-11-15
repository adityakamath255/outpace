import re
from bisect import bisect_right
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal, localcontext
from fractions import Fraction
from itertools import pairwise
from typing import Literal, Self
from uuid import uuid4

_DURATION_ERROR = "Use minutes, like 90, or hours and minutes, like 1h30m."
_UNSCORED_ERROR = "Unscored tasks have no done state."
_CLOCK_ERROR = "Use a 24-hour time, like 07:30, 0730 or 7."
DAY_MINUTES = 24 * 60


def identifier() -> str:
    return uuid4().hex


def points(value: str) -> Fraction:
    value = value.strip()
    if not re.fullmatch(r"\d+(?:\.\d+)?", value):
        raise ValueError("Use a nonnegative decimal for points, such as 2.5.")
    return Fraction(value)


def parse_minutes(value: str) -> int | None:
    """Reads 90 or 1h30m as minutes. Task checks the range."""
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


def minutes_text(minutes: int) -> str:
    """Writes minutes the way parse_minutes reads them, such as 1h30m or 24h."""
    hours, rest = divmod(minutes, 60)
    return (f"{hours}h" if hours else "") + (f"{rest}m" if rest or not hours else "")


@dataclass(frozen=True)
class Task:
    title: str
    # None means unscored: a plain list item with no done state
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
            # a task has to fit in a day's schedule
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
        earned = self.earned if keep_progress else Fraction(0)
        return replace(self, id=identifier(), earned=earned)

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
    """Minutes from the list's origin: midnight for a day, or the span's start. In a
    day, values past DAY_MINUTES are on the next day."""

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

    @property
    def next_day(self) -> bool:
        return self.start >= DAY_MINUTES


def parse_clock(text: str) -> int:
    """Minutes into the day from a 24-hour time, written like 07:30, 7:30,
    0730, 730 or a bare hour like 7."""
    match = re.fullmatch(r"(\d{1,2})(?::?(\d{2}))?", text.strip())
    if match is None or int(match[1]) > 23 or int(match[2] or 0) > 59:
        raise ValueError(_CLOCK_ERROR)
    return int(match[1]) * 60 + int(match[2] or 0)


def over_a_day(intervals: tuple[Interval, ...]) -> bool:
    """Whether intervals, in start order and not overlapping, end more than 24
    hours after the first starts."""
    return bool(intervals) and intervals[-1].end - intervals[0].start > DAY_MINUTES


def schedule(clocks: Iterable[tuple[int, int]]) -> tuple[Interval, ...]:
    """Intervals from start and end clock times, in minutes, written in order.

    The first starts on the list's day. Each later one starts at the first time
    with its clock reading at or after the previous end, so writing past
    midnight continues into the next day."""
    intervals: list[Interval] = []
    for start, end in clocks:
        previous_end = intervals[-1].end if intervals else 0
        while start < previous_end:
            start += DAY_MINUTES
        intervals.append(Interval(start, start + (end - start) % DAY_MINUTES))
    # writing intervals out of order would push them onto the next day
    if over_a_day(tuple(intervals)):
        raise ValueError(
            "The schedule spans more than 24 hours; write its intervals in order."
        )
    return tuple(intervals)


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


def parse_days(text: str) -> tuple[date, ...]:
    """Dates from a comma-separated list of dates and ranges like 2026-11-25 -
    2026-11-27, in order and without repeats. Blank text gives none."""
    days: set[date] = set()
    for part in filter(None, (part.strip() for part in text.split(","))):
        first, _, last = part.partition(" - ")
        start = parse_date(first)
        end = parse_date(last) if last else start
        if end < start:
            raise ValueError(f"The range {part} must end after it starts.")
        days.update(start + timedelta(days=n) for n in range((end - start).days + 1))
    return tuple(sorted(days))


def days_text(days: tuple[date, ...]) -> str:
    """days, in order, as parse_days reads them, with each run as a range."""
    runs: list[list[date]] = []
    for day in days:
        if runs and day - runs[-1][-1] == timedelta(days=1):
            runs[-1].append(day)
        else:
            runs.append([day])
    return ", ".join(
        run[0].isoformat()
        if len(run) == 1
        else f"{run[0].isoformat()} - {run[-1].isoformat()}"
        for run in runs
    )


def clock(minutes: int) -> str:
    return f"{minutes // 60 % 24:02d}:{minutes % 60:02d}"


def seconds_since(origin: datetime, now: datetime) -> int:
    """Seconds from a list's origin, comparable with its intervals."""
    return int((now - origin).total_seconds())


@dataclass(frozen=True)
class Tally:
    """A list's earned and available points as of a moment."""

    at: datetime
    earned: Fraction
    available: Fraction


class Plan:
    """Sections of tasks and the edits every kind of list shares. Each kind
    is a frozen dataclass with a sections field, which check_sections has
    vetted, and tallies of its points over time, oldest first."""

    sections: tuple[Section, ...]
    tallies: tuple[Tally, ...]

    def with_sections(self, sections: tuple[Section, ...]) -> Self:
        raise NotImplementedError

    def tasks(self) -> Iterator[Task]:
        return (task for section in self.sections for task in section.tasks)

    def _section_index(self, section_id: str) -> int:
        for index, section in enumerate(self.sections):
            if section.id == section_id:
                return index
        raise ValueError("Choose a section.")

    def rename_section(self, section_id: str, title: str) -> Self:
        index = self._section_index(section_id)
        sections = list(self.sections)
        sections[index] = replace(sections[index], title=title)
        return self.with_sections(tuple(sections))

    def insert_section(self, section: Section, index: int) -> Self:
        return self.with_sections(
            self.sections[:index] + (section,) + self.sections[index:]
        )

    def move_section(self, section_id: str, offset: int) -> Self:
        index = self._section_index(section_id)
        destination = index + offset
        if not 0 <= destination < len(self.sections):
            return self
        sections = list(self.sections)
        sections[index], sections[destination] = sections[destination], sections[index]
        return self.with_sections(tuple(sections))

    def remove_section(self, section_id: str) -> Self:
        index = self._section_index(section_id)
        if len(self.sections) == 1:
            # a list keeps at least one section, so removing the last one empties it
            return self.with_sections((replace(self.sections[0], tasks=()),))
        return self.with_sections(self.sections[:index] + self.sections[index + 1 :])

    def insert_task(self, task: Task, section_id: str, index: int) -> Self:
        s = self._section_index(section_id)
        sections = list(self.sections)
        tasks = sections[s].tasks
        sections[s] = replace(
            sections[s], tasks=tasks[:index] + (task,) + tasks[index:]
        )
        return self.with_sections(tuple(sections))

    def replace_task(self, task: Task) -> Self:
        """Replaces the task with the same id, wherever it is."""
        return self.with_sections(
            tuple(
                replace(s, tasks=tuple(task if t.id == task.id else t for t in s.tasks))
                for s in self.sections
            )
        )

    def remove_task(self, task_id: str) -> Self:
        return self.with_sections(
            tuple(
                replace(s, tasks=tuple(t for t in s.tasks if t.id != task_id))
                for s in self.sections
            )
        )

    def _task_position(self, task_id: str) -> tuple[int, int] | None:
        for s, section in enumerate(self.sections):
            for index, task in enumerate(section.tasks):
                if task.id == task_id:
                    return s, index
        return None

    def move_task(self, task_id: str, offset: Literal[-1, 1]) -> Self:
        """Moves a task up or down one place, crossing into the neighbouring section at
        either end."""
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
        return self.with_sections(
            tuple(
                replace(section, tasks=tuple(tasks))
                for section, tasks in zip(self.sections, sections, strict=True)
            )
        )


def check_sections(sections: tuple[Section, ...]) -> None:
    if not sections:
        raise ValueError("A list needs at least one section.")
    ids = [section.id for section in sections]
    ids += [task.id for section in sections for task in section.tasks]
    if len(ids) != len(set(ids)):
        raise ValueError("Task and section identities must be unique.")


def fresh_copy(sections: tuple[Section, ...]) -> tuple[Section, ...]:
    """The same plan under new identities, with progress starting over."""
    return tuple(section.duplicate(keep_progress=False) for section in sections)


@dataclass(frozen=True)
class Day(Plan):
    date: date
    # stored in start order whatever order they are given in
    intervals: tuple[Interval, ...]
    sections: tuple[Section, ...]
    tallies: tuple[Tally, ...] = ()

    def __post_init__(self) -> None:
        check_sections(self.sections)
        ordered = tuple(sorted(self.intervals, key=lambda interval: interval.start))
        object.__setattr__(self, "intervals", ordered)
        if any(a.end > b.start for a, b in pairwise(ordered)):
            raise ValueError("Scheduled intervals cannot overlap.")
        if over_a_day(ordered):
            raise ValueError("A schedule cannot span more than 24 hours.")

    def with_sections(self, sections: tuple[Section, ...]) -> Self:
        return replace(self, sections=sections)

    @property
    def origin(self) -> datetime:
        return datetime.combine(self.date, time())

    @property
    def blank(self) -> bool:
        """True when nothing is planned. Unsaved days load as blank."""
        return (
            not self.intervals
            and len(self.sections) == 1
            and not self.sections[0].title
            and not self.sections[0].tasks
        )

    def copy_to(self, list_date: date) -> Day:
        return Day(list_date, self.intervals, fresh_copy(self.sections))


@dataclass(frozen=True)
class Span(Plan):
    """A list from start to end. Its time counts toward the stats, except the whole
    days in off. Spans may overlap."""

    title: str
    # local times to the minute, which is all the store keeps
    start: datetime
    end: datetime
    sections: tuple[Section, ...]
    id: str = field(default_factory=identifier)
    # dates the span overlaps, stored in order without repeats
    off: tuple[date, ...] = ()
    tallies: tuple[Tally, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "title", self.title.strip())
        check_sections(self.sections)
        moments = (self.start, self.end)
        if any(m.tzinfo or m.second or m.microsecond for m in moments):
            raise ValueError("A span's start and end are local times to the minute.")
        if self.end <= self.start:
            raise ValueError("A span must end after it starts.")
        object.__setattr__(self, "off", tuple(sorted(set(self.off))))
        for day in self.off:
            midnight = datetime.combine(day, time())
            if not (midnight < self.end and midnight + timedelta(days=1) > self.start):
                raise ValueError("Days off must fall within the span.")

    def with_sections(self, sections: tuple[Section, ...]) -> Self:
        return replace(self, sections=sections)

    @property
    def origin(self) -> datetime:
        return self.start

    @property
    def intervals(self) -> tuple[Interval, ...]:
        def minutes(moment: datetime) -> int:
            return int((moment - self.start).total_seconds()) // 60

        intervals = []
        counted_from = self.start
        for day in self.off:
            midnight = datetime.combine(day, time())
            if counted_from < midnight:
                intervals.append(Interval(minutes(counted_from), minutes(midnight)))
            counted_from = max(counted_from, midnight + timedelta(days=1))
        if counted_from < self.end:
            intervals.append(Interval(minutes(counted_from), minutes(self.end)))
        return tuple(intervals)


def tallied(plan: Day | Span, now: datetime) -> Day | Span:
    """plan with a tally of its points as of now added, unless the points equal the
    last tally's. A list without tallies counts as having had no points. A clock set
    back stamps the tally at the last one's time, so tallies stay in order."""
    earned, available = score(plan.tasks())
    last = (
        (plan.tallies[-1].earned, plan.tallies[-1].available)
        if plan.tallies
        else (0, 0)
    )
    if (earned, available) == last:
        return plan
    at = max(now, plan.tallies[-1].at) if plan.tallies else now
    return replace(plan, tallies=(*plan.tallies, Tally(at, earned, available)))


def progress(scheduled: int, earned: Fraction, available: Fraction) -> int:
    """earned points as whole seconds of the schedule, so pace, progress minus
    elapsed, agrees with the figures shown."""
    return int(scheduled * earned / available)


def tally_at(plan: Day | Span, moment: datetime) -> Tally | None:
    """The last tally at or before moment, or None before the first. Tallies are in
    time order."""
    index = bisect_right(plan.tallies, moment, key=lambda tally: tally.at)
    return plan.tallies[index - 1] if index else None


def pace_at(plan: Day | Span, moment: datetime) -> int | None:
    """Seconds ahead of the schedule at moment, as the summary's pace counts them,
    from the last tally at or before it. None before the first tally, and without
    points or a schedule."""
    tally = tally_at(plan, moment)
    if tally is None or not tally.available:
        return None
    elapsed, scheduled = time_progress(
        plan.intervals, seconds_since(plan.origin, moment)
    )
    if not scheduled:
        return None
    return progress(scheduled, tally.earned, tally.available) - elapsed


def on_pace_at(plan: Day | Span, moment: datetime) -> Fraction | None:
    """The points that would make pace zero at moment: the available points of the
    last tally at or before it, in the share of the schedule elapsed. None before the
    first tally, and without a schedule."""
    tally = tally_at(plan, moment)
    if tally is None:
        return None
    elapsed, scheduled = time_progress(
        plan.intervals, seconds_since(plan.origin, moment)
    )
    if not scheduled:
        return None
    return tally.available * elapsed / scheduled


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


def work(tasks: Iterable[Task]) -> tuple[Fraction, Fraction]:
    """Seconds of task durations done and needed. A task counts as done by the
    share of its points earned, so an unscored task is never done."""
    done = Fraction(0)
    needed = Fraction(0)
    for task in tasks:
        if task.minutes is not None:
            seconds = task.minutes * 60
            needed += seconds
            if task.available is not None:
                done += seconds * task.earned / task.available
    return done, needed


def time_progress(intervals: tuple[Interval, ...], now: int) -> tuple[int, int]:
    """Elapsed and total scheduled seconds, now being seconds_since the list's
    origin."""
    return (
        sum(min(max(now - i.start * 60, 0), i.seconds) for i in intervals),
        sum(i.seconds for i in intervals),
    )


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


def percentage(value: Fraction) -> str:
    hundredths = int(value * 10_000)
    sign = "-" if hundredths < 0 else ""
    whole, decimal = divmod(abs(hundredths), 100)
    return f"{sign}{whole}.{decimal:02d}"


def per_point(seconds: int | Fraction, available: Fraction) -> int | None:
    """Whole seconds per point, or None without points."""
    return int(Fraction(seconds) / available) if available else None


def timed(tasks: Iterable[Task]) -> tuple[Task, ...]:
    """The scored tasks with a duration, the ones a duration per point counts."""
    return tuple(t for t in tasks if t.minutes and t.available is not None)


def completion(task: Task) -> Fraction | None:
    if task.available is None:
        return None
    return task.earned / task.available


def duration_per_point(task: Task) -> Fraction | None:
    if task.minutes is None or task.available is None:
        return None
    return task.minutes / task.available


@dataclass(frozen=True)
class Figures:
    """Totals of some tasks and, for a list, of its schedule as of a moment, before
    formatting. Times are in seconds; without a schedule, elapsed and scheduled are
    0. The timed totals count only timed tasks."""

    earned: Fraction
    available: Fraction
    worked: Fraction
    needed: Fraction
    timed_available: Fraction
    timed_needed: Fraction
    # scored tasks without a duration
    undurated: int
    elapsed: int
    scheduled: int

    @property
    def progress(self) -> int | None:
        if not (self.scheduled and self.available):
            return None
        return progress(self.scheduled, self.earned, self.available)

    @property
    def pace(self) -> int | None:
        worth = self.progress
        return None if worth is None else worth - self.elapsed

    @property
    def free(self) -> Fraction | None:
        """The share of the schedule left after task durations, negative when they
        overrun it."""
        if not (self.scheduled and self.needed):
            return None
        return (self.scheduled - self.needed) / self.scheduled

    @property
    def schedule_per_point(self) -> int | None:
        return per_point(self.scheduled, self.available) if self.scheduled else None

    @property
    def duration_per_point(self) -> int | None:
        return per_point(self.timed_needed, self.timed_available)


def figures(
    tasks: Iterable[Task], intervals: tuple[Interval, ...] = (), now: int = 0
) -> Figures:
    """The figures of tasks against intervals, now being seconds_since the list's
    origin."""
    tasks = tuple(tasks)
    earned, available = score(tasks)
    worked, needed = work(tasks)
    timed_tasks = timed(tasks)
    _, timed_available = score(timed_tasks)
    _, timed_needed = work(timed_tasks)
    elapsed, scheduled = time_progress(intervals, now)
    return Figures(
        earned,
        available,
        worked,
        needed,
        timed_available,
        timed_needed,
        sum(t.minutes is None and t.available is not None for t in tasks),
        elapsed,
        scheduled,
    )
