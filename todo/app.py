import argparse
import sqlite3
from dataclasses import replace
from datetime import date, datetime, timedelta
from fractions import Fraction
from pathlib import Path

from platformdirs import user_data_path
from rich.table import Table
from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
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
        *(Text(value, style=style) for value in (marker, title, points, estimate))
    )
    return table


def progress_text(
    label: str, value: str, progress: Fraction | None, color: str
) -> Text:
    text = Text(label + "\n", style="bold cyan")
    text.append(value, style=f"bold {color}")
    text.append(
        "  " + percentage(progress) + ("%" if progress is not None else ""),
        style=color,
    )
    if progress is not None:
        filled = int(progress * 20)
        text.append("\n" + "━" * filled, style=color)
        text.append("─" * (20 - filled), style="dim")
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
    table.add_row(Text(section.title, style="bold cyan"), Text(summary, style="cyan"))
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


class TaskList(OptionList):
    BINDINGS = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("g", "first", "First", show=False),
        Binding("G", "last", "Last", show=False),
    ]


class DayPicker(ModalScreen):
    BINDINGS = [("escape", "dismiss", "Cancel"), ("ctrl+enter", "open", "Open")]

    def __init__(self, list_date: date):
        super().__init__()
        self.initial_date = list_date

    def compose(self) -> ComposeResult:
        with Container(classes="dialog", id="day-dialog"):
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
            with Horizontal(classes="dialog-buttons"):
                yield Button("Cancel (Esc)", id="cancel", action="screen.dismiss")
                yield Button(
                    "Open (Ctrl+Enter)",
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
    BINDINGS = [("escape", "dismiss", "Cancel"), ("ctrl+enter", "save", "Save")]

    def __init__(self, task: Task, section_id: str):
        super().__init__()
        self.edited_task = task
        self.section_id = section_id

    def compose(self) -> ComposeResult:
        with Container(classes="dialog", id="points-dialog"):
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
            with Horizontal(classes="dialog-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Save", id="save", variant="primary")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    @on(Input.Submitted)
    @on(Button.Pressed, "#save")
    def action_save(self) -> None:
        try:
            task = replace(
                self.edited_task, earned=points(self.query_one("#earned", Input).value)
            )
            self.app.save_change(
                self.app.day.put_task(task, self.section_id), "Points saved.", task.id
            )
        except (ValueError, sqlite3.Error, OSError) as error:
            self.query_one("#error", Static).update(str(error))
            return
        self.dismiss()

    @on(Button.Pressed, "#cancel")
    def cancel(self) -> None:
        self.dismiss()


class TaskEditor(ModalScreen):
    BINDINGS = [("escape", "dismiss", "Cancel"), ("ctrl+enter", "save", "Save")]

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
                    yield Label("Available points", classes="field-label")
                    yield Input(
                        number(task.available)
                        if task and task.available is not None
                        else "",
                        placeholder="Blank = unscored",
                        id="available",
                    )
                with Vertical():
                    yield Label("Earned points", classes="field-label")
                    yield Input(number(task.earned) if task else "0", id="earned")
            yield Label("Estimated minutes (optional)", classes="field-label")
            yield Input(
                str(task.minutes) if task and task.minutes is not None else "",
                id="minutes",
            )
            yield Label("Section", classes="field-label")
            yield Select(
                [(Text(s.title), s.id) for s in self.day.sections],
                value=self.section_id,
                allow_blank=False,
                id="section",
            )
            yield Label("Or create a section", classes="field-label")
            yield Input(placeholder="New section name (optional)", id="new-section")
            yield Static("", id="error", markup=False)
            with Horizontal(classes="dialog-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Save", id="save", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#title", Input).focus()

    @on(Button.Pressed, "#save")
    def action_save(self) -> None:
        try:
            available_text = self.query_one("#available", Input).value.strip()
            available = points(available_text) if available_text else None
            minute_text = self.query_one("#minutes", Input).value.strip()
            if minute_text and not minute_text.isdecimal():
                raise ValueError("Estimated minutes must be a positive whole number.")
            task = Task(
                title=self.query_one("#title", Input).value.strip(),
                available=available,
                earned=points(self.query_one("#earned", Input).value)
                if available is not None
                else Fraction(0),
                minutes=int(minute_text) if minute_text else None,
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
            self.app.save_change(day, "Task saved.", task.id)
        except (ValueError, sqlite3.Error, OSError) as error:
            self.query_one("#error", Static).update(str(error))
            return
        self.dismiss()

    @on(Button.Pressed, "#cancel")
    def cancel(self) -> None:
        self.dismiss()


class DeleteTask(ModalScreen):
    BINDINGS = [("escape", "dismiss", "Cancel")]

    def __init__(self, task: Task):
        super().__init__()
        self.task_to_delete = task

    def compose(self) -> ComposeResult:
        with Container(classes="dialog", id="delete-dialog"):
            yield Label("Delete task?", classes="dialog-title")
            yield Static(self.task_to_delete.title, markup=False)
            yield Static(
                "You can undo this until you switch days or quit.", classes="muted"
            )
            yield Static("", id="error", markup=False)
            with Horizontal(classes="dialog-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Delete", id="delete", variant="error")

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "delete":
            try:
                self.app.save_change(
                    self.app.day.remove_task(self.task_to_delete.id), "Task deleted."
                )
            except (ValueError, sqlite3.Error, OSError) as error:
                self.query_one("#error", Static).update(str(error))
                return
        self.dismiss()


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
        ("ctrl+enter", "save", "Save"),
        ("ctrl+n", "add_interval", "Add interval"),
    ]

    def __init__(self, day: Day):
        super().__init__()
        self.edited_day = day

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog", id="schedule-dialog"):
            yield Label("Edit schedule", classes="dialog-title")
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
            with Horizontal(classes="dialog-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Save", id="save", variant="primary")

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

    @on(Button.Pressed, "#save")
    def action_save(self) -> None:
        try:
            intervals = tuple(
                sorted(
                    (row.read_interval() for row in self.query(IntervalRow)),
                    key=lambda interval: interval.start,
                )
            )
            day = replace(self.app.day, intervals=intervals)
            self.app.save_change(day, "Schedule saved.")
        except (ValueError, sqlite3.Error, OSError) as error:
            self.query_one("#error", Static).update(str(error))
            return
        self.dismiss()

    @on(Button.Pressed, "#cancel")
    def cancel(self) -> None:
        self.dismiss()


class TodoApp(App):
    CSS_PATH = "app.tcss"
    TITLE = "todo"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("a", "add", "Add"),
        Binding("e", "edit", "Edit"),
        Binding("s", "schedule", "Schedule"),
        Binding("space", "complete", "Done"),
        Binding("d,delete", "delete", "Delete"),
        Binding("u", "undo", "Undo"),
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
        self.history: list[tuple[Day, bool]] = []

    def _load_day(self, list_date: date) -> None:
        saved = self.store.load(list_date)
        self.day = saved if saved is not None else empty_day(list_date)
        self.persisted = saved is not None

    def compose(self) -> ComposeResult:
        with Horizontal(id="day-bar"):
            yield Button(
                "‹",
                id="previous-day",
                compact=True,
                tooltip="Previous day ([)",
                action="app.shift_day(-1)",
            )
            yield Button(
                "",
                id="choose-day",
                compact=True,
                tooltip="Choose day (Ctrl+D)",
                action="app.choose_day",
            )
            yield Button(
                "›",
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
                    task_row("", "Task", "Points", "Est.", "bold cyan"),
                    id="columns",
                )
                yield TaskList(id="tasks")
                yield Static("Add your first task with a or Add below.", id="empty")
            with VerticalScroll(id="sidebar"):
                yield Static("", id="score", markup=False)
                yield Static("", id="time", markup=False)
                yield Static("", id="difference", markup=False)
                yield Static("", id="pace", markup=False)
                yield Static("", id="details", markup=False)
                yield Button(
                    "Edit schedule",
                    id="edit-schedule",
                    compact=True,
                    action="app.schedule",
                )
                yield Static("", id="schedule", markup=False)
                yield Static("", id="estimates", markup=False)
        yield Static(
            "The list needs 60 columns and 12 rows.\nResize the terminal, or q to quit.",
            id="too-small",
        )
        yield Static("", id="status", markup=False)
        yield Footer(compact=True)

    def on_mount(self) -> None:
        self.theme = "ansi-dark"
        self.redraw()
        self.set_interval(1, self.refresh_clock)
        self.query_one("#tasks", TaskList).focus()
        self.query_one("#status", Static).update(
            "Enter points · Space complete · J/K move · h/l panes"
        )

    def on_resize(self, event: events.Resize) -> None:
        self.default_screen.set_class(event.size.width < 80, "compact")
        self.default_screen.set_class(
            event.size.width < 60 or event.size.height < 12, "too-small"
        )

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        if isinstance(event.widget, TaskList):
            self.show_analytics(False)
        elif event.widget.id == "sidebar" or any(
            ancestor.id == "sidebar" for ancestor in event.widget.ancestors
        ):
            self.show_analytics(True)

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
        self.query_one("#status", Static).update(
            "List opened. Undo starts fresh for this day."
        )

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
                    state, color = "~", "yellow"
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
        now = datetime.now()
        day = self.day
        self.query_one(
            "#choose-day", Button
        ).label = f"{day.date:%a} {day.date.isoformat()} ▾"
        self.query_one("#previous-day", Button).disabled = day.date == date.min
        self.query_one("#next-day", Button).disabled = day.date == date.max
        self.query_one("#today", Button).disabled = day.date == now.date()
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
            "Scheduled time",
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
            style="bold yellow",
        )
        self.query_one("#difference", Static).update(text)
        text = Text("Pace\n", style="bold cyan")
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
        text = Text()
        if not day.intervals:
            text.append("No intervals\n", style="dim")
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
        text = Text("Selected task\n", style="bold magenta")
        if selected:
            section, task = selected
            text.append(task.title, style="bold default")
            if task.available is not None:
                text.append(
                    f"\n{number(task.earned)} / {number(task.available)} points",
                    style="default",
                )
            if task.minutes is not None:
                text.append(f"\nEstimate          {task.minutes}m", style="default")
                if task.available is not None:
                    text.append(
                        f"\nTime / point      {per_point(task.minutes * 60, task.available)}",
                        style="default",
                    )
                    text.append("\nFull estimate, minutes:seconds", style="dim")
            else:
                text.append("\nEstimate not set", style="dim")
        else:
            text.append("Add a task to begin.\n", style="dim")
        self.query_one("#details", Static).update(text)
        tasks = [task for _, task in self.day.tasks()]
        estimates = [task.minutes for task in tasks if task.minutes is not None]
        text = Text("Estimates\n", style="bold cyan")
        text.append(
            f"{sum(estimates)}m across {len(estimates)} tasks\n", style="default"
        )
        missing = len(tasks) - len(estimates)
        if missing:
            text.append(f"{missing} without an estimate", style="dim")
        self.query_one("#estimates", Static).update(text)

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

    def save_change(self, day: Day, message: str, task_id: str | None = None) -> None:
        if day == self.day:
            return
        self.store.save(day, expected=self.day if self.persisted else None)
        self.history.append((self.day, self.persisted))
        self.day = day
        self.persisted = True
        self.redraw(task_id)
        self.query_one("#status", Static).update(message)

    def action_add(self) -> None:
        selected = self.selected()
        self.push_screen(
            TaskEditor(
                self.day, None, selected[0].id if selected else self.day.sections[0].id
            )
        )

    def action_edit(self) -> None:
        if selected := self.selected():
            self.push_screen(TaskEditor(self.day, selected[1], selected[0].id))

    def action_schedule(self) -> None:
        self.push_screen(ScheduleEditor(self.day))

    def action_complete(self) -> None:
        if selected := self.selected():
            section, task = selected
            try:
                self.save_change(
                    self.day.put_task(task.toggle(), section.id),
                    "Completion saved.",
                    task.id,
                )
            except (ValueError, sqlite3.Error, OSError) as error:
                self.notify(
                    str(error), title="Could not save", severity="error", timeout=8
                )

    def action_delete(self) -> None:
        if selected := self.selected():
            self.push_screen(DeleteTask(selected[1]))

    def action_move(self, offset: int) -> None:
        if selected := self.selected():
            try:
                self.save_change(
                    self.day.move_task(selected[1].id, offset),
                    "Task moved.",
                    selected[1].id,
                )
            except (ValueError, sqlite3.Error, OSError) as error:
                self.notify(
                    str(error), title="Could not save", severity="error", timeout=8
                )

    def action_undo(self) -> None:
        if not self.history:
            self.query_one("#status", Static).update("Nothing to undo.")
            return
        previous, was_persisted = self.history[-1]
        try:
            if was_persisted:
                self.store.save(previous, expected=self.day)
            else:
                self.store.delete(self.day)
        except (ValueError, sqlite3.Error, OSError) as error:
            self.notify(str(error), title="Could not undo", severity="error", timeout=8)
            return
        self.history.pop()
        self.day = previous
        self.persisted = was_persisted
        self.redraw()
        self.query_one("#status", Static).update("Last change undone.")

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
