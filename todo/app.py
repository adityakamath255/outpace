import argparse
import sqlite3
from dataclasses import replace
from datetime import date, datetime, timedelta
from fractions import Fraction
from pathlib import Path
from typing import NamedTuple

from platformdirs import user_data_path
from rich.table import Table
from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    Footer,
    Input,
    Label,
    OptionList,
    Select,
    Static,
)
from textual.widgets.option_list import Option

from .db import Store
from .model import (
    Day,
    Interval,
    Section,
    Task,
    duration,
    empty_day,
    number,
    parse_interval,
    parse_minutes,
    per_point,
    percentage,
    points,
    ratio,
    score,
    time_progress,
)


def task_row(marker: str, title: str, points: str, estimate: str, style: str) -> Table:
    table = Table.grid(padding=(0, 1), collapse_padding=True, expand=True)
    table.add_column(width=1)
    table.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
    table.add_column(width=9, justify="right", no_wrap=True, overflow="ellipsis")
    table.add_column(width=6, justify="right", no_wrap=True, overflow="ellipsis")
    table.add_row(
        Text(marker, style=style),
        Text(title),
        Text(points, style=style),
        Text(estimate, style="dim"),
    )
    return table


def progress_text(
    label: str, value: str, progress: Fraction | None, color: str
) -> Text:
    text = Text()
    text.append(f"{label:<8}", style="dim")
    text.append(value, style=f"bold {color}")
    if progress is not None:
        filled = int(progress * 20)
        text.append("\n" + "━" * filled, style=color)
        text.append("─" * (20 - filled), style="dim")
        text.append(f" {percentage(progress):>6}%", style=color)
    return text


def section_row(section: Section, total: Fraction) -> Table:
    earned, available = score(section.tasks)
    if available:
        summary = (
            f"{number(earned)}/{number(available)}  "
            f"{percentage(ratio(available, total))}% of total"
        )
    else:
        summary = (
            f"{sum(task.complete for task in section.tasks)}/{len(section.tasks)} done"
        )
    table = Table.grid(padding=(0, 1), expand=True)
    table.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
    table.add_column(justify="right", no_wrap=True, overflow="ellipsis")
    table.add_row(Text(section.title, style="bold magenta"), Text(summary, style="dim"))
    return table


def schedule_status(intervals: tuple[Interval, ...], now: datetime) -> str:
    if not intervals:
        return "No schedule"
    if now < min(interval.start for interval in intervals):
        return "Not started"
    if now >= max(interval.end for interval in intervals):
        return "Schedule ended"
    if any(interval.start <= now < interval.end for interval in intervals):
        return "In progress"
    return "Between intervals"


MAX_MESSAGES = 5


class Change(NamedTuple):
    previous: Day
    previous_persisted: bool
    description: str
    task_id: str | None


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
        "Tasks",
        (
            ("j ↓", "next task"),
            ("k ↑", "previous task"),
            ("g", "first task"),
            ("G", "last task"),
            ("⏎", "set earned points; toggle an unscored task"),
            ("space", "toggle done"),
            ("+ -", "earned points up or down by one"),
            ("a", "add a task to the selected section"),
            ("e", "edit task"),
            ("dd del", "delete task"),
            ("J K", "move task down or up within its section"),
            ("u", "undo"),
        ),
    ),
    ("Sections", (("r", "rename section"),)),
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
        "Panes",
        (
            ("h ←", "task pane"),
            ("l →", "analytics pane; j k scroll it"),
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


class AnalyticsPane(VimScroll):
    pass


class FormSelect(Select):
    # the expanded menu handles Enter before this binding is considered
    BINDINGS = [Binding("enter", "screen.save", "Save", show=False)]


class DialogButtons(Horizontal):
    BINDINGS = [
        Binding("left,up,h,k", "move(-1)", "Previous button", show=False),
        Binding("right,down,j,l", "move(1)", "Next button", show=False),
    ]

    def action_move(self, offset: int) -> None:
        buttons = [button for button in self.query(Button) if not button.disabled]
        focused = self.screen.focused
        if focused in buttons:
            buttons[(buttons.index(focused) + offset) % len(buttons)].focus()


class DayPicker(ModalScreen):
    BINDINGS = [("escape", "dismiss", "Cancel")]

    def __init__(self, list_date: date):
        super().__init__()
        self.initial_date = list_date

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog", id="day-dialog"):
            yield Label("Open a day's list", classes="dialog-title")
            yield Label("Date (YYYY-MM-DD)", classes="field-label")
            yield Input(
                self.initial_date.isoformat(), id="list-date", select_on_focus=True
            )
            yield Static(
                "A new date starts an empty list. Undo resets when you switch days.",
                classes="muted",
            )
            yield Static("", id="error", markup=False)
            with DialogButtons():
                yield Button("Cancel (Esc)", id="cancel", action="screen.dismiss")
                yield Button(
                    "Open (Enter)",
                    id="open",
                    variant="primary",
                    action="screen.open",
                )

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    @on(Input.Submitted)
    def action_open(self) -> None:
        value = self.query_one("#list-date", Input).value.strip()
        try:
            list_date = date.fromisoformat(value)
            if value != list_date.isoformat():
                raise ValueError
        except ValueError:
            self.query_one("#error", Static).update("Use a date in YYYY-MM-DD format.")
            return
        try:
            self.app.switch_day(list_date)
        except (ValueError, sqlite3.Error, OSError) as error:
            self.query_one("#error", Static).update(str(error))
            return
        self.dismiss()


class PointsEditor(ModalScreen):
    BINDINGS = [("escape", "dismiss", "Cancel")]

    def __init__(self, task: Task, section_id: str):
        super().__init__()
        self.edited_task = task
        self.section_id = section_id

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog", id="points-dialog"):
            yield Label("Earned points", classes="dialog-title")
            yield Static(self.edited_task.title, markup=False, classes="muted")
            yield Label(
                f"Available: {number(self.edited_task.available)}",
                classes="field-label",
            )
            yield Input(
                number(self.edited_task.earned), id="earned", select_on_focus=True
            )
            yield Static("", id="error", markup=False)
            with DialogButtons():
                yield Button("Cancel (Esc)", id="cancel", action="screen.dismiss")
                yield Button(
                    "Save (Enter)",
                    id="save",
                    variant="primary",
                    action="screen.save",
                )

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    @on(Input.Submitted)
    def action_save(self) -> None:
        try:
            task = replace(
                self.edited_task, earned=points(self.query_one("#earned", Input).value)
            )
            self.app.save_change(
                self.app.day.put_task(task, self.section_id),
                progress_change(self.edited_task, task),
                task_id=task.id,
            )
        except (ValueError, sqlite3.Error, OSError) as error:
            self.query_one("#error", Static).update(str(error))
            return
        self.dismiss()


class TaskEditor(ModalScreen):
    BINDINGS = [("escape", "dismiss", "Cancel")]

    def __init__(self, day: Day, task: Task | None, section_id: str):
        super().__init__()
        self.day = day
        self.edited_task = task
        self.section_id = section_id

    def compose(self) -> ComposeResult:
        task = self.edited_task
        with VerticalScroll(classes="dialog", id="task-dialog"):
            yield Label("Edit task" if task else "Add task", classes="dialog-title")
            yield Label("Title", classes="field-label")
            yield Input(task.title if task else "", id="title")
            with Horizontal(classes="fields"):
                with Vertical():
                    yield Label("Earned points", classes="field-label")
                    yield Input(number(task.earned) if task else "0", id="earned")
                with Vertical():
                    yield Label("Available points", classes="field-label")
                    yield Input(
                        number(task.available)
                        if task and task.available is not None
                        else "",
                        placeholder="Blank = unscored",
                        id="available",
                    )
            yield Label("Estimated minutes (optional)", classes="field-label")
            yield Input(
                str(task.minutes) if task and task.minutes is not None else "",
                id="minutes",
            )
            yield Label("Section (Space/Down to choose)", classes="field-label")
            yield FormSelect(
                [(Text(s.title), s.id) for s in self.day.sections],
                value=self.section_id,
                allow_blank=False,
                id="section",
            )
            yield Label("Or create a section", classes="field-label")
            yield Input(placeholder="New section name (optional)", id="new-section")
            yield Static("", id="error", markup=False)
            with DialogButtons():
                yield Button("Cancel (Esc)", id="cancel", action="screen.dismiss")
                yield Button(
                    "Save (Enter)",
                    id="save",
                    variant="primary",
                    action="screen.save",
                )

    def on_mount(self) -> None:
        self.query_one("#title", Input).focus()

    @on(Input.Submitted)
    def action_save(self) -> None:
        try:
            available_text = self.query_one("#available", Input).value.strip()
            available = points(available_text) if available_text else None
            task = Task(
                title=self.query_one("#title", Input).value.strip(),
                available=available,
                earned=points(self.query_one("#earned", Input).value)
                if available is not None
                else Fraction(0),
                minutes=parse_minutes(self.query_one("#minutes", Input).value),
                checked=self.edited_task.complete
                if self.edited_task and available is None
                else False,
                **({"id": self.edited_task.id} if self.edited_task else {}),
            )
            day = self.app.day
            section_id = self.query_one("#section", Select).value
            new_title = self.query_one("#new-section", Input).value.strip()
            if new_title:
                section = Section(new_title)
                day = replace(day, sections=day.sections + (section,))
                section_id = section.id
            day = day.put_task(task, section_id)
            change = f"{'edited' if self.edited_task else 'added'} {task.title}"
            self.app.save_change(day, change, task_id=task.id)
        except (ValueError, sqlite3.Error, OSError) as error:
            self.query_one("#error", Static).update(str(error))
            return
        self.dismiss()


class SectionEditor(ModalScreen):
    BINDINGS = [("escape", "dismiss", "Cancel")]

    def __init__(self, day: Day, section_id: str):
        super().__init__()
        self.edited_day = day
        self.section_id = section_id

    def compose(self) -> ComposeResult:
        section = next(s for s in self.edited_day.sections if s.id == self.section_id)
        with VerticalScroll(classes="dialog", id="section-dialog"):
            yield Label("Rename section", classes="dialog-title")
            yield Label("Section (Space/Down to choose)", classes="field-label")
            yield FormSelect(
                [
                    (Text(f"{index}. {s.title}"), s.id)
                    for index, s in enumerate(self.edited_day.sections, 1)
                ],
                value=self.section_id,
                allow_blank=False,
                id="section",
            )
            yield Label("Name", classes="field-label")
            yield Input(section.title, id="section-name", select_on_focus=True)
            yield Static("", id="error", markup=False)
            with DialogButtons():
                yield Button("Cancel (Esc)", id="cancel", action="screen.dismiss")
                yield Button(
                    "Save (Enter)",
                    id="save",
                    variant="primary",
                    action="screen.save",
                )

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    @on(Select.Changed, "#section")
    def section_changed(self, event: Select.Changed) -> None:
        if event.value is Select.NULL:
            return
        section = next(s for s in self.edited_day.sections if s.id == event.value)
        self.query_one(Input).value = section.title

    @on(Input.Submitted)
    def action_save(self) -> None:
        try:
            section_id = self.query_one("#section", Select).value
            day = self.app.day.rename_section(
                section_id, self.query_one("#section-name", Input).value
            )
            old, new = (
                next(s.title for s in d.sections if s.id == section_id)
                for d in (self.app.day, day)
            )
            self.app.save_change(day, f"renamed section {old} to {new}")
        except (ValueError, sqlite3.Error, OSError) as error:
            self.query_one("#error", Static).update(str(error))
            return
        self.dismiss()


class HelpScreen(ModalScreen):
    BINDINGS = [
        Binding("escape,question_mark,q", "dismiss", "Close"),
    ]

    def compose(self) -> ComposeResult:
        table = Table.grid(padding=(0, 2))
        table.add_column(no_wrap=True)
        table.add_column()
        for index, (group, rows) in enumerate(HELP):
            if index:
                table.add_row()
            table.add_row(Text(group, style="bold magenta"))
            for keys, description in rows:
                table.add_row(Text(keys, style="bold blue"), description)
        with VimScroll(classes="dialog", id="help-dialog"):
            yield Static(table)

    def on_mount(self) -> None:
        self.query_one(VimScroll).focus()


class IntervalRow(Horizontal):
    def __init__(self, list_date: date, interval: Interval | None = None):
        super().__init__(classes="interval-row")
        self.list_date = list_date
        self.initial_interval = interval

    def compose(self) -> ComposeResult:
        interval = self.initial_interval
        with Vertical(classes="interval-clock"):
            yield Label("Start")
            yield Input(
                f"{interval.start:%H:%M}" if interval else "",
                placeholder="07:30",
                classes="interval-start",
            )
        with Vertical(classes="interval-clock"):
            yield Label("End")
            yield Input(
                f"{interval.end:%H:%M}" if interval else "",
                placeholder="11:00",
                classes="interval-end",
            )
        yield Checkbox(
            "Starts next day",
            value=interval is not None and interval.start.date() > self.list_date,
        )
        yield Button("Remove", classes="remove-interval")

    def read_interval(self) -> Interval:
        return parse_interval(
            self.list_date,
            self.query_one(".interval-start", Input).value,
            self.query_one(".interval-end", Input).value,
            next_day=self.query_one(Checkbox).value,
        )

    @on(Button.Pressed, ".remove-interval")
    async def remove_interval(self, event: Button.Pressed) -> None:
        event.stop()
        await self.remove()


class ScheduleEditor(ModalScreen):
    BINDINGS = [
        ("escape", "dismiss", "Cancel"),
        ("ctrl+n", "add_interval", "Add interval"),
    ]

    def __init__(self, day: Day):
        super().__init__()
        self.edited_day = day

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog", id="schedule-dialog"):
            yield Label("Schedule", classes="dialog-title")
            yield Static(
                "Use 24-hour times. An end before its start means overnight. "
                "Tick Starts next day for an interval beginning after midnight. "
                "Remove all intervals to clear the schedule.",
                classes="muted",
            )
            with Vertical(id="interval-rows"):
                for interval in self.edited_day.intervals:
                    yield IntervalRow(self.edited_day.date, interval)
            yield Button("Add interval", id="add-interval")
            yield Static("", id="error", markup=False)
            with DialogButtons():
                yield Button("Cancel (Esc)", id="cancel", action="screen.dismiss")
                yield Button(
                    "Save (Enter)",
                    id="save",
                    variant="primary",
                    action="screen.save",
                )

    def on_mount(self) -> None:
        first_start = next(iter(self.query(".interval-start")), None)
        if first_start is None:
            self.query_one("#add-interval", Button).focus()
        else:
            first_start.focus()

    def on_resize(self) -> None:
        self.set_class(self.size.width < 72, "narrow")

    @on(Button.Pressed, "#add-interval")
    async def action_add_interval(self) -> None:
        row = IntervalRow(self.edited_day.date)
        await self.query_one("#interval-rows", Vertical).mount(row)
        row.query_one(".interval-start", Input).focus()

    @on(Input.Submitted)
    def action_save(self) -> None:
        try:
            intervals = tuple(
                sorted(
                    (row.read_interval() for row in self.query(IntervalRow)),
                    key=lambda interval: interval.start,
                )
            )
            day = replace(self.app.day, intervals=intervals)
            self.app.save_change(day, "changed the schedule")
        except (ValueError, sqlite3.Error, OSError) as error:
            self.query_one("#error", Static).update(str(error))
            return
        self.dismiss()


class ReuseDay(ModalScreen):
    BINDINGS = [("escape", "dismiss", "Cancel")]

    def __init__(self, list_date: date, sources: tuple[date, ...]):
        super().__init__()
        self.list_date = list_date
        self.sources = sources

    def compose(self) -> ComposeResult:
        source = next((d for d in self.sources if d < self.list_date), self.sources[0])
        with VerticalScroll(classes="dialog", id="reuse-dialog"):
            yield Label("Reuse a day's list", classes="dialog-title")
            yield Label("Copy from (Space/Down to choose)", classes="field-label")
            yield FormSelect(
                [(f"{d:%a} {d.isoformat()}", d) for d in self.sources],
                value=source,
                allow_blank=False,
                id="source-day",
            )
            notice = Text()
            notice.append(
                f"Replaces the list for {self.list_date.isoformat()}.\n", style="bold"
            )
            notice.append(
                "Copies tasks, sections, points, estimates, and schedule. "
                "Earned points and completion reset.\n\n"
            )
            notice.append("Undo restores this day's previous list.", style="dim")
            yield Static(notice, id="reuse-notice")
            yield Static("", id="error", markup=False)
            with DialogButtons():
                yield Button("Cancel (Esc)", id="cancel", action="screen.dismiss")
                yield Button(
                    "Reuse (Enter)",
                    id="save",
                    variant="primary",
                    action="screen.save",
                )

    def on_mount(self) -> None:
        self.query_one(Select).focus()

    def action_save(self) -> None:
        try:
            source = self.app.store.load(self.query_one(Select).value)
            if source is None:
                raise ValueError("That day's list no longer exists. Choose another.")
            self.app.save_change(
                source.copy_to(self.list_date),
                f"replaced the list with {source.date.isoformat()}'s",
            )
        except (ValueError, sqlite3.Error, OSError) as error:
            self.query_one("#error", Static).update(str(error))
            return
        self.dismiss()


class TodoApp(App):
    CSS_PATH = "app.tcss"
    TITLE = "todo"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("a", "add", "Add"),
        Binding("c", "reuse", "Reuse", show=False),
        Binding("e", "edit", "Edit"),
        Binding("r", "rename_section", "Rename section", show=False),
        Binding("s", "schedule", "Schedule", show=False),
        Binding("space", "complete", "Done"),
        Binding("plus", "step(1)", "Points", key_display="+/-"),
        Binding("minus", "step(-1)", "Points down", show=False),
        # TaskList handles the dd chord before bindings see d
        Binding("delete", "delete", "Delete", key_display="dd"),
        Binding("u", "undo", "Undo"),
        Binding("question_mark", "help", "Keys"),
        Binding("J", "move(1)", "Move down", show=False),
        Binding("K", "move(-1)", "Move up", show=False),
        Binding("h,left", "tasks", "Tasks", show=False),
        Binding("l,right", "details", "Details", show=False),
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
        self._load_day(list_date)
        self.history: list[Change] = []

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

    def compose(self) -> ComposeResult:
        with Horizontal(id="day-bar"):
            yield Button(
                "",
                id="choose-day",
                compact=True,
                tooltip="Choose day (Ctrl+D)",
                action="app.choose_day",
            )
            yield Button(
                Text("[ Prev"),
                id="previous-day",
                compact=True,
                tooltip="Previous day ([)",
                action="app.shift_day(-1)",
            )
            yield Button(
                Text("] Next"),
                id="next-day",
                compact=True,
                tooltip="Next day (])",
                action="app.shift_day(1)",
            )
            yield Button(
                "Today",
                id="today",
                compact=True,
                tooltip="Today (t)",
                action="app.today",
            )
            yield Button(
                "Analytics →", id="toggle-view", compact=True, action="app.toggle_view"
            )
        with Horizontal(id="body"):
            with Vertical(id="task-pane"):
                yield Static(
                    task_row("", "Task", "Points", "Est.", "dim"),
                    id="columns",
                )
                yield TaskList(id="tasks")
                yield Static("a  Add a task\nc  Reuse a day's list", id="empty")
            with AnalyticsPane(id="sidebar"):
                yield Static("", id="score", markup=False)
                yield Static("", id="time", markup=False)
                yield Static("", id="difference", markup=False)
                yield Static("", id="pace", markup=False)
                yield Button(
                    "Schedule",
                    id="edit-schedule",
                    compact=True,
                    action="app.schedule",
                    classes="pane-action",
                )
                yield Static("", id="schedule", markup=False)
                yield Static("", id="estimates", markup=False)
                yield Static("", id="details", markup=False)
        yield Footer(compact=True)

    def on_mount(self) -> None:
        self.theme = "ansi-dark"
        self.redraw()
        self.set_interval(1, self.refresh_clock)
        self.query_one("#tasks", TaskList).focus()

    def on_resize(self, event: events.Resize) -> None:
        self.default_screen.set_class(event.size.width < 72, "compact")
        self.default_screen.set_class(event.size.width < 60, "narrow")

    def on_descendant_focus(self) -> None:
        focused = self.focused
        ancestors = (focused, *focused.ancestors) if focused is not None else ()
        pane = next(
            (w for w in ancestors if isinstance(w, (TaskList, AnalyticsPane))), None
        )
        if pane is not None:
            self.show_analytics(isinstance(pane, AnalyticsPane))

    def show_analytics(self, show: bool) -> None:
        self.default_screen.set_class(show, "analytics")
        self.query_one("#toggle-view", Button).label = (
            "← Tasks" if show else "Analytics →"
        )

    def action_toggle_view(self) -> None:
        if self.default_screen.has_class("analytics"):
            self.action_tasks()
        else:
            self.action_details()

    def action_choose_day(self) -> None:
        self.push_screen(DayPicker(self.day.date))

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
        except (ValueError, sqlite3.Error, OSError) as error:
            self.notify(
                str(error), title="Could not open list", severity="error", timeout=8
            )

    def switch_day(self, list_date: date) -> None:
        if list_date == self.day.date:
            return
        self._load_day(list_date)
        self.history.clear()
        self.query_one("#tasks", TaskList).highlighted = None
        self.redraw()
        self.action_tasks()

    def selected(self) -> tuple[Section, Task] | None:
        option = self.query_one("#tasks", TaskList).highlighted_option
        if option is None:
            return None
        return next(((s, t) for s, t in self.day.tasks() if t.id == option.id), None)

    def redraw(self, task_id: str | None = None) -> None:
        widget = self.query_one("#tasks", TaskList)
        selected = self.selected()
        task_id = task_id or (selected[1].id if selected else None)
        old_index = widget.highlighted or 0
        options = []
        _, total = score(task for _, task in self.day.tasks())
        for section in self.day.sections:
            label = section_row(section, total)
            options.append(Option(label, id=f"section-{section.id}", disabled=True))
            for task in section.tasks:
                if task.complete:
                    state, color = "✓", "green"
                elif task.earned:
                    state, color = "~", "blue"
                else:
                    state, color = "", "default"
                row = task_row(
                    state,
                    task.title,
                    f"{number(task.earned)}/{number(task.available)}"
                    if task.available is not None
                    else "",
                    f"{task.minutes}m" if task.minutes is not None else "",
                    color,
                )
                options.append(Option(row, id=task.id))
        widget.clear_options().add_options(options)
        enabled = [index for index, option in enumerate(options) if not option.disabled]
        if enabled:
            target = next(
                (index for index in enabled if options[index].id == task_id), None
            )
            widget.highlighted = (
                target
                if target is not None
                else min(enabled, key=lambda i: abs(i - old_index))
            )
        else:
            widget.highlighted = None
        self.query_one("#empty", Static).display = not enabled
        self.refresh_clock()
        self.refresh_details()

    def refresh_clock(self) -> None:
        if not self.is_running:
            return
        now = datetime.now()
        day = self.day
        self.query_one(
            "#choose-day", Button
        ).label = f"{day.date:%a} {day.date.isoformat()} ▾"
        self.query_one("#previous-day", Button).disabled = day.date == date.min
        self.query_one("#next-day", Button).disabled = day.date == date.max
        earned, available = score(t for _, t in day.tasks())
        elapsed, total = time_progress(day.intervals, now)
        progress = ratio(earned, available)
        time_ratio = ratio(elapsed, total)
        difference = (
            progress - time_ratio
            if progress is not None and time_ratio is not None
            else None
        )
        self.query_one("#score", Static).update(
            progress_text(
                "Points", f"{number(earned)} / {number(available)}", progress, "green"
            )
        )
        text = progress_text(
            "Time",
            f"{duration(elapsed)} / {duration(total)}",
            time_ratio,
            "cyan",
        )
        text.append("\n" + schedule_status(day.intervals, now), style="default")
        self.query_one("#time", Static).update(text)
        text = Text("Score − time  ")
        text.append(
            percentage(difference, signed=True)
            + (" pp" if difference is not None else ""),
            style="bold magenta",
        )
        self.query_one("#difference", Static).update(text)
        text = Text()
        text.append(
            f"Time / point      {per_point(total, available)}\n", style="default"
        )
        if not total:
            remaining = "n/a"
        elif elapsed == total:
            remaining = "Ended"
        else:
            remaining = per_point(total - elapsed, available - earned)
        text.append(f"Remaining / point {remaining}", style="default")
        text.append("\nminutes:seconds", style="dim")
        self.query_one("#pace", Static).update(text)
        for selector in ("#time", "#difference", "#pace", "#schedule"):
            self.query_one(selector).display = bool(day.intervals)
        text = Text()
        for interval in day.intervals:
            start_offset = (interval.start.date() - day.date).days
            end_offset = (interval.end.date() - day.date).days
            start = f"{interval.start:%H:%M}" + (
                f" (+{start_offset} day)" if start_offset else ""
            )
            end = f"{interval.end:%H:%M}" + (
                f" (+{end_offset} day)" if end_offset else ""
            )
            text.append(
                f"{start} to {end}\n",
                style="default",
            )
        text.rstrip()
        self.query_one("#schedule", Static).update(text)

    def refresh_details(self) -> None:
        selected = self.selected()
        text = Text()
        text.append("Task\n", style="bold magenta")
        if selected:
            section, task = selected
            text.append(task.title, style="bold default")
            if task.minutes is not None and task.available is not None:
                text.append(
                    f"\nTime / point      {per_point(task.minutes * 60, task.available)}",
                    style="blue",
                )
                text.append(" mm:ss", style="dim")
        self.query_one("#details", Static).update(text)
        self.query_one("#details", Static).display = selected is not None
        tasks = [task for _, task in self.day.tasks()]
        estimates = [task.minutes for task in tasks if task.minutes is not None]
        text = Text()
        text.append("Estimates (h:mm)\n", style="bold magenta")
        hours, minutes = divmod(sum(estimates), 60)
        text.append(
            f"{hours}:{minutes:02d} across {len(estimates)} tasks\n", style="default"
        )
        missing = len(tasks) - len(estimates)
        if missing:
            text.append(f"{missing} without an estimate", style="dim")
        self.query_one("#estimates", Static).update(text)
        self.query_one("#estimates", Static).display = bool(estimates)

    @on(OptionList.OptionHighlighted, "#tasks")
    def highlighted(self) -> None:
        self.refresh_details()

    @on(OptionList.OptionSelected, "#tasks")
    def edit_points(self) -> None:
        selected = self.selected()
        if selected:
            section, task = selected
            if task.available is None:
                self.action_complete()
            else:
                self.push_screen(PointsEditor(task, section.id))

    def save_change(
        self, day: Day, description: str, task_id: str | None = None
    ) -> None:
        if day == self.day:
            return
        self.store.save(day, expected=self.day if self.persisted else None)
        self.history.append(Change(self.day, self.persisted, description, task_id))
        self.day = day
        self.persisted = True
        self.redraw(task_id)

    def save_or_notify(
        self, day: Day, description: str, task_id: str | None = None
    ) -> None:
        try:
            self.save_change(day, description, task_id)
        except (ValueError, sqlite3.Error, OSError) as error:
            self.notify(str(error), title="Could not save", severity="error", timeout=8)

    def action_add(self) -> None:
        selected = self.selected()
        self.push_screen(
            TaskEditor(
                self.day, None, selected[0].id if selected else self.day.sections[0].id
            )
        )

    def action_reuse(self) -> None:
        try:
            sources = tuple(d for d in self.store.list_dates() if d != self.day.date)
        except (ValueError, sqlite3.Error, OSError) as error:
            self.notify(str(error), title="Could not open lists", severity="error")
            return
        if not sources:
            self.notify("No other saved lists yet.")
            return
        self.push_screen(ReuseDay(self.day.date, sources))

    def action_edit(self) -> None:
        if selected := self.selected():
            self.push_screen(TaskEditor(self.day, selected[1], selected[0].id))

    def action_rename_section(self) -> None:
        selected = self.selected()
        section = selected[0] if selected else self.day.sections[0]
        self.push_screen(SectionEditor(self.day, section.id))

    def action_schedule(self) -> None:
        self.push_screen(ScheduleEditor(self.day))

    def action_complete(self) -> None:
        if selected := self.selected():
            self.save_progress(*selected, selected[1].toggle())

    def action_step(self, delta: int) -> None:
        if selected := self.selected():
            self.save_progress(*selected, selected[1].step(delta))

    def save_progress(self, section: Section, before: Task, after: Task) -> None:
        self.save_or_notify(
            self.day.put_task(after, section.id),
            progress_change(before, after),
            task_id=after.id,
        )

    def action_delete(self) -> None:
        if selected := self.selected():
            task = selected[1]
            self.save_or_notify(
                self.day.remove_task(task.id), f"deleted {task.title}", task_id=task.id
            )

    def action_move(self, offset: int) -> None:
        if selected := self.selected():
            task = selected[1]
            self.save_or_notify(
                self.day.move_task(task.id, offset),
                f"moved {task.title} {'down' if offset > 0 else 'up'}",
                task_id=task.id,
            )

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_undo(self) -> None:
        if not self.history:
            self.notify("Nothing to undo.", timeout=2)
            return
        change = self.history[-1]
        try:
            if change.previous_persisted:
                self.store.save(change.previous, expected=self.day)
            else:
                self.store.delete(self.day)
        except (ValueError, sqlite3.Error, OSError) as error:
            self.notify(str(error), title="Could not undo", severity="error", timeout=8)
            return
        self.history.pop()
        self.day = change.previous
        self.persisted = change.previous_persisted
        self.redraw(change.task_id)
        self.notify(f"Undid: {change.description}", timeout=4, markup=False)

    def action_tasks(self) -> None:
        self.show_analytics(False)
        self.query_one("#tasks", TaskList).focus()

    def action_details(self) -> None:
        self.show_analytics(True)
        self.query_one("#sidebar", VerticalScroll).focus()


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
    except (ValueError, sqlite3.Error, OSError) as error:
        parser.exit(2, f"todo: {error}\n")
    app.run()
