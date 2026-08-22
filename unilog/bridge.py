"""
bridge.py — the join between PRISM's extraction engine and the Unilog layer.
===========================================================================

This is the piece that was missing. PRISM extracts attributes with evidence,
confidence and a grounding verdict; `unilog/descriptions.py` builds the five
commerce formats from a `ProductRecord`. Until now nothing connected them, so
descriptions were only ever built from hand-written demo records.

`to_product_record()` closes that gap, and it does so through the trust layer
rather than around it.

Why the trust gate matters here
-------------------------------
The guidelines are explicit that a fluent description made of invented values
scores zero. So the bridge does not pass everything it is given. Each candidate
attribute must clear `MIN_CONFIDENCE` and must not carry a status that means
"we could not stand behind this":

    verified                 -> admitted
    flagged_low_confidence   -> admitted only if confidence >= MIN_CONFIDENCE
    ai_enriched              -> withheld by default (inferred, not sourced)
    flagged_ungrounded       -> always withheld (evidence not found in source)
    flagged_validation_error  -> always withheld (failed a deterministic rule)
    missing                  -> nothing to pass

Every withheld field is recorded in `BridgeReport.withheld` with the reason, so
the omission is reportable rather than silent. That turns PRISM's grounding work
into a measurable input-quality gate on the description pipeline instead of a
badge in the UI.

`include_enriched=True` relaxes the AI-inference rule for demos, but the report
still marks those attributes so they never masquerade as sourced values.
"""
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from . import descriptions as desc_mod

# Statuses that may contribute a value to a commerce description.
ADMISSIBLE_STATUSES = {"verified", "flagged_low_confidence"}
# Never admitted: the evidence could not be located, or a rule rejected it.
BLOCKED_STATUSES = {"flagged_ungrounded", "flagged_validation_error", "missing"}

MIN_CONFIDENCE = 0.65

# PRISM field key -> (description label, treat as key attribute in the title)
FIELD_TO_ATTRIBUTE: Dict[str, Tuple[str, bool]] = {
    "voltage_rating": ("Voltage", True),
    "current_rating": ("Current", True),
    "ip_rating": ("Enclosure Rating", True),
    "connector_type": ("Connector Type", True),
    "material": ("Material", True),
    "mounting_type": ("Mounting", True),
    "certifications": ("Certifications", False),
    "operating_temperature_min": ("Min Operating Temperature", False),
    "operating_temperature_max": ("Max Operating Temperature", False),
}

# Fields consumed as record identity rather than as attributes.
IDENTITY_FIELDS = {"product_name", "category"}

# Dynamic identity/identifier fields belong in dedicated export columns, not
# in customer-facing description prose. Keeping them out also prevents the UOM
# normalizer from misreading an MPN suffix (for example the final "G" in
# DCB518ASTS06G) as a measurement unit.
DESCRIPTION_EXCLUDED_EXTRA_KEYS = {
    "brand", "manufacturer", "manufacturer_name", "mfr",
    "mpn", "manufacturer_part_number", "mfg_part_num", "part_number",
    "model", "model_no", "model_number",
    "sku", "stock_no", "stock_number", "item_number",
    "upc", "ean", "gtin", "barcode",
    "http", "https", "url", "source_url", "product_url",
    "estimated_arrival", "estimated_arrival_on", "arrival_date",
    "availability", "available", "ship_to_store", "shipping",
}


def _normalized_extra_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")


def is_non_product_extra(raw_key: Any, label: Any, value: Any) -> bool:
    keys = {_normalized_extra_key(raw_key), _normalized_extra_key(label)}
    return bool(keys & DESCRIPTION_EXCLUDED_EXTRA_KEYS) or bool(
        re.match(r"^https?://", str(value or "").strip(), flags=re.IGNORECASE)
    )


@dataclass
class BridgeReport:
    """Audit trail for one conversion: what was admitted, what was withheld and why."""
    admitted: List[str] = field(default_factory=list)
    withheld: List[Dict[str, Any]] = field(default_factory=list)
    inferred_admitted: List[str] = field(default_factory=list)

    @property
    def admitted_count(self) -> int:
        return len(self.admitted)

    @property
    def withheld_count(self) -> int:
        return len(self.withheld)

    @property
    def trust_gate_pass_pct(self) -> float:
        """% of populated fields that were good enough to publish."""
        total = self.admitted_count + self.withheld_count
        return round((self.admitted_count / total) * 100, 1) if total else 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "attributes_admitted": self.admitted_count,
            "attributes_withheld": self.withheld_count,
            "trust_gate_pass_pct": self.trust_gate_pass_pct,
            "admitted": self.admitted,
            "withheld": self.withheld,
            "ai_inferred_admitted": self.inferred_admitted,
        }


def _field_of(record: Any, key: str) -> Any:
    """Read a field from either a pydantic record or a plain dict."""
    if isinstance(record, dict):
        return record.get(key)
    return getattr(record, key, None)


def _attr_of(obj: Any, key: str, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _description_unit(value: Any, unit: Any, label: Any = None, snippet: Any = None) -> Optional[Any]:
    """Suppress a unit when that same unit is already embedded in the value."""
    if not unit:
        return unit
    text = str(value or "").strip()
    u = str(unit).strip()
    if _normalized_extra_key(u) == _normalized_extra_key(label) or u.lower() == "grit":
        return None
    evidence = str(snippet or "")
    if re.search(r'(?:\b(?:inches|inch|in)\b|[\"″])', evidence, re.IGNORECASE):
        if u.lower() in {"mm", "millimeter", "millimeters"}:
            return "in"
    if not text or not u:
        return unit

    # Handles both "230 V" and "230V"; avoids false positives like A in MAX.
    if re.search(rf"(?i)(?:^|[\s\d/]){re.escape(u)}(?=\b|\s|/|$)", text):
        return None
    return unit


def _admissible(fobj: Any, include_enriched: bool) -> Tuple[bool, Optional[str]]:
    """Decide whether one extracted field may reach a description.

    Returns (ok, reason_if_not).
    """
    value = _attr_of(fobj, "value")
    if value is None or str(value).strip() == "":
        return False, "no value extracted"

    status = str(_attr_of(fobj, "validation_status", "missing"))
    try:
        conf = float(_attr_of(fobj, "confidence_score", 0.0) or 0.0)
    except (TypeError, ValueError):
        conf = 0.0

    if status in BLOCKED_STATUSES:
        if status == "flagged_ungrounded":
            return False, "evidence could not be located in the source (possible hallucination)"
        if status == "flagged_validation_error":
            return False, f"failed a deterministic validation rule: {_attr_of(fobj, 'validation_message') or 'rule error'}"
        return False, "field is missing"

    if status == "ai_enriched":
        if not include_enriched:
            return False, "value was AI-inferred, not sourced — withheld from commerce content"
        return True, None

    if status not in ADMISSIBLE_STATUSES:
        return False, f"unrecognised validation status '{status}'"

    if conf < MIN_CONFIDENCE:
        return False, f"confidence {conf:.2f} below the {MIN_CONFIDENCE} publication threshold"

    return True, None


def to_product_record(record: Any,
                      brand: Optional[str] = None,
                      manufacturer: Optional[str] = None,
                      series: Optional[str] = None,
                      mpn: Optional[str] = None,
                      item_type: Optional[str] = None,
                      include_enriched: bool = False,
                      include_dynamic: bool = True) -> Tuple["desc_mod.ProductRecord", BridgeReport]:
    """Convert a PRISM ProductIntelligenceRecord into a description-ready record.

    Anything not supplied explicitly is taken from the extracted record, so the
    caller can override identity (brand/MPN from the master data) while letting
    the attributes come from extraction.
    """
    report = BridgeReport()
    attributes: List[desc_mod.Attribute] = []

    # ---- the eleven fixed fields -----------------------------------------
    for key, (label, is_key) in FIELD_TO_ATTRIBUTE.items():
        fobj = _field_of(record, key)
        ok, why = _admissible(fobj, include_enriched)
        if not ok:
            # Only report fields that actually held something; genuinely absent
            # fields are not interesting.
            if why != "no value extracted" and why != "field is missing":
                report.withheld.append({"attribute": label, "field": key, "reason": why})
            continue

        attributes.append(desc_mod.Attribute(
            label=label,
            value=_attr_of(fobj, "value"),
            unit=_description_unit(_attr_of(fobj, "value"), _attr_of(fobj, "unit")),
            is_key=is_key,
        ))
        report.admitted.append(label)
        if str(_attr_of(fobj, "validation_status")) == "ai_enriched":
            report.inferred_admitted.append(label)

    # ---- dynamic attributes discovered beyond the fixed schema -----------
    if include_dynamic:
        extras = _field_of(record, "extra_attributes") or {}
        seen_values = {
            _normalized_extra_key(a.value) for a in attributes
            if _normalized_extra_key(a.value)
        }
        category_obj = _field_of(record, "category")
        identity_values = {
            _normalized_extra_key(v) for v in (
                brand, manufacturer, series, mpn,
                _attr_of(category_obj, "value"),
            ) if _normalized_extra_key(v)
        }
        if isinstance(extras, dict):
            for raw_key, fobj in extras.items():
                label = str(_attr_of(fobj, "label", None) or raw_key).replace("_", " ").strip().title()
                if is_non_product_extra(raw_key, label, _attr_of(fobj, "value")):
                    continue
                normalized_value = _normalized_extra_key(_attr_of(fobj, "value"))
                if normalized_value and normalized_value in (seen_values | identity_values):
                    continue
                ok, why = _admissible(fobj, include_enriched)
                if not ok:
                    if why not in ("no value extracted", "field is missing"):
                        report.withheld.append({"attribute": label, "field": raw_key, "reason": why})
                    continue
                attributes.append(desc_mod.Attribute(
                    label=label,
                    value=_attr_of(fobj, "value"),
                    unit=_description_unit(
                        _attr_of(fobj, "value"), _attr_of(fobj, "unit"), label,
                        _attr_of(fobj, "source_snippet"),
                    ),
                    is_key=False,
                ))
                if normalized_value:
                    seen_values.add(normalized_value)
                report.admitted.append(label)
                if str(_attr_of(fobj, "validation_status")) == "ai_enriched":
                    report.inferred_admitted.append(label)

    # ---- identity --------------------------------------------------------
    if item_type is None:
        cat = _field_of(record, "category")
        ok, _ = _admissible(cat, include_enriched=True)
        if ok:
            item_type = str(_attr_of(cat, "value")).strip()

    if mpn is None:
        pname = _field_of(record, "product_name")
        raw_name = _attr_of(pname, "value")
        if raw_name:
            mpn = _extract_mpn_from_name(str(raw_name))

    rec = desc_mod.ProductRecord(
        brand=brand,
        manufacturer=manufacturer,
        series=series,
        mpn=mpn,
        item_type=item_type,
        attributes=attributes,
    )
    return rec, report


def _extract_mpn_from_name(name: str) -> Optional[str]:
    """Pull a part-number-looking token out of an extracted product name.

    `_guess_product_name` in the extractor produces names like
    "CN-M12-5P Circular Connector"; the leading token is the MPN.
    """
    import re
    m = re.match(r"\s*([A-Z0-9]{2,}(?:[-/][A-Z0-9]+)+)", str(name).upper())
    if m:
        return m.group(1)
    return None


def build_from_prism(record: Any,
                     normalizer=None,
                     brand: Optional[str] = None,
                     manufacturer: Optional[str] = None,
                     series: Optional[str] = None,
                     mpn: Optional[str] = None,
                     item_type: Optional[str] = None,
                     include_enriched: bool = False) -> Dict[str, Any]:
    """One-call convenience: PRISM record -> all five descriptions + audit.

    Returns the five formats, the char-limit compliance summary, and the trust
    report explaining anything that was withheld.
    """
    rec, report = to_product_record(
        record, brand=brand, manufacturer=manufacturer, series=series,
        mpn=mpn, item_type=item_type, include_enriched=include_enriched,
    )
    built = desc_mod.build_all_descriptions(rec, normalizer)
    return {
        "descriptions": built,
        "compliance": desc_mod.compliance_summary(built),
        "trust_report": report.as_dict(),
        "record": {
            "brand": rec.effective_brand(),
            "mpn": rec.mpn,
            "item_type": rec.item_type,
            "attribute_count": len(rec.attributes),
        },
    }
