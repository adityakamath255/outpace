import argparse
import re
import sqlite3
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime, timedelta
from fractions import Fraction
from pathlib import Path
from typing import Literal, NamedTuple

from platformdirs import user_data_path
from rich.console import Console, ConsoleOptions, Group, RenderableType, RenderResult
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

from .db import Conflict, Store
from .model import (
    DAY_MINUTES,
    Day,
    Interval,
    Section,
    Task,
    clock,
    duration,
    empty_day,
    format_schedule,
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

# marker, earned, available, estimate, time per point; the title takes the rest
CELL_WIDTHS = (1, 5, 4, 6, 8)
FIXED_WIDTH = sum(CELL_WIDTHS) + len(CELL_WIDTHS)
BAR_WIDTH = 32
MAX_MESSAGES = 5


def units(value: str) -> Text:
    """Dims the unit letters of durations like 3h40m so the digits stand out."""
    text = Text()
    position = 0
    for match in re.finditer(r"(?<=\d)[hms]", value):
        text.append(value[position : match.start()])
        text.append(match.group(), style="dim")
        position = match.end()
    text.append(value[position:])
    return text


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
        units(duration(task.minutes * 60)) if task.minutes else "",
        units(per_point(task.minutes * 60, task.available))
        if task.minutes and task.available is not None
        else "",
    )
    return row(cells, Text(task.title))


def section_label(section: Section) -> str:
    return f"section {section.title}" if section.title else "an untitled section"


def section_summary(section: Section, total: Fraction) -> tuple[str, ...]:
    """The summary parts of a section's rule, most important first."""
    if not section.tasks:
        return ()
    earned, available = score(section.tasks)
    if available:
        share = f"{percentage(ratio(available, total))}% of total"
        return f"{number(earned)}/{number(available)}", share
    return (
        f"{sum(task.complete for task in section.tasks)}/{len(section.tasks)} done",
    )


class SectionRule:
    """A rule line that starts a section: ── title ─────── summary ──"""

    def __init__(self, section: Section, total: Fraction):
        self.title = section.title
        self.summary = section_summary(section, total)

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        width = options.max_width
        left = Text("──", style="dim")
        if self.title:
            left.append(" ")
            left.append(self.title, style="bold")
            left.append(" ")
        # the title identifies the row, so the summary gives way first
        for count in range(len(self.summary), -1, -1):
            parts = "  ".join(self.summary[:count])
            right = Text(f" {parts} ──" if parts else "──", style="dim")
            if left.cell_len + right.cell_len < width:
                break
        left.truncate(max(width - right.cell_len - 1, 3))
        fill = max(width - left.cell_len - right.cell_len, 1)
        yield left + Text("─" * fill, style="dim") + right


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


def schedule_status(intervals: tuple[Interval, ...], now: int) -> str:
    """now is seconds_into the list's day."""
    if not intervals:
        return "No schedule"
    if now < min(interval.start for interval in intervals) * 60:
        return "Not started"
    if now >= max(interval.end for interval in intervals) * 60:
        return "Schedule ended"
    if any(i.start * 60 <= now < i.end * 60 for i in intervals):
        return "In progress"
    return "Between intervals"


def schedule_blocks(
    day: Day, now: datetime, earned: Fraction, available: Fraction, width: int
) -> list[RenderableType]:
    now_seconds = seconds_into(day.date, now)
    elapsed, total = time_progress(day.intervals, now_seconds)
    progress, time_ratio = ratio(earned, available), ratio(elapsed, total)
    value = units(duration(elapsed)) + Text(" / ") + units(duration(total))
    status = Text()
    if progress is not None and time_ratio is not None:
        difference = progress - time_ratio
        # the sign carries the meaning; colour only backs it up
        if difference > 0:
            style, word = "bold blue", "ahead"
        elif difference < 0:
            style, word = "bold yellow", "behind"
        else:
            style, word = "bold", "on pace"
        status.append(f"{percentage(difference, signed=True)} pp {word}\n", style=style)
    status.append(schedule_status(day.intervals, now_seconds).lower())
    if not total:
        remaining = Text("n/a")
    elif elapsed == total:
        remaining = Text("ended")
    else:
        remaining = units(per_point(total - elapsed, available - earned))
    pace = Table.grid(padding=(0, 2))
    pace.add_column(style="dim", no_wrap=True)
    pace.add_column(no_wrap=True)
    pace.add_row("time / point", units(per_point(total, available)))
    pace.add_row("remaining / point", remaining)
    intervals = Text()
    for index, interval in enumerate(day.intervals):
        if index:
            intervals.append("  ")
        intervals.append(f"{clock(interval.start)}-{clock(interval.end)}")
        if interval.start >= DAY_MINUTES:
            intervals.append(" next day", style="dim")
    return [meter("time", value, time_ratio, "cyan", width), status, pace, intervals]


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
    else:
        blocks.append(Text("no schedule · s to set one", style="dim"))
    estimates = [task.minutes for task in tasks if task.minutes is not None]
    if estimates:
        text = units(duration(sum(estimates) * 60))
        text.append(f" estimated across {len(estimates)} tasks")
        missing = sum(
            task.minutes is None and task.available is not None for task in tasks
        )
        if missing:
            text.append(f"\n{missing} scored without an estimate", style="dim")
        blocks.append(text)
    if full_title is not None:
        blocks.append(Text(full_title, style="bold"))
    spaced: list[RenderableType] = []
    for block in blocks:
        if spaced:
            spaced.append(Text())
        spaced.append(block)
    return Group(*spaced)


class Change(NamedTuple):
    previous: Day
    previous_persisted: bool
    # the stored day this change produced; undo applies only while it is current
    after: Day
    description: str
    # the row to select after undoing: a task or section id
    subject: str | None


def progress_change(before: Task, after: Task) -> str:
    if after.available is None:
        return f"marked {after.title} {'done' if after.checked else 'not done'}"
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
            ("g", "first row"),
            ("G", "last row"),
            ("⏎", "set points, toggle unscored, or rename section"),
            ("space", "toggle done"),
            ("+ -", "earned points up or down by one"),
            ("a", "add tasks to the selected section"),
            ("e", "edit task, or rename section"),
            ("o", "new untitled section below"),
            ("r", "rename section; blank for untitled"),
            ("dd del", "delete task, or section with its tasks"),
            ("J K", "move down or up, across sections"),
            ("u", "undo, kept per day until you quit"),
        ),
    ),
    (
        "Days",
        (
            ("[ ]", "previous or next day"),
            ("t", "today"),
            ("^d", "open a date"),
            ("c", "replace this list with another day's"),
            ("s", "edit schedule"),
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
    ("App", (("?", "show this list"), ("q ^q", "quit"))),
)


class TaskList(OptionList):
    BINDINGS = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("g", "first", "First", show=False),
        Binding("G", "last", "Last", show=False),
    ]
    delete_pending = False

    # dd deletes; Textual bindings cannot express chords, so any other key or
    # leaving the list cancels a pending d
    async def on_key(self, event: events.Key) -> None:
        if event.key != "d":
            self.delete_pending = False
            return
        event.stop()
        if self.delete_pending:
            self.delete_pending = False
            await self.run_action("app.delete")
        else:
            self.delete_pending = True

    def on_blur(self) -> None:
        self.delete_pending = False


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

    @on(Input.Submitted)
    def action_save(self) -> None:
        try:
            self.save()
        except RECOVERABLE as error:
            self.query_one("#error", Static).update(str(error))


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
            yield Static(self.hint, classes="muted", markup=False)

    def save(self) -> None:
        self.on_submit(self.query_one(Input).value)
        self.dismiss()


class TaskEditor(Form):
    """Edits one task, or adds tasks one after another until closed; on_submit
    saves each, raising a RECOVERABLE error to keep the draft."""

    def __init__(
        self, task: Task | None, heading: str, on_submit: Callable[[Task], None]
    ):
        super().__init__()
        self.edited_task = task
        self.heading = heading
        self.on_submit = on_submit

    def compose(self) -> ComposeResult:
        task = self.edited_task
        if task:
            hint = "⏎ save · esc cancel · blank points for unscored"
        else:
            hint = "⏎ add and start the next · esc close · blank points for unscored"
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
                yield Label("min")
                yield Input(
                    str(task.minutes) if task and task.minutes else "",
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
            yield Static(hint, classes="muted", markup=False)

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
        self.query_one("#error", Static).update("")
        self.query_one("#added", Static).update(f"added {task.title}")
        self.query_one("#available", Input).focus()


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
            text.append(group, style="bold magenta")
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
        Binding("a", "add", "Add"),
        Binding("o", "new_section", "Section"),
        Binding("e", "edit", "Edit"),
        Binding("space", "complete", "Done"),
        Binding("plus", "step(1)", "Points", key_display="+/-"),
        Binding("minus", "step(-1)", "Points down", show=False),
        # TaskList handles the dd chord before bindings see d
        Binding("delete", "delete", "Delete", key_display="dd"),
        Binding("u", "undo", "Undo"),
        Binding("question_mark", "help", "Keys"),
        Binding("r", "rename_section", "Rename section", show=False),
        Binding("c", "reuse", "Reuse", show=False),
        Binding("s", "schedule", "Schedule", show=False),
        Binding("J", "move(1)", "Move down", show=False),
        Binding("K", "move(-1)", "Move up", show=False),
        Binding("ctrl+d", "choose_day", "Choose day", show=False),
        Binding("[", "shift_day(-1)", "Previous day", show=False),
        Binding("]", "shift_day(1)", "Next day", show=False),
        Binding("t", "today", "Today", show=False),
        Binding("q", "quit", "Quit"),
        Binding("ctrl+q", "quit", "Quit", show=False),
    ]

    def __init__(self, store: Store, list_date: date):
        super().__init__(ansi_color=True)
        self.store = store
        # undo history per date, kept while the app runs
        self.histories: dict[date, list[Change]] = {}
        self._load_day(list_date)

    @property
    def history(self) -> tuple[Change, ...]:
        return tuple(self.histories.get(self.day.date, ()))

    def on_notify(self) -> None:
        # textual has no public limit on toasts, and this handler runs before
        # textual's own adds the new one, so trim once the message is handled
        self.call_next(self.trim_messages)

    def trim_messages(self) -> None:
        for notification in list(self._notifications)[:-MAX_MESSAGES]:
            self._unnotify(notification)

    def _load_day(self, list_date: date) -> None:
        saved = self.store.load(list_date)
        self.day = saved if saved is not None else empty_day(list_date)
        self.persisted = saved is not None
        history = self.histories.get(list_date)
        # another session changed this day since the last change here, so
        # undoing would overwrite their work; each entry depends on the next
        if history and history[-1].after != self.day:
            del self.histories[list_date]

    def compose(self) -> ComposeResult:
        yield Static("", id="date")
        with Horizontal(id="body"):
            with Vertical(id="task-pane"):
                yield Static(
                    row(tuple(map(Text, ("", "done", "pts", "est", "t/pt"))), ""),
                    id="columns",
                )
                yield TaskList(id="tasks")
                yield Static("a  add tasks\nc  copy another day's list", id="empty")
            yield Static("", id="stats")
        yield Footer(compact=True)

    def on_mount(self) -> None:
        self.theme = "ansi-dark"
        self.redraw(self.first_row())
        self.set_interval(1, self.refresh_stats)
        self.query_one("#tasks", TaskList).focus()

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
        self._load_day(list_date)
        self.redraw(self.first_row())

    def selected(self) -> tuple[Section, Task | None]:
        """The highlighted row: a task with its section, or a section's rule.

        A day always has a section and redraw always highlights a row, so a
        missing row is a bug rather than a state to handle."""
        option = self.query_one("#tasks", TaskList).highlighted_option
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
        return next((task.id for _, task in self.day.tasks()), self.day.sections[0].id)

    def redraw(self, selection: str | None = None) -> None:
        widget = self.query_one("#tasks", TaskList)
        old = widget.highlighted_option
        selection = selection or (old.id if old else None)
        old_index = widget.highlighted
        _, total = score(task for _, task in self.day.tasks())
        options = []
        for section in self.day.sections:
            # section and task ids are unique across both kinds (see Day)
            options.append(Option(SectionRule(section, total), id=section.id))
            options.extend(Option(task_row(task), id=task.id) for task in section.tasks)
        widget.clear_options().add_options(options)
        ids = [option.id for option in options]
        if selection in ids:
            widget.highlighted = ids.index(selection)
        else:
            # the selected row is gone, so keep the cursor where it was
            widget.highlighted = min(old_index or 0, len(ids) - 1)
        self.query_one("#empty", Static).display = not any(self.day.tasks())
        self.query_one("#date", Static).update(
            Text(f"{self.day.date:%a} {self.day.date.isoformat()}", style="bold")
        )
        self.refresh_stats()

    def refresh_stats(self) -> None:
        if not self.is_running:
            return
        full_title = None
        _, task = self.selected()
        if task is not None:
            tasks = self.query_one("#tasks", TaskList)
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
        section, task = self.selected()
        if task is None:
            self.rename_section(section)
        elif task.available is None:
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
        self.store.save(day, expected=self.day if self.persisted else None)
        self.histories.setdefault(day.date, []).append(
            Change(self.day, self.persisted, day, description, subject)
        )
        self.day = day
        self.persisted = True
        self.redraw(select or subject)

    def save_or_notify(
        self,
        day: Day,
        description: str,
        subject: str | None = None,
        *,
        select: str | None = None,
    ) -> None:
        try:
            self.save_change(day, description, subject, select=select)
        except RECOVERABLE as error:
            self.notify(str(error), title="Could not save", severity="error", timeout=8)

    def action_add(self) -> None:
        section = self.selected()[0]
        self.push_screen(
            TaskEditor(
                None,
                f"Add tasks to {section_label(section)}",
                lambda task: self.save_change(
                    self.day.add_task(task, section.id), f"added {task.title}", task.id
                ),
            )
        )

    def action_new_section(self) -> None:
        section = Section("")
        self.save_or_notify(
            self.day.insert_section(section, after=self.selected()[0].id),
            f"added {section_label(section)}",
            section.id,
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
            if source is None:
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
        if task is None:
            self.rename_section(section)
        else:
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

    def action_rename_section(self) -> None:
        self.rename_section(self.selected()[0])

    def rename_section(self, section: Section) -> None:
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
                "07:30-11:00, 16:00-23:00; past midnight continues into the "
                "next day; blank clears",
                lambda value: self.save_change(
                    replace(self.day, intervals=parse_schedule(value)),
                    "changed the schedule",
                ),
            )
        )

    def action_complete(self) -> None:
        _, task = self.selected()
        if task is not None:
            self.save_progress(task, task.toggle())

    def action_step(self, delta: int) -> None:
        _, task = self.selected()
        if task is not None:
            self.save_progress(task, task.step(delta))

    def save_progress(self, before: Task, after: Task) -> None:
        self.save_or_notify(
            self.day.replace_task(after), progress_change(before, after), after.id
        )

    def action_delete(self) -> None:
        section, task = self.selected()
        if task is not None:
            # stay inside the section: landing on the next section's rule would
            # make a repeated dd delete that whole section
            tasks = section.tasks
            index = tasks.index(task)
            neighbours = tasks[index + 1 : index + 2] or tasks[index - 1 : index]
            self.save_or_notify(
                self.day.remove_task(task.id),
                f"deleted {task.title}",
                task.id,
                select=neighbours[0].id if neighbours else section.id,
            )
            return
        count = len(section.tasks)
        tasks = f" and its {count} task{'s' if count != 1 else ''}" if count else ""
        removed = self.day.remove_section(section.id)
        verb = (
            "cleared" if len(removed.sections) == len(self.day.sections) else "deleted"
        )
        self.save_or_notify(
            removed, f"{verb} {section_label(section)}{tasks}", section.id
        )

    def action_move(self, offset: Literal[-1, 1]) -> None:
        section, task = self.selected()
        direction = "down" if offset > 0 else "up"
        if task is not None:
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

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_undo(self) -> None:
        history = self.histories.get(self.day.date)
        if not history:
            self.notify("Nothing to undo.", timeout=2)
            return
        change = history[-1]
        try:
            if change.previous_persisted:
                self.store.save(change.previous, expected=change.after)
            else:
                self.store.delete(change.after)
        except Conflict as error:
            # the stored day no longer matches this history, so none of it applies
            del self.histories[self.day.date]
            self.notify(str(error), title="Could not undo", severity="error", timeout=8)
            return
        except RECOVERABLE as error:
            self.notify(str(error), title="Could not undo", severity="error", timeout=8)
            return
        history.pop()
        self.day = change.previous
        self.persisted = change.previous_persisted
        self.redraw(change.subject)
        self.notify(f"Undid: {change.description}", timeout=4, markup=False)


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
        type=date.fromisoformat,
        default=date.today(),
        help="list date, YYYY-MM-DD (default: today)",
    )
    args = parser.parse_args()
    try:
        store = Store(args.database.expanduser())
        app = TodoApp(store, args.date)
    except RECOVERABLE as error:
        parser.exit(2, f"todo: {error}\n")
    app.run()
