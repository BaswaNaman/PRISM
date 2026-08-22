import re
from typing import Dict, Any, Tuple
from app.schema import ExtractedField, ProductIntelligenceRecord

CONFIDENCE_THRESHOLD = 0.65

VALID_IP_RATINGS = {
    "IP20", "IP40", "IP54", "IP55", "IP65", "IP66", "IP67", "IP68", "IP69K"
}

def normalize_field_units(field_name: str, value: Any, unit: str) -> Tuple[Any, str]:
    """10/10 FEATURE: Normalizes units to standard industrial metrics: V, A, °C, KG, BAR, MM."""
    if value is None:
        return value, unit
        
    try:
        val_float = float(value)
    except (ValueError, TypeError):
        return value, unit

    # Core normalization
    if "voltage" in field_name.lower():
        if unit and unit.upper() == "MV": return round(val_float / 1000.0, 3), "V"
        if unit and unit.upper() in ["KV", "KILOVOLT"]: return round(val_float * 1000.0, 1), "V"
        return val_float, "V"

    if "current" in field_name.lower():
        if unit and unit.upper() == "MA": return round(val_float / 1000.0, 3), "A"
        if unit and unit.upper() in ["KA", "KILOAMP"]: return round(val_float * 1000.0, 1), "A"
        return val_float, "A"

    if "temperature" in field_name.lower() or "temp" in field_name.lower():
        if unit and ("F" in unit.upper()): return round((val_float - 32.0) * (5.0 / 9.0), 1), "°C"
        if unit and "K" in unit.upper() and "C" not in unit.upper(): return round(val_float - 273.15, 1), "°C"
        return val_float, "°C"

    # Dynamic Unit Normalization for Extra Attributes
    if "weight" in field_name.lower() or "mass" in field_name.lower():
        if unit and "LB" in unit.upper(): return round(val_float * 0.453592, 2), "kg"
        if unit and "G" == unit.upper(): return round(val_float / 1000.0, 3), "kg"
        if unit and "OZ" in unit.upper(): return round(val_float * 0.0283495, 2), "kg"
        return val_float, "kg"

    if "pressure" in field_name.lower():
        if unit and "PSI" in unit.upper(): return round(val_float * 0.0689476, 2), "bar"
        if unit and "MPA" in unit.upper(): return round(val_float * 10.0, 2), "bar"
        return val_float, "bar"

    if "length" in field_name.lower() or "distance" in field_name.lower() or "size" in field_name.lower():
        if unit and "INCH" in unit.upper() or unit == '"': return round(val_float * 25.4, 1), "mm"
        if unit and "CM" in unit.upper(): return round(val_float * 10.0, 1), "mm"
        if unit and "M" == unit.upper(): return round(val_float * 1000.0, 1), "mm"
        return val_float, "mm"

    return value, unit


def validate_and_enrich_record(record: ProductIntelligenceRecord, raw_text: str) -> ProductIntelligenceRecord:
    raw_lower = raw_text.lower()
    fields_dict = {
        "product_name": record.product_name,
        "category": record.category,
        "voltage_rating": record.voltage_rating,
        "current_rating": record.current_rating,
        "ip_rating": record.ip_rating,
        "connector_type": record.connector_type,
        "operating_temperature_min": record.operating_temperature_min,
        "operating_temperature_max": record.operating_temperature_max,
        "material": record.material,
        "certifications": record.certifications,
        "mounting_type": record.mounting_type
    }

    # 1. Normalize units for standard numeric fields
    for field_name, field_obj in fields_dict.items():
        if field_obj.value is not None and field_obj.unit:
            norm_val, norm_unit = normalize_field_units(field_name, field_obj.value, field_obj.unit)
            field_obj.value = norm_val
            field_obj.unit = norm_unit

    # 1.5. Normalize units for dynamic Extra Attributes
    for dyn_key, dyn_field in record.extra_attributes.items():
        if dyn_field.value is not None and dyn_field.unit:
            norm_val, norm_unit = normalize_field_units(dyn_key, dyn_field.value, dyn_field.unit)
            dyn_field.value = norm_val
            dyn_field.unit = norm_unit

    # 2. Check Required Attributes
    required_fields = ["product_name", "category"]
    for req in required_fields:
        field_obj = fields_dict[req]
        if field_obj.value is None or str(field_obj.value).strip() == "":
            field_obj.validation_status = "missing"
            field_obj.validation_message = f"Required field '{field_obj.label}' is missing from product record."

    # 2.5 Category-aware applicability.
    category_text = str(record.category.value or "").lower()
    product_text = str(record.product_name.value or "").lower()
    applicability_text = f"{category_text} {product_text} {raw_lower}"

    # Category-family applicability for non-electrical abrasives/consumables.
    # These products do not meaningfully use the fixed electrical/sensor fields
    # below, so null values should be marked not_applicable rather than missing.
    non_electrical_consumable = any(term in applicability_text for term in (
        "sanding belt", "sanding disc", "sandpaper", "abrasive paper",
        "abrasive sheet", "flap disc", "grinding wheel",
        "cut-off wheel", "cut off wheel", "cutoff wheel",
        "cut-off disc", "cut off disc", "cutoff disc",
        "psa disc", "abrasive disc",
    ))

    if non_electrical_consumable:
        not_applicable_fields = {
            "voltage_rating", "current_rating", "ip_rating", "connector_type",
            "operating_temperature_min", "operating_temperature_max",
            "material", "certifications", "mounting_type",
        }
        for field_name in not_applicable_fields:
            field_obj = fields_dict[field_name]
            if field_obj.value is None:
                field_obj.validation_status = "not_applicable"
                field_obj.validation_message = (
                    f"{field_obj.label} is not applicable to the resolved product category "
                    f"'{record.category.value}'."
                )

    # 3. Voltage Rating Range Check (0 - 1000 V)
    # voltage_rating.value can now also be a preserved range string such as
    # "10-30" or "10–30" (see extractor.py's voltage heuristic), produced when
    # the source expressed a min...max rating rather than a single number.
    # Both endpoints are validated against the same plausible envelope instead
    # of the field being incorrectly rejected as "not a valid number".
    v_field = record.voltage_rating
    if v_field.value is not None:
        v_text = str(v_field.value).strip()

        # Accept grounded multi-platform cordless-tool voltage specifications
        # such as "12V/20V MAX", "12 V / 20 V", or "18V/20V".
        # Every declared voltage is still validated against the same 0-1000 V envelope.
        multi_voltage_match = re.match(
            r"^\s*(-?\d+(?:\.\d+)?)\s*V?\s*/\s*(-?\d+(?:\.\d+)?)\s*V?(?:\s+MAX)?\s*$",
            v_text,
            re.IGNORECASE,
        )

        if multi_voltage_match:
            voltages = [float(multi_voltage_match.group(1)), float(multi_voltage_match.group(2))]
            if any(v < 0 or v > 1000 for v in voltages):
                v_field.validation_status = "flagged_validation_error"
                v_field.validation_message = (
                    f"Voltage values ({voltages[0]}V/{voltages[1]}V) outside plausible industrial range "
                    "(0V - 1000V)."
                )
        else:
            range_match = re.match(r"^\s*(-?\d+(?:\.\d+)?)\s*[-–—]\s*(-?\d+(?:\.\d+)?)\s*$", v_text)
            if range_match:
                v_lo, v_hi = float(range_match.group(1)), float(range_match.group(2))
                if v_lo < 0 or v_lo > 1000 or v_hi < 0 or v_hi > 1000:
                    v_field.validation_status = "flagged_validation_error"
                    v_field.validation_message = f"Voltage range ({v_lo}V-{v_hi}V) outside plausible industrial range (0V - 1000V)."
            else:
                try:
                    v_val = float(v_field.value)
                    if v_val < 0 or v_val > 1000:
                        v_field.validation_status = "flagged_validation_error"
                        v_field.validation_message = f"Voltage value ({v_val}V) outside plausible industrial range (0V - 1000V)."
                except (ValueError, TypeError):
                    v_field.validation_status = "flagged_validation_error"
                    v_field.validation_message = f"Voltage rating '{v_field.value}' is not a valid number."

    # 4. Current Rating Range Check (0 - 200 A)
    c_field = record.current_rating
    if c_field.value is not None:
        try:
            c_val = float(c_field.value)
            if c_val < 0 or c_val > 200:
                c_field.validation_status = "flagged_validation_error"
                c_field.validation_message = f"Current value ({c_val}A) outside plausible range (0A - 200A)."
        except (ValueError, TypeError):
            c_field.validation_status = "flagged_validation_error"
            c_field.validation_message = f"Current rating '{c_field.value}' is not a valid number."

    # 5. IP Rating Format Check & Domain Rules
    ip_field = record.ip_rating
    if ip_field.value is not None:
        # Normalize away ALL formatting noise (spaces, underscores, hyphens) --
        # not just hyphens -- so 'IP 67', 'ip67', 'IP_67' and 'IP-67' all
        # canonicalize to the same 'IP67' before the whitelist check. Genuinely
        # invalid codes (e.g. 'IP99') still fail the membership check below
        # exactly as before -- this only normalizes formatting, never widens
        # what counts as valid.
        ip_clean = re.sub(r"[\s_\-]", "", str(ip_field.value).strip().upper())
        if ip_clean not in VALID_IP_RATINGS:
            ip_field.validation_status = "flagged_validation_error"
            ip_field.validation_message = f"Invalid IP Code format '{ip_field.value}'. Expected standard codes like IP67, IP68, IP65, IP20."
        else:
            ip_field.value = ip_clean

    # 6. Operating Temperature Min/Max & Logical Consistency Check
    t_min_field = record.operating_temperature_min
    t_max_field = record.operating_temperature_max

    if t_min_field.value is not None and t_max_field.value is not None:
        try:
            t_min = float(t_min_field.value)
            t_max = float(t_max_field.value)
            if t_min > t_max:
                t_min_field.validation_status = "flagged_validation_error"
                t_min_field.validation_message = f"Contradiction: Min temperature ({t_min}°C) cannot exceed max temperature ({t_max}°C)."
                t_max_field.validation_status = "flagged_validation_error"
                t_max_field.validation_message = f"Contradiction: Max temperature ({t_max}°C) is less than min temperature ({t_min}°C)."
        except (ValueError, TypeError):
            pass

    if t_min_field.value is not None:
        try:
            if float(t_min_field.value) < -60 or float(t_min_field.value) > 100:
                t_min_field.validation_status = "flagged_validation_error"
                t_min_field.validation_message = f"Operating temp min ({t_min_field.value}°C) is out of plausible industrial limits (-60°C to 100°C)."
        except (ValueError, TypeError):
            pass

    if t_max_field.value is not None:
        try:
            if float(t_max_field.value) < -20 or float(t_max_field.value) > 250:
                t_max_field.validation_status = "flagged_validation_error"
                t_max_field.validation_message = f"Operating temp max ({t_max_field.value}°C) is out of plausible industrial limits (-20°C to 250°C)."
        except (ValueError, TypeError):
            pass

    # 7. Contradiction Detection Rules
    if any(k in raw_lower for k in ["submersible", "underwater", "deep well"]):
        if ip_field.value in ["IP20", "IP40", "IP54"]:
            ip_field.validation_status = "flagged_validation_error"
            ip_field.validation_message = f"Critical Contradiction: Submersible/underwater application declared, but IP rating is indoor-only '{ip_field.value}'."

    conn_type_str = str(record.connector_type.value or "").upper()
    if ("M8" in conn_type_str or "M12" in conn_type_str) and c_field.value is not None:
        try:
            if float(c_field.value) > 20:
                c_field.validation_status = "flagged_validation_error"
                c_field.validation_message = f"Contradiction: Compact {conn_type_str} connector specified with unsafe current rating ({c_field.value}A > 20A)."
        except (ValueError, TypeError):
            pass

    # 8. Confidence Score Threshold Check (Includes extra_attributes now)
    # NOTE: statuses set upstream that must be preserved and never promoted to
    # "verified" here: rule errors, missing, ungrounded evidence, AI-inferred,
    # and values withheld pending human review ("needs_review" -- e.g. a
    # generic material mention that is not specific enough to justify a
    # catalog value such as "Nickel-plated brass").
    PROTECTED = {"flagged_validation_error", "missing", "not_applicable", "flagged_ungrounded", "ai_enriched", "needs_review"}
    all_fields = list(fields_dict.values()) + list(record.extra_attributes.values())
    for field_obj in all_fields:
        if field_obj.validation_status not in PROTECTED:
            if field_obj.value is not None:
                # A value can only be verified if its evidence was located in the
                # source. is_grounded is None for the exempt product_name field.
                is_grounded = getattr(field_obj, "is_grounded", None)
                if is_grounded is False:
                    field_obj.validation_status = "flagged_ungrounded"
                    field_obj.validation_message = "Value could not be traced to a verbatim snippet in the source text (possible hallucination). Human review required."
                elif field_obj.confidence_score < CONFIDENCE_THRESHOLD:
                    field_obj.validation_status = "flagged_low_confidence"
                    field_obj.validation_message = f"AI confidence score ({int(field_obj.confidence_score*100)}%) is below verification threshold (65%). Human review required."
                else:
                    field_obj.validation_status = "verified"
                    field_obj.validation_message = "Passed deterministic rule validation, evidence grounding, and confidence threshold."

    # Recalculate summary stats. Non-applicable fields do not make a product
    # incomplete and are excluded from the completeness denominator.
    applicable_fields = [f for f in all_fields if f.validation_status != "not_applicable"]
    total = len(applicable_fields)
    verified = sum(1 for f in applicable_fields if f.validation_status == "verified")
    flagged = sum(1 for f in applicable_fields if f.validation_status in ["flagged_low_confidence", "flagged_validation_error", "flagged_ungrounded", "needs_review"])
    missing = sum(1 for f in applicable_fields if f.validation_status == "missing")
    enriched = sum(1 for f in applicable_fields if f.validation_status == "ai_enriched")

    scores = [f.confidence_score for f in applicable_fields if f.value is not None]
    avg_confidence = round(sum(scores) / len(scores), 2) if scores else 0.0

    record.total_fields = total
    record.verified_fields_count = verified
    record.flagged_fields_count = flagged
    record.missing_fields_count = missing
    record.enriched_fields_count = enriched
    record.overall_confidence = avg_confidence

    if flagged > 0 or missing > 0 or enriched > 0:
        record.overall_status = "needs_review"
    else:
        record.overall_status = "verified"

    return record