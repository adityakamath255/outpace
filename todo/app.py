import argparse
import sqlite3
from collections.abc import Callable, Iterable
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
    DAY_MINUTES,
    Day,
    Section,
    Task,
    format_schedule,
    interval_text,
    number,
    parse_date,
    parse_minutes,
    parse_schedule,
    per_point,
    percentage,
    points,
    ratio,
    score,
    seconds_into,
    time_progress,
)

# failures shown to the user while the draft or the list stays as it was
RECOVERABLE = (ValueError, sqlite3.Error, OSError)

# marker, earned, available, duration, time per point; the title takes the rest
CELL_WIDTHS = (1, 5, 4, 6, 8)
FIXED_WIDTH = sum(CELL_WIDTHS) + len(CELL_WIDTHS)
BAR_WIDTH = 32
MAX_MESSAGES = 5


def duration(seconds: int) -> Text:
    """Like 3h40m, with the unit letters dimmed so the digits stand out."""
    parts = zip((seconds // 3600, seconds // 60 % 60, seconds % 60), "hms", strict=True)
    text = Text()
    for value, unit in parts:
        if value:
            text.append(str(value))
            text.append(unit, style="dim")
    return text if text.plain else Text.assemble("0", ("s", "dim"))


def pace(seconds: int, available: Fraction) -> Text:
    value = per_point(seconds, available)
    return Text("-") if value is None else duration(value)


def row(cells: tuple[Text | str, ...], title: Text | str) -> Table:
    table = Table.grid(padding=(0, 1), expand=True)
    for width in CELL_WIDTHS:
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
        pace(task.minutes * 60, task.available)
        if task.minutes and task.available is not None
        else "",
    )
    return row(cells, Text(task.title))


def completion(task: Task) -> Fraction | None:
    if task.available is None:
        return None
    return task.earned / task.available


def time_per_point(task: Task) -> Fraction | None:
    if task.minutes is None or task.available is None:
        return None
    return task.minutes / task.available


class Column(NamedTuple):
    """A sortable column of the task list."""

    header: str
    # None marks a task without the value; such tasks sort last
    value: Callable[[Task], Fraction | int | None]
    # the direction a first click gives and the menu lists first
    descending: bool
    # how the menu names each direction: ascending, then descending
    words: tuple[str, str]


COLUMNS = (
    Column("done", completion, False, ("least done first", "most done first")),
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
        "t/pt",
        time_per_point,
        False,
        ("least time per point first", "most time per point first"),
    ),
)


class Sort(NamedTuple):
    column: Column
    descending: bool

    @property
    def words(self) -> str:
        return self.column.words[self.descending]


def arrange(tasks: tuple[Task, ...], sort: Sort | None) -> tuple[Task, ...]:
    """Tasks in display order; a sort is stable, so ties keep stored order."""
    if sort is None:
        return tasks
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
    for width, column in zip(CELL_WIDTHS, (None, *COLUMNS), strict=True):
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
    share = ratio(available, total)
    if not available or share == 1:
        return ""
    return f"{percentage(share)}% of total"


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

    Time per point counts only tasks with both a duration and points."""
    earned, available = score(section.tasks)
    minutes = sum(task.minutes or 0 for task in section.tasks)
    if not available and not minutes:
        return None
    paced = [
        (task.minutes, task.available)
        for task in section.tasks
        if task.minutes and task.available is not None
    ]
    paced_points = sum((points for _, points in paced), Fraction(0))
    style = "bold"
    cells = (
        "",
        Text(number(earned) if available else "", style=style),
        Text(number(available) if available else "", style=style),
        # unit letters stay undimmed, so the totals read as one block
        Text(duration(minutes * 60).plain if minutes else "", style=style),
        Text(
            pace(sum(m for m, _ in paced) * 60, paced_points).plain if paced else "",
            style=style,
        ),
    )
    return row(cells, Text("subtotal", style=style))


def meter(
    label: str, value: Text, progress: Fraction | None, color: str, width: int
) -> Text:
    text = Text()
    text.append(f"{label:<8}", style="dim")
    text.append_text(value)
    if progress is None:
        return text
    share = f" {percentage(progress)}%"
    bar = max(width - len(share), 1)
    filled = min(int(progress * bar), bar)
    text.append("\n" + "━" * filled, style=color)
    text.append("─" * (bar - filled), style="dim")
    text.append(share, style=color)
    return text


def schedule_blocks(
    day: Day, now: datetime, earned: Fraction, available: Fraction, width: int
) -> list[RenderableType]:
    now_seconds = seconds_into(day.date, now)
    elapsed, total = time_progress(day.intervals, now_seconds)
    progress, time_ratio = ratio(earned, available), ratio(elapsed, total)
    value = duration(elapsed) + Text(" / ") + duration(total)
    blocks: list[RenderableType] = [meter("time", value, time_ratio, "cyan", width)]
    if progress is not None and time_ratio is not None:
        difference = progress - time_ratio
        # the word gives the direction, so the number goes unsigned
        if difference:
            word = "ahead" if difference > 0 else "behind"
            line = f"{percentage(abs(difference))} pp {word}"
        else:
            line = "on pace"
        blocks.append(Text(line, style="bold"))
    # with the schedule over, no time is left to spread across points
    remaining = (
        Text("-") if elapsed == total else pace(total - elapsed, available - earned)
    )
    rates = Table.grid(padding=(0, 2))
    rates.add_column(style="dim", no_wrap=True)
    rates.add_column(no_wrap=True)
    rates.add_row("time / pt", pace(total, available))
    rates.add_row("left / pt", remaining)
    blocks.append(rates)
    return blocks


def plan(day: Day, tasks: list[Task]) -> Group:
    """The schedule against the task durations: how much time the plan leaves."""
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="dim", no_wrap=True)
    grid.add_column()
    for index, interval in enumerate(day.intervals):
        text = Text(interval_text(interval))
        if interval.start >= DAY_MINUTES:
            text.append(" next day", style="dim")
        grid.add_row("schedule" if index == 0 else "", text)
    # styled like the footer's keys
    hint = Text.assemble(("@", "bold blue"), " ", "edit" if day.intervals else "set")
    grid.add_row("" if day.intervals else "schedule", hint)
    durations = [task.minutes for task in tasks if task.minutes is not None]
    blocks: list[RenderableType] = [grid]
    if durations:
        needed = sum(durations) * 60
        grid.add_row("needed", duration(needed))
        scheduled = sum(interval.seconds for interval in day.intervals)
        # the word carries the meaning; colour only backs it up
        if scheduled and needed <= scheduled:
            grid.add_row(Text("spare", style="blue"), duration(scheduled - needed))
        elif scheduled:
            over = duration(needed - scheduled)
            over.stylize("yellow")
            grid.add_row(Text("over", style="bold yellow"), over)
        missing = sum(
            task.minutes is None and task.available is not None for task in tasks
        )
        if missing:
            blocks.append(Text(f"{missing} scored without a duration", style="dim"))
    return Group(*blocks)


def stats(day: Day, now: datetime, width: int, full_title: str | None) -> Group:
    width = min(width, BAR_WIDTH)
    tasks = [task for _, task in day.tasks()]
    earned, available = score(tasks)
    value = Text(f"{number(earned)} / {number(available)}", style="bold")
    blocks: list[RenderableType] = [
        meter("points", value, ratio(earned, available), "green", width)
    ]
    if day.intervals:
        blocks += schedule_blocks(day, now, earned, available, width)
    blocks.append(plan(day, tasks))
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
        """Shows a message line, which takes no room while empty."""
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
        self, title: str, value: str, hint: str, on_submit: Callable[[str], None]
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
        self, task: Task | None, heading: str, on_submit: Callable[[Task], None]
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
        Binding("question_mark", "help", "Keys"),
        Binding("c", "reuse", "Reuse", show=False),
        Binding("@", "schedule", "Schedule", show=False),
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
        Binding("q", "quit", "Quit"),
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
            with Vertical(id="task-pane"):
                yield Static(header_row(None), id="columns")
                yield VimList(id="tasks")
                yield Static("", id="empty")
            yield Static("", id="stats")
        yield Footer(compact=True)

    def on_mount(self) -> None:
        self.theme = "ansi-dark"
        self.redraw(self.first_row())
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
        self.redraw(self.first_row())

    def selected(self) -> tuple[Section, Task | None]:
        """The highlighted row: a task with its section, or a section's rule.

        A day always has a section and redraw always highlights a row, so a
        missing row is a bug rather than a state to handle."""
        option = self.query_one("#tasks", VimList).highlighted_option
        if option is not None:
            for section in self.day.sections:
                if option.id == section.id:
                    return section, None
                for task in section.tasks:
                    if task.id == option.id:
                        return section, task
        raise RuntimeError("the task list has no highlighted row")

    def first_row(self) -> str:
        """Where a freshly opened day starts: its first task, else its first rule."""
        ordered = self.sorted_tasks()
        if ordered:
            return ordered[0].id
        tasks = self.all_shown()
        return tasks[0].id if tasks else self.day.sections[0].id

    def shown(self, tasks: Iterable[Task]) -> tuple[Task, ...]:
        if not self.hide_done:
            return tuple(tasks)
        return tuple(task for task in tasks if not task.complete)

    def all_shown(self) -> tuple[Task, ...]:
        return self.shown(task for _, task in self.day.tasks())

    def hidden_count(self) -> int:
        """How many done tasks the filter hides."""
        return sum(t.complete for _, t in self.day.tasks()) if self.hide_done else 0

    def sorted_tasks(self) -> tuple[Task, ...] | None:
        """Every shown task in sorted order, or None while the list shows
        sections: unsorted, or sorted with nothing shown, which would leave no
        row."""
        tasks = self.all_shown()
        if self.sort is None or not tasks:
            return None
        return arrange(tasks, self.sort)

    def refuse_while_sorted(self, action: str) -> bool:
        """Says so when the sorted list hides the order or sections an action
        works on."""
        if self.sort is None or self.sorted_tasks() is None:
            return False
        self.notify(
            f"Sorted by {self.sort.words}; choose stored order with s to {action}.",
            timeout=4,
        )
        return True

    def redraw(self, selection: str | None = None) -> None:
        widget = self.query_one("#tasks", VimList)
        old = widget.highlighted_option
        selection = selection or (old.id if old else None)
        old_index = widget.highlighted
        _, total = score(task for _, task in self.day.tasks())
        ordered = self.sorted_tasks()
        if ordered is not None:
            options = [Option(task_row(task), id=task.id) for task in ordered]
        else:
            options = []
            for section in self.day.sections:
                # section and task ids are unique across both kinds (see Day)
                options.append(Option(SectionRule(section, total), id=section.id))
                options.extend(
                    Option(task_row(t), id=t.id) for t in self.shown(section.tasks)
                )
                subtotal = subtotal_row(section)
                if subtotal is not None:
                    # disabled, so the cursor passes over it
                    options.append(Option(subtotal, disabled=True))
        widget.clear_options().add_options(options)
        ids = [option.id for option in options]
        if selection is not None and selection in ids:
            widget.highlighted = ids.index(selection)
        else:
            # the selected row is gone, so keep the cursor where it was, or
            # move up within its section rather than onto the next one
            widget.highlighted = widget.selectable_near(old_index or 0, -1)
        empty = self.query_one("#empty", Static)
        if any(self.day.tasks()):
            empty.update("all done · f shows them")
        else:
            empty.update("o  add tasks\nc  copy another day's list")
        empty.display = not self.all_shown()
        self.query_one("#columns", Static).update(header_row(self.sort))
        line = Text(f"{self.day.date:%a} {self.day.date.isoformat()}", style="bold")
        if self.hide_done:
            line.append(f"   {self.hidden_count()} done hidden · ", style="dim")
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
        pane = self.query_one("#stats", Static)
        pane.update(
            stats(self.day, datetime.now(), pane.content_region.width, full_title)
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
            self.edit_points(task, task.available)

    def save_change(
        self,
        day: Day,
        description: str,
        subject: str | None = None,
        *,
        select: str | None = None,
    ) -> None:
        """Save a change about the subject row, which is selected after the
        change unless select names another row, and again after undoing."""
        if day == self.day:
            return
        self.store.save(day)
        self.histories.setdefault(day.date, []).append(
            Change(self.day, description, subject)
        )
        self.redos.pop(day.date, None)
        self.day = day
        self.redraw(select or subject)

    def save_or_notify(
        self,
        day: Day,
        description: str,
        subject: str | None = None,
        *,
        select: str | None = None,
    ) -> bool:
        """Whether the day is saved, unchanged counting as saved."""
        try:
            self.save_change(day, description, subject, select=select)
        except RECOVERABLE as error:
            self.notify(str(error), title="Could not save", severity="error", timeout=8)
            return False
        return True

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

    def edit_points(self, task: Task, available: Fraction) -> None:
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
            Prompt(
                "Schedule",
                format_schedule(self.day.intervals),
                "⏎ save · esc cancel · 24-hour times in order, like "
                "07:30 - 11:00, 16:00 - 23:00; past midnight continues into the "
                "next day; blank clears",
                lambda value: self.save_change(
                    replace(self.day, intervals=parse_schedule(value)),
                    "changed the schedule",
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
            # sorted, any neighbour will do; otherwise stay inside the section,
            # since landing on the next section's rule would make a repeated x
            # delete that whole section
            tasks = self.sorted_tasks() or self.shown(section.tasks)
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
        if task is not None and self.hidden_count():
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
            tasks = self.shown(section.tasks)
            target = tasks[-1].id if tasks else section.id
        widget = self.query_one("#tasks", VimList)
        widget.highlighted = widget.get_option_index(target)

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
        app = TodoApp(store, args.date)
    except RECOVERABLE as error:
        parser.exit(2, f"todo: {error}\n")
    try:
        app.run()
    finally:
        store.close()
