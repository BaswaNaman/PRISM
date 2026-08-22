"""
PRISM — Smart, Explainable Enrichment Engine
===========================================
Enforces the 3-Tier Rule:
1. Verified: Directly from source text.
2. AI Enriched: Logically inferred from available evidence or domain patterns (with reasoning).
3. Missing: No evidence or logical bridge exists.
"""

import os
import json
import re
from typing import Dict, Any, Optional, List
from app.schema import ExtractedField, ProductIntelligenceRecord

try:
    from google import genai
    from google.genai import types as genai_types
    HAS_GEMINI_SDK = True
except ImportError:
    HAS_GEMINI_SDK = False

ENRICHABLE_FIELDS = [
    "category", "voltage_rating", "current_rating", "ip_rating", "connector_type",
    "operating_temperature_min", "operating_temperature_max", "material", "certifications", "mounting_type"
]

AI_ORIGIN = "AI Logical Inference (Domain Context)"

# ---------------------------------------------------------------------------
# EVIDENCE-SAFE MATERIAL INFERENCE
# ---------------------------------------------------------------------------
# Explicit compound material/coating specifications (e.g. "nickel-plated
# brass") are specific enough to stand as a source-grounded fact on their own.
# This mirrors the SPECIFIC_MATERIALS pattern used by the deterministic
# heuristics extractor (app/extractor.py) so the same bar for "specific
# enough" is applied consistently whether the value is picked up during
# direct extraction or during this domain-inference pass.
SPECIFIC_MATERIAL_PATTERN = re.compile(
    r'\b(nickel-plated brass|chrome-plated brass|stainless steel 316l|'
    r'die-cast zinc alloy|polyamide pa66|aluminium alloy|aluminum alloy|'
    r'aluminum anodized|anodized aluminium|anodized aluminum|zinc alloy)\b',
    re.IGNORECASE,
)

# Bare generic metal/family words. On their own these only prove that *a*
# metal or material family was mentioned somewhere in the text -- they do NOT
# prove a specific alloy/coating combination such as "Nickel-plated brass".
# Any one (or even several) of these appearing must never be combined into a
# fabricated compound material.
GENERIC_MATERIAL_WORDS = ["metal", "brass", "stainless", "steel", "alloy", "chrome", "nickel"]


def _safe_conf(raw: Any, default: float = 0.60) -> float:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, v))

def _smart_domain_inference(record: ProductIntelligenceRecord, raw_text: str) -> Dict[str, Dict[str, Any]]:
    name = str(record.product_name.value or "").lower()
    category = str(record.category.value or "").lower()
    blob = f"{name} {category} {raw_text.lower()}"
    
    inferences: Dict[str, Dict[str, Any]] = {}

    # Context Rule 1: Proximity / Inductive Sensors (e.g., M18, M8, E2E)
    if any(k in blob for k in ["proximity", "inductive", "sensor", "m8", "m12", "m18"]):
        if record.category.value is None:
            inferences["category"] = {
                "value": "Inductive Proximity Sensor", 
                "unit": None, 
                "confidence_score": 0.75, 
                "reasoning": "Product identifier/keywords strongly indicate an inductive proximity sensor class."
            }
        
        # If it's a tubular/threaded sensor shape (M-series), mounting is inherently threaded
        if any(m in blob for m in ["m5", "m8", "m12", "m18", "m23", "tubular"]):
            if record.mounting_type.value is None:
                inferences["mounting_type"] = {
                    "value": "Threaded", 
                    "unit": None, 
                    "confidence_score": 0.82, 
                    "reasoning": "Inferred from M-series tubular barrel form factor requiring a threaded interface."
                }
        
        # Standard industrial proximity sensors frequently default to IP67 if sealed body
        if "ip67" in blob and record.ip_rating.value is None:
            pass # Handled by direct extraction ideally, but good context backup

    # Context Rule 2: Material inference is evidence-safe.
    #
    # Inferring an alloy purely from a thread code (e.g. "M18") is a
    # hallucination and is deliberately NOT allowed -- and neither is
    # inferring a *specific* alloy/coating combination (e.g. "Nickel-plated
    # brass") from a single generic metal word like "nickel" or "chrome".
    # Generic evidence proves only that *some* metal/material family was
    # mentioned; it does not prove which specific alloy or coating the
    # product actually uses.
    #
    #   - An explicit compound material phrase found verbatim in the source
    #     (e.g. "nickel-plated brass") IS specific enough to stand on its own
    #     and is surfaced as a source-grounded value.
    #   - Bare generic words (metal, brass, steel, stainless, chrome, nickel,
    #     alloy) are NOT sufficient to justify a specific material value.
    #     When only generic evidence exists, the field is withheld (value
    #     stays null) and flagged "needs_review" instead of guessing.
    #   - No material evidence at all -> leave the field out of `inferences`
    #     entirely so it stays genuinely "missing".
    if record.material.value is None:
        specific_mat_match = SPECIFIC_MATERIAL_PATTERN.search(raw_text)
        if specific_mat_match:
            inferences["material"] = {
                "value": specific_mat_match.group(0),
                "unit": None,
                "confidence_score": 0.88,
                "reasoning": (f"Source text explicitly states the compound material "
                              f"specification '{specific_mat_match.group(0)}'; this is a "
                              f"directly source-grounded statement, not a guess."),
            }
        else:
            generic_hits = [m for m in GENERIC_MATERIAL_WORDS if m in blob]
            if generic_hits:
                inferences["material"] = {
                    "value": None,
                    "unit": None,
                    "confidence_score": 0.0,
                    "reasoning": (
                        f"Source mentions generic material term(s) {generic_hits} but does not "
                        f"state a specific alloy or coating combination. Generic evidence alone "
                        f"is not sufficient to infer a specific material such as "
                        f"'Nickel-plated brass' -- withholding the value until the source (or a "
                        f"human reviewer) confirms the exact material."
                    ),
                    "withheld": True,
                }
            # else: no material evidence whatsoever -> leave truly missing.

    # Context Rule 3: Standard Industrial Operating Voltages
    if "dc" in blob and "10-30" in blob and record.voltage_rating.value is None:
        inferences["voltage_rating"] = {
            "value": 30.0,
            "unit": "V",
            "confidence_score": 0.70,
            "reasoning": "Extracted upper limit of standard 10–30V DC operating range."
        }

    return inferences

def recompute_summary(record: ProductIntelligenceRecord) -> None:
    core = [
        record.product_name, record.category, record.voltage_rating,
        record.current_rating, record.ip_rating, record.connector_type,
        record.operating_temperature_min, record.operating_temperature_max,
        record.material, record.certifications, record.mounting_type,
    ]
    all_fields = core + list(record.extra_attributes.values())

    # Keep enrichment summaries consistent with validator.py:
    # fields already marked not_applicable are not missing, not flagged, and do
    # not belong in the completeness denominator for this product category.
    fields = [f for f in all_fields if f.validation_status != "not_applicable"]

    record.total_fields = len(fields)
    record.verified_fields_count = sum(1 for f in fields if f.validation_status == "verified")
    # "needs_review" covers values withheld for insufficient evidence (e.g. a
    # generic material mention that cannot justify a specific alloy/coating
    # value) -- it is a human-review bucket, so it counts alongside the other
    # flagged statuses.
    record.flagged_fields_count = sum(1 for f in fields if f.validation_status in ["flagged_low_confidence", "flagged_validation_error", "flagged_ungrounded", "needs_review"])
    record.missing_fields_count = sum(1 for f in fields if f.validation_status == "missing")
    record.enriched_fields_count = sum(1 for f in fields if f.validation_status == "ai_enriched")

    scores = [f.confidence_score for f in fields if f.value is not None]
    record.overall_confidence = round(sum(scores) / len(scores), 2) if scores else 0.0

    if record.flagged_fields_count > 0 or record.missing_fields_count > 0 or record.enriched_fields_count > 0:
        record.overall_status = "needs_review"
    else:
        record.overall_status = "verified"

def enrich_missing_fields(record: ProductIntelligenceRecord, raw_text: str) -> ProductIntelligenceRecord:
    missing_keys = [
        key for key in ENRICHABLE_FIELDS
        if getattr(record, key).validation_status != "not_applicable"
        and (
            getattr(record, key).value is None
            or str(getattr(record, key).value).strip() == ""
        )
    ]

    if not missing_keys:
        recompute_summary(record)
        return record

    # Fetch context-aware smart inferences
    domain_inferences = _smart_domain_inference(record, raw_text)

    for key in missing_keys:
        if key not in domain_inferences:
            continue  # Truly missing, leave as null!

        spec = domain_inferences[key]
        field: ExtractedField = getattr(record, key)

        if spec.get("withheld"):
            # Insufficient evidence for a specific value: never invent one.
            # The field stays null, but it's tagged "needs_review" (rather
            # than silently remaining "missing") so a human reviewer sees
            # *why* -- generic evidence exists but does not support a
            # specific catalog value.
            field.value = None
            field.unit = None
            field.confidence_score = _safe_conf(spec.get("confidence_score"), 0.0)
            field.source_type = "ai_inference"
            field.source_origin = AI_ORIGIN
            field.validation_status = "needs_review"
            field.reasoning = f"[Insufficient Evidence] {spec.get('reasoning')}"
            field.is_reviewed = False
            continue

        field.value = spec.get("value")
        field.unit = spec.get("unit")
        field.confidence_score = _safe_conf(spec.get("confidence_score"), 0.60)
        field.source_type = "ai_inference"
        field.source_origin = AI_ORIGIN
        field.validation_status = "ai_enriched"
        field.reasoning = f"[Educated Inference] {spec.get('reasoning')}"
        field.is_reviewed = False

    recompute_summary(record)
    return record