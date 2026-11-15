package io.github.adityakamath255.todo.model

import java.time.Duration
import java.time.LocalDate
import java.time.LocalDateTime
import java.time.OffsetDateTime
import java.time.ZoneId
import java.util.UUID

// Titles are stored trimmed, days off in order without repeats, and a day's intervals
// in start order. Constructors check those forms instead of producing them, so code
// that builds values from input normalises it first.

private const val UNSCORED_ERROR = "Unscored tasks have no done state."

fun identifier(): String = UUID.randomUUID().toString().replace("-", "")

private fun checkTrimmed(title: String) =
    check(title == title.trim()) { "titles are stored trimmed" }

data class Task(
    val title: String,
    // null means unscored: a plain list item with no done state
    val available: Rational?,
    val earned: Rational = Rational.ZERO,
    val minutes: Int? = null,
    val id: String = identifier(),
) {
    init {
        checkTrimmed(title)
        require(title.isNotEmpty()) { "Give the task a title." }
        if (available != null) {
            require(available.signum > 0 && earned.signum >= 0 && earned <= available) {
                "Available points must be positive; earned points must be between zero " +
                    "and available points."
            }
        } else {
            require(earned.signum == 0) { "Unscored tasks cannot have earned points." }
        }
        if (minutes != null) {
            require(minutes > 0) { "A duration must be positive." }
            // a task has to fit in a day's schedule
            require(minutes <= DAY_MINUTES) { "A duration cannot exceed 24 hours." }
        }
    }

    val complete: Boolean get() = available != null && earned == available

    fun toggle(): Task {
        require(available != null) { UNSCORED_ERROR }
        return copy(earned = if (complete) Rational.ZERO else available)
    }

    /** Changes a task's plan: a done task stays done, and partial progress is capped
     * at the new available points. */
    fun edit(title: String, available: Rational?, minutes: Int?): Task {
        val earned = when {
            available == null -> Rational.ZERO
            complete -> available
            else -> minOf(earned, available)
        }
        return copy(title = title.trim(), available = available, earned = earned, minutes = minutes)
    }

    fun step(delta: Int): Task {
        require(available != null) { UNSCORED_ERROR }
        return copy(earned = minOf(maxOf(earned + delta.toLong(), Rational.ZERO), available))
    }
}

data class Section(
    // an empty title is an untitled group, like a blank line in Markdown
    val title: String,
    val tasks: List<Task> = emptyList(),
    val id: String = identifier(),
) {
    init {
        checkTrimmed(title)
    }
}

/** Minutes from the list's origin: midnight for a day, or the span's start. In a day,
 * values past DAY_MINUTES are on the next day. */
data class Interval(val start: Int, val end: Int) {
    init {
        require(start >= 0) { "An interval cannot start before its day." }
        require(end > start) { "An interval must end after it starts." }
    }

    val seconds: Long get() = (end - start) * 60L
}

/** Whether intervals, in start order and not overlapping, end more than 24 hours after
 * the first starts. */
fun overADay(intervals: List<Interval>): Boolean =
    intervals.isNotEmpty() && intervals.last().end - intervals.first().start > DAY_MINUTES

/** A list's earned and available points as of an instant. at carries its UTC offset,
 * so tallies keep their order across a change of time zone. */
data class Tally(val at: OffsetDateTime, val earned: Rational, val available: Rational) {
    /** at as a local time in zone, comparable with schedules. */
    fun local(zone: ZoneId): LocalDateTime = at.atZoneSameInstant(zone).toLocalDateTime()
}

/** Sections of tasks with tallies of their points over time, oldest first. */
sealed interface Plan {
    val sections: List<Section>
    val tallies: List<Tally>

    /** The moment the intervals count from. */
    val origin: LocalDateTime
    val intervals: List<Interval>

    fun withSections(sections: List<Section>): Plan
    fun withTallies(tallies: List<Tally>): Plan

    fun tasks(): List<Task> = sections.flatMap { it.tasks }

    /** Replaces the task with the same id, wherever it is. */
    fun replaceTask(task: Task): Plan = withSections(
        sections.map { section ->
            section.copy(tasks = section.tasks.map { if (it.id == task.id) task else it })
        },
    )
}

private fun checkSections(sections: List<Section>) {
    require(sections.isNotEmpty()) { "A list needs at least one section." }
    val ids = sections.map { it.id } + sections.flatMap { s -> s.tasks.map { it.id } }
    require(ids.size == ids.toSet().size) { "Task and section identities must be unique." }
}

data class Day(
    val date: LocalDate,
    override val intervals: List<Interval>,
    override val sections: List<Section>,
    override val tallies: List<Tally> = emptyList(),
) : Plan {
    init {
        checkSections(sections)
        check(intervals == intervals.sortedBy { it.start }) { "intervals are stored in start order" }
        require(intervals.zipWithNext().all { (a, b) -> a.end <= b.start }) {
            "Scheduled intervals cannot overlap."
        }
        require(!overADay(intervals)) { "A schedule cannot span more than 24 hours." }
    }

    override val origin: LocalDateTime get() = date.atStartOfDay()

    override fun withSections(sections: List<Section>) = copy(sections = sections)

    override fun withTallies(tallies: List<Tally>) = copy(tallies = tallies)
}

/** A list from start to end. Its time counts toward the figures, except the whole days
 * in off. Spans may overlap. */
data class Span(
    val title: String,
    // local times to the minute, which is all the store keeps
    val start: LocalDateTime,
    val end: LocalDateTime,
    override val sections: List<Section>,
    val id: String = identifier(),
    val off: List<LocalDate> = emptyList(),
    override val tallies: List<Tally> = emptyList(),
) : Plan {
    init {
        checkTrimmed(title)
        checkSections(sections)
        require(listOf(start, end).all { it.second == 0 && it.nano == 0 }) {
            "A span's start and end are local times to the minute."
        }
        require(end > start) { "A span must end after it starts." }
        check(off == off.distinct().sorted()) { "days off are stored in order without repeats" }
        for (day in off) {
            val midnight = day.atStartOfDay()
            require(midnight < end && midnight.plusDays(1) > start) {
                "Days off must fall within the span."
            }
        }
    }

    override val origin: LocalDateTime get() = start

    override val intervals: List<Interval>
        get() {
            fun minutes(moment: LocalDateTime) = Duration.between(start, moment).toMinutes().toInt()

            val intervals = mutableListOf<Interval>()
            var countedFrom = start
            for (day in off) {
                val midnight = day.atStartOfDay()
                if (countedFrom < midnight) intervals += Interval(minutes(countedFrom), minutes(midnight))
                countedFrom = maxOf(countedFrom, midnight.plusDays(1))
            }
            if (countedFrom < end) intervals += Interval(minutes(countedFrom), minutes(end))
            return intervals
        }

    override fun withSections(sections: List<Section>) = copy(sections = sections)

    override fun withTallies(tallies: List<Tally>) = copy(tallies = tallies)
}

/** Whole seconds from origin to now, rounded toward zero, comparable with intervals. */
fun secondsSince(origin: LocalDateTime, now: LocalDateTime): Long {
    val between = Duration.between(origin, now)
    return if (between.isNegative && between.nano > 0) between.seconds + 1 else between.seconds
}

/** plan with a tally of its points as of now added, unless the points equal the last
 * tally's. A list without tallies counts as having had no points. now is a local time
 * in zone. A clock set back stamps the tally at the last one's time, so tallies stay
 * in order. */
fun tallied(plan: Plan, now: LocalDateTime, zone: ZoneId): Plan {
    val (earned, available) = score(plan.tasks())
    val last = plan.tallies.lastOrNull()
    if (earned == (last?.earned ?: Rational.ZERO) && available == (last?.available ?: Rational.ZERO)) {
        return plan
    }
    var at = now.atZone(zone).toOffsetDateTime()
    if (last != null && last.at.isAfter(at)) at = last.at
    return plan.withTallies(plan.tallies + Tally(at, earned, available))
}

/** earned points as whole seconds of the schedule, so pace, progress minus elapsed,
 * agrees with the figures shown. */
fun progress(scheduled: Long, earned: Rational, available: Rational): Long =
    (scheduled * earned / available).truncate()

/** The last tally at or before moment, a local time in zone, or null before the first.
 * Tallies are in time order. */
fun tallyAt(plan: Plan, moment: LocalDateTime, zone: ZoneId): Tally? {
    val instant = moment.atZone(zone).toInstant()
    // the index of the first tally after instant, as Python's bisect_right finds it
    var low = 0
    var high = plan.tallies.size
    while (low < high) {
        val middle = (low + high) / 2
        if (plan.tallies[middle].at.toInstant() > instant) high = middle else low = middle + 1
    }
    return plan.tallies.getOrNull(low - 1)
}

/** Seconds ahead of the schedule at moment, as the summary's pace counts them, from the
 * last tally at or before it. null before the first tally, and without points or a
 * schedule. */
fun paceAt(plan: Plan, moment: LocalDateTime, zone: ZoneId): Long? {
    val tally = tallyAt(plan, moment, zone) ?: return null
    if (tally.available.signum == 0) return null
    val (elapsed, scheduled) = timeProgress(plan.intervals, secondsSince(plan.origin, moment))
    if (scheduled == 0L) return null
    return progress(scheduled, tally.earned, tally.available) - elapsed
}

/** The points that would make pace zero at moment: the available points of the last
 * tally at or before it, in the share of the schedule elapsed. null before the first
 * tally, and without a schedule. */
fun onPaceAt(plan: Plan, moment: LocalDateTime, zone: ZoneId): Rational? {
    val tally = tallyAt(plan, moment, zone) ?: return null
    val (elapsed, scheduled) = timeProgress(plan.intervals, secondsSince(plan.origin, moment))
    if (scheduled == 0L) return null
    return tally.available * elapsed / scheduled
}

/** Earned and available points of the scored tasks. */
fun score(tasks: List<Task>): Pair<Rational, Rational> {
    var earned = Rational.ZERO
    var available = Rational.ZERO
    for (task in tasks) {
        if (task.available != null) {
            earned += task.earned
            available += task.available
        }
    }
    return earned to available
}

/** Seconds of task durations done and needed. A task counts as done by the share of
 * its points earned, so an unscored task is never done. */
fun work(tasks: List<Task>): Pair<Rational, Rational> {
    var done = Rational.ZERO
    var needed = Rational.ZERO
    for (task in tasks) {
        val minutes = task.minutes ?: continue
        val seconds = Rational.of(minutes * 60L)
        needed += seconds
        if (task.available != null) done += seconds * task.earned / task.available
    }
    return done to needed
}

/** Elapsed and total scheduled seconds, now being secondsSince the list's origin. */
fun timeProgress(intervals: List<Interval>, now: Long): Pair<Long, Long> = Pair(
    intervals.sumOf { minOf(maxOf(now - it.start * 60L, 0L), it.seconds) },
    intervals.sumOf { it.seconds },
)

/** Whole seconds per point, or null without points. */
fun perPoint(seconds: Rational, available: Rational): Long? =
    if (available.signum == 0) null else (seconds / available).truncate()

/** The scored tasks with a duration, the ones a duration per point counts. */
fun timed(tasks: List<Task>): List<Task> =
    tasks.filter { it.minutes != null && it.available != null }

fun completion(task: Task): Rational? = task.available?.let { task.earned / it }

fun durationPerPoint(task: Task): Rational? {
    val minutes = task.minutes ?: return null
    val available = task.available ?: return null
    return Rational.of(minutes.toLong()) / available
}

/** Totals of some tasks and, for a list, of its schedule as of a moment, before
 * formatting. Times are in seconds; without a schedule, elapsed and scheduled are 0.
 * The timed totals count only timed tasks. */
data class Figures(
    val earned: Rational,
    val available: Rational,
    val worked: Rational,
    val needed: Rational,
    val timedAvailable: Rational,
    val timedNeeded: Rational,
    // scored tasks without a duration
    val undurated: Int,
    val elapsed: Long,
    val scheduled: Long,
) {
    val progress: Long?
        get() = if (scheduled == 0L || available.signum == 0) null
        else progress(scheduled, earned, available)

    val pace: Long? get() = progress?.let { it - elapsed }

    /** The share of the schedule left after task durations, negative when they overrun
     * it. */
    val free: Rational?
        get() = if (scheduled == 0L || needed.signum == 0) null
        else (Rational.of(scheduled) - needed) / scheduled

    val schedulePerPoint: Long?
        get() = if (scheduled == 0L) null else perPoint(Rational.of(scheduled), available)

    val durationPerPoint: Long? get() = perPoint(timedNeeded, timedAvailable)
}

/** The figures of tasks against intervals, now being secondsSince the list's origin. */
fun figures(tasks: List<Task>, intervals: List<Interval> = emptyList(), now: Long = 0): Figures {
    val (earned, available) = score(tasks)
    val (worked, needed) = work(tasks)
    val timedTasks = timed(tasks)
    val (_, timedAvailable) = score(timedTasks)
    val (_, timedNeeded) = work(timedTasks)
    val (elapsed, scheduled) = timeProgress(intervals, now)
    return Figures(
        earned,
        available,
        worked,
        needed,
        timedAvailable,
        timedNeeded,
        tasks.count { it.minutes == null && it.available != null },
        elapsed,
        scheduled,
    )
}
