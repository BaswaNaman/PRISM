"""
Decimal <-> fraction inch conversion, per Decimal_Fraction.xlsx.
===============================================================

Why this exists
---------------
Manufacturers publish decimals (0.5, 50.25). Trade buyers search fractions
(1/2, 50-1/4). The Unilog standard writes dimensions in fraction form, using a
hyphen between the whole number and the fraction:

    0.5      -> 1/2
    50.25    -> 50-1/4
    0.015625 -> 1/64

This module is generated arithmetically over all 64ths, so it reproduces the
full 1/64 ... 63/64 table in the client workbook exactly without needing the
file. Values are matched to the nearest 1/64 within a tolerance; anything that
is not a clean 64th is returned unchanged, because silently rounding a real
measurement would corrupt data.
"""

from fractions import Fraction
from typing import Optional, Dict, List, Tuple
import re

# Exact denominators used by the trade. 64 is the finest granularity in the
# client's table; the coarser ones are preferred when a value reduces cleanly.
MAX_DENOMINATOR = 64
TOLERANCE = 1e-9


def build_decimal_fraction_table() -> List[Tuple[str, float]]:
    """Reproduce Decimal_Fraction.xlsx: every n/64 from 1/64 to 63/64.

    Returns a list of (fraction_string, decimal_value) in ascending order. The
    fraction string is fully reduced, matching the workbook (e.g. 32/64 -> 1/2).
    """
    rows = []
    for numerator in range(1, MAX_DENOMINATOR):
        frac = Fraction(numerator, MAX_DENOMINATOR)
        rows.append((f"{frac.numerator}/{frac.denominator}", float(frac)))
    return rows


# Reverse lookup: decimal (rounded to 9dp) -> reduced fraction string.
DECIMAL_TO_FRACTION: Dict[float, str] = {
    round(dec, 9): frac for frac, dec in build_decimal_fraction_table()
}


def decimal_to_fraction(value, max_denominator: int = MAX_DENOMINATOR) -> Optional[str]:
    """Convert a decimal to Unilog fraction form.

    0.5     -> "1/2"
    50.25   -> "50-1/4"
    3.0     -> "3"
    0.3333  -> None  (not a clean 64th; caller should keep the decimal)
    """
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None

    sign = "-" if num < 0 else ""
    num = abs(num)

    whole = int(num)
    remainder = num - whole

    if remainder < TOLERANCE:
        return f"{sign}{whole}"

    # Snap to the nearest 1/max_denominator and confirm it is genuinely exact.
    scaled = remainder * max_denominator
    nearest = round(scaled)
    if nearest == 0 or nearest >= max_denominator:
        return None
    if abs(scaled - nearest) > 1e-6:
        return None  # not a clean fraction — do not fabricate precision

    frac = Fraction(int(nearest), max_denominator)
    frac_str = f"{frac.numerator}/{frac.denominator}"

    if whole == 0:
        return f"{sign}{frac_str}"
    return f"{sign}{whole}-{frac_str}"


def fraction_to_decimal(text) -> Optional[float]:
    """Parse Unilog fraction form back to a float.

    "50-1/4" -> 50.25 ; "1/2" -> 0.5 ; "3" -> 3.0 ; "2 1/2" -> 2.5
    """
    if text is None:
        return None
    s = str(text).strip()
    if not s:
        return None

    neg = s.startswith("-") and re.search(r"\d", s)
    # Distinguish a leading minus sign from the whole-fraction separator hyphen.
    body = s[1:].strip() if (neg and not re.match(r"^-\d+/\d+$", s)) else s
    body = body.lstrip("-") if body is not s else body

    m = re.match(r"^(\d+)\s*[-\s]\s*(\d+)\s*/\s*(\d+)$", body)
    if m:
        whole, n, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if d == 0:
            return None
        val = whole + n / d
        return -val if neg else val

    m = re.match(r"^(\d+)\s*/\s*(\d+)$", body)
    if m:
        n, d = int(m.group(1)), int(m.group(2))
        if d == 0:
            return None
        val = n / d
        return -val if neg else val

    try:
        val = float(body)
        return -val if neg and val > 0 else val
    except ValueError:
        return None


DIMENSION_WITH_UNIT_RE = re.compile(
    r"(?<![\d/\-])(\d+\.\d+)\s*(in|inch|inches|\")(?![a-z])",
    re.IGNORECASE,
)


def convert_decimals_in_text(text: str) -> str:
    """Rewrite decimal inch dimensions inside a free-text string to fraction form.

    "50.25 in W x 24.5 in D" -> "50-1/4 in W x 24-1/2 in D"

    Only touches numbers explicitly followed by an inch unit, so voltages,
    weights and tolerances are left alone.
    """
    if not text:
        return text

    def _sub(m):
        frac = decimal_to_fraction(m.group(1))
        if frac is None:
            return m.group(0)
        return f"{frac} in"

    return DIMENSION_WITH_UNIT_RE.sub(_sub, str(text))
