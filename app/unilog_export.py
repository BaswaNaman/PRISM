"""
unilog_export.py — PRISM ProductIntelligenceRecord -> Unilog delivery format.
==============================================================================

This module is the *last* step of the pipeline:

    input -> discovery/ingestion -> extraction -> validation/enrichment
          -> ProductIntelligenceRecord -> save DB -> [this module] -> CSV/XLSX

It never re-runs discovery, extraction, validation, or enrichment. It takes an
already-processed `ProductIntelligenceRecord` and reshapes it into one row of
the Unilog delivery schema, whose exact header order lives in
`data/unilog_expected_output_headers.csv` (read at runtime, never hand-typed
here, so a template change never silently drifts out of sync with this code).

What this module reuses instead of reimplementing
---------------------------------------------------
* `unilog.bridge.build_from_prism()` — the five commerce description strings
  (Invoice/Mobile/Title/Long/Web) and MPN-from-name extraction. The factual web
  description is delivered through `MARKETING_DESCRIPTION`; it contains only
  trust-gated source facts and does not add promotional claims.
* `unilog.classify.ItemTypeClassifier` — `Classpath`, and `Dept`/`Class`/
  `Fine` derived by splitting the classpath on `>` (matches the sample data).
* `unilog.uom` — unit-abbreviation normalization and numeric cleanup, reused
  via `uom.DEFAULT`, the same normalizer object `descriptions.py` uses.
* `app.sourcing.evaluate_url()` — the manufacturer/distributor/marketplace
  classifier, reused (not reimplemented) to decide whether a discovery URL is
  allowed into `MFR URL`.

Important note on the two different "what counts as good data" rules in this
file: `bridge.build_from_prism()` applies its own, *stricter* admissibility
gate before a value reaches a description string (min confidence, excludes
ai_enriched by default, etc.) — that gate is unrelated to and intentionally
untouched by this module, because generated prose is a different risk than a
raw attribute value. For everything else (fixed identity columns, and the
dynamic `ATTRIBUTE_LABEL/VALUE/UOM N` slots), the only rule this module
applies is the one specified for the export adapter itself: skip a field
if its `validation_status` is "missing" or "not_applicable", or it has no
value. ("not_applicable" is emitted by the current applicability logic and is
intentionally excluded from delivery output.)

MFR URL policy (do not change without re-reading this)
--------------------------------------------------------
`record.source_origin` / `record.fetch_metadata` are provenance text, not a
confirmed manufacturer identity — PRISM can select a distributor page under
the `allow_distributors` sourcing policy, so "the URL PRISM fetched from" is
never, by itself, a manufacturer URL. `MFR URL` is therefore populated only
from caller-supplied information, in this strict priority order:

    1. an explicit `manufacturer_url` the caller supplies
    2. a `discovery_source_url` the caller supplies, but ONLY if
       `sourcing.evaluate_url()` positively classifies it as
       category == "manufacturer"
    3. otherwise, blank

Nothing is ever parsed out of `record.source_origin` and written here.
"""

from __future__ import annotations

import csv
import io
import os
import re
from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, List, Optional, Tuple

from app.schema import ProductIntelligenceRecord
from app import sourcing
from unilog import bridge as bridge_mod
from unilog import classify as classify_mod
from unilog import lov as lov_mod
from unilog import uom as uom_mod

# ---------------------------------------------------------------------------
# Header template
# ---------------------------------------------------------------------------
# The canonical, byte-exact header row copied from the Unilog Expected Output
# CSV. Read once and cached; never hand-maintained as a Python literal, so the
# header order in this module can never silently diverge from the template.
DEFAULT_HEADERS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "unilog_expected_output_headers.csv",
)

_HEADERS_CACHE: Optional[List[str]] = None


def load_expected_headers(path: Optional[str] = None, force_reload: bool = False) -> List[str]:
    """Read the delivery-schema header row, exactly, preserving order.

    Cached for the default path only, so repeated calls (e.g. one per row in
    a batch export) don't re-open the file. Pass an explicit `path` to bypass
    the cache (used by tests).
    """
    global _HEADERS_CACHE
    if path is None and _HEADERS_CACHE is not None and not force_reload:
        return list(_HEADERS_CACHE)

    target = path or DEFAULT_HEADERS_PATH
    with open(target, "r", encoding="utf-8-sig", newline="") as fh:
        headers = next(csv.reader(fh))
    headers = [h.strip() for h in headers]

    if path is None:
        _HEADERS_CACHE = list(headers)
    return headers


_ATTR_LABEL_RE = re.compile(r"^ATTRIBUTE_LABEL (\d+)$")


def _attribute_slot_numbers(headers: List[str]) -> List[int]:
    """Discover which numbered attribute slots actually exist in the header
    row, instead of assuming a fixed count. Sorted ascending."""
    nums = []
    for h in headers:
        m = _ATTR_LABEL_RE.match(h)
        if m:
            nums.append(int(m.group(1)))
    return sorted(nums)


# ---------------------------------------------------------------------------
# Export policy constants
# ---------------------------------------------------------------------------
# Per spec: skip a field only for these statuses (plus "no value at all",
# checked separately) -- every other status (flagged_*, ai_enriched,
# needs_review, verified) is still exported if it carries a value.
SKIP_STATUSES = {"missing", "not_applicable"}

# PRISM field key -> already consumed by a fixed delivery column, so it is
# never also placed in a numbered attribute slot.
FIXED_CONSUMED_CORE_KEYS = {"product_name", "certifications", "category"}

# Remaining core fields with no dedicated delivery column of their own: these
# are packed into the numbered attribute slots, in this order, ahead of
# extra_attributes. (`category` is consumed as classification input only —
# see FIXED_CONSUMED_CORE_KEYS — not duplicated into a slot.)
DYNAMIC_CORE_FIELD_KEYS = [
    "voltage_rating",
    "current_rating",
    "ip_rating",
    "connector_type",
    "operating_temperature_min",
    "operating_temperature_max",
    "material",
    "mounting_type",
]

# unilog.bridge / unilog.descriptions field_name -> delivery column.
DESC_FIELD_TO_COLUMN = {
    "Invoice Desc": "INVOICE_DESC",
    "Mobile Desc": "MOBILE_DESC",
    "Product Title / Short Desc": "SHORT_DESC",
    "Long Description": "LONG_DESC1",
    "Web / Online Desc": "MARKETING_DESCRIPTION",
}

# extra_attributes key/label (normalized: lowercased, spaces/underscores/
# hyphens stripped) -> (value column, uom column or None). When a match is
# found the entry is routed here INSTEAD OF a generic numbered slot, so it is
# never written twice. This is a best-effort heuristic: PRISM's extractor has
# no dedicated manufacturer/brand/logistics fields today, so these only fire
# if such a key happens to land in extra_attributes.
LOGISTICS_ALIASES: Dict[str, Tuple[str, Optional[str]]] = {
    "weight": ("WEIGHT", "WEIGHT_UOM"),
    "mass": ("WEIGHT", "WEIGHT_UOM"),
    "netweight": ("WEIGHT", "WEIGHT_UOM"),
    "itemweight": ("WEIGHT", "WEIGHT_UOM"),
    "upc": ("UPC", None),
    "itemupc": ("UPC", None),
    "ean": ("EAN", None),
    "ean13": ("EAN", None),
    "gtin": ("GTIN", None),
    "gtin12": ("GTIN", None),
    "gtin13": ("GTIN", None),
    "gtin14": ("GTIN", None),
    "unspsc": ("UNSPSC", None),
    "countryoforigin": ("Country Of Origin", None),
    "country": ("Country Of Origin", None),
    "length": ("LENGTH", "LENGTH_UOM"),
    "width": ("WIDTH", "WIDTH_UOM"),
    "height": ("HEIGHT", "HEIGHT_UOM"),
    "volume": ("VOLUME", "VOLUME_UOM"),
}

IDENTITY_ALIASES = {
    "manufacturer": {"manufacturer", "manufacturername", "mfr", "mfrname"},
    "brand": {"brand", "brandname"},
    "mpn": {"mpn", "mfgpartnum", "manufacturerpartnumber", "manufacturerpartno", "partnumber", "modelnumber", "modelno"},
}


def _normalize_key(s: Any) -> str:
    return re.sub(r"[\s_\-]+", "", str(s or "").strip().lower())


def _identity_key(value: Any) -> str:
    """Comparison key for a small, explicit identity alias table."""
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


_CANONICAL_BRANDS = {
    "dewalt": "DEWALT",
    "dewlt": "DEWALT",
    "blackdecker": "BLACK+DECKER",
    "blackanddecker": "BLACK+DECKER",
}

_CANONICAL_MANUFACTURERS = {
    "stanleyblackdecker": "Stanley Black & Decker",
    "stanleyblackanddecker": "Stanley Black & Decker",
    "blackdeckerdewalt": "Stanley Black & Decker",
    "blackanddeckerdewalt": "Stanley Black & Decker",
    "blackdeckerdewlt": "Stanley Black & Decker",
    "blackanddeckerdewlt": "Stanley Black & Decker",
}


def _normalize_identity_values(manufacturer: Any, brand: Any) -> Tuple[str, str]:
    """Normalize only known aliases; unknown identities pass through unchanged."""
    raw_manufacturer = str(manufacturer or "").strip()
    raw_brand = str(brand or "").strip()
    normalized_brand = _CANONICAL_BRANDS.get(_identity_key(raw_brand), raw_brand)
    normalized_manufacturer = _CANONICAL_MANUFACTURERS.get(
        _identity_key(raw_manufacturer), raw_manufacturer
    )
    return normalized_manufacturer, normalized_brand


_HOURS_THEN_CAPACITY_RE = re.compile(
    r"(?P<hours>\d+(?:\.\d+)?)\s*(?:hours?|hrs?)\b.{0,45}?"
    r"(?P<capacity>\d+(?:\.\d+)?)\s*Ah\b",
    re.IGNORECASE,
)
_CAPACITY_THEN_HOURS_RE = re.compile(
    r"(?P<capacity>\d+(?:\.\d+)?)\s*Ah\b.{0,45}?"
    r"(?P<hours>\d+(?:\.\d+)?)\s*(?:hours?|hrs?)\b",
    re.IGNORECASE,
)


def _runtime_claims(text: str) -> List[Tuple[float, float]]:
    """Return (battery capacity Ah, runtime hours) claims in one feature."""
    claims: List[Tuple[float, float]] = []
    for pattern in (_HOURS_THEN_CAPACITY_RE, _CAPACITY_THEN_HOURS_RE):
        for match in pattern.finditer(text):
            claim = (float(match.group("capacity")), float(match.group("hours")))
            if claim not in claims:
                claims.append(claim)
    return claims


def _conflicting_runtime_feature_indexes(features: List[str]) -> set:
    """Reject every feature that disagrees on runtime for one battery size."""
    by_capacity: Dict[float, Dict[float, set]] = {}
    for index, text in enumerate(features):
        for capacity, hours in _runtime_claims(text):
            by_capacity.setdefault(capacity, {}).setdefault(hours, set()).add(index)
    rejected = set()
    for by_hours in by_capacity.values():
        if len(by_hours) > 1:
            for indexes in by_hours.values():
                rejected.update(indexes)
    return rejected


_NON_PRODUCT_DYNAMIC_KEYS = {
    "http", "https", "url", "sourceurl", "producturl",
    "estimatedarrival", "estimatedarrivalon", "arrivaldate",
    "availability", "available", "shiptostore", "shipping",
}


def _is_non_product_dynamic(raw_key: Any, label: Any, value: Any) -> bool:
    return (
        _normalize_key(raw_key) in _NON_PRODUCT_DYNAMIC_KEYS
        or _normalize_key(label) in _NON_PRODUCT_DYNAMIC_KEYS
        or bool(re.match(r"^https?://", str(value or "").strip(), re.IGNORECASE))
    )


def _safe_attribute_unit(field_obj: Any, label: Any) -> Optional[Any]:
    """Suppress semantic pseudo-units and prefer units explicit in evidence."""
    unit = getattr(field_obj, "unit", None)
    if not unit:
        return unit
    if _normalize_key(unit) == _normalize_key(label) or str(unit).lower() == "grit":
        return None
    snippet = str(getattr(field_obj, "source_snippet", None) or "")
    if (str(unit).lower() in {"mm", "millimeter", "millimeters"}
            and re.search(r'(?:\b(?:inches|inch|in)\b|[\"″])', snippet, re.IGNORECASE)):
        return "in"
    return unit


# ---------------------------------------------------------------------------
# Value formatting -- reuses unilog.uom's numeric/unit helpers rather than
# re-implementing number cleanup.
# ---------------------------------------------------------------------------
def _clean_number(value: Any, normalizer) -> str:
    try:
        return normalizer._clean_number(value)  # reuse, not duplicate
    except Exception:
        return str(value)


def _format_value(value: Any, normalizer) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        parts = [_format_value(v, normalizer) for v in value]
        return " | ".join(p for p in parts if p.strip())
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (int, float)):
        return _clean_number(value, normalizer)
    return str(value).strip()


def _normalize_uom(unit: Any, normalizer) -> str:
    if not unit:
        return ""
    try:
        approved, _note = normalizer.normalize_unit(unit)  # reuse, not duplicate
        return approved or str(unit)
    except Exception:
        return str(unit)


def _is_exportable(field_obj: Any) -> bool:
    """The one inclusion rule the export adapter applies itself: skip
    missing/not_applicable status, or no value at all. Nothing else -- a
    flagged or AI-enriched value with a real value is still exported."""
    if field_obj is None:
        return False
    status = getattr(field_obj, "validation_status", None)
    if status in SKIP_STATUSES:
        return False
    value = getattr(field_obj, "value", None)
    if value is None:
        return False
    if isinstance(value, str) and not value.strip():
        return False
    return True


def _find_exportable_extra(record: ProductIntelligenceRecord, aliases: set) -> Optional[Any]:
    """Return the first exportable extra attribute whose key/label matches aliases."""
    for raw_key, fobj in (getattr(record, "extra_attributes", None) or {}).items():
        if not _is_exportable(fobj):
            continue
        label = getattr(fobj, "label", None) or ""
        if _normalize_key(raw_key) in aliases or _normalize_key(label) in aliases:
            return fobj
    return None


_MPN_FORBIDDEN_TOKEN_RE = re.compile(r"\b(?:UPC|EAN|GTIN)\b", re.IGNORECASE)

def _is_safe_mpn_candidate(value: Any) -> bool:
    """Reject obviously contaminated identifier strings before export."""
    if value is None:
        return False
    text = str(value).strip()
    if not text or len(text) > 80 or "\n" in text or "\r" in text:
        return False
    if _MPN_FORBIDDEN_TOKEN_RE.search(text):
        return False
    return True


def _find_safe_mpn_extra(record: ProductIntelligenceRecord) -> Optional[Any]:
    """Return the first exportable clean MPN/model-number extra attribute."""
    aliases = IDENTITY_ALIASES["mpn"]
    for raw_key, fobj in (getattr(record, "extra_attributes", None) or {}).items():
        if not _is_exportable(fobj):
            continue
        label = getattr(fobj, "label", None) or ""
        if _normalize_key(raw_key) not in aliases and _normalize_key(label) not in aliases:
            continue
        if _is_safe_mpn_candidate(getattr(fobj, "value", None)):
            return fobj
    return None


_EMBEDDED_ATTRIBUTE_RE = re.compile(
    r"\b(?:package|item|product|overall)\s+"
    r"(?:height|width|length|weight|depth|diameter)\b\s*[:=]?\s*[-+]?\d",
    re.IGNORECASE,
)

def _looks_like_merged_attribute_value(value: Any) -> bool:
    """Detect conservative scraper-merged Label/Value tails."""
    if not isinstance(value, str):
        return False
    return bool(_EMBEDDED_ATTRIBUTE_RE.search(value))


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------
@dataclass
class ExportResult:
    """One record's export row, plus a diagnostics trail (never written to
    the CSV/XLSX itself -- useful for the API response and for tests)."""
    row: Dict[str, str]
    headers: List[str]
    warnings: List[str] = dc_field(default_factory=list)
    slots_used: int = 0
    slots_available: int = 0

    def ordered_values(self) -> List[str]:
        return [self.row.get(h, "") for h in self.headers]


# ---------------------------------------------------------------------------
# Core: one record -> one row
# ---------------------------------------------------------------------------
def build_export_row(
    record: ProductIntelligenceRecord,
    *,
    headers: Optional[List[str]] = None,
    manufacturer: Optional[str] = None,
    brand: Optional[str] = None,
    series: Optional[str] = None,
    mpn_override: Optional[str] = None,
    manufacturer_url: Optional[str] = None,
    discovery_source_url: Optional[str] = None,
    include_enriched_descriptions: bool = False,
    registry: Optional[Any] = None,
) -> ExportResult:
    """Convert one already-processed PRISM record into one Unilog delivery row.

    Every argument beyond `record` is optional, caller-supplied context (e.g.
    from master data or a human reviewer) -- this function never invents
    values on its own, and every field it cannot populate stays blank.
    """
    headers = headers or load_expected_headers()
    row: Dict[str, str] = {h: "" for h in headers}
    warnings: List[str] = []
    nz = uom_mod.DEFAULT

    # ---- Product Name ------------------------------------------------
    pname_field = getattr(record, "product_name", None)
    product_name_value: Optional[str] = None
    if _is_exportable(pname_field):
        product_name_value = _format_value(pname_field.value, nz)
        if "Product Name" in row:
            row["Product Name"] = product_name_value

    # ---- Certifications -> Standard/Approvals -------------------------
    cert_field = getattr(record, "certifications", None)
    if _is_exportable(cert_field) and "Standard/Approvals" in row:
        row["Standard/Approvals"] = _format_value(cert_field.value, nz)

    # ---- Identity resolution -------------------------------------------
    # Original sparse-row MPN is authoritative identity input. Only if it is
    # unavailable do we fall back to a clean validated MPN/model-number extra,
    # then the conservative product-name parser.
    mpn = str(mpn_override or "").strip()
    if mpn and not _is_safe_mpn_candidate(mpn):
        warnings.append(f"caller-supplied MPN rejected as contaminated: {mpn!r}")
        mpn = ""

    if not mpn:
        extracted_mpn = _find_safe_mpn_extra(record)
        if extracted_mpn:
            mpn = _format_value(extracted_mpn.value, nz)

    if not mpn and product_name_value:
        try:
            parsed_mpn = bridge_mod._extract_mpn_from_name(product_name_value) or ""
            if _is_safe_mpn_candidate(parsed_mpn):
                mpn = parsed_mpn
        except Exception as exc:
            warnings.append(f"MPN extraction failed: {exc}")
    if mpn:
        for col in ("Mfg_Part_Num", "MANUFACTURER_PART_NUMBER"):
            if col in row:
                row[col] = mpn

    extracted_manufacturer = _find_exportable_extra(record, IDENTITY_ALIASES["manufacturer"])
    resolved_manufacturer = (
        _format_value(extracted_manufacturer.value, nz) if extracted_manufacturer else (manufacturer or "")
    )
    if resolved_manufacturer and "MANUFACTURER_NAME" in row:
        row["MANUFACTURER_NAME"] = resolved_manufacturer

    extracted_brand = _find_exportable_extra(record, IDENTITY_ALIASES["brand"])
    resolved_brand = _format_value(extracted_brand.value, nz) if extracted_brand else (brand or "")

    raw_manufacturer, raw_brand = resolved_manufacturer, resolved_brand
    resolved_manufacturer, resolved_brand = _normalize_identity_values(
        resolved_manufacturer, resolved_brand
    )
    if resolved_manufacturer != raw_manufacturer:
        warnings.append(
            f"normalized manufacturer {raw_manufacturer!r} to {resolved_manufacturer!r}"
        )
    if resolved_brand != raw_brand:
        warnings.append(f"normalized brand {raw_brand!r} to {resolved_brand!r}")

    if resolved_manufacturer and "MANUFACTURER_NAME" in row:
        row["MANUFACTURER_NAME"] = resolved_manufacturer
    if resolved_brand and "BRAND_NAME" in row:
        row["BRAND_NAME"] = resolved_brand

    # Use resolved identity for description generation too.
    manufacturer = resolved_manufacturer or manufacturer
    brand = resolved_brand or brand

    # ---- MFR URL: strict priority, reusing app.sourcing ----------------
    mfr_url = ""
    if manufacturer_url:
        mfr_url = manufacturer_url
    elif discovery_source_url:
        try:
            verdict = sourcing.evaluate_url(discovery_source_url)
            if getattr(verdict, "category", None) == "manufacturer":
                mfr_url = discovery_source_url
            else:
                warnings.append(
                    f"discovery_source_url not used for MFR URL: classified as "
                    f"'{getattr(verdict, 'category', 'unknown')}', not manufacturer."
                )
        except Exception as exc:
            warnings.append(f"could not evaluate discovery_source_url: {exc}")
    if mfr_url and "MFR URL" in row:
        row["MFR URL"] = mfr_url

    # ---- Classification: Classpath / Dept / Class / Fine ---------------
    # (reused from unilog.classify, not reimplemented)
    category_field = getattr(record, "category", None)
    category_value = category_field.value if _is_exportable(category_field) else None
    item_type: Optional[str] = None
    try:
        reg = registry if registry is not None else lov_mod.LOVRegistry()
        classifier = classify_mod.ItemTypeClassifier(
            reg, extra_types=[category_value] if category_value else None
        )
        classify_text = " ".join(str(x) for x in (product_name_value, category_value) if x).strip()
        cls = classifier.classify(classify_text) if classify_text else None
        if cls is not None:
            item_type = getattr(cls, "item_type", None) or category_value
            classpath = getattr(cls, "classpath", None)
            if classpath and "Classpath" in row:
                row["Classpath"] = classpath
                parts = [p.strip() for p in classpath.split(">") if p.strip()]
                if len(parts) >= 1 and "Dept" in row:
                    row["Dept"] = parts[0]
                if len(parts) >= 2 and "Class" in row:
                    row["Class"] = parts[1]
                if len(parts) >= 3 and "Fine" in row:
                    row["Fine"] = parts[2]
            elif item_type:
                # Preserve a verified source category as a flat class when no
                # client taxonomy is installed. Never invent hierarchy/UNSPSC.
                if "Classpath" in row:
                    row["Classpath"] = item_type
                if "Class" in row:
                    row["Class"] = item_type
                warnings.append(
                    "client taxonomy unavailable; exported verified item type as a flat classpath"
                )
        else:
            item_type = category_value
    except Exception as exc:
        warnings.append(f"classification unavailable: {exc}")
        item_type = category_value

    # ---- Descriptions (reused from unilog.bridge / unilog.descriptions) --
    # Note: build_from_prism() applies ITS OWN, stricter admissibility gate
    # before a value reaches these strings -- see module docstring.
    try:
        built = bridge_mod.build_from_prism(
            record,
            normalizer=nz,
            brand=brand,
            manufacturer=manufacturer,
            series=series,
            mpn=mpn,
            item_type=item_type,
            include_enriched=include_enriched_descriptions,
        )
        descriptions = built.get("descriptions", {}) or {}
        for desc_key, column in DESC_FIELD_TO_COLUMN.items():
            text = (descriptions.get(desc_key) or {}).get("text")
            if text and column in row:
                row[column] = text
    except Exception as exc:
        warnings.append(f"description build failed: {exc}")

    # ---- Item Features --------------------------------------------------
    features = [str(feature).strip() for feature in (getattr(record, "features", None) or [])]
    raw_source_text = str(getattr(record, "raw_input", None) or "")
    feature_number = 1
    conflicting_feature_indexes = _conflicting_runtime_feature_indexes(features)

    for feature_index, feature in enumerate(features):
        if feature_number > 20:
            break

        text = str(feature).strip()
        if not text:
            continue

        if feature_index in conflicting_feature_indexes:
            warnings.append(f"Skipped conflicting runtime feature: {text!r}")
            continue

        # Features are publication-facing claims. Older saved records store
        # them as strings without per-feature status metadata, so require the
        # exact claim to occur in the retained source text before publishing.
        if text not in raw_source_text:
            warnings.append(f"Skipped ungrounded feature: {text!r}")
            continue

        column = f"ITEM_FEATURES_{feature_number}"
        if column in row:
            row[column] = text

        feature_number += 1

    # ---- Dynamic attribute slots ----------------------------------------
    # Sequential packing only -- no field is ever assumed to belong to a
    # particular slot number (spec requirement).
    slot_items: List[Tuple[str, Any, Optional[str]]] = []

    for key in DYNAMIC_CORE_FIELD_KEYS:
        fobj = getattr(record, key, None)
        if not _is_exportable(fobj):
            continue
        label = (getattr(fobj, "label", None) or key.replace("_", " ").title()).strip()
        unit = _safe_attribute_unit(fobj, label)
        slot_items.append((label, fobj.value, unit))

    consumed_logistics_cols: set = set()
    extra_attrs = getattr(record, "extra_attributes", None) or {}
    for raw_key, fobj in extra_attrs.items():
        if not _is_exportable(fobj):
            continue
        label = (getattr(fobj, "label", None) or str(raw_key).replace("_", " ").title()).strip()
        norm_key = _normalize_key(raw_key)
        norm_label = _normalize_key(label)

        # Identity fields were already routed to their dedicated static columns.
        if any(norm_key in aliases or norm_label in aliases for aliases in IDENTITY_ALIASES.values()):
            continue

        if _is_non_product_dynamic(raw_key, label, getattr(fobj, "value", None)):
            warnings.append(f"Skipped non-product metadata attribute: {label!r}")
            continue

        if _looks_like_merged_attribute_value(getattr(fobj, "value", None)):
            warnings.append(
                f"Skipped malformed merged attribute {label!r}: "
                f"{_format_value(getattr(fobj, 'value', None), nz)!r}"
            )
            continue

        alias = LOGISTICS_ALIASES.get(norm_key) or LOGISTICS_ALIASES.get(norm_label)
        if alias:
            value_col, uom_col = alias
            if value_col in row and value_col not in consumed_logistics_cols:
                row[value_col] = _format_value(fobj.value, nz)
                unit = _safe_attribute_unit(fobj, label)
                if uom_col and uom_col in row and unit:
                    row[uom_col] = _normalize_uom(unit, nz)
                consumed_logistics_cols.add(value_col)
                continue  # routed to a fixed column, not a generic slot

        unit = _safe_attribute_unit(fobj, label)
        slot_items.append((label, fobj.value, unit))

    slot_numbers = _attribute_slot_numbers(headers)
    max_slots = len(slot_numbers)
    used = 0
    for i, (label, value, unit) in enumerate(slot_items):
        if i >= max_slots:
            dropped = len(slot_items) - max_slots
            warnings.append(
                f"{dropped} attribute(s) beyond slot {max_slots} were dropped "
                f"(schema has no more slots; nothing was invented to fit them)."
            )
            break
        n = slot_numbers[i]
        label_col, value_col, uom_col = (
            f"ATTRIBUTE_LABEL {n}", f"ATTRIBUTE_VALUE {n}", f"ATTRIBUTE_UOM {n}",
        )
        if label_col in row:
            row[label_col] = str(label)
        if value_col in row:
            row[value_col] = _format_value(value, nz)
        if unit and uom_col in row:
            row[uom_col] = _normalize_uom(unit, nz)
        used += 1

    return ExportResult(row=row, headers=headers, warnings=warnings,
                        slots_used=used, slots_available=max_slots)


# ---------------------------------------------------------------------------
# Batch-ready wrappers (one record today; same call shape works for many)
# ---------------------------------------------------------------------------
def build_export_rows(
    records: List[ProductIntelligenceRecord],
    *,
    headers: Optional[List[str]] = None,
    **kwargs: Any,
) -> List[ExportResult]:
    headers = headers or load_expected_headers()
    return [build_export_row(r, headers=headers, **kwargs) for r in records]


def rows_to_csv_bytes(results: List[ExportResult], headers: Optional[List[str]] = None) -> bytes:
    headers = headers or (results[0].headers if results else load_expected_headers())
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=headers, extrasaction="ignore", restval="")
    writer.writeheader()
    for res in results:
        writer.writerow({h: res.row.get(h, "") for h in headers})
    return buf.getvalue().encode("utf-8")


def rows_to_xlsx_bytes(results: List[ExportResult], headers: Optional[List[str]] = None) -> Optional[bytes]:
    """Returns None (rather than raising) if openpyxl isn't installed, so a
    CSV-only environment still exports cleanly."""
    try:
        from openpyxl import Workbook
    except ImportError:
        return None

    headers = headers or (results[0].headers if results else load_expected_headers())
    wb = Workbook()
    ws = wb.active
    ws.title = "Unilog Export"
    ws.append(headers)
    for res in results:
        ws.append([res.row.get(h, "") for h in headers])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def export_single_record(
    record: ProductIntelligenceRecord,
    **kwargs: Any,
) -> Tuple[ExportResult, bytes, Optional[bytes]]:
    """One-call convenience for the common case: one record -> (result, csv_bytes, xlsx_bytes_or_None)."""
    result = build_export_row(record, **kwargs)
    csv_bytes = rows_to_csv_bytes([result], result.headers)
    xlsx_bytes = rows_to_xlsx_bytes([result], result.headers)
    return result, csv_bytes, xlsx_bytes
