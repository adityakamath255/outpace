import argparse
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import replace
from datetime import date, datetime, timedelta
from fractions import Fraction
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
from textual.screen import ModalScreen
from textual.widgets import (
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
    Task,
    clock,
    interval_text,
    number,
    parse_clock,
    parse_date,
    parse_minutes,
    per_point,
    percentage,
    points,
    schedule,
    score,
    seconds_into,
    time_progress,
    work,
)

# failures shown to the user while the draft or the list stays as it was
RECOVERABLE = (ValueError, sqlite3.Error, OSError)

MAX_MESSAGES = 5
# the stats pane's content width: all the pane has at 80 columns, and what
# every block lines up to at any size
PANE_WIDTH = 25
# the label column of the per-point table; bars start after it
LABEL_WIDTH = 7


def duration(seconds: int) -> Text:
    """Like 3h40m, with the unit letters dimmed so the digits stand out."""
    parts = zip((seconds // 3600, seconds // 60 % 60, seconds % 60), "hms", strict=True)
    text = Text()
    for value, unit in parts:
        if value:
            text.append(str(value))
            text.append(unit, style="dim")
    return text if text.plain else Text.assemble("0", ("s", "dim"))


def rate(seconds: int | Fraction, available: Fraction) -> Text:
    value = per_point(seconds, available)
    return Text("-") if value is None else duration(value)


def timed(tasks: tuple[Task, ...]) -> tuple[Task, ...]:
    """The tasks work per point counts: those with both a duration and points."""
    return tuple(t for t in tasks if t.minutes and t.available is not None)


def completion(task: Task) -> Fraction | None:
    if task.available is None:
        return None
    return task.earned / task.available


def work_per_point(task: Task) -> Fraction | None:
    if task.minutes is None or task.available is None:
        return None
    return task.minutes / task.available


class Column(NamedTuple):
    """A sortable column of the task list."""

    header: str
    # cells, wide enough for the longest value it shows
    width: int
    # None marks a task without the value; such tasks sort last
    value: Callable[[Task], Fraction | int | None]
    # the direction a first click gives and the menu lists first
    descending: bool
    # how the menu names each direction: ascending, then descending
    words: tuple[str, str]


COLUMNS = (
    Column("done", 5, completion, False, ("least done first", "most done first")),
    Column(
        "pts",
        4,
        lambda task: task.available,
        True,
        ("fewest points first", "most points first"),
    ),
    Column(
        "dur",
        6,
        lambda task: task.minutes,
        False,
        ("shortest duration first", "longest duration first"),
    ),
    Column(
        "w/pt",
        8,
        work_per_point,
        False,
        ("least work per point first", "most work per point first"),
    ),
)


# the cells before the title: a done or partial marker, then the columns
WIDTHS = (1, *(column.width for column in COLUMNS))
FIXED_WIDTH = sum(WIDTHS) + len(WIDTHS)


def row(cells: tuple[Text | str, ...], title: Text | str) -> Table:
    """Cells in WIDTHS order, then the title in the rest of the line."""
    table = Table.grid(padding=(0, 1), expand=True)
    for width, _ in zip(WIDTHS, cells, strict=True):
        table.add_column(width=width, justify="right", no_wrap=True)
    table.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
    table.add_row(*cells, title)
    return table


def task_row(task: Task) -> Table:
    if task.complete:
        marker, color = "✓", "green"
    elif task.earned:
        marker, color = "~", "blue"
    else:
        marker, color = "", ""
    scored = task.available is not None
    cells = (
        Text(marker, style=color),
        Text(number(task.earned) if scored else "", style=color),
        number(task.available) if task.available is not None else "",
        duration(task.minutes * 60) if task.minutes else "",
        rate(task.minutes * 60, task.available)
        if task.minutes and task.available is not None
        else "",
    )
    return row(cells, Text(task.title))


class Sort(NamedTuple):
    column: Column
    descending: bool

    @property
    def words(self) -> str:
        return self.column.words[self.descending]


def arrange(tasks: tuple[Task, ...], sort: Sort) -> tuple[Task, ...]:
    """Tasks in sorted order; a sort is stable, so ties keep stored order."""
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
    """What the task pane shows of a day under a sort and the done filter."""

    day: Day
    # as chosen, for the column names, even while sections are shown
    sort: Sort | None
    # each section with its tasks that the filter leaves, in stored order
    groups: tuple[tuple[Section, tuple[Task, ...]], ...]
    # the shown tasks in sorted order, or None while sections are shown:
    # unsorted, or sorted with no task shown, which would leave no row
    flat: tuple[Task, ...] | None
    # how many done tasks the filter hides
    hidden: int

    def shown_in(self, section: Section) -> tuple[Task, ...]:
        return next(tasks for s, tasks in self.groups if s.id == section.id)

    def shown(self) -> tuple[Task, ...]:
        return tuple(task for _, tasks in self.groups for task in tasks)

    def first_row(self) -> str:
        """Where a freshly opened day starts: its first task, else its first rule."""
        tasks = self.flat or self.shown()
        return tasks[0].id if tasks else self.day.sections[0].id

    def neighbours(self, section: Section) -> tuple[Task, ...]:
        """The tasks a deleted task's selection may pass to: any while sorted;
        otherwise its section's, since landing on the next section's rule
        would make a repeated x delete that whole section."""
        return self.flat or self.shown_in(section)


def view(day: Day, sort: Sort | None, hide_done: bool) -> View:
    groups = tuple(
        (s, tuple(t for t in s.tasks if not (hide_done and t.complete)))
        for s in day.sections
    )
    shown = tuple(task for _, tasks in groups for task in tasks)
    flat = arrange(shown, sort) if sort is not None and shown else None
    hidden = sum(task.complete for task in day.tasks()) if hide_done else 0
    return View(day, sort, groups, flat, hidden)


def next_sort(current: Sort | None, column: Column) -> Sort | None:
    """Clicking a column: its natural direction, then the reverse, then off."""
    if current is None or current.column is not column:
        return Sort(column, column.descending)
    if current.descending == column.descending:
        return Sort(column, not column.descending)
    return None


def column_at(x: int) -> Column | None:
    """The sortable column at x cells into a row, matching row's grid."""
    start = 0
    for width, column in zip(WIDTHS, (None, *COLUMNS), strict=True):
        if start <= x < start + width:
            return column
        start += width + 1
    return None


def header_row(sort: Sort | None) -> RenderableType:
    """The column names; sorted, a rule under them stands in for the first
    section's rule."""
    cells: list[Text | str] = [""]
    for column in COLUMNS:
        if sort is not None and sort.column is column:
            arrow = "↓" if sort.descending else "↑"
            cells.append(Text(column.header + arrow, style="bold"))
        else:
            cells.append(Text(column.header, style="dim"))
    names = row(tuple(cells), "")
    return names if sort is None else Group(names, Rule(style="dim"))


def section_label(section: Section) -> str:
    return f"section {section.title}" if section.title else "an untitled section"


def section_share(section: Section, total: Fraction) -> str:
    """The section's part of the day's points, or nothing when that says
    nothing: no points, or all of them."""
    _, available = score(section.tasks)
    if not available or available == total:
        return ""
    return f"{percentage(available / total)}% of total"


class SectionRule:
    """A rule line that starts a section: ── title ─────── share ──"""

    def __init__(self, section: Section, total: Fraction):
        self.title = section.title
        self.share = section_share(section, total)

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        width = options.max_width
        left = Text("──", style="dim")
        if self.title:
            left.append(" ")
            left.append(self.title, style="bold")
            left.append(" ")
        right = Text(f" {self.share} ──" if self.share else "──", style="dim")
        # the title identifies the row, so the share gives way first
        if left.cell_len + right.cell_len >= width:
            right = Text("──", style="dim")
        left.truncate(max(width - right.cell_len - 1, 3))
        fill = max(width - left.cell_len - right.cell_len, 1)
        yield left + Text("─" * fill, style="dim") + right


def subtotal_row(section: Section) -> Table | None:
    """A section's totals in the task columns, or None with nothing to total.

    Work per point counts only tasks with both a duration and points."""
    earned, available = score(section.tasks)
    minutes = sum(task.minutes or 0 for task in section.tasks)
    if not available and not minutes:
        return None
    timed_tasks = timed(section.tasks)
    _, timed_points = score(timed_tasks)
    _, timed_seconds = work(timed_tasks)
    style = "bold"
    cells = (
        "",
        Text(number(earned) if available else "", style=style),
        Text(number(available) if available else "", style=style),
        # unit letters stay undimmed, so the totals read as one block
        Text(duration(minutes * 60).plain if minutes else "", style=style),
        Text(
            rate(timed_seconds, timed_points).plain if timed_tasks else "", style=style
        ),
    )
    title = Text("subtotal", style=style)
    if available:
        title.append(f"  {percentage(earned / available)}%")
    return row(cells, title)


def labelled(*rows: tuple[str, Text]) -> Table:
    """Labels at the left edge and values at the pane's right edge."""
    table = Table.grid(padding=(0, 1))
    table.width = PANE_WIDTH
    table.add_column(style="dim", no_wrap=True)
    table.add_column(justify="right", no_wrap=True, ratio=1)
    for cells in rows:
        table.add_row(*cells)
    return table


def total(label: str, done: Text, whole: Text, progress: Fraction, color: str) -> Group:
    """done / whole, over a bar that ends in its percentage."""
    share = f" {percentage(progress)}%"
    length = PANE_WIDTH - LABEL_WIDTH - 1 - len(share)
    filled = min(int(progress * length), length)
    bar = Text.assemble(
        " " * (LABEL_WIDTH + 1),
        ("━" * filled, color),
        ("─" * (length - filled), "dim"),
        (share, color),
    )
    return Group(labelled((label, done + Text(" / ") + whole)), bar)


def verdict(seconds: int | Fraction, good: str, bad: str) -> Text:
    """The word gives the direction, so the duration goes unsigned and colour
    only backs the word up. Both come from the whole seconds, so under a second
    either way reads as 0s on the good side."""
    whole = int(seconds)
    word, color = (good, "blue") if whole >= 0 else (bad, "yellow")
    return Text.assemble((word, color), " ", duration(abs(whole)))


def schedule_rows(day: Day) -> Table:
    if not day.intervals:
        return labelled(("schedule", Text("none", style="dim")))
    rows: list[tuple[str, Text]] = []
    for index, interval in enumerate(day.intervals):
        text = Text(interval_text(interval))
        if interval.next_day:
            text.append(" next day", style="dim")
        rows.append(("schedule" if index == 0 else "", text))
    return labelled(*rows)


def stats(day: Day, now: datetime, full_title: str | None) -> Group:
    """The day's totals, time and work per point, and verdicts on the time left.

    Time comes from the schedule and work from the task durations; unscored
    tasks take time, so their durations count as work never done."""
    tasks = tuple(day.tasks())
    earned, available = score(tasks)
    elapsed, scheduled = time_progress(day.intervals, seconds_into(day.date, now))
    worked, needed = work(tasks)
    timed_tasks = timed(tasks)
    timed_earned, timed_available = score(timed_tasks)
    timed_worked, timed_needed = work(timed_tasks)

    totals: list[RenderableType] = []
    if scheduled:
        totals.append(
            total(
                "time",
                duration(elapsed),
                duration(scheduled),
                Fraction(elapsed, scheduled),
                "cyan",
            )
        )
    if available:
        totals.append(
            total(
                "pts",
                Text(number(earned)),
                Text(number(available)),
                earned / available,
                "green",
            )
        )
    if needed:
        totals.append(
            total(
                "work",
                duration(int(worked)),
                duration(int(needed)),
                worked / needed,
                "green",
            )
        )

    blocks: list[RenderableType] = [schedule_rows(day), *totals]
    if available and (scheduled or timed_available):
        rates = Table.grid(padding=(0, 1))
        rates.add_column(min_width=LABEL_WIDTH, style="dim", no_wrap=True)
        # room for 9h59m59s per point in each, which fills the pane
        rates.add_column(min_width=8, justify="right", no_wrap=True)
        rates.add_column(min_width=8, justify="right", no_wrap=True)
        rates.add_row("", Text("left", style="dim"), Text("all", style="dim"))
        if scheduled:
            # with the schedule over, no time is left to spread across points
            left = (
                Text("-")
                if elapsed == scheduled
                else rate(scheduled - elapsed, available - earned)
            )
            rates.add_row("time/pt", left, rate(scheduled, available))
        if timed_available:
            rates.add_row(
                "work/pt",
                rate(timed_needed - timed_worked, timed_available - timed_earned),
                rate(timed_needed, timed_available),
            )
        blocks.append(rates)

    verdicts: list[tuple[str, Text]] = []
    if scheduled and available:
        # the schedule spread evenly over the points, as far as they're earned;
        # whole seconds, so pace is the difference of the two durations shown
        worth = int(scheduled * earned / available)
        ahead = worth - elapsed
        verdicts.append(("earned", duration(worth)))
        verdicts.append(
            ("pace", verdict(ahead, "ahead", "behind") if ahead else Text("on pace"))
        )
    if scheduled and needed:
        verdicts.append(("all day", verdict(scheduled - needed, "spare", "over")))
        verdicts.append(
            (
                "from now",
                verdict(scheduled - elapsed - (needed - worked), "spare", "over"),
            )
        )
    if verdicts:
        blocks.append(labelled(*verdicts))

    missing = sum(t.minutes is None and t.available is not None for t in tasks)
    if needed and missing:
        blocks.append(Text(f"{missing} scored without a duration", style="dim"))
    if full_title is not None:
        blocks.append(Text(full_title, style="bold"))
    spaced: list[RenderableType] = []
    for block in blocks:
        if spaced:
            spaced.append(Text())
        spaced.append(block)
    return Group(*spaced)


class Deleted(NamedTuple):
    """The row x last removed, which p and P put back as a copy."""

    date: date
    row: Task | Section


class Change(NamedTuple):
    # the day undoing returns to, or on a redo stack the day redoing does
    restores: Day
    description: str
    # the row to select afterwards: a task or section id
    subject: str | None


def progress_change(before: Task, after: Task) -> str:
    if after.available is None:
        raise RuntimeError("unscored tasks have no progress to describe")
    available = number(after.available)
    return (
        f"changed {after.title} from {number(before.earned)}/{available} "
        f"to {number(after.earned)}/{available}"
    )


# keys use Textual's key displays so tests can check every binding is listed
HELP = (
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
        "Days",
        (
            ("[ ]", "previous or next day"),
            ("t", "today"),
            ("T", "open a date"),
            ("c", "replace this list with another day's"),
            ("@", "edit schedule"),
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
    ("App", (("?", "show this list"), ("q", "quit"))),
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
        # every row is one line, so a page of lines is a page of rows
        step = max(self.scrollable_content_region.height // 2, 1)
        self.highlighted = self.selectable_near(
            self.highlighted + direction * step, direction
        )

    def selectable_near(self, index: int, direction: int) -> int:
        """The row at index, clamped to the list, if the cursor may rest there;
        else the nearest one in direction, else the nearest the other way."""
        options = self.options
        start = min(max(index, 0), len(options) - 1)
        for step in (direction, -direction):
            probe = start
            while 0 <= probe < len(options):
                if not options[probe].disabled:
                    return probe
                probe += step
        raise RuntimeError("the task list has no selectable row")


class TaskPane(Vertical):
    """The column names, the task list and its hint, all drawn from one
    View. The cursor stays on a row the view shows and may select."""

    def compose(self) -> ComposeResult:
        yield Static(header_row(None), id="columns")
        with Vertical(id="list-area"):
            yield VimList(id="tasks")
            yield Static("", id="empty")

    @property
    def selected_id(self) -> str | None:
        option = self.query_one(VimList).highlighted_option
        return option.id if option else None

    def select(self, row_id: str) -> None:
        widget = self.query_one(VimList)
        widget.highlighted = widget.get_option_index(row_id)

    def show(self, view: View, selection: str | None = None) -> None:
        """Draws view, selecting the row selection names if shown, else the
        row that was selected, else the nearest row where the cursor was."""
        widget = self.query_one(VimList)
        selection = selection or self.selected_id
        old_index = widget.highlighted
        options: list[Option] = []
        if view.flat is not None:
            options = [Option(task_row(task), id=task.id) for task in view.flat]
        else:
            _, total = score(view.day.tasks())
            for section, tasks in view.groups:
                # section and task ids are unique across both kinds (see Day)
                options.append(Option(SectionRule(section, total), id=section.id))
                options.extend(Option(task_row(t), id=t.id) for t in tasks)
                subtotal = subtotal_row(section)
                if subtotal is not None:
                    options.append(Option(subtotal, disabled=True))
        widget.set_options(options)
        ids = [option.id for option in options]
        if selection is not None and selection in ids:
            widget.highlighted = ids.index(selection)
        else:
            # upward, so the cursor stays in its section rather than reaching
            # the next one
            widget.highlighted = widget.selectable_near(old_index or 0, -1)
        empty = self.query_one("#empty", Static)
        if any(view.day.tasks()):
            empty.update("all done · f shows them")
        else:
            empty.update("o  add tasks\nc  copy another day's list")
        empty.display = not view.shown()
        self.query_one("#columns", Static).update(header_row(view.sort))


class VimScroll(VerticalScroll):
    BINDINGS = [
        Binding("j", "scroll_down", "Scroll down", show=False),
        Binding("k", "scroll_up", "Scroll up", show=False),
    ]


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
    """Asks for one line; on_submit acts on it, raising a RECOVERABLE error to
    keep the prompt open with the reason shown."""

    def __init__(
        self, title: str, value: str, hint: str, on_submit: Callable[[str], object]
    ):
        super().__init__()
        self.title_text = title
        self.value = value
        self.hint = hint
        self.on_submit = on_submit

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog", id="prompt-dialog"):
            yield Label(self.title_text, classes="dialog-title", markup=False)
            yield Input(self.value, compact=True)
            yield Static("", id="error", markup=False)
            yield Static(self.hint, classes="muted hint", markup=False)

    def save(self) -> None:
        self.on_submit(self.query_one(Input).value)
        self.dismiss()


class TaskEditor(Form):
    """Edits one task, or adds tasks one after another until closed; on_submit
    saves each, raising a RECOVERABLE error to keep the draft."""

    # what a field accepts, told in the hint while it has focus
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
        if task:
            self.keys = "⏎ save · esc cancel"
        else:
            self.keys = "⏎ add and start the next · esc close"

    def compose(self) -> ComposeResult:
        task = self.edited_task
        with Vertical(classes="dialog", id="task-dialog"):
            yield Label(self.heading, classes="dialog-title", markup=False)
            with Horizontal(id="task-fields"):
                yield Label("pts")
                yield Input(
                    number(task.available)
                    if task and task.available is not None
                    else "",
                    id="available",
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
                yield Label("title")
                yield Input(
                    task.title if task else "",
                    id="title",
                    compact=True,
                    select_on_focus=False,
                )
            yield Static("", id="error", markup=False)
            # the list is hidden behind the dialog, so confirm each added task
            yield Static("", id="added", classes="muted", markup=False)
            yield Static(self.keys, id="hint", classes="muted hint", markup=False)

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        note = self.FIELD_HINTS.get(event.widget.id or "")
        hint = f"{self.keys} · {note}" if note else self.keys
        self.query_one("#hint", Static).update(hint)

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
        self.query_one("#available", Input).focus()


def time_field(value: str, column: str) -> Input:
    return Input(value, restrict=r"[0-9:]*", max_length=5, compact=True, classes=column)


def read_time(row: int, column: str, text: str) -> int:
    """A row's start or end time, with the row and column named on failure."""
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


class AddLine(Static, can_focus=True):
    """A line that adds an interval row when chosen, in place of a button."""

    BINDINGS = [Binding("enter,space", "screen.add_row", "Add interval", show=False)]

    async def on_click(self) -> None:
        await self.run_action("screen.add_row")


class ScheduleEditor(Form):
    """Edits a schedule as one row per interval, written in order, so a row
    that starts before the previous one ends falls on the next day. Blank rows
    drop out on save; on_submit saves the intervals, raising a RECOVERABLE
    error to keep the draft."""

    BINDINGS = [
        Binding("ctrl+n", "add_row", "Add interval", show=False),
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
            with Vertical(id="intervals"):
                for interval in self.intervals or (None,):
                    yield self.row(interval)
            yield AddLine("+ add interval", id="add-interval")
            yield Static("", id="error", markup=False)
            yield Static(
                "⏎ save · esc cancel · ^n add · blank removes\n"
                "24-hour times, like 07:30, 0730 or 7",
                classes="muted hint",
                markup=False,
            )

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
        """Each readable row's duration, and next day where the order carries
        its start past midnight; nothing while the schedule can't be read."""
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
                text.append("next day  ", style="dim")
            note.update(text + duration(interval.seconds))

    def focused_row(self) -> Horizontal | None:
        parent = self.focused.parent if self.focused else None
        return parent if isinstance(parent, Horizontal) else None

    async def action_add_row(self) -> None:
        """A blank row below the focused one, or at the end."""
        row = self.row(None)
        await self.query_one("#intervals").mount(row, after=self.focused_row())
        row.query_one(".start", Input).focus()

    def action_move(self, offset: int) -> None:
        """Up or down a row, keeping to the start or end column; below the
        last row is the add line."""
        rows = list(self.query(".interval").results(Horizontal))
        add = self.query_one(AddLine)
        if self.focused is add:
            if offset < 0:
                rows[-1].query_one(".start", Input).focus()
            return
        current = self.focused_row()
        if current is None or self.focused is None:
            return
        index = rows.index(current) + offset
        column = ".end" if self.focused.has_class("end") else ".start"
        if index >= len(rows):
            add.focus()
        elif index >= 0:
            rows[index].query_one(column, Input).focus()

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
            yield Static("⏎ sort · esc cancel", classes="muted hint", markup=False)

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

    def compose(self) -> ComposeResult:
        text = Text()
        width = max(len(keys) for _, rows in HELP for keys, _ in rows) + 2
        for index, (group, rows) in enumerate(HELP):
            if index:
                text.append("\n\n")
            text.append(group, style="bold")
            for keys, description in rows:
                text.append(f"\n{keys:<{width}}", style="bold blue")
                text.append(description)
        with VimScroll(classes="dialog", id="help-dialog"):
            yield Static(text)


class TodoApp(App):
    CSS_PATH = "app.tcss"
    TITLE = "todo"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("o", "add(True)", "Add"),
        Binding("O", "add(False)", "Add above", show=False),
        Binding("#", "new_section", "Section"),
        Binding("e", "edit", "Edit"),
        Binding("space", "complete", "Done"),
        Binding("plus", "step(1)", "Points", key_display="+/-"),
        Binding("minus", "step(-1)", "Points down", show=False),
        Binding("x", "delete", "Delete"),
        Binding("delete", "delete", "Delete", show=False),
        Binding("p", "put(True)", "Put", show=False),
        Binding("P", "put(False)", "Put above", show=False),
        Binding("u", "undo", "Undo"),
        Binding("ctrl+r", "redo", "Redo", show=False),
        Binding("@", "schedule", "Schedule"),
        Binding("question_mark", "help", "Keys"),
        Binding("c", "reuse", "Reuse", show=False),
        Binding("s", "sort", "Sort", show=False),
        Binding("f", "toggle_done", "Hide done", show=False),
        Binding("J", "move(1)", "Move down", show=False),
        Binding("K", "move(-1)", "Move up", show=False),
        Binding("}", "jump_section(1)", "Next section", show=False),
        Binding("{", "jump_section(-1)", "Previous section", show=False),
        Binding("T", "choose_day", "Choose day", show=False),
        Binding("[", "shift_day(-1)", "Previous day", show=False),
        Binding("]", "shift_day(1)", "Next day", show=False),
        Binding("t", "today", "Today", show=False),
        # hidden for room at 80 columns; ? lists it
        Binding("q", "quit", "Quit", show=False),
    ]

    def __init__(self, store: Store, list_date: date):
        super().__init__(ansi_color=True)
        self.store = store
        # undo and redo stacks per date, kept while the app runs; the store's
        # lock means nothing else changes a day behind them
        self.histories: dict[date, list[Change]] = {}
        self.redos: dict[date, list[Change]] = {}
        self.deleted: Deleted | None = None
        # views that apply to every day until quit, never stored
        self.sort: Sort | None = None
        self.hide_done = False
        self.day = store.load(list_date)

    def on_notify(self) -> None:
        # textual has no public limit on toasts, and this handler runs before
        # textual's own adds the new one, so trim once the message is handled
        self.call_next(self.trim_messages)

    def trim_messages(self) -> None:
        for notification in list(self._notifications)[:-MAX_MESSAGES]:
            self._unnotify(notification)

    def compose(self) -> ComposeResult:
        yield Static("", id="date")
        with Horizontal(id="body"):
            yield TaskPane(id="task-pane")
            yield Static("", id="stats")
        yield Footer(compact=True)

    def on_mount(self) -> None:
        self.theme = "ansi-dark"
        self.redraw(self.current_view().first_row())
        self.set_interval(1, self.refresh_stats)
        self.query_one("#tasks", VimList).focus()

    def action_choose_day(self) -> None:
        self.push_screen(
            Prompt(
                "Open a day's list",
                self.day.date.isoformat(),
                "⏎ open · esc cancel · YYYY-MM-DD; a new date starts an empty list",
                lambda value: self.switch_day(parse_date(value)),
            )
        )

    def action_shift_day(self, offset: int) -> None:
        try:
            target = self.day.date + timedelta(days=offset)
        except OverflowError:
            return
        self.navigate_day(target)

    def action_today(self) -> None:
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
        """The highlighted row: a task with its section, or a section's rule.

        A day always has a section and redraw always highlights a row, so a
        missing row is a bug rather than a state to handle."""
        row_id = self.query_one(TaskPane).selected_id
        for section in self.day.sections:
            if row_id == section.id:
                return section, None
            for task in section.tasks:
                if task.id == row_id:
                    return section, task
        raise RuntimeError("the task list has no highlighted row")

    def current_view(self) -> View:
        return view(self.day, self.sort, self.hide_done)

    def refuse_while_sorted(self, action: str) -> bool:
        """Says so when the sorted list hides the order or sections an action
        works on."""
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
        line = Text(f"{self.day.date:%a} {self.day.date.isoformat()}", style="bold")
        if self.hide_done:
            line.append(f"   {current.hidden} done hidden · ", style="dim")
            line.append("f", style="bold blue")
            line.append(" shows them", style="dim")
        self.query_one("#date", Static).update(line)
        self.refresh_stats()

    def refresh_stats(self) -> None:
        if not self.is_running:
            return
        full_title = None
        _, task = self.selected()
        if task is not None:
            tasks = self.query_one("#tasks", VimList)
            # option padding takes one column on each side
            width = tasks.scrollable_content_region.width - 2 - FIXED_WIDTH
            if Text(task.title).cell_len > width:
                full_title = task.title
        self.query_one("#stats", Static).update(
            stats(self.day, datetime.now(), full_title)
        )

    @on(OptionList.OptionHighlighted, "#tasks")
    def highlighted(self) -> None:
        self.refresh_stats()

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
        day: Day,
        description: str,
        subject: str | None = None,
        *,
        select: str | None = None,
    ) -> bool:
        """Save a change about the subject row, which is selected after the
        change unless select names another row, and again after undoing.
        Returns whether anything changed."""
        if day == self.day:
            return False
        self.store.save(day)
        self.histories.setdefault(day.date, []).append(
            Change(self.day, description, subject)
        )
        self.redos.pop(day.date, None)
        self.day = day
        self.redraw(select or subject)
        return True

    def save_or_notify(
        self,
        day: Day,
        description: str,
        subject: str | None = None,
        *,
        select: str | None = None,
    ) -> bool:
        try:
            return self.save_change(day, description, subject, select=select)
        except RECOVERABLE as error:
            self.notify(str(error), title="Could not save", severity="error", timeout=8)
            return False

    def insertion_point(self, below: bool) -> tuple[Section, int]:
        """Where a task goes relative to the selected row; a rule's section
        starts just below it, so both directions give its top."""
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
                self.day.insert_task(task, section.id, index),
                f"added {task.title}",
                task.id,
            )
            # each further task goes below the last, like lines typed after o
            index += 1

        self.push_screen(
            TaskEditor(None, f"Add tasks to {section_label(section)}", add)
        )

    def action_new_section(self) -> None:
        if self.refuse_while_sorted("add sections"):
            return
        index = self.day.sections.index(self.selected()[0]) + 1

        def create(title: str) -> None:
            section = Section(title)
            self.save_change(
                self.day.insert_section(section, index),
                f"added {section_label(section)}",
                section.id,
            )

        self.push_screen(
            Prompt("New section", "", "⏎ add · esc cancel · blank for untitled", create)
        )

    def action_reuse(self) -> None:
        try:
            sources = tuple(d for d in self.store.list_dates() if d != self.day.date)
        except RECOVERABLE as error:
            self.notify(str(error), title="Could not open lists", severity="error")
            return
        if not sources:
            self.notify("No other saved lists yet.")
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
                "⏎ copy · esc cancel · progress starts over; u undoes",
                reuse,
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
                        self.day.replace_task(edited),
                        f"edited {edited.title}",
                        edited.id,
                    ),
                )
            )
            return

        def rename(value: str) -> None:
            day = self.day.rename_section(section.id, value)
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
                "⏎ save · esc cancel · blank for untitled",
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
                self.day.replace_task(after), progress_change(task, after), task.id
            )

        self.push_screen(
            Prompt(
                f"Points earned on {task.title}",
                number(task.earned),
                f"⏎ save · esc cancel · out of {number(available)}",
                save,
            )
        )

    def action_schedule(self) -> None:
        self.push_screen(
            ScheduleEditor(
                self.day.intervals,
                lambda intervals: self.save_change(
                    replace(self.day, intervals=intervals), "changed the schedule"
                ),
            )
        )

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
            self.day.replace_task(after), progress_change(task, after), after.id
        )

    def action_delete(self) -> None:
        section, task = self.selected()
        if task is not None:
            tasks = self.current_view().neighbours(section)
            index = tasks.index(task)
            neighbours = tasks[index + 1 : index + 2] or tasks[index - 1 : index]
            if self.save_or_notify(
                self.day.remove_task(task.id),
                f"deleted {task.title}",
                task.id,
                select=neighbours[0].id if neighbours else section.id,
            ):
                self.deleted = Deleted(self.day.date, task)
            return
        count = len(section.tasks)
        tasks = f" and its {count} task{'s' if count != 1 else ''}" if count else ""
        removed = self.day.remove_section(section.id)
        verb = (
            "cleared" if len(removed.sections) == len(self.day.sections) else "deleted"
        )
        if self.save_or_notify(
            removed, f"{verb} {section_label(section)}{tasks}", section.id
        ):
            self.deleted = Deleted(self.day.date, section)

    def action_put(self, below: bool) -> None:
        if self.refuse_while_sorted("put rows back"):
            return
        if self.deleted is None:
            self.notify("Nothing deleted to put.", timeout=2)
            return
        # progress belongs to the day it was made on
        keep_progress = self.deleted.date == self.day.date
        copied = self.deleted.row.duplicate(keep_progress=keep_progress)
        if isinstance(copied, Task):
            section, index = self.insertion_point(below)
            day = self.day.insert_task(copied, section.id, index)
            label = copied.title
        else:
            index = self.day.sections.index(self.selected()[0]) + below
            day = self.day.insert_section(copied, index)
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
                self.day.move_task(task.id, offset),
                f"moved {task.title} {direction}",
                task.id,
            )
        else:
            self.save_or_notify(
                self.day.move_section(section.id, offset),
                f"moved {section_label(section)} {direction}",
                section.id,
            )

    def action_jump_section(self, offset: Literal[-1, 1]) -> None:
        """Vim's paragraph motions over rules: } to the next rule, or the last
        row from the last section; { to this section's rule, or from a rule
        to the one before."""
        if self.refuse_while_sorted("jump between sections"):
            return
        section, task = self.selected()
        sections = self.day.sections
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
        # only the names: a rule may sit under them
        column = column_at(offset.x) if offset and offset.y == 0 else None
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
        source: dict[date, list[Change]],
        target: dict[date, list[Change]],
        verb: str,
        past: str,
    ) -> None:
        """Return to the day on top of source, recording the way back on target."""
        stack = source.get(self.day.date)
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
        target.setdefault(self.day.date, []).append(change._replace(restores=self.day))
        self.day = change.restores
        self.redraw(change.subject)
        self.notify(f"{past}: {change.description}", timeout=4, markup=False)


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
        type=parse_date,
        default=date.today(),
        help="list date, YYYY-MM-DD (default: today)",
    )
    args = parser.parse_args()
    try:
        store = Store(args.database.expanduser())
    except RECOVERABLE as error:
        parser.exit(2, f"todo: {error}\n")
    # the store holds an exclusive lock, so release it however the app ends
    with closing(store):
        try:
            app = TodoApp(store, args.date)
        except RECOVERABLE as error:
            parser.exit(2, f"todo: {error}\n")
        app.run()
