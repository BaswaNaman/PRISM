"""
Focused regression tests for the PRISM final quality-fix pass.

Covers:
  1. IP67            -> valid
  2. "IP 67"          -> valid, normalizes the same as IP67
  3. invalid IP code  -> still rejected
  4. "10...30 VDC"    -> both voltage endpoints preserved
  5. single voltage   -> existing behavior preserved
  (+ a category regression covering the "wrong hint contradicted by
  in-text keyword" fix, and a "hint still used as fallback" check so
  that fix doesn't regress the normal case.)

Drop this file into your existing tests/ directory (next to your other
extractor/validator tests) so it picks up your real `app.schema` models
and fixtures/conftest instead of needing its own stubs.
"""

import pytest
from app import extractor
from app import validator
from app.schema import ProductIntelligenceRecord


def _build_and_validate(raw_text, **kwargs):
    record = extractor.process_raw_product_text(
        raw_text, input_mode="manual", source_type="manual_input",
        source_origin="Manual Text Input", **kwargs,
    )
    return validator.validate_and_enrich_record(record, raw_text)


# ---------------------------------------------------------------------------
# BUG 1: IP rating normalization
# ---------------------------------------------------------------------------

def test_ip67_is_valid():
    record = _build_and_validate("Sensor housing. IP67 rated. Voltage 24 VDC.")
    assert record.ip_rating.value == "IP67"
    assert record.ip_rating.validation_status != "flagged_validation_error"


def test_ip_with_space_normalizes_and_is_valid():
    record = _build_and_validate("Sensor housing. IP 67 rated. Voltage 24 VDC.")
    # Normalized to the same canonical form as "IP67", not rejected as invalid.
    assert record.ip_rating.value == "IP67"
    assert record.ip_rating.validation_status != "flagged_validation_error"


def test_invalid_ip_code_still_rejected():
    record = _build_and_validate("Sensor housing. IP99 rated. Voltage 24 VDC.")
    assert record.ip_rating.validation_status == "flagged_validation_error"
    assert "Invalid IP Code" in (record.ip_rating.validation_message or "")


# ---------------------------------------------------------------------------
# BUG 2: voltage range preservation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("separator_text", [
    "10...30 VDC",
    "10\u202630 VDC",   # ellipsis character
    "10-30 VDC",
    "10 to 30 VDC",
])
def test_voltage_range_preserves_both_endpoints(separator_text):
    raw_text = f"Proximity sensor. Supply voltage: {separator_text} operating range."
    record = _build_and_validate(raw_text)
    value = str(record.voltage_rating.value)
    assert "10" in value, f"lower bound dropped: {value!r}"
    assert "30" in value, f"upper bound dropped: {value!r}"
    # Regardless of which separator the SOURCE text used (ascii hyphen,
    # en dash, ellipsis, or "to"), the range must be recognized as grounded
    # and pass validation -- not just have both numbers present.
    assert record.voltage_rating.is_grounded is True
    assert record.voltage_rating.validation_status == "verified"


def test_ascii_hyphen_range_snippet_matches_en_dash_stored_value():
    """Regression for the grounding/normalization mismatch: the extractor
    stores voltage ranges with an en dash ("10\u201330") to avoid the sign
    ambiguity of a plain hyphen, but the cited SOURCE snippet is quoted
    verbatim from raw_text and may itself use a plain ASCII hyphen
    ("10-30 VDC"). These must be recognized as the same numeric range
    instead of being flagged ungrounded."""
    raw_text = "Proximity sensor. Supply voltage: 10-30 VDC operating range."
    record = _build_and_validate(raw_text)
    assert record.voltage_rating.value == "10\u201330"
    assert record.voltage_rating.source_snippet == "Supply voltage: 10-30 VDC"
    assert record.voltage_rating.is_grounded is True
    assert record.voltage_rating.validation_status == "verified"


def test_numeric_grounding_still_rejects_unrelated_numbers():
    """The separator-normalization fix must not weaken grounding generally --
    a value whose number(s) genuinely aren't in the cited snippet must still
    be flagged ungrounded."""
    assert extractor._numeric_claim_matches_evidence("999", "Supply voltage: 10-30 VDC") is False


def test_numeric_grounding_leaves_negative_temperature_signs_alone():
    """A leading minus sign (never preceded by a digit) must not be treated
    as a range separator -- "-40" in an operating-temperature range is a
    real negative number, not a stray hyphen between two numbers."""
    assert extractor._numeric_claim_matches_evidence(
        "-40", "Operating temp: -40C to 90C") is True
    assert extractor._numeric_claim_matches_evidence(
        "-99", "Operating temp: -40C to 90C") is False


def test_single_voltage_still_a_plain_number():
    record = _build_and_validate("Sensor. Rated voltage: 24 VDC nominal supply.")
    assert record.voltage_rating.value == 24.0
    assert record.voltage_rating.validation_status == "verified"


def test_voltage_range_does_not_fabricate_missing_minimum():
    # Single-ended spec ("up to 30 VDC") must stay a single value, never
    # silently become a range with an invented lower bound.
    record = _build_and_validate("Sensor. Max voltage: 30 VDC.")
    assert record.voltage_rating.value == 30.0


def test_implausible_voltage_range_endpoint_still_rejected():
    record = _build_and_validate("Widget. Supply voltage: 5...99999 VDC range.")
    assert record.voltage_rating.value is None
    assert record.voltage_rating.validation_status == "missing"


# ---------------------------------------------------------------------------
# BUG 3: category hint vs. grounded in-text keyword
# ---------------------------------------------------------------------------

def test_specific_text_keyword_overrides_contradicting_hint():
    raw_text = (
        "DW-AS-513-M12 Proximity Sensor from Contrinex. Sensing distance 5mm. "
        "M12 connector interface."
    )
    record = _build_and_validate(raw_text, category_hint="Industrial Connector")
    assert record.category.value == "Industrial Sensor"
    # And it must be grounded now (snippet actually came from raw_text).
    assert record.category.validation_status != "flagged_ungrounded"


def test_hint_still_used_when_no_specific_text_keyword():
    raw_text = "PN-2200 industrial widget for general automation use."
    record = _build_and_validate(raw_text, category_hint="Industrial Widget")
    assert record.category.value == "Industrial Widget"
