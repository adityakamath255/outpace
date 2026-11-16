# outpace

outpace is a task list for a day or a longer stretch of time. Tasks carry points and durations, and the list has a schedule. As you earn points, the app converts them into time on the schedule and shows how far ahead of or behind the clock you are.

It is meant for people who can't judge by feel whether their time is going well. The app makes that judgment from the list and the clock, and also shows when a plan is too big for its schedule.

The complete version is a terminal app written in Python with [Textual](https://textual.textualize.io/). An Android version in Kotlin has its model ported and tested, but no interface yet.

## Running locally

Requires Python 3.14 and [uv](https://docs.astral.sh/uv/).

```bash
cd tui
uv run todo
```

Options:

```text
--database PATH      SQLite file, default: todo/todo.sqlite3 in the user-data directory
--date YYYY-MM-DD    list to open, default: today
```

On Linux the default database is `~/.local/share/todo/todo.sqlite3`. The app holds an exclusive lock on the file while it runs.

## Using it

A list is either a day or a span. A day has a schedule of one or more intervals of clock time, which may run past midnight. A span runs from one date and time to another, can leave out whole days off, and may overlap other spans. `tab` switches between days and spans, `[` and `]` move between lists, and `?` shows every key.

A task has a title and, optionally, points and a duration. Points are nonnegative decimals. Durations are minutes, written `90` or `1h30m`. Tasks can be grouped into sections.

Points are yours to assign. One scheme is a constant times importance, urgency, difficulty, and duration. With it, sorting by `dur/pt` puts the tasks with the highest importance × urgency × difficulty first. A task's duration is the time you choose to give it in that list, which need not match how long the task would take to finish.

`space` marks a task done and `+` and `-` change its earned points by one. `enter` sets earned points to any value, decimals included. Completing a task leaves it where it is, and changing a done task's points keeps it done.

A task without points is unscored. It has no done state, but its duration still counts against the schedule. Unscored tasks suit things that go on all day, things to avoid doing, and things that can only happen after the rest of the list, such as going to bed on time.

Tasks have no start times. The schedule is one pool of time for the whole list, and the tasks can be done in any order. The list stays editable during and after its schedule. Tasks can be added, removed, and reweighted at any time, and the figures are recomputed from the list as it stands.

## Figures

The summary above the tasks shows:

- `elapsed`: scheduled time gone by, out of the whole schedule.
- `points`: points earned, out of all points.
- `work`: task durations, each counted in proportion to the share of its points earned. A task with 2 of 4 points earned counts half its duration. The app has no timer.
- `progress`: points earned, expressed as scheduled time. Earning half the points on an eight-hour schedule is four hours of progress.
- `pace`: progress minus elapsed. A pace of `-20m` means you should have reached your current points 20 minutes ago.
- `sched/pt`: scheduled time per point (`span/pt` for spans).
- `dur/pt`: duration per point, over tasks that have both.
- `free`: the share of the schedule that task durations leave unfilled. It goes negative when the durations add up to more than the schedule.

Each task row shows its score, duration, and `dur/pt`. Section rules show the section's share of the list's points, and sections with points or durations end with a subtotal row.

`v` cycles between the tasks, a pace graph, and a points graph. Both graphs run along the clock time of the whole schedule. The points graph draws earned points and total points up to now, so you can tell whether a falling pace comes from added tasks or from slow progress. The graphs always use the list's current schedule.

`c` replaces the open list with another list's tasks and resets their earned points, for example to start a day from the one before. Copying a day also copies its schedule. `u` and `ctrl+r` undo and redo. Each list keeps its own history until the app quits.

## Implementation

The Python app is in `tui/todo`:

- [`model.py`](tui/todo/model.py) defines days, spans, sections, and tasks, parses input, and computes every summary figure as one `Figures` value. Points are `Fraction`s, so totals and shares are exact.
- [`db.py`](tui/todo/db.py) stores each list as JSON in SQLite, with one table for days and one for spans. The schema version is kept in `user_version`, and older files are migrated when opened.
- [`app.py`](tui/todo/app.py) is the Textual interface, which formats `Figures` and draws the bars and graphs.

Each list keeps a history of tallies: a time, the points earned, and the points available. A tally is added whenever a change leaves the points different from the last one, and undo and redo carry tallies with the change they belong to. The graphs read from this history. Tally times are stored with their UTC offset. Schedules and span bounds are local wall-clock times.

[`spec/cases.json`](spec/cases.json) holds inputs and expected outputs for parsing, formatting, figures, task changes, tallies, and storage, generated from the Python model in the Asia/Kolkata zone. Other ports test against it. Regenerate it from `tui/` with:

```bash
.venv/bin/python -m tests.shared_cases
```

`tests/test_shared_cases.py` fails when the file is out of date.

The Android project in `android/` is a Gradle build. `model/` is a plain Kotlin port of the model that passes every shared case, and `app/` is a placeholder Compose activity. Phone storage will use the same SQLite schema.

## Tests

From `tui/`:

```bash
uv run pytest
```

The Kotlin model's tests need a JDK and the Android SDK. From `android/`:

```bash
./gradlew :model:test
```

## Limitations

- Schedules are local wall-clock times. After a move to another time zone, the graphs place earlier tallies in the new zone.
- The summary shows a list as it is now, while the graphs show its history, so on a past day the two can disagree.
- The graphs draw spans but are not tuned for long ones.
- The days view fits an 80×24 terminal with about a dozen task rows. The spans view can be cut off at 80 columns.
- Undo history and deleted spans are lost when the app quits.
- The Android app has no interface or storage yet. The two apps will not sync until a later version.

## License

[MIT](LICENSE)
