package io.github.adityakamath255.todo.model

import java.math.BigDecimal
import java.math.MathContext
import java.math.RoundingMode
import java.time.LocalDate
import java.time.format.DateTimeParseException
import java.util.Locale

// reading and writing the values people type; invalid input throws
// IllegalArgumentException with a message to show them

const val DAY_MINUTES = 24 * 60

private const val DURATION_ERROR = "Use minutes, like 90, or hours and minutes, like 1h30m."
private const val CLOCK_ERROR = "Use a 24-hour time, like 07:30, 0730 or 7."

fun points(value: String): Rational {
    val text = value.trim()
    require(Regex("""\d+(?:\.\d+)?""").matches(text)) {
        "Use a nonnegative decimal for points, such as 2.5."
    }
    return Rational.parse(text)
}

/** Reads 90 or 1h30m as minutes, or null when blank. Task checks the range. */
fun parseMinutes(value: String): Int? {
    val text = value.trim().lowercase()
    if (text.isEmpty()) return null
    if (Regex("[0-9]+").matches(text)) return text.toInt()
    val match = Regex("""(?:([0-9]+)h)?\s*(?:([0-9]+)m)?""").matchEntire(text)
    val (hours, minutes) = match?.destructured ?: throw IllegalArgumentException(DURATION_ERROR)
    require(hours.isNotEmpty() || minutes.isNotEmpty()) { DURATION_ERROR }
    return (hours.toIntOrNull() ?: 0) * 60 + (minutes.toIntOrNull() ?: 0)
}

/** Writes minutes the way parseMinutes reads them, such as 1h30m or 24h. */
fun minutesText(minutes: Int): String {
    val hours = minutes / 60
    val rest = minutes % 60
    return (if (hours > 0) "${hours}h" else "") + (if (rest > 0 || hours == 0) "${rest}m" else "")
}

/** Minutes into the day from a 24-hour time, written like 07:30, 7:30, 0730, 730 or
 * a bare hour like 7. */
fun parseClock(text: String): Int {
    val match = Regex("""(\d{1,2})(?::?(\d{2}))?""").matchEntire(text.trim())
        ?: throw IllegalArgumentException(CLOCK_ERROR)
    val hours = match.groupValues[1].toInt()
    val minutes = match.groupValues[2].ifEmpty { "0" }.toInt()
    require(hours <= 23 && minutes <= 59) { CLOCK_ERROR }
    return hours * 60 + minutes
}

fun clock(minutes: Int): String =
    String.format(Locale.ROOT, "%02d:%02d", minutes / 60 % 24, minutes % 60)

fun intervalText(interval: Interval): String =
    "${clock(interval.start)} - ${clock(interval.end)}"

/** Intervals from start and end clock times, in minutes, written in order.
 *
 * The first starts on the list's day. Each later one starts at the first time with
 * its clock reading at or after the previous end, so writing past midnight continues
 * into the next day. */
fun schedule(clocks: List<Pair<Int, Int>>): List<Interval> {
    val intervals = mutableListOf<Interval>()
    for ((clockStart, end) in clocks) {
        var start = clockStart
        val previousEnd = intervals.lastOrNull()?.end ?: 0
        while (start < previousEnd) start += DAY_MINUTES
        intervals += Interval(start, start + (end - start).mod(DAY_MINUTES))
    }
    // writing intervals out of order would push them onto the next day
    require(!overADay(intervals)) {
        "The schedule spans more than 24 hours; write its intervals in order."
    }
    return intervals
}

fun parseDate(value: String): LocalDate {
    val text = value.trim()
    val error = "Use a date in YYYY-MM-DD format."
    require(Regex("""\d{4}-\d{2}-\d{2}""").matches(text)) { error }
    val parsed = try {
        LocalDate.parse(text)
    } catch (_: DateTimeParseException) {
        throw IllegalArgumentException(error)
    }
    // Python's dates start at year 1
    require(parsed.year >= 1) { error }
    return parsed
}

/** Dates from a comma-separated list of dates and ranges like 2026-11-25 -
 * 2026-11-27, in order and without repeats. Blank text gives none. */
fun parseDays(text: String): List<LocalDate> {
    val days = sortedSetOf<LocalDate>()
    for (part in text.split(",").map { it.trim() }.filter { it.isNotEmpty() }) {
        val separator = part.indexOf(" - ")
        val first = if (separator < 0) part else part.substring(0, separator)
        val last = if (separator < 0) "" else part.substring(separator + 3)
        val start = parseDate(first)
        val end = if (last.isNotEmpty()) parseDate(last) else start
        require(end >= start) { "The range $part must end after it starts." }
        generateSequence(start) { it.plusDays(1) }.takeWhile { it <= end }.forEach(days::add)
    }
    return days.toList()
}

/** days, in order, as parseDays reads them, with each run as a range. */
fun daysText(days: List<LocalDate>): String {
    val runs = mutableListOf<MutableList<LocalDate>>()
    for (day in days) {
        val run = runs.lastOrNull()
        if (run != null && run.last().plusDays(1) == day) run += day else runs += mutableListOf(day)
    }
    return runs.joinToString(", ") { run ->
        if (run.size == 1) "${run[0]}" else "${run.first()} - ${run.last()}"
    }
}

/** A fraction as a decimal without trailing zeros. Values from decimal input always
 * end; others round at the precision the Python model uses. */
fun number(value: Rational): String {
    if (value.denominator == java.math.BigInteger.ONE) return "${value.numerator}"
    val precision = value.numerator.toString().length + value.denominator.bitLength()
    return BigDecimal(value.numerator)
        .divide(BigDecimal(value.denominator), MathContext(precision, RoundingMode.HALF_EVEN))
        .toPlainString()
        .trimEnd('0')
        .trimEnd('.')
}

/** value as a percentage with two decimals, rounded toward zero. */
fun percentage(value: Rational): String {
    val hundredths = (value * 10_000).truncate()
    val sign = if (hundredths < 0) "-" else ""
    val magnitude = kotlin.math.abs(hundredths)
    return "$sign${magnitude / 100}.${String.format(Locale.ROOT, "%02d", magnitude % 100)}"
}
