import argparse
import sqlite3
from collections.abc import Callable, Iterable, Iterator
from contextlib import closing
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from fractions import Fraction
from itertools import chain
from pathlib import Path
from typing import Literal, NamedTuple

from platformdirs import user_data_path
from rich.console import Console, ConsoleOptions, Group, RenderableType, RenderResult
from rich.rule import Rule
from rich.table import Table
from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.geometry import Offset
from textual.message import Message
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import (
    ContentSwitcher,
    Footer,
    Input,
    Label,
    OptionList,
    Static,
)
from textual.widgets.option_list import Option

from .db import Store
from .model import (
    Day,
    Interval,
    Section,
    Span,
    Task,
    clock,
    days_text,
    fresh_copy,
    interval_text,
    number,
    pace_at,
    parse_clock,
    parse_date,
    parse_days,
    parse_minutes,
    per_point,
    percentage,
    points,
    progress,
    schedule,
    score,
    seconds_since,
    tallied,
    time_progress,
    work,
)

# catppuccin macchiato
CRUST = "#181926"
BASE = "#24273a"
SURFACE0 = "#363a4f"
SURFACE1 = "#494d64"
SURFACE2 = "#5b6078"
OVERLAY1 = "#8087a2"
OVERLAY2 = "#939ab7"
SUBTEXT0 = "#a5adcb"
TEXT = "#cad3f5"
ROSEWATER = "#f4dbd6"
LAVENDER = "#b7bdf8"
BLUE = "#8aadf4"
SAPPHIRE = "#7dc4e4"
TEAL = "#8bd5ca"
GREEN = "#a6da95"
YELLOW = "#eed49f"
RED = "#ed8796"

LABEL = SUBTEXT0
# units, notes and inactive controls
MUTED = OVERLAY1
# rule lines and the unfilled part of bars
RULE = SURFACE2
KEY = f"bold {BLUE}"

THEME = Theme(
    name="macchiato",
    primary=BLUE,
    warning=YELLOW,
    error=RED,
    success=GREEN,
    foreground=TEXT,
    background=BASE,
    surface=SURFACE0,
    panel=SURFACE1,
    dark=True,
    variables={
        "label": LABEL,
        "muted": MUTED,
        "blue": BLUE,
        "information": TEAL,
        "surface0": SURFACE0,
        "surface1": SURFACE1,
        "surface2": SURFACE2,
        "lavender": LAVENDER,
        "input-cursor-background": ROSEWATER,
        "input-cursor-foreground": CRUST,
        "input-selection-background": f"{OVERLAY2} 30%",
    },
)

# errors shown to the user, leaving the draft or list unchanged
RECOVERABLE = (ValueError, sqlite3.Error, OSError)

MAX_MESSAGES = 5


def duration(seconds: int) -> Text:
    parts = zip(
        (seconds // 86400, seconds // 3600 % 24, seconds // 60 % 60, seconds % 60),
        "dhms",
        strict=True,
    )
    text = Text()
    for value, unit in parts:
        if value:
            text.append(str(value))
            text.append(unit, style=MUTED)
    return text if text.plain else Text.assemble("0", ("s", MUTED))


def rate(seconds: int | Fraction, available: Fraction) -> Text:
    value = per_point(seconds, available)
    return Text("-") if value is None else duration(value)


def timed(tasks: tuple[Task, ...]) -> tuple[Task, ...]:
    return tuple(t for t in tasks if t.minutes and t.available is not None)


def completion(task: Task) -> Fraction | None:
    if task.available is None:
        return None
    return task.earned / task.available


def duration_per_point(task: Task) -> Fraction | None:
    if task.minutes is None or task.available is None:
        return None
    return task.minutes / task.available


class Column(NamedTuple):
    header: str
    # None sorts last
    value: Callable[[Task], Fraction | int | None]
    # True if the first click sorts descending (also listed first in the menu)
    descending: bool
    words: tuple[str, str]


COLUMNS = (
    Column("score", completion, False, ("least done first", "most done first")),
    Column(
        "pts",
        lambda task: task.available,
        True,
        ("fewest points first", "most points first"),
    ),
    Column(
        "dur",
        lambda task: task.minutes,
        False,
        ("shortest duration first", "longest duration first"),
    ),
    Column(
        "dur/pt",
        duration_per_point,
        False,
        ("least duration per point first", "most duration per point first"),
    ),
)
SCORE, _, DURATION, DURATION_PER_POINT = COLUMNS

EARNED_WIDTH = 5
AVAILABLE_WIDTH = 4
SCORE_WIDTH = EARNED_WIDTH + 1 + AVAILABLE_WIDTH
GAP = 3


class Slot(NamedTuple):
    """A column of the task table between the done marker and the title. It appears
    when some task in the list has a value for it, as drawn decides, and truncates
    longer values."""

    column: Column
    # blank cells before the value
    gap: int
    width: int
    justify: Literal["left", "right"]
    drawn: Callable[[tuple[Task, ...]], bool]


# in the order they are drawn
SLOTS = (
    # left-justified, since right justification strips the padding that aligns the
    # slashes
    Slot(
        SCORE,
        2,
        SCORE_WIDTH,
        "left",
        lambda ts: any(t.available is not None for t in ts),
    ),
    # fits 23h59m
    Slot(DURATION, GAP, 6, "right", lambda ts: any(t.minutes for t in ts)),
    # fits 9h59m59s
    Slot(DURATION_PER_POINT, GAP, 8, "right", lambda ts: bool(timed(ts))),
)


def drawn_slots(tasks: Iterable[Task]) -> tuple[Slot, ...]:
    tasks = tuple(tasks)
    return tuple(slot for slot in SLOTS if slot.drawn(tasks))


def row(
    slots: tuple[Slot, ...],
    marker: Text,
    values: dict[Column, Text | str],
    title: Text,
) -> Table:
    """A task table row. values holds a cell for every slot's column."""
    table = Table.grid(expand=True)
    table.add_column(width=1, no_wrap=True)
    cells: list[Text | str] = [marker]
    for slot in slots:
        table.add_column(
            width=slot.gap + slot.width, justify=slot.justify, no_wrap=True
        )
        cells.append(Text(" " * slot.gap) + values[slot.column])
    table.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
    cells.append(Text(" " * GAP) + title)
    table.add_row(*cells)
    return table


def score_cell(earned: Text, available: Text) -> Text:
    """earned/available, aligned on the slash."""
    earned, available = earned.copy(), available.copy()
    earned.align("right", EARNED_WIDTH)
    available.align("left", AVAILABLE_WIDTH)
    return Text.assemble(earned, ("/", MUTED), available)


def task_row(task: Task, slots: tuple[Slot, ...]) -> Table:
    if task.complete:
        marker, color = "✓", GREEN
    elif task.earned:
        marker, color = "~", BLUE
    else:
        marker, color = "", ""
    scored = (
        Text()
        if task.available is None
        else score_cell(
            Text(number(task.earned), style=color), Text(number(task.available))
        )
    )
    return row(
        slots,
        Text(marker, style=color),
        {
            SCORE: scored,
            DURATION: duration(task.minutes * 60) if task.minutes else "",
            DURATION_PER_POINT: rate(task.minutes * 60, task.available)
            if task.minutes and task.available is not None
            else "",
        },
        Text(task.title, style=MUTED if task.complete else ""),
    )


class Sort(NamedTuple):
    column: Column
    descending: bool

    @property
    def words(self) -> str:
        return self.column.words[self.descending]


def arrange(tasks: tuple[Task, ...], sort: Sort) -> tuple[Task, ...]:
    """Stable sort. Tasks without the value go last."""
    keyed: list[tuple[Fraction | int, Task]] = []
    missing: list[Task] = []
    for task in tasks:
        value = sort.column.value(task)
        if value is None:
            missing.append(task)
        else:
            keyed.append((value, task))
    keyed.sort(key=lambda pair: pair[0], reverse=sort.descending)
    return tuple(task for _, task in keyed) + tuple(missing)


class View(NamedTuple):
    """The task pane's rows for a list under a sort and the done filter."""

    plan: Day | Span
    sort: Sort | None
    groups: tuple[tuple[Section, tuple[Task, ...]], ...]
    # sorted tasks, or None when sections are drawn: there is no sort, or no task is
    # shown
    flat: tuple[Task, ...] | None
    hidden: int

    def shown_in(self, section: Section) -> tuple[Task, ...]:
        return next(tasks for s, tasks in self.groups if s.id == section.id)

    def shown(self) -> tuple[Task, ...]:
        return tuple(task for _, tasks in self.groups for task in tasks)

    def first_row(self) -> str:
        """The row to select when a list opens: its first task, else its first rule."""
        tasks = self.flat or self.shown()
        return tasks[0].id if tasks else self.plan.sections[0].id

    def neighbours(self, section: Section) -> tuple[Task, ...]:
        """The tasks a deleted task's selection may move to: any task while sorted,
        otherwise only its section's. Selecting the next section's rule would let a
        repeated x delete that whole section."""
        return self.flat or self.shown_in(section)


def view(plan: Day | Span, sort: Sort | None, hide_done: bool) -> View:
    groups = tuple(
        (s, tuple(t for t in s.tasks if not (hide_done and t.complete)))
        for s in plan.sections
    )
    shown = tuple(task for _, tasks in groups for task in tasks)
    flat = arrange(shown, sort) if sort is not None and shown else None
    hidden = sum(task.complete for task in plan.tasks()) if hide_done else 0
    return View(plan, sort, groups, flat, hidden)


def next_sort(current: Sort | None, column: Column) -> Sort | None:
    """The sort after a click on column: its first direction, then the reverse, then
    none."""
    if current is None or current.column is not column:
        return Sort(column, column.descending)
    if current.descending == column.descending:
        return Sort(column, not column.descending)
    return None


def column_at(x: int, slots: tuple[Slot, ...]) -> Column | None:
    """The column named at x in a header row. Points sort only from the menu, since
    the score column sorts by the share done."""
    # after the done marker
    start = 1
    for slot in slots:
        end = start + slot.gap + slot.width
        if start <= x < end:
            return slot.column
        start = end
    return None


def column_name(column: Column, sort: Sort | None) -> Text:
    if sort is not None and sort.column is column:
        arrow = "↓" if sort.descending else "↑"
        return Text(column.header + arrow, style="bold")
    return Text(column.header, style=LABEL)


def header_row(sort: Sort | None, slots: tuple[Slot, ...]) -> RenderableType:
    """The column names, with a rule below them while sorted, since the sorted view has
    no section rules."""
    values: dict[Column, Text | str] = {
        slot.column: column_name(slot.column, sort) for slot in SLOTS
    }
    # centred over the values' slash
    values[SCORE] = Text(" " * (EARNED_WIDTH - 2)) + values[SCORE]
    names = row(slots, Text(), values, Text("title", style=LABEL))
    return names if sort is None else Group(names, Rule(style=RULE))


def section_label(section: Section) -> str:
    return f"section {section.title}" if section.title else "an untitled section"


def section_share(section: Section, total: Fraction) -> str:
    """Percent of the list's points in this section, or empty at 0% and 100%."""
    _, available = score(section.tasks)
    if not available or available == total:
        return ""
    return f"{percentage(available / total)}% of points"


class SectionRule:
    """A rule line that starts a section: ── title ─────── share ──"""

    def __init__(self, section: Section, total: Fraction):
        self.title = section.title
        self.share = section_share(section, total)

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        width = options.max_width
        left = Text("──", style=RULE)
        if self.title:
            left.append(" ")
            left.append(self.title, style="bold")
            left.append(" ")
        right = Text("──", style=RULE)
        if self.share:
            share = Text.assemble(" ", (self.share, MUTED), " ", right)
            if left.cell_len + share.cell_len < width:
                right = share
        left.truncate(max(width - right.cell_len - 1, 3))
        fill = max(width - left.cell_len - right.cell_len, 1)
        yield left + Text("─" * fill, style=RULE) + right


def subtotal_row(section: Section, slots: tuple[Slot, ...]) -> Table | None:
    """A section's totals, or None when it has neither points nor durations. Work per
    point counts only tasks with both."""
    earned, available = score(section.tasks)
    minutes = sum(task.minutes or 0 for task in section.tasks)
    if not available and not minutes:
        return None
    timed_tasks = timed(section.tasks)
    _, timed_points = score(timed_tasks)
    _, timed_seconds = work(timed_tasks)
    style = "bold"
    title = Text("subtotal", style=style)
    if available:
        title.append(f"  {percentage(earned / available)}%")
    return row(
        slots,
        Text(),
        {
            SCORE: score_cell(
                Text(number(earned), style), Text(number(available), style)
            )
            if available
            else Text(),
            # .plain drops the units' colour
            DURATION: Text(duration(minutes * 60).plain, style) if minutes else "",
            DURATION_PER_POINT: Text(rate(timed_seconds, timed_points).plain, style)
            if timed_tasks
            else "",
        },
        title,
    )


# the longest label, "schedule", and two spaces
LABEL_WIDTH = 10
# cells on each side of a bar
GUTTER = 2
SHARE_WIDTH = len("100.00%")


def signed(seconds: int) -> str:
    if not seconds:
        return "0"
    return ("+" if seconds > 0 else "-") + duration(abs(seconds)).plain


def pace_color(ahead: int) -> str:
    return GREEN if ahead > 0 else YELLOW if ahead < 0 else TEXT


def pace(ahead: int) -> Text:
    return Text(signed(ahead), style=pace_color(ahead)) if ahead else duration(0)


def moment(value: datetime) -> str:
    return f"{value:%Y-%m-%d %H:%M}"


def span_text(span: Span) -> str:
    return f"{moment(span.start)} - {moment(span.end)}"


def widest(seconds: int) -> int:
    """Seconds whose duration is as wide as any up to seconds: the same count of the
    largest unit, and every smaller unit at its most."""
    for unit in (86400, 3600, 60):
        if seconds >= unit:
            return seconds // unit * unit + unit - 1
    return 59


class Bar(NamedTuple):
    label: str
    value: Text
    progress: Fraction
    color: str


def bars(plan: Day | Span, elapsed: int) -> tuple[Bar, ...]:
    """A bar each for time, points and work, leaving out those without a total."""
    tasks = tuple(plan.tasks())
    earned, available = score(tasks)
    _, scheduled = time_progress(plan.intervals, 0)
    worked, needed = work(tasks)
    found = []
    if scheduled:
        value = duration(elapsed) + " / " + duration(scheduled)
        found.append(Bar("elapsed", value, Fraction(elapsed, scheduled), TEAL))
    if available:
        value = Text(f"{number(earned)} / {number(available)}")
        found.append(Bar("points", value, earned / available, GREEN))
    if needed:
        value = duration(int(worked)) + " / " + duration(int(needed))
        found.append(Bar("work", value, worked / needed, SAPPHIRE))
    return tuple(found)


def value_width(plan: Day | Span) -> int:
    """Cells for the bars' values. Elapsed time gets the widest the schedule allows, so
    values keep their right edge while the clock runs; the rest change only with the
    list."""
    _, scheduled = time_progress(plan.intervals, 0)
    return max((bar.value.cell_len for bar in bars(plan, widest(scheduled))), default=0)


def labelled(
    label: str, value: Text, width: int, label_width: int = LABEL_WIDTH
) -> Text:
    """label at the left edge and value ending width cells after the label column."""
    return Text.assemble(
        (f"{label:<{label_width}}", LABEL), " " * (width - value.cell_len), value
    )


def schedule_row(plan: Day | Span) -> Text:
    match plan:
        case Day(intervals=()):
            label, value = "schedule", Text("none", style=MUTED)
        case Day():
            label = "schedule"
            value = Text(", ").join(
                Text.assemble(
                    interval_text(i), (" next day" if i.next_day else "", MUTED)
                )
                for i in plan.intervals
            )
        case Span():
            label, value = "span", Text(span_text(plan))
    # a schedule wider than the values runs on to the right, where its row has no bar
    return labelled(label, value, max(value_width(plan), value.cell_len))


def progress_bar(progress: Fraction, color: str, length: int) -> Text:
    """length cells showing progress, rounded down to the half cell, so the bar is full
    only at 100%.

    A glyph has one colour, so a one-cell gap separates the fill from the track, with
    the value at its middle. On a cell boundary the gap is two half cells, which add
    up to one cell even when a cell is an odd number of pixels wide."""
    middle = min(int(progress * length * 2), length * 2)
    if middle == 0:
        return Text("─" * length, style=RULE)
    if middle == length * 2:
        return Text("━" * length, style=color)
    cell, half = divmod(middle, 2)
    if half:
        # the gap is that whole cell
        return Text.assemble(
            ("━" * cell, color), " ", ("─" * (length - cell - 1), RULE)
        )
    # the gap is the right half of the cell before and the left half of this one
    return Text.assemble(
        ("━" * (cell - 1), color),
        ("╸", color),
        ("╶", RULE),
        ("─" * (length - cell - 1), RULE),
    )


# a labelled value, or a note
Figure = tuple[str, Text] | Text


class FigureColumn(NamedTuple):
    figures: tuple[Figure | None, Figure | None]
    label_width: int
    value_width: int
    # cells for the whole column, notes included
    width: int


class Summary:
    """The stats above the task list: a bar each for time, points and work, and the
    figures derived from them in columns. Rows without a value are left out. Unscored
    tasks' durations count as work that never gets done."""

    def __init__(self, plan: Day | Span, now: datetime):
        tasks = tuple(plan.tasks())
        earned, available = score(tasks)
        elapsed, scheduled = time_progress(
            plan.intervals, seconds_since(plan.origin, now)
        )
        _, needed = work(tasks)
        timed_tasks = timed(tasks)
        _, timed_available = score(timed_tasks)
        _, timed_needed = work(timed_tasks)

        self.values = value_width(plan)
        # pace changes with the clock, so its column gets the widest it can be
        self.pace_width = (
            len("-") + duration(widest(scheduled)).cell_len
            if scheduled and available
            else 0
        )
        self.bars = bars(plan, elapsed)

        progress_row = pace_row = free_row = None
        if scheduled and available:
            worth = progress(scheduled, earned, available)
            progress_row = ("progress", duration(worth))
            pace_row = ("pace", pace(worth - elapsed))
        if scheduled and needed:
            free = (scheduled - needed) / scheduled
            style = YELLOW if free < 0 else ""
            free_row = ("free", Text(f"{percentage(free)}%", style=style))
        missing = sum(t.minutes is None and t.available is not None for t in tasks)
        # columns of two figures each
        self.columns: tuple[tuple[Figure | None, Figure | None], ...] = (
            (progress_row, pace_row),
            (
                (
                    "sched/pt" if isinstance(plan, Day) else "span/pt",
                    rate(scheduled, available),
                )
                if scheduled and available
                else None,
                ("dur/pt", rate(timed_needed, timed_available))
                if timed_available
                else None,
            ),
            (
                free_row,
                Text(f"{missing} scored, no duration", style=MUTED)
                if needed and missing
                else None,
            ),
        )

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        values = self.values
        length = max(
            options.max_width - LABEL_WIDTH - values - 2 * GUTTER - SHARE_WIDTH, 1
        )
        for bar in self.bars:
            share = f"{percentage(bar.progress)}%"
            line = Text(no_wrap=True, overflow="ellipsis")
            line.append_text(labelled(bar.label, bar.value, values))
            line.append(" " * GUTTER)
            line.append_text(progress_bar(bar.progress, bar.color, length))
            line.append(f"{share:>{GUTTER + SHARE_WIDTH}}", bar.color)
            yield line
        # each column as wide as its own labels and values, and equally spaced: the
        # first under the bars' labels and the second where the bars start
        columns: list[FigureColumn] = []
        for index, column in enumerate(self.columns):
            if not any(column):
                continue
            figures = [cell for cell in column if isinstance(cell, tuple)]
            notes = [cell.cell_len for cell in column if isinstance(cell, Text)]
            label_width = max((len(label) for label, _ in figures), default=0) + 2
            widths = [value.cell_len for _, value in figures]
            if index == 0:
                widths.append(self.pace_width)
            value_width = max(widths, default=0)
            width = max([label_width + value_width if figures else 0, *notes])
            columns.append(FigureColumn(column, label_width, value_width, width))
        if self.bars and columns:
            yield Text()
        gap = 3 * GUTTER
        if any(self.columns[0]):
            gap = max(LABEL_WIDTH + values + GUTTER - columns[0].width, GUTTER)
        starts = []
        start = 0
        for column in columns:
            starts.append(start)
            start += column.width + gap
        for row in range(2):
            if not any(column.figures[row] for column in columns):
                continue
            line = Text(no_wrap=True, overflow="ellipsis")
            for column, start in zip(columns, starts, strict=True):
                line.append(" " * (start - line.cell_len))
                cell = column.figures[row]
                if isinstance(cell, tuple):
                    cell = labelled(*cell, column.value_width, column.label_width)
                if cell is not None:
                    line.append_text(cell)
                line.append(" " * (start + column.width - line.cell_len))
            line.rstrip()
            yield line


# seconds between pace labels, and minutes between time labels, before doubling
PACE_STEPS = (300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400)
TIME_STEPS = (15, 30, 60, 120, 180, 360, 720, 1440, 2880, 10080)
# braille dot bits by column, then row, within a cell
DOTS = ((0x01, 0x02, 0x04, 0x40), (0x08, 0x10, 0x20, 0x80))


def doubling(step: int) -> Iterator[int]:
    while True:
        yield step
        step *= 2


def pace_scale(low: int, high: int, rows: int) -> tuple[int, int, int]:
    """The seconds and rows between pace labels, and the row of zero, for the finest
    scale that fits low to high in rows (at least two), with labels two to four rows
    apart. Of equally fine scales, the one with fewer labels."""

    def zero_row(step: int, per_step: int) -> int | None:
        zero = -(-max(high, 0) * per_step // step)
        dots_below = -(-max(-low, 0) * per_step * 4 // step)
        return zero if zero * 4 + dots_below <= rows * 4 - 1 else None

    finest_first = sorted(
        ((step, per_step) for step in PACE_STEPS for per_step in (2, 3, 4)),
        key=lambda scale: (Fraction(*scale), -scale[0]),
    )
    wider = ((step, 4) for step in doubling(PACE_STEPS[-1] * 2))
    return next(
        (step, per_step, zero)
        for step, per_step in chain(finest_first, wider)
        if (zero := zero_row(step, per_step)) is not None
    )


def time_axis(start: datetime, end: datetime, left: int, cells: int) -> Text:
    """Times from start to end under cells columns that begin left cells in, each
    centred on its moment, at a step that leaves room between them."""
    minutes = (end - start).total_seconds() / 60
    every = next(
        m
        for m in chain(TIME_STEPS, doubling(TIME_STEPS[-1] * 2))
        if cells * m / minutes >= 8
    )
    midnight = datetime.combine(start.date(), time())
    moment = midnight + timedelta(
        minutes=-(-int((start - midnight).total_seconds() / 60) // every) * every
    )
    axis = Text(style=MUTED)
    while moment <= end:
        text = f"{moment:%H:%M}" if every < 1440 else f"{moment:%m-%d}"
        column = left + round(
            (moment - start).total_seconds() / 60 / minutes * (cells - 1)
        )
        at = column - len(text) // 2
        if (
            at >= axis.cell_len + (1 if axis.cell_len else 0)
            and at + len(text) <= left + cells
        ):
            axis.append(" " * (at - axis.cell_len) + text)
        moment += timedelta(minutes=every)
    return axis


class GraphView(Static, can_focus=True):
    """Takes focus while it shows, which leaves the task list's keys off."""


class PaceGraph:
    """Pace from the first scheduled minute to the last, drawn up to now in braille
    dots (two columns and four rows to a cell), above a line of times. It fills the
    height it is given."""

    def __init__(self, plan: Day | Span, now: datetime):
        self.plan = plan
        self.now = now

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        yield pace_graph(self.plan, self.now, options.max_width, options.height or 0)


def pace_graph(plan: Day | Span, now: datetime, width: int, height: int) -> Text:
    if not plan.intervals:
        return Text("no schedule to keep pace with", style=MUTED)
    if not any(tally.available for tally in plan.tallies):
        return Text("the graph starts with the next change to points", style=MUTED)
    start = plan.origin + timedelta(minutes=plan.intervals[0].start)
    end = plan.origin + timedelta(minutes=plan.intervals[-1].end)
    stop = min(now, end)
    if stop < start:
        return Text("pace shows once the schedule starts", style=MUTED)
    rows = height - 1
    if rows < 2:
        return Text()

    # pace falls steadily between tallies, so its extremes are at the ends and on
    # either side of each tally
    moments = [start, stop]
    for tally in plan.tallies:
        if start < tally.at <= stop:
            moments += [tally.at - timedelta(seconds=1), tally.at]
    values = [v for v in (pace_at(plan, m) for m in moments) if v is not None]
    step, per_step, zero = pace_scale(
        min(values, default=0), max(values, default=0), rows
    )
    labels = {
        row: signed((zero - row) // per_step * step)
        for row in range(zero % per_step, rows, per_step)
    }
    left = max(len(label) for label in labels.values()) + 2
    plot_width = width - left
    if plot_width < 10:
        return Text()

    def dot_row(value: int) -> int:
        return zero * 4 - round(value / step * per_step * 4)

    cells = [[0] * plot_width for _ in range(rows)]
    colors = [[RULE] * plot_width for _ in range(rows)]

    def dot(x: int, y: int, color: str) -> None:
        cells[y // 4][x // 2] |= DOTS[x % 2][y % 4]
        colors[y // 4][x // 2] = color

    columns = plot_width * 2
    for x in range(0, columns, 3):
        dot(x, dot_row(0), RULE)
    previous = None
    for x in range(columns):
        moment = start + (end - start) * x / (columns - 1)
        if moment > stop:
            break
        value = pace_at(plan, moment)
        if value is None:
            previous = None
            continue
        y = dot_row(value)
        # a vertical run joins a jump at a tally
        joined = y if previous is None else previous
        for run in range(min(y, joined), max(y, joined) + 1):
            dot(x, run, pace_color(value))
        previous = y

    lines = []
    for row in range(rows):
        line = Text(f"{labels.get(row, ''):>{left - 2}}  ", style=MUTED)
        for cell, color in zip(cells[row], colors[row], strict=True):
            line.append(chr(0x2800 + cell) if cell else " ", style=color)
        lines.append(line)
    lines.append(time_axis(start, end, left, plot_width))
    return Text("\n").join(lines)


class ListArea(ContentSwitcher):
    """The task list, or the pace graph in its place. Its keys work in both."""

    # Screen binds tab to focus_next, which runs before app bindings, and a priority
    # binding would also catch tab in dialogs
    BINDINGS = [Binding("tab", "app.toggle_mode", "Days or spans", show=False)]


def plan_key(plan: Day | Span) -> date | str:
    match plan:
        case Day():
            return plan.date
        case Span():
            return plan.id


class Deleted(NamedTuple):
    """The row x last removed, which p and P put back as a copy."""

    source: date | str
    row: Task | Section


class Change(NamedTuple):
    # the list before the change (or after it, on the redo stack)
    restores: Day | Span
    description: str
    subject: str | None


def progress_change(before: Task, after: Task) -> str:
    if after.available is None:
        raise RuntimeError("unscored tasks have no progress to describe")
    available = number(after.available)
    return (
        f"changed {after.title} from {number(before.earned)}/{available} "
        f"to {number(after.earned)}/{available}"
    )


# keys use Textual's key displays so tests can check every binding is listed.
# Two columns side by side at wide sizes, stacked otherwise; the terms end the second.
HELP = (
    (
        (
            "Tasks and sections",
            (
                ("j ↓", "next row"),
                ("k ↑", "previous row"),
                ("} {", "next or previous section"),
                ("^d ^u", "half a page down or up"),
                ("g", "first row"),
                ("G", "last row"),
                ("⏎", "set earned points"),
                ("space", "toggle done"),
                ("+ -", "earned points up or down by one"),
                ("o", "add tasks below; on a section's rule, at its top"),
                ("O", "add tasks above"),
                ("#", "new section below this one"),
                ("e", "edit task, or rename section"),
                ("x del", "delete task, or section with its tasks"),
                ("p P", "put the last deleted row below or above"),
                ("J K", "move down or up, across sections"),
                ("f", "hide or show done tasks"),
                ("s", "sort across sections; or click a column name"),
                ("u", "undo, kept per day until you quit"),
                ("^r", "redo; any new change clears it"),
            ),
        ),
        (
            "Dialogs",
            (
                ("⏎", "save"),
                ("esc", "cancel"),
                ("tab", "next field"),
            ),
        ),
    ),
    (
        (
            "Days and spans",
            (
                ("tab", "switch between days and spans"),
                ("[ ]", "previous or next day or span, or a new span"),
                ("t", "today"),
                ("T", "open a date, or the spans"),
                ("c", "replace this list with another's"),
                ("@", "edit the schedule, or a span's title and time"),
                ("v", "show the pace graph, or the tasks again"),
            ),
        ),
        ("App", (("?", "show this list"), ("q", "quit"))),
    ),
)

# in the order the terms appear on screen
GLOSSARY = (
    ("spans", "lists that cover a stretch of time"),
    ("elapsed", "scheduled time gone by"),
    ("points", "points earned out of all points"),
    ("work", "duration done, in proportion to points"),
    ("progress", "points earned, as scheduled time"),
    ("pace", "progress minus elapsed"),
    ("sched/pt", "scheduled time per point"),
    ("span/pt", "a span's time per point"),
    ("dur/pt", "duration per point"),
    ("free", "share of the schedule no duration fills"),
    ("score", "points earned out of the task's points"),
    ("✓ ~", "done, partly done"),
    ("of points", "a section's share of the list's points"),
)


class VimList(OptionList):
    BINDINGS = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("g", "first", "First", show=False),
        Binding("G", "last", "Last", show=False),
        Binding("ctrl+d", "half_page(1)", "Half page down", show=False),
        Binding("ctrl+u", "half_page(-1)", "Half page up", show=False),
    ]

    def action_half_page(self, direction: int) -> None:
        if self.highlighted is None:
            return
        step = max(self.scrollable_content_region.height // 2, 1)
        self.highlighted = self.selectable_near(
            self.highlighted + direction * step, direction
        )

    def selectable_near(self, index: int, direction: int) -> int:
        options = self.options
        start = min(max(index, 0), len(options) - 1)
        for step in (direction, -direction):
            probe = start
            while 0 <= probe < len(options):
                if not options[probe].disabled:
                    return probe
                probe += step
        raise RuntimeError("the task list has no selectable row")


class TaskList(VimList):
    """A click moves the cursor and does nothing else, and a right-click also asks for
    the row menu."""

    # on the list, so they stop while it is hidden
    BINDINGS = [
        Binding("o", "app.add(True)", "add"),
        Binding("O", "app.add(False)", "add above", show=False),
        Binding("#", "app.new_section", "section"),
        Binding("e", "app.edit", "edit"),
        Binding("space", "app.complete", "done"),
        Binding("plus", "app.step(1)", "points", key_display="+/-"),
        Binding("minus", "app.step(-1)", "points down", show=False),
        Binding("x", "app.delete", "delete"),
        Binding("delete", "app.delete", "delete", show=False),
        Binding("p", "app.put(True)", "put", show=False),
        Binding("P", "app.put(False)", "put above", show=False),
        Binding("s", "app.sort", "sort", show=False),
        Binding("f", "app.toggle_done", "hide done", show=False),
        Binding("J", "app.move(1)", "move down", show=False),
        Binding("K", "app.move(-1)", "move up", show=False),
        Binding("}", "app.jump_section(1)", "next section", show=False),
        Binding("{", "app.jump_section(-1)", "previous section", show=False),
    ]

    class MenuRequested(Message):
        def __init__(self, screen_offset: Offset):
            super().__init__()
            self.screen_offset = screen_offset

    def on_click(self, event: events.Click) -> None:
        # OptionList's own handler selects the row, as ⏎ does
        event.prevent_default()
        index = event.style.meta.get("option")
        if index is None or self.options[index].disabled:
            return
        self.highlighted = index
        if event.button == 3:
            self.post_message(self.MenuRequested(event.screen_offset))


class MenuItem(NamedTuple):
    key: str
    words: str
    run: Callable[[], object]


class RowMenu(ModalScreen[Callable[[], object] | None]):
    """Groups of actions with their keys, separated by rules and drawn at a screen
    position. Dismisses with the chosen action for the caller to run."""

    BINDINGS = [("escape", "dismiss", "Close")]

    def __init__(self, groups: tuple[tuple[MenuItem, ...], ...], at: Offset):
        super().__init__()
        self.at = at
        # OptionList doesn't count separators, so option indexes match this tuple
        self.items = tuple(item for group in groups for item in group)
        keys = max(len(item.key) for item in self.items) + 2
        self.options: list[Text | None] = []
        for group in groups:
            if self.options:
                self.options.append(None)
            self.options.extend(
                Text.assemble((f"{item.key:<{keys}}", KEY), item.words)
                for item in group
            )

    def compose(self) -> ComposeResult:
        yield VimList(*self.options, id="row-menu")

    def on_mount(self) -> None:
        menu = self.query_one("#row-menu", VimList)
        # 4 = border + option padding, 2 = border
        width = max(line.cell_len for line in self.options if line) + 4
        height = len(self.options) + 2
        menu.styles.width = width
        menu.styles.height = height
        screen = self.app.size
        x = min(self.at.x, max(screen.width - width, 0))
        y = self.at.y + 1
        if y + height > screen.height:
            y = self.at.y - height
        if y < 0:
            y = max(screen.height - height, 0)
        menu.styles.offset = (x, y)
        # no starting highlight, which would look like a second hover
        menu.highlighted = None

    def on_click(self, event: events.Click) -> None:
        if event.widget is self:
            self.dismiss(None)

    @on(OptionList.OptionSelected)
    def choose(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self.items[event.option_index].run)


class TaskPane(Vertical):
    """The column names, the task list and the empty-list hint, drawn from one View."""

    def compose(self) -> ComposeResult:
        yield Static(header_row(None, ()), id="columns")
        with Vertical(id="list-area"):
            yield TaskList(id="tasks")
            yield Static("", id="empty")

    @property
    def selected_id(self) -> str | None:
        option = self.query_one(VimList).highlighted_option
        return option.id if option else None

    def select(self, row_id: str) -> None:
        widget = self.query_one(VimList)
        widget.highlighted = widget.get_option_index(row_id)

    def show(self, view: View, selection: str | None = None) -> None:
        """Draws view, keeping the selection and scroll position where it can.
        selection, if given and shown, is selected instead."""
        widget = self.query_one(VimList)
        selection = selection or self.selected_id
        old_index = widget.highlighted
        scrolled = widget.scroll_y
        slots = drawn_slots(view.plan.tasks())
        options: list[Option] = []
        if view.flat is not None:
            options = [Option(task_row(task, slots), id=task.id) for task in view.flat]
        else:
            _, total = score(view.plan.tasks())
            for section, tasks in view.groups:
                # ids are unique across sections and tasks (see check_sections)
                options.append(Option(SectionRule(section, total), id=section.id))
                options.extend(Option(task_row(t, slots), id=t.id) for t in tasks)
                subtotal = subtotal_row(section, slots)
                if subtotal is not None:
                    options.append(Option(subtotal, disabled=True))
        widget.set_options(options)
        # set_options resets scroll_y to 0
        widget.scroll_y = scrolled
        ids = [option.id for option in options]
        if selection is not None and selection in ids:
            widget.highlighted = ids.index(selection)
        else:
            # search upward to stay in the same section
            widget.highlighted = widget.selectable_near(old_index or 0, -1)
        empty = self.query_one("#empty", Static)
        if any(view.plan.tasks()):
            empty.update(Text.assemble("all done · ", ("f", KEY), " shows them"))
        else:
            copied = "day's list" if isinstance(view.plan, Day) else "span's tasks"
            empty.update(
                Text.assemble(
                    ("o", KEY),
                    "  add tasks\n",
                    ("c", KEY),
                    f"  copy another {copied}",
                )
            )
        columns = self.query_one("#columns", Static)
        columns.update(header_row(view.sort, slots))
        columns.display = bool(view.shown())
        empty.display = not columns.display


class VimScroll(VerticalScroll):
    BINDINGS = [
        Binding("j", "scroll_down", "Scroll down", show=False),
        Binding("k", "scroll_up", "Scroll up", show=False),
    ]


class LineButton(Static):
    def __init__(self, label: str | Text, action: str, **kwargs):
        super().__init__(label, markup=False, **kwargs)
        self.action = action

    async def on_click(self) -> None:
        await self.action_press()

    async def action_press(self) -> None:
        await self.run_action(self.action)


def bottom_row(note: str, *buttons: tuple[str, str]) -> Horizontal:
    return Horizontal(
        Static(note, id="hint", classes="muted", markup=False),
        *(LineButton(label, action, classes="button") for label, action in buttons),
        classes="bottom-row",
    )


def save_and_cancel(
    note: str, verb: str = "save", cancel: str = "cancel"
) -> Horizontal:
    return bottom_row(note, (verb, "screen.save"), (cancel, "screen.dismiss"))


class Form(ModalScreen):
    """A dialog whose save either succeeds or keeps the draft and says why.

    Subclasses compose an #error Static and implement save, which raises a
    RECOVERABLE error to keep the dialog open and dismisses it when done."""

    BINDINGS = [("escape", "dismiss", "Cancel")]
    AUTO_FOCUS = "Input"

    def save(self) -> None:
        raise NotImplementedError

    def show(self, selector: str, message: str) -> None:
        line = self.query_one(selector, Static)
        line.update(message)
        line.display = bool(message)

    @on(Input.Submitted)
    def action_save(self) -> None:
        try:
            self.save()
        except RECOVERABLE as error:
            self.show("#error", str(error))


class Prompt(Form):
    def __init__(
        self,
        title: str,
        value: str,
        verb: str,
        note: str,
        on_submit: Callable[[str], object],
        field: str = "",
    ):
        super().__init__()
        self.field = field
        self.title_text = title
        self.value = value
        self.verb = verb
        self.note = note
        self.on_submit = on_submit

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog", id="prompt-dialog"):
            yield Label(self.title_text, classes="dialog-title", markup=False)
            yield Input(self.value, compact=True, classes=self.field)
            yield Static("", id="error", markup=False)
            yield save_and_cancel(self.note, self.verb)

    def save(self) -> None:
        self.on_submit(self.query_one(Input).value)
        self.dismiss()


class TaskEditor(Form):
    """Edits one task, or keeps adding tasks until closed. on_submit raises a
    RECOVERABLE error to keep the draft."""

    FIELD_HINTS = {
        "available": "blank for unscored",
        "minutes": "minutes, or like 1h30m",
    }

    def __init__(
        self, task: Task | None, heading: str, on_submit: Callable[[Task], object]
    ):
        super().__init__()
        self.edited_task = task
        self.heading = heading
        self.on_submit = on_submit

    def compose(self) -> ComposeResult:
        task = self.edited_task
        with Vertical(classes="dialog", id="task-dialog"):
            yield Label(self.heading, classes="dialog-title", markup=False)
            with Horizontal(id="task-fields"):
                yield Label("title")
                yield Input(
                    task.title if task else "",
                    id="title",
                    compact=True,
                    select_on_focus=False,
                )
                yield Label("pts")
                yield Input(
                    number(task.available)
                    if task and task.available is not None
                    else "",
                    id="available",
                    classes="points-field",
                    compact=True,
                    select_on_focus=False,
                )
                yield Label("duration")
                yield Input(
                    duration(task.minutes * 60).plain
                    if task and task.minutes is not None
                    else "",
                    id="minutes",
                    compact=True,
                    select_on_focus=False,
                )
            yield Static("", id="error", markup=False)
            # the dialog hides the list, so show what was added
            yield Static("", id="added", classes="muted", markup=False)
            if self.edited_task:
                yield save_and_cancel("")
            else:
                yield save_and_cancel("", "add", "close")

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        note = self.FIELD_HINTS.get(event.widget.id or "", "")
        self.query_one("#hint", Static).update(note)

    def save(self) -> None:
        available_text = self.query_one("#available", Input).value.strip()
        available = points(available_text) if available_text else None
        title = self.query_one("#title", Input).value
        minutes = parse_minutes(self.query_one("#minutes", Input).value)
        if self.edited_task:
            self.on_submit(self.edited_task.edit(title, available, minutes))
            self.dismiss()
            return
        task = Task(title, available, minutes=minutes)
        self.on_submit(task)
        for field in self.query(Input):
            field.value = ""
        self.show("#error", "")
        self.show("#added", f"added {task.title}")
        self.query_one("#title", Input).focus()


def time_field(value: str, column: str) -> Input:
    return Input(value, restrict=r"[0-9:]*", max_length=5, compact=True, classes=column)


def read_time(row: int, column: str, text: str) -> int:
    if not text.strip():
        raise ValueError(f"Row {row} is missing its {column} time.")
    try:
        return parse_clock(text)
    except ValueError as error:
        raise ValueError(f"Row {row} {column}: {error}") from None


def read_row(row: int, start: str, end: str) -> tuple[int, int] | None:
    """A row's start and end times, or None when both are blank."""
    if not start.strip() and not end.strip():
        return None
    times = read_time(row, "start", start), read_time(row, "end", end)
    if times[0] == times[1]:
        raise ValueError(f"Row {row} starts and ends at the same time.")
    return times


class AddLine(LineButton, can_focus=True):
    """Adds a schedule row. It takes focus, so up and down can reach it past the last
    row."""

    BINDINGS = [Binding("enter,space", "press", "Add interval", show=False)]


class ScheduleEditor(Form):
    """One row per interval, in order. A row that starts before the previous one ends is
    on the next day, and blank rows are dropped. on_submit raises a RECOVERABLE error
    to keep the draft."""

    BINDINGS = [
        # priority so a focused Input doesn't swallow these. The time fields reject +
        # and - anyway.
        Binding("plus", "add_row", "Add interval", show=False, priority=True),
        Binding("minus", "remove_row", "Remove interval", show=False, priority=True),
        Binding("up", "move(-1)", "Previous row", show=False),
        Binding("down", "move(1)", "Next row", show=False),
    ]

    def __init__(
        self,
        intervals: tuple[Interval, ...],
        on_submit: Callable[[tuple[Interval, ...]], object],
    ):
        super().__init__()
        self.intervals = intervals
        self.on_submit = on_submit

    @staticmethod
    def row(interval: Interval | None) -> Horizontal:
        start, end = (
            ("", "")
            if interval is None
            else (clock(interval.start), clock(interval.end))
        )
        return Horizontal(
            time_field(start, "start"),
            Label("-"),
            time_field(end, "end"),
            Static("", classes="note"),
            classes="interval",
        )

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog", id="schedule-dialog"):
            yield Label("Schedule", classes="dialog-title")
            yield Static(
                "24-hour times, like 07:30, 0730 or 7",
                id="time-format",
                classes="muted",
                markup=False,
            )
            with VerticalScroll(id="intervals"):
                for interval in self.intervals or (None,):
                    yield self.row(interval)
            with Horizontal(id="row-actions"):
                yield AddLine(
                    Text.assemble(("+", KEY), " add interval"),
                    "screen.add_row",
                    classes="row-action",
                )
                # must not take focus, since the action removes the focused row
                yield LineButton(
                    Text.assemble(("-", KEY), " remove interval"),
                    "screen.remove_row",
                    classes="row-action",
                )
            yield Static("", id="error", markup=False)
            yield save_and_cancel("")

    def rows(self) -> list[tuple[Input, Input, Static]]:
        return [
            (
                row.query_one(".start", Input),
                row.query_one(".end", Input),
                row.query_one(".note", Static),
            )
            for row in self.query(".interval")
        ]

    def on_mount(self) -> None:
        self.update_notes()

    @on(Input.Changed)
    def update_notes(self) -> None:
        """Shows each row's duration, and "next day" where it applies. Shows nothing if
        the schedule doesn't parse."""
        rows = self.rows()
        readable: dict[int, tuple[int, int]] = {}
        for row, (start, end, _) in enumerate(rows, 1):
            try:
                times = read_row(row, start.value, end.value)
            except ValueError:
                continue
            if times is not None:
                readable[row] = times
        try:
            shown = dict(zip(readable, schedule(readable.values()), strict=True))
        except ValueError:
            shown = {}
        for row, (_, _, note) in enumerate(rows, 1):
            interval = shown.get(row)
            if interval is None:
                note.update("")
                continue
            text = Text()
            if interval.next_day:
                text.append("next day  ", style=MUTED)
            note.update(text + duration(interval.seconds))

    def focused_row(self) -> Horizontal | None:
        parent = self.focused.parent if self.focused else None
        if isinstance(parent, Horizontal) and parent.has_class("interval"):
            return parent
        return None

    async def action_add_row(self) -> None:
        row = self.row(None)
        await self.query_one("#intervals").mount(row, after=self.focused_row())
        field = row.query_one(".start", Input)
        # focus would scroll before the next layout, and Textual skips scrolling to
        # a widget that has no size yet, so the new row could stay out of view. This
        # scrolls as focus does, after the layout.
        field.focus(scroll_visible=False)

        def reveal() -> None:
            if not self.can_view_entire(field):
                self.scroll_to_center(field, origin_visible=True)

        self.call_after_refresh(reveal)

    def action_move(self, offset: int) -> None:
        """Moves focus a row up or down, keeping its column. Below the last row is the
        add line."""
        rows = list(self.query(".interval").results(Horizontal))
        add = self.query_one(AddLine)
        if self.focused is add:
            if offset < 0 and rows:
                rows[-1].query_one(".start", Input).focus()
            return
        current = self.focused_row()
        if current is None:
            return
        index = rows.index(current) + offset
        if index >= len(rows):
            add.focus()
        elif index >= 0:
            rows[index].query_one(self.focused_column(), Input).focus()

    def focused_column(self) -> str:
        focused = self.focused
        return ".end" if focused is not None and focused.has_class("end") else ".start"

    async def action_remove_row(self) -> None:
        """Removes the focused row. Focus goes to the same column of a neighbouring row,
        or to the add line when no row is left."""
        current = self.focused_row()
        if current is None:
            return
        column = self.focused_column()
        rows = list(self.query(".interval").results(Horizontal))
        index = rows.index(current)
        rows.pop(index)
        await current.remove()
        if rows:
            rows[min(index, len(rows) - 1)].query_one(column, Input).focus()
        else:
            self.query_one(AddLine).focus()
        # removing a row can change which later rows start on the next day
        self.update_notes()

    def save(self) -> None:
        rows = (
            read_row(row, start.value, end.value)
            for row, (start, end, _) in enumerate(self.rows(), 1)
        )
        self.on_submit(schedule(times for times in rows if times is not None))
        self.dismiss()


class SortMenu(ModalScreen):
    BINDINGS = [("escape", "dismiss", "Cancel")]

    def __init__(self, current: Sort | None, on_choose: Callable[[Sort | None], None]):
        super().__init__()
        self.current = current
        self.on_choose = on_choose
        self.choices: list[Sort | None] = [None]
        for column in COLUMNS:
            self.choices += [
                Sort(column, column.descending),
                Sort(column, not column.descending),
            ]

    def compose(self) -> ComposeResult:
        options = []
        for choice in self.choices:
            mark = "✓ " if choice == self.current else "  "
            options.append(Option(mark + (choice.words if choice else "stored order")))
        with Vertical(classes="dialog", id="sort-dialog"):
            yield Label("Sort tasks across sections", classes="dialog-title")
            yield VimList(*options, id="sort-options")
            yield bottom_row("", ("cancel", "screen.dismiss"))

    def on_mount(self) -> None:
        self.query_one(VimList).highlighted = self.choices.index(self.current)

    @on(OptionList.OptionSelected)
    def choose(self, event: OptionList.OptionSelected) -> None:
        self.dismiss()
        self.on_choose(self.choices[event.option_index])


class HelpScreen(ModalScreen):
    BINDINGS = [
        Binding("escape,question_mark,q", "dismiss", "Close"),
    ]
    # the two columns fit side by side from here
    HORIZONTAL_BREAKPOINTS = [(120, "-wide")]

    def compose(self) -> ComposeResult:
        key_width = (
            max(len(keys) for column in HELP for _, rows in column for keys, _ in rows)
            + 2
        )
        left, right = Text(), Text()
        for text, column in zip((left, right), HELP, strict=True):
            for index, (group, rows) in enumerate(column):
                if index:
                    text.append("\n\n")
                text.append(group, style="bold")
                for keys, description in rows:
                    text.append(f"\n{keys:<{key_width}}", style=KEY)
                    text.append(description)
        right.append("\n\nTerms", style="bold")
        term_width = max(len(term) for term, _ in GLOSSARY) + 2
        for term, meaning in GLOSSARY:
            right.append(f"\n{term:<{term_width}}", style="bold")
            right.append(meaning)
        with VimScroll(classes="dialog", id="help-dialog"):
            with Horizontal(id="help-columns"):
                yield Static(left)
                yield Static(right, id="help-right")
            yield bottom_row("", ("close", "screen.dismiss"))


def read_moment(name: str, day_text: str, time_text: str, *, end: bool) -> datetime:
    """A start or end from its date and time fields, naming the field on failure. A
    blank time means the start of the date, or for an end, midnight after it."""
    try:
        day = parse_date(day_text)
    except ValueError as error:
        raise ValueError(f"{name} date: {error}") from None
    midnight = datetime.combine(day, time())
    if not time_text.strip():
        return midnight + timedelta(days=1) if end else midnight
    try:
        minutes = parse_clock(time_text)
    except ValueError as error:
        raise ValueError(f"{name} time: {error}") from None
    return midnight + timedelta(minutes=minutes)


class SpanEditor(Form):
    def __init__(
        self,
        span: Span | None,
        on_submit: Callable[[str, datetime, datetime, tuple[date, ...]], object],
    ):
        super().__init__()
        self.span = span
        self.on_submit = on_submit

    def compose(self) -> ComposeResult:
        span = self.span

        def moment_fields(value: datetime | None, name: str) -> list[Input]:
            day = Input(
                "" if value is None else value.date().isoformat(),
                restrict=r"[0-9-]*",
                max_length=10,
                compact=True,
                id=f"{name}-date",
                classes="date-field",
            )
            moment = time_field("" if value is None else f"{value:%H:%M}", "time-field")
            moment.id = f"{name}-time"
            return [day, moment]

        with Vertical(classes="dialog", id="span-dialog"):
            yield Label(
                "New span" if span is None else "Edit span", classes="dialog-title"
            )
            yield Static(
                "dates like 2026-11-20; times like 07:30, or blank",
                id="time-format",
                classes="muted",
                markup=False,
            )
            with Horizontal(classes="span-row"):
                yield Label("title")
                yield Input(
                    "" if span is None else span.title,
                    id="span-title",
                    compact=True,
                    select_on_focus=False,
                )
            with Horizontal(classes="span-row"):
                yield Label("from")
                yield from moment_fields(None if span is None else span.start, "from")
                yield Label("to", id="to-label")
                yield from moment_fields(None if span is None else span.end, "to")
            with Horizontal(classes="span-row"):
                yield Label("off")
                yield Input(
                    "" if span is None else days_text(span.off),
                    id="span-off",
                    compact=True,
                    select_on_focus=False,
                )
            yield Static("", id="error", markup=False)
            yield save_and_cancel("", "add" if span is None else "save")

    def read(self) -> tuple[datetime, datetime]:
        def value(selector: str) -> str:
            return self.query_one(selector, Input).value

        start = read_moment("From", value("#from-date"), value("#from-time"), end=False)
        end = read_moment("To", value("#to-date"), value("#to-time"), end=True)
        return start, end

    def on_mount(self) -> None:
        self.update_length()

    @on(Input.Changed)
    def update_length(self) -> None:
        try:
            start, end = self.read()
        except ValueError:
            length = Text()
        else:
            length = (
                duration(int((end - start).total_seconds())) if end > start else Text()
            )
        self.query_one("#hint", Static).update(length)

    def save(self) -> None:
        start, end = self.read()
        try:
            off = parse_days(self.query_one("#span-off", Input).value)
        except ValueError as error:
            raise ValueError(f"Days off: {error}") from None
        self.on_submit(self.query_one("#span-title", Input).value, start, end, off)
        self.dismiss()


def span_status(span: Span, now: datetime) -> str:
    if now < span.start:
        return "later"
    return "now" if now < span.end else "ended"


def picker_lines(spans: tuple[Span, ...], now: datetime) -> list[Text]:
    """One row per span: title, span, percent done and status, with - for a missing
    value."""
    titles = [span.title or "-" for span in spans]
    width = max((Text(title).cell_len for title in titles), default=0)
    lines = []
    for span, title in zip(spans, titles, strict=True):
        earned, available = score(span.tasks())
        done = f"{percentage(earned / available)}%" if available else "-"
        line = Text(title)
        line.pad_right(width - line.cell_len)
        line.append(f"  {span_text(span)}  {done:>7}  ")
        line.append(f"{span_status(span, now):<5}", style=MUTED)
        lines.append(line)
    return lines


class SpanPicker(ModalScreen):
    """The spans, latest start first, under title. Choosing one calls on_choose."""

    BINDINGS = [("escape", "dismiss", "Close")]

    def __init__(
        self,
        title: str,
        spans: tuple[Span, ...],
        on_choose: Callable[[Span], object],
        selected: str | None = None,
        buttons: tuple[tuple[str, str], ...] = (("cancel", "screen.dismiss"),),
    ):
        super().__init__()
        self.title_text = title
        self.spans = spans
        self.on_choose = on_choose
        self.selected = selected
        self.buttons = buttons

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog", id="picker-dialog"):
            yield Label(self.title_text, classes="dialog-title")
            yield VimList(id="picker")
            yield Static("no spans yet", id="no-spans", classes="muted")
            yield bottom_row("", *self.buttons)

    def on_mount(self) -> None:
        self.show(self.spans, self.selected)

    def show(self, spans: tuple[Span, ...], selected: str | None) -> None:
        picker = self.query_one("#picker", VimList)
        old_index = picker.highlighted or 0
        self.spans = tuple(reversed(spans))
        picker.set_options(picker_lines(self.spans, datetime.now()))
        ids = [span.id for span in self.spans]
        if self.spans:
            picker.highlighted = (
                ids.index(selected)
                if selected in ids
                else min(old_index, len(self.spans) - 1)
            )
        picker.display = bool(self.spans)
        self.query_one("#no-spans").display = not self.spans
        # the list lost focus while hidden
        if self.spans:
            picker.focus()

    @on(OptionList.OptionSelected)
    def choose(self, event: OptionList.OptionSelected) -> None:
        self.dismiss()
        self.on_choose(self.spans[event.option_index])


class SpanManager(SpanPicker):
    """The span picker that also adds spans and deletes and restores them. delete and
    restore return the spans left, or None from restore when nothing was deleted."""

    BINDINGS = [
        Binding("o", "new", "New span", show=False),
        Binding("x", "delete", "Delete span", show=False),
        Binding("u", "restore", "Restore span", show=False),
    ]

    def __init__(
        self,
        spans: tuple[Span, ...],
        on_choose: Callable[[Span], object],
        selected: str | None,
        *,
        new: Callable[[], object],
        delete: Callable[[Span], tuple[Span, ...]],
        restore: Callable[[], tuple[Span, ...] | None],
    ):
        super().__init__(
            "Spans",
            spans,
            on_choose,
            selected,
            (
                ("new", "screen.new"),
                ("delete", "screen.delete"),
                ("restore", "screen.restore"),
                ("close", "screen.dismiss"),
            ),
        )
        self.new = new
        self.delete = delete
        self.restore = restore

    def action_new(self) -> None:
        self.dismiss()
        self.new()

    def action_delete(self) -> None:
        index = self.query_one("#picker", VimList).highlighted
        if index is None:
            return
        span = self.spans[index]
        self.show(self.delete(span), None)
        name = span.title or span_text(span)
        self.app.notify(
            f"Deleted {name}; restore brings it back.", timeout=4, markup=False
        )

    def action_restore(self) -> None:
        restored = self.restore()
        if restored is None:
            self.app.notify("No deleted span to restore.", timeout=2)
            return
        self.show(restored, None)


class TodoApp(App):
    CSS_PATH = "app.tcss"
    TITLE = "todo"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("u", "undo", "undo"),
        Binding("ctrl+r", "redo", "redo", show=False),
        # check_action picks which one shows
        Binding("@", "edit_time('days')", "schedule"),
        Binding("@", "edit_time('spans')", "span"),
        Binding("question_mark", "help", "keys"),
        Binding("c", "copy", "copy", show=False),
        Binding("T", "choose", "choose", show=False),
        Binding("[", "shift(-1)", "previous", show=False),
        Binding("]", "shift(1)", "next", show=False),
        Binding("t", "today", "today", show=False),
        Binding("v", "toggle_graph", "graph", show=False),
        # no room in the footer at 80 columns
        Binding("q", "quit", "quit", show=False),
    ]

    def __init__(self, store: Store, list_date: date):
        super().__init__()
        # before the stylesheet is parsed, which needs the theme's variables
        self.register_theme(THEME)
        self.theme = THEME.name
        self.store = store
        # per list, for this session only. Safe because the store is locked.
        self.histories: dict[date | str, list[Change]] = {}
        self.redos: dict[date | str, list[Change]] = {}
        self.deleted: Deleted | None = None
        self.deleted_spans: list[Span] = []
        # session-wide, not saved
        self.sort: Sort | None = None
        self.hide_done = False
        # showing_spans is True only while span is set
        self.day = store.load(list_date)
        self.span: Span | None = None
        self.showing_spans = False

    @property
    def plan(self) -> Day | Span:
        if not self.showing_spans:
            return self.day
        if self.span is None:
            raise RuntimeError("spans mode always has a span")
        return self.span

    def set_plan(self, plan: Day | Span) -> None:
        match plan:
            case Day():
                self.day = plan
            case Span():
                self.span = plan

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool:
        if action == "edit_time" and parameters:
            return parameters[0] == ("spans" if self.showing_spans else "days")
        return True

    def on_notify(self) -> None:
        # textual has no public limit on toasts, and this handler runs before textual's
        # own adds the new one, so trim after the message is handled
        self.call_next(self.trim_messages)

    def trim_messages(self) -> None:
        for notification in list(self._notifications)[:-MAX_MESSAGES]:
            self._unnotify(notification)

    def compose(self) -> ComposeResult:
        with Horizontal(id="date-line"):
            yield LineButton("‹", "app.shift(-1)", classes="link")
            yield Static("", id="date")
            yield LineButton("›", "app.shift(1)", classes="link")
            yield LineButton("jump to today", "app.today", id="today", classes="button")
            yield LineButton("", "app.toggle_done", id="hidden", classes="link")
            yield Static("", id="date-gap")
            yield LineButton("days", "app.show_mode('days')", id="days-mode")
            yield Static("·", id="mode-dot")
            yield LineButton("spans", "app.show_mode('spans')", id="spans-mode")
        with Vertical(id="summary"):
            yield LineButton("", "app.edit_time", id="schedule")
            yield Static("", id="totals")
        with ListArea(initial="task-pane"):
            yield TaskPane(id="task-pane")
            yield GraphView(id="graph")
        yield Footer(compact=True)

    def on_mount(self) -> None:
        self.redraw(self.current_view().first_row())
        self.set_interval(1, self.refresh_stats)
        self.query_one("#tasks", VimList).focus()

    def action_choose(self) -> None:
        if self.showing_spans:
            self.choose_span()
        else:
            self.choose_day()

    def choose_day(self) -> None:
        self.push_screen(
            Prompt(
                "Open a day's list",
                self.day.date.isoformat(),
                "open",
                "YYYY-MM-DD; a new date starts empty",
                lambda value: self.switch_day(parse_date(value)),
                "date-field",
            )
        )

    def action_shift(self, offset: int) -> None:
        if self.showing_spans:
            spans = self.store.spans()
            index = [plan_key(s) for s in spans].index(plan_key(self.plan)) + offset
            if 0 <= index < len(spans):
                self.open_span(spans[index])
            elif index == len(spans):
                self.new_span()
            return
        try:
            target = self.day.date + timedelta(days=offset)
        except OverflowError:
            return
        self.navigate_day(target)

    def action_today(self) -> None:
        if self.showing_spans:
            self.action_show_mode("days")
        self.navigate_day(date.today())

    def navigate_day(self, target: date) -> None:
        try:
            self.switch_day(target)
        except RECOVERABLE as error:
            self.notify(
                str(error), title="Could not open list", severity="error", timeout=8
            )

    def switch_day(self, list_date: date) -> None:
        if list_date == self.day.date:
            return
        self.day = self.store.load(list_date)
        self.redraw(self.current_view().first_row())

    def selected(self) -> tuple[Section, Task | None]:
        """The highlighted row: a task with its section, or a section's rule."""
        row_id = self.query_one(TaskPane).selected_id
        for section in self.plan.sections:
            if row_id == section.id:
                return section, None
            for task in section.tasks:
                if task.id == row_id:
                    return section, task
        raise RuntimeError("the task list has no highlighted row")

    def current_view(self) -> View:
        return view(self.plan, self.sort, self.hide_done)

    def refuse_while_sorted(self, action: str) -> bool:
        """Notifies and returns True when the sorted view hides the order or sections an
        action needs."""
        if self.sort is None or self.current_view().flat is None:
            return False
        self.notify(
            f"Sorted by {self.sort.words}; choose stored order with s to {action}.",
            timeout=4,
        )
        return True

    def redraw(self, selection: str | None = None) -> None:
        current = self.current_view()
        self.query_one(TaskPane).show(current, selection)
        plan = self.plan
        match plan:
            case Day():
                heading = f"{plan.date:%a} {plan.date.isoformat()}"
            case Span():
                heading = plan.title or span_text(plan)
        self.query_one("#date", Static).update(Text(heading, style="bold"))
        self.query_one("#schedule", Static).update(schedule_row(plan))
        self.query_one("#today").display = (
            isinstance(plan, Day) and plan.date != date.today()
        )
        self.query_one("#days-mode").set_class(not self.showing_spans, "mode-on")
        self.query_one("#spans-mode").set_class(self.showing_spans, "mode-on")
        self.refresh_bindings()
        hidden = self.query_one("#hidden", LineButton)
        hidden.display = self.hide_done
        hidden.update(
            Text.assemble(
                (f"{current.hidden} done hidden · ", MUTED),
                ("f", KEY),
                (" shows them", MUTED),
            )
        )
        self.refresh_stats()

    def refresh_stats(self) -> None:
        if not self.is_running:
            return
        now = datetime.now()
        self.query_one("#totals", Static).update(Summary(self.plan, now))
        if self.query_one(ListArea).current == "graph":
            self.query_one("#graph", Static).update(PaceGraph(self.plan, now))

    def action_toggle_graph(self) -> None:
        area = self.query_one(ListArea)
        if area.current == "graph":
            area.current = "task-pane"
            self.query_one("#tasks", VimList).focus()
        else:
            area.current = "graph"
            self.query_one(GraphView).focus()
        self.refresh_stats()

    @on(TaskList.MenuRequested)
    def open_row_menu(self, event: TaskList.MenuRequested) -> None:
        self.push_screen(
            RowMenu((self.row_actions(), self.day_actions()), event.screen_offset),
            self.run_chosen,
        )

    def run_chosen(self, action: Callable[[], object] | None) -> None:
        if action is not None:
            action()

    def row_actions(self) -> tuple[MenuItem, ...]:
        """Menu items for the selected row, most used first. Actions refused while
        sorted are still listed and explain the refusal when chosen."""
        _, task = self.selected()
        items: list[MenuItem] = []
        if task is None:
            items += [
                MenuItem("e", "rename", self.action_edit),
                MenuItem("o", "add tasks", lambda: self.action_add(True)),
                MenuItem("#", "new section", self.action_new_section),
            ]
        else:
            if task.available is not None:
                items += [
                    MenuItem(
                        "space",
                        "not done" if task.complete else "done",
                        self.action_complete,
                    ),
                    MenuItem("⏎", "set points", self.activate),
                    MenuItem("+", "point up", lambda: self.action_step(1)),
                    MenuItem("-", "point down", lambda: self.action_step(-1)),
                ]
            items += [
                MenuItem("e", "edit", self.action_edit),
                MenuItem("o", "add below", lambda: self.action_add(True)),
                MenuItem("O", "add above", lambda: self.action_add(False)),
            ]
        items += [
            MenuItem("J", "move down", lambda: self.action_move(1)),
            MenuItem("K", "move up", lambda: self.action_move(-1)),
        ]
        if self.deleted is not None:
            items += [
                MenuItem("p", "put below", lambda: self.action_put(True)),
                MenuItem("P", "put above", lambda: self.action_put(False)),
            ]
        items.append(MenuItem("x", "delete", self.action_delete))
        return tuple(items)

    def day_actions(self) -> tuple[MenuItem, ...]:
        kind = "span" if self.showing_spans else "day"
        return (
            MenuItem(
                "T",
                "open a span" if self.showing_spans else "open a date",
                self.action_choose,
            ),
            MenuItem("c", f"copy a {kind}", self.action_copy),
            MenuItem(
                "f",
                "show done" if self.hide_done else "hide done",
                self.action_toggle_done,
            ),
            MenuItem("^r", "redo", self.action_redo),
            MenuItem("q", "quit", self.exit),
        )

    @on(OptionList.OptionSelected, "#tasks")
    def activate(self) -> None:
        _, task = self.selected()
        if task is None:
            return
        if task.available is None:
            self.action_complete()
        else:
            self.edit_points(task)

    def save_change(
        self,
        plan: Day | Span,
        description: str,
        subject: str | None = None,
        *,
        select: str | None = None,
    ) -> bool:
        """Saves a change to the open list and records it for undo. The subject row is
        selected afterwards, and again after undoing, unless select names another
        row. Returns whether anything changed."""
        before = self.plan
        if plan == before:
            return False
        plan = tallied(plan, datetime.now())
        self.store.save(plan)
        key = plan_key(plan)
        self.histories.setdefault(key, []).append(Change(before, description, subject))
        self.redos.pop(key, None)
        self.set_plan(plan)
        self.redraw(select or subject)
        return True

    def save_or_notify(
        self,
        plan: Day | Span,
        description: str,
        subject: str | None = None,
        *,
        select: str | None = None,
    ) -> bool:
        try:
            return self.save_change(plan, description, subject, select=select)
        except RECOVERABLE as error:
            self.notify(str(error), title="Could not save", severity="error", timeout=8)
            return False

    def insertion_point(self, below: bool) -> tuple[Section, int]:
        """The section and index for a task next to the selected row. On a rule, both
        directions give the top of its section."""
        section, task = self.selected()
        if task is None:
            return section, 0
        return section, section.tasks.index(task) + below

    def action_add(self, below: bool) -> None:
        if self.refuse_while_sorted("add tasks"):
            return
        section, index = self.insertion_point(below)

        def add(task: Task) -> None:
            nonlocal index
            self.save_change(
                self.plan.insert_task(task, section.id, index),
                f"added {task.title}",
                task.id,
            )
            index += 1

        self.push_screen(
            TaskEditor(None, f"Add tasks to {section_label(section)}", add)
        )

    def action_new_section(self) -> None:
        if self.refuse_while_sorted("add sections"):
            return
        index = self.plan.sections.index(self.selected()[0]) + 1

        def create(title: str) -> None:
            section = Section(title)
            self.save_change(
                self.plan.insert_section(section, index),
                f"added {section_label(section)}",
                section.id,
            )

        self.push_screen(Prompt("New section", "", "add", "blank for untitled", create))

    def action_copy(self) -> None:
        if self.showing_spans:
            self.copy_span()
        else:
            self.copy_day()

    def copy_day(self) -> None:
        try:
            sources = tuple(d for d in self.store.list_dates() if d != self.day.date)
        except RECOVERABLE as error:
            self.notify(str(error), title="Could not read saved days", severity="error")
            return
        if not sources:
            self.notify("No other saved days yet.")
            return
        latest = next((d for d in sources if d < self.day.date), sources[0])

        def reuse(value: str) -> None:
            source = self.store.load(parse_date(value))
            if source.blank:
                raise ValueError(f"There is no saved list for {value.strip()}.")
            self.save_change(
                source.copy_to(self.day.date),
                f"replaced the list with {source.date.isoformat()}'s",
            )

        self.push_screen(
            Prompt(
                f"Replace the list for {self.day.date:%a %Y-%m-%d} with a copy of",
                latest.isoformat(),
                "copy",
                "progress starts over; u undoes",
                reuse,
                "date-field",
            )
        )

    def action_edit(self) -> None:
        section, task = self.selected()
        if task is not None:
            self.push_screen(
                TaskEditor(
                    task,
                    "Edit task",
                    lambda edited: self.save_change(
                        self.plan.replace_task(edited),
                        f"edited {edited.title}",
                        edited.id,
                    ),
                )
            )
            return

        def rename(value: str) -> None:
            day = self.plan.rename_section(section.id, value)
            renamed = next(s for s in day.sections if s.id == section.id)
            self.save_change(
                day,
                f"renamed {section_label(section)} to {section_label(renamed)}",
                section.id,
            )

        self.push_screen(
            Prompt(
                "Rename section",
                section.title,
                "save",
                "blank for untitled",
                rename,
            )
        )

    def edit_points(self, task: Task) -> None:
        if task.available is None:
            raise RuntimeError("unscored tasks have no points to set")
        available = task.available

        def save(value: str) -> None:
            after = replace(task, earned=points(value))
            self.save_change(
                self.plan.replace_task(after), progress_change(task, after), task.id
            )

        self.push_screen(
            Prompt(
                f"Points earned on {task.title}",
                number(task.earned),
                "save",
                f"out of {number(available)}",
                save,
                "points-field",
            )
        )

    def action_edit_time(self, mode: str = "") -> None:
        """Opens the schedule editor for a day or the span editor for a span. Only
        check_action reads mode."""
        match self.plan:
            case Day() as day:
                self.push_screen(
                    ScheduleEditor(
                        day.intervals,
                        lambda intervals: self.save_change(
                            replace(day, intervals=intervals), "changed the schedule"
                        ),
                    )
                )
            case Span() as span:
                self.push_screen(
                    SpanEditor(
                        span,
                        lambda title, start, end, off: self.save_change(
                            replace(span, title=title, start=start, end=end, off=off),
                            "changed the span",
                        ),
                    )
                )

    def action_toggle_mode(self) -> None:
        self.action_show_mode("days" if self.showing_spans else "spans")

    def action_show_mode(self, mode: str) -> None:
        if mode == "days":
            self.showing_spans = False
            self.redraw(self.current_view().first_row())
            return
        if self.showing_spans:
            return
        if self.span is not None:
            self.open_span(self.span)
            return
        spans = self.store.spans()
        if not spans:
            self.new_span()
            return
        now = datetime.now()
        current = [s for s in spans if s.start <= now < s.end]
        self.open_span((current or list(spans))[-1])

    def open_span(self, span: Span) -> None:
        self.span = span
        self.showing_spans = True
        self.redraw(self.current_view().first_row())

    def new_span(self) -> None:
        def create(
            title: str, start: datetime, end: datetime, off: tuple[date, ...]
        ) -> None:
            span = Span(title, start, end, (Section(""),), off=off)
            self.store.save(span)
            self.open_span(span)

        self.push_screen(SpanEditor(None, create))

    def choose_span(self) -> None:
        self.push_screen(
            SpanManager(
                self.store.spans(),
                self.open_span,
                self.span.id if self.span is not None else None,
                new=self.new_span,
                delete=self.delete_span,
                restore=self.restore_span,
            )
        )

    def copy_span(self) -> None:
        open_id = plan_key(self.plan)
        sources = tuple(s for s in self.store.spans() if s.id != open_id)
        if not sources:
            self.notify("No other spans yet.")
            return

        def copy(source: Span) -> None:
            name = source.title or span_text(source)
            # the tallies tracked the replaced tasks
            self.save_change(
                replace(self.plan, sections=fresh_copy(source.sections), tallies=()),
                f"replaced the tasks with {name}'s",
            )

        self.push_screen(
            SpanPicker("Replace this span's tasks with a copy of", sources, copy)
        )

    def delete_span(self, span: Span) -> tuple[Span, ...]:
        """Deletes span and returns the spans left. restore_span can bring it back."""
        spans = self.store.spans()
        index = [s.id for s in spans].index(span.id)
        self.store.delete_span(span.id)
        self.deleted_spans.append(span)
        left = spans[:index] + spans[index + 1 :]
        if self.span is not None and self.span.id == span.id:
            neighbours = left[index : index + 1] or left[index - 1 : index]
            self.span = neighbours[0] if neighbours else None
            self.showing_spans = self.showing_spans and self.span is not None
            self.redraw(self.current_view().first_row())
        return left

    def restore_span(self) -> tuple[Span, ...] | None:
        """Restores the span deleted last and returns all spans, or None when nothing
        was deleted."""
        if not self.deleted_spans:
            return None
        self.store.save(self.deleted_spans.pop())
        return self.store.spans()

    def action_complete(self) -> None:
        self.change_progress(Task.toggle)

    def action_step(self, delta: int) -> None:
        self.change_progress(lambda task: task.step(delta))

    def change_progress(self, change: Callable[[Task], Task]) -> None:
        _, task = self.selected()
        if task is None:
            return
        try:
            after = change(task)
        except ValueError as error:
            self.notify(str(error), timeout=4)
            return
        self.save_or_notify(
            self.plan.replace_task(after), progress_change(task, after), after.id
        )

    def action_delete(self) -> None:
        section, task = self.selected()
        if task is not None:
            tasks = self.current_view().neighbours(section)
            index = tasks.index(task)
            neighbours = tasks[index + 1 : index + 2] or tasks[index - 1 : index]
            if self.save_or_notify(
                self.plan.remove_task(task.id),
                f"deleted {task.title}",
                task.id,
                select=neighbours[0].id if neighbours else section.id,
            ):
                self.deleted = Deleted(plan_key(self.plan), task)
            return
        count = len(section.tasks)
        tasks = f" and its {count} task{'s' if count != 1 else ''}" if count else ""
        removed = self.plan.remove_section(section.id)
        verb = (
            "cleared" if len(removed.sections) == len(self.plan.sections) else "deleted"
        )
        if self.save_or_notify(
            removed, f"{verb} {section_label(section)}{tasks}", section.id
        ):
            self.deleted = Deleted(plan_key(self.plan), section)

    def action_put(self, below: bool) -> None:
        if self.refuse_while_sorted("put rows back"):
            return
        if self.deleted is None:
            self.notify("Nothing deleted to put.", timeout=2)
            return
        # keep progress only when putting back into the same list
        keep_progress = self.deleted.source == plan_key(self.plan)
        copied = self.deleted.row.duplicate(keep_progress=keep_progress)
        if isinstance(copied, Task):
            section, index = self.insertion_point(below)
            day = self.plan.insert_task(copied, section.id, index)
            label = copied.title
        else:
            index = self.plan.sections.index(self.selected()[0]) + below
            day = self.plan.insert_section(copied, index)
            label = section_label(copied)
        self.save_or_notify(day, f"put {label}", copied.id)

    def action_move(self, offset: Literal[-1, 1]) -> None:
        if self.refuse_while_sorted("move tasks"):
            return
        section, task = self.selected()
        direction = "down" if offset > 0 else "up"
        if task is not None and self.current_view().hidden:
            # a move could swap with a hidden neighbour and show no change
            self.notify(
                "Done tasks are hidden; show them with f to move tasks.", timeout=4
            )
        elif task is not None:
            self.save_or_notify(
                self.plan.move_task(task.id, offset),
                f"moved {task.title} {direction}",
                task.id,
            )
        else:
            self.save_or_notify(
                self.plan.move_section(section.id, offset),
                f"moved {section_label(section)} {direction}",
                section.id,
            )

    def action_jump_section(self, offset: Literal[-1, 1]) -> None:
        """Vim's paragraph motions, with section rules as the blank lines."""
        if self.refuse_while_sorted("jump between sections"):
            return
        section, task = self.selected()
        sections = self.plan.sections
        index = sections.index(section)
        if offset < 0:
            target = section.id if task else sections[max(index - 1, 0)].id
        elif index + 1 < len(sections):
            target = sections[index + 1].id
        else:
            tasks = self.current_view().shown_in(section)
            target = tasks[-1].id if tasks else section.id
        self.query_one(TaskPane).select(target)

    def action_sort(self) -> None:
        self.push_screen(SortMenu(self.sort, self.set_sort))

    def action_toggle_done(self) -> None:
        self.hide_done = not self.hide_done
        self.redraw()

    def set_sort(self, sort: Sort | None) -> None:
        self.sort = sort
        self.redraw()

    @on(events.Click, "#columns")
    def sort_by_column(self, event: events.Click) -> None:
        offset = event.get_content_offset(self.query_one("#columns"))
        slots = drawn_slots(self.plan.tasks())
        # only row 0 has column names
        column = column_at(offset.x, slots) if offset and offset.y == 0 else None
        if column is not None:
            self.set_sort(next_sort(self.sort, column))

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_undo(self) -> None:
        self.restore(self.histories, self.redos, "undo", "Undid")

    def action_redo(self) -> None:
        self.restore(self.redos, self.histories, "redo", "Redid")

    def restore(
        self,
        source: dict[date | str, list[Change]],
        target: dict[date | str, list[Change]],
        verb: str,
        past: str,
    ) -> None:
        """Undo or redo: restore the change on top of source, and push the current
        list onto target."""
        key = plan_key(self.plan)
        stack = source.get(key)
        if not stack:
            self.notify(f"Nothing to {verb}.", timeout=2)
            return
        change = stack[-1]
        try:
            self.store.save(change.restores)
        except RECOVERABLE as error:
            self.notify(
                str(error), title=f"Could not {verb}", severity="error", timeout=8
            )
            return
        stack.pop()
        target.setdefault(key, []).append(change._replace(restores=self.plan))
        self.set_plan(change.restores)
        self.redraw(change.subject)
        self.notify(f"{past}: {change.description}", timeout=4, markup=False)


def date_argument(value: str) -> date:
    # argparse shows its own message for a ValueError, but this one's
    try:
        return parse_date(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="A daily task list with points and scheduled time."
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=user_data_path("todo") / "todo.sqlite3",
        help="SQLite file (default: user-data directory)",
    )
    parser.add_argument(
        "--date",
        type=date_argument,
        default=date.today(),
        help="list date, YYYY-MM-DD (default: today)",
    )
    args = parser.parse_args()
    try:
        store = Store(args.database.expanduser())
    except RECOVERABLE as error:
        parser.exit(2, f"todo: {error}\n")
    with closing(store):
        try:
            app = TodoApp(store, args.date)
        except RECOVERABLE as error:
            parser.exit(2, f"todo: {error}\n")
        app.run()
