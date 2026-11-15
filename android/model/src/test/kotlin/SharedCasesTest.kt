package io.github.adityakamath255.todo.model

import java.io.File
import java.time.LocalDate
import java.time.LocalDateTime
import java.time.ZoneId
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.int
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

/** Checks this model against spec/cases.json, which the Python model generates; its
 * generator, tui/tests/shared_cases.py, describes the format. */
class SharedCasesTest {
    private val cases = Json.parseToJsonElement(File(System.getProperty("cases")).readText()).jsonObject
    private val zone = ZoneId.of(cases.getValue("zone").jsonPrimitive.content)
    private val plans = cases.getValue("plans").jsonObject

    private fun check(group: String, compute: (JsonElement) -> JsonElement) {
        val failures = cases.getValue(group).jsonArray.mapNotNull { case ->
            val input = case.jsonObject.getValue("input")
            val expected = case.jsonObject.getValue("output")
            val actual = try {
                compute(input)
            } catch (error: IllegalArgumentException) {
                buildJsonObject { put("error", error.message) }
            }
            if (actual == expected) null else "$input\n  expected $expected\n  got      $actual"
        }
        assertTrue(failures.isEmpty()) {
            "${failures.size} $group cases differ:\n" + failures.take(5).joinToString("\n")
        }
    }

    private fun string(value: JsonElement) = value.jsonPrimitive.content

    private fun text(value: Any?): JsonElement = if (value == null) JsonNull else JsonPrimitive(value.toString())

    private fun number(value: Number?): JsonElement = if (value == null) JsonNull else JsonPrimitive(value)

    private fun planFrom(value: JsonElement): Plan {
        val fields = value.jsonObject
        val data = fields.getValue("data").jsonObject
        return fields["date"]?.let { decodeDay(LocalDate.parse(string(it)), data) }
            ?: decodeSpan(string(fields.getValue("id")), data)
    }

    private fun named(input: JsonElement): Plan = planFrom(plans.getValue(string(input.jsonObject.getValue("plan"))))

    private fun moment(input: JsonElement, key: String): LocalDateTime =
        LocalDateTime.parse(string(input.jsonObject.getValue(key)))

    private fun intervals(intervals: List<Interval>) = buildJsonArray {
        for (interval in intervals) add(buildJsonArray { add(JsonPrimitive(interval.start)); add(JsonPrimitive(interval.end)) })
    }

    private fun tally(tally: Tally?): JsonElement = if (tally == null) JsonNull else buildJsonArray {
        add(JsonPrimitive(isoformat(tally.at)))
        add(JsonPrimitive(tally.earned.toString()))
        add(JsonPrimitive(tally.available.toString()))
    }

    private fun storedTask(value: JsonElement): Task = decodeTask(value.jsonObject)

    @Test
    fun points() = check("points") { JsonPrimitive(points(string(it)).toString()) }

    @Test
    fun minutes() = check("minutes") { number(parseMinutes(string(it))) }

    @Test
    fun minutesText() = check("minutes_text") { JsonPrimitive(minutesText(it.jsonPrimitive.int)) }

    @Test
    fun clock() = check("clock") { JsonPrimitive(parseClock(string(it))) }

    @Test
    fun schedule() = check("schedule") { input ->
        val clocks = input.jsonArray.map { pair -> pair.jsonArray.let { it[0].jsonPrimitive.int to it[1].jsonPrimitive.int } }
        intervals(schedule(clocks))
    }

    @Test
    fun intervalText() = check("interval_text") { input ->
        val (start, end) = input.jsonArray.map { it.jsonPrimitive.int }
        JsonPrimitive(intervalText(Interval(start, end)))
    }

    @Test
    fun date() = check("date") { JsonPrimitive(parseDate(string(it)).toString()) }

    @Test
    fun days() = check("days") { input -> JsonArray(parseDays(string(input)).map { JsonPrimitive(it.toString()) }) }

    @Test
    fun daysText() = check("days_text") { input ->
        JsonPrimitive(daysText(input.jsonArray.map { LocalDate.parse(string(it)) }))
    }

    @Test
    fun number() = check("number") { JsonPrimitive(number(Rational.parse(string(it)))) }

    @Test
    fun percentage() = check("percentage") { JsonPrimitive(percentage(Rational.parse(string(it)))) }

    @Test
    fun spanIntervals() = check("span_intervals") { intervals(planFrom(plans.getValue(string(it))).intervals) }

    @Test
    fun figures() = check("figures") { input ->
        val plan = named(input)
        val totals = figures(plan.tasks(), plan.intervals, secondsSince(plan.origin, moment(input, "now")))
        buildJsonObject {
            put("earned", text(totals.earned))
            put("available", text(totals.available))
            put("worked", text(totals.worked))
            put("needed", text(totals.needed))
            put("timed_available", text(totals.timedAvailable))
            put("timed_needed", text(totals.timedNeeded))
            put("undurated", number(totals.undurated))
            put("elapsed", number(totals.elapsed))
            put("scheduled", number(totals.scheduled))
            put("progress", number(totals.progress))
            put("pace", number(totals.pace))
            put("free", text(totals.free))
            put("schedule_per_point", number(totals.schedulePerPoint))
            put("duration_per_point", number(totals.durationPerPoint))
        }
    }

    @Test
    fun at() = check("at") { input ->
        val plan = named(input)
        val moment = moment(input, "moment")
        buildJsonObject {
            put("tally", tally(tallyAt(plan, moment, zone)))
            put("pace", number(paceAt(plan, moment, zone)))
            put("on_pace", text(onPaceAt(plan, moment, zone)))
        }
    }

    @Test
    fun tallied() = check("tallied") { input ->
        var plan = named(input)
        if (input.jsonObject.getValue("toggle").jsonPrimitive.boolean) {
            plan = plan.replaceTask(plan.tasks().first().toggle())
        }
        JsonArray(tallied(plan, moment(input, "now"), zone).tallies.map(::tally))
    }

    @Test
    fun taskChange() = check("task_change") { input ->
        val fields = input.jsonObject
        val before = storedTask(fields.getValue("task"))
        val after = when (val change = string(fields.getValue("change"))) {
            "toggle" -> before.toggle()
            "step" -> before.step(fields.getValue("delta").jsonPrimitive.int)
            "edit" -> before.edit(
                string(fields.getValue("title")),
                fields.getValue("available").let { if (it is JsonNull) null else Rational.parse(string(it)) },
                fields.getValue("minutes").jsonPrimitive.intOrNull,
            )
            else -> error("unknown change $change")
        }
        encodeTask(after)
    }

    @Test
    fun taskValues() = check("task_values") { input ->
        val task = storedTask(input)
        buildJsonObject {
            put("complete", task.complete)
            put("completion", text(completion(task)))
            put("duration_per_point", text(durationPerPoint(task)))
        }
    }

    @Test
    fun storage() = check("storage") { input ->
        when (val plan = planFrom(input)) {
            is Day -> encodeDay(plan)
            is Span -> encodeSpan(plan)
        }
    }
}
