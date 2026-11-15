package io.github.adityakamath255.todo.model

import java.time.LocalDate
import java.time.LocalDateTime
import java.time.OffsetDateTime
import java.time.format.DateTimeFormatter
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.int
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put

// The JSON documents the TUI's database stores: one per day, keyed by its ISO date,
// and one per span, keyed by its id. Times are written as Python's isoformat writes
// them, so both apps store the same text.

private val MINUTE = DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm")
private val SECOND = DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss")
private val MICROSECOND = DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss.SSSSSS")
private val OFFSET = DateTimeFormatter.ofPattern("xxx")

/** A tally time as Python's isoformat writes it: seconds always, microseconds when
 * there are any, and the offset as +HH:MM. */
fun isoformat(at: OffsetDateTime): String {
    val local = at.toLocalDateTime()
    val format = if (local.nano / 1000 == 0) SECOND else MICROSECOND
    return local.format(format) + at.format(OFFSET)
}

private fun encodeSections(sections: List<Section>) = buildJsonArray {
    for (section in sections) {
        add(
            buildJsonObject {
                put("id", section.id)
                put("title", section.title)
                put("tasks", buildJsonArray { section.tasks.forEach { add(encodeTask(it)) } })
            },
        )
    }
}

fun encodeTask(task: Task): JsonObject = buildJsonObject {
    put("id", task.id)
    put("title", task.title)
    put("available", task.available?.toString())
    put("earned", task.earned.toString())
    put("minutes", task.minutes)
}

private fun encodeTallies(tallies: List<Tally>) = buildJsonArray {
    for (tally in tallies) {
        add(
            buildJsonArray {
                add(JsonPrimitive(isoformat(tally.at)))
                add(JsonPrimitive(tally.earned.toString()))
                add(JsonPrimitive(tally.available.toString()))
            },
        )
    }
}

fun encodeDay(day: Day): JsonObject = buildJsonObject {
    put(
        "intervals",
        buildJsonArray {
            for (interval in day.intervals) {
                add(buildJsonArray { add(JsonPrimitive(interval.start)); add(JsonPrimitive(interval.end)) })
            }
        },
    )
    put("sections", encodeSections(day.sections))
    put("tallies", encodeTallies(day.tallies))
}

fun encodeSpan(span: Span): JsonObject = buildJsonObject {
    put("title", span.title)
    put("start", span.start.format(MINUTE))
    put("end", span.end.format(MINUTE))
    put("off", buildJsonArray { span.off.forEach { add(JsonPrimitive(it.toString())) } })
    put("sections", encodeSections(span.sections))
    put("tallies", encodeTallies(span.tallies))
}

private fun JsonElement.string(): String = jsonPrimitive.content

private fun decodeSections(data: JsonElement): List<Section> = data.jsonArray.map { section ->
    val fields = section.jsonObject
    Section(
        fields.getValue("title").string(),
        fields.getValue("tasks").jsonArray.map { decodeTask(it.jsonObject) },
        fields.getValue("id").string(),
    )
}

fun decodeTask(fields: JsonObject): Task {
    val available = fields.getValue("available")
    // older days also have a "checked" field, which is ignored
    return Task(
        fields.getValue("title").string(),
        if (available is JsonNull) null else Rational.parse(available.string()),
        Rational.parse(fields.getValue("earned").string()),
        fields.getValue("minutes").jsonPrimitive.intOrNull,
        fields.getValue("id").string(),
    )
}

private fun decodeTallies(data: JsonElement?): List<Tally> =
    data?.jsonArray?.map { tally ->
        val (at, earned, available) = tally.jsonArray.map { it.string() }
        Tally(OffsetDateTime.parse(at), Rational.parse(earned), Rational.parse(available))
    } ?: emptyList()

fun decodeDay(date: LocalDate, text: String): Day = decodeDay(date, Json.parseToJsonElement(text).jsonObject)

fun decodeDay(date: LocalDate, data: JsonObject): Day = Day(
    date,
    data.getValue("intervals").jsonArray.map { interval ->
        val (start, end) = interval.jsonArray.map { it.jsonPrimitive.int }
        Interval(start, end)
    },
    decodeSections(data.getValue("sections")),
    // days saved before tallies have none
    decodeTallies(data["tallies"]),
)

fun decodeSpan(id: String, text: String): Span = decodeSpan(id, Json.parseToJsonElement(text).jsonObject)

fun decodeSpan(id: String, data: JsonObject): Span = Span(
    data.getValue("title").string(),
    LocalDateTime.parse(data.getValue("start").string()),
    LocalDateTime.parse(data.getValue("end").string()),
    decodeSections(data.getValue("sections")),
    id,
    // spans saved before days off or tallies have neither
    (data["off"] as? JsonArray)?.map { LocalDate.parse(it.string()) } ?: emptyList(),
    decodeTallies(data["tallies"]),
)
