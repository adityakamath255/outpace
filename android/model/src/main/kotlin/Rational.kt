package io.github.adityakamath255.todo.model

import java.math.BigInteger

/** An exact fraction in lowest terms, with the sign on the numerator. Points and the
 * figures derived from them use it, so they agree exactly with the Python model. */
class Rational private constructor(
    val numerator: BigInteger,
    val denominator: BigInteger,
) : Comparable<Rational> {
    companion object {
        val ZERO = of(0)

        fun of(numerator: Long, denominator: Long = 1): Rational =
            of(BigInteger.valueOf(numerator), BigInteger.valueOf(denominator))

        fun of(numerator: BigInteger, denominator: BigInteger): Rational {
            if (denominator.signum() == 0) throw ArithmeticException("division by zero")
            val divisor = numerator.gcd(denominator)
            val sign = denominator.signum().toBigInteger()
            return Rational(numerator / divisor * sign, denominator.abs() / divisor)
        }

        private val FORMAT = Regex("""([+-]?)(\d+)(?:\.(\d+)|/(\d+))?""")

        /** Reads "5", "5/2" or "2.5", the forms the store and the cases use. */
        fun parse(text: String): Rational {
            val match = requireNotNull(FORMAT.matchEntire(text.trim())) {
                "Invalid fraction: $text"
            }
            val (sign, whole, decimals, denominator) = match.destructured
            var value = when {
                decimals.isNotEmpty() -> of(
                    BigInteger(whole + decimals),
                    BigInteger.TEN.pow(decimals.length),
                )
                denominator.isNotEmpty() -> of(BigInteger(whole), BigInteger(denominator))
                else -> of(BigInteger(whole), BigInteger.ONE)
            }
            if (sign == "-") value = -value
            return value
        }
    }

    val signum: Int get() = numerator.signum()

    operator fun plus(other: Rational) = of(
        numerator * other.denominator + other.numerator * denominator,
        denominator * other.denominator,
    )

    operator fun minus(other: Rational) = this + -other

    operator fun unaryMinus() = Rational(-numerator, denominator)

    operator fun times(other: Rational) =
        of(numerator * other.numerator, denominator * other.denominator)

    operator fun div(other: Rational) =
        of(numerator * other.denominator, denominator * other.numerator)

    operator fun plus(other: Long) = this + of(other)

    operator fun times(other: Long) = this * of(other)

    operator fun div(other: Long) = this / of(other)

    /** The integer part, rounded toward zero like Python's int(). */
    fun truncate(): Long = (numerator / denominator).longValueExact()

    override fun compareTo(other: Rational): Int =
        (numerator * other.denominator).compareTo(other.numerator * denominator)

    override fun equals(other: Any?): Boolean =
        other is Rational && numerator == other.numerator && denominator == other.denominator

    override fun hashCode(): Int = 31 * numerator.hashCode() + denominator.hashCode()

    /** "5" or "5/2", as Python writes a Fraction. */
    override fun toString(): String =
        if (denominator == BigInteger.ONE) "$numerator" else "$numerator/$denominator"
}

operator fun Long.times(other: Rational) = other * this
