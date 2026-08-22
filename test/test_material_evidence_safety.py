"""
test_material_evidence_safety.py — Regression tests for the evidence-safe
material inference fix.

BACKGROUND (the bug being regression-tested):
`app/enricher.py`'s domain-inference pass used to turn ANY single mention of
a generic metal/material word ("metal", "brass", "steel", "stainless",
"chrome", "nickel") into the specific catalog value "Nickel-plated brass" —
even though none of those words, alone, prove that specific alloy/coating
combination. This file locks in the fix:

  * Direct source facts (e.g. "Material: Brass") remain source-grounded and
    may become "verified".
  * A single generic material word must NEVER be turned into a specific
    compound material value such as "Nickel-plated brass".
  * An explicit, source-stated compound value (e.g. "Nickel-plated brass
    housing") IS accepted as source-grounded.
  * When evidence is insufficient for a specific value, PRISM withholds the
    value (stays null) and marks the field "needs_review" rather than
    guessing — it must never silently become "verified".
  * The same bar applies even if an LLM extraction path tries to claim a
    specific compound value while only quoting a generic word as evidence
    (defense-in-depth check in app/extractor.py).

Run with:
    pytest -q test_material_evidence_safety.py
"""

import os

import pytest

from app.extractor import process_raw_product_text
import app.extractor as extractor
from app.validator import validate_and_enrich_record
from app.enricher import enrich_missing_fields, _smart_domain_inference
from app.schema import ExtractedField, ProductIntelligenceRecord, FetchMetadata


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _no_llm_keys(monkeypatch):
    """Force the heuristics fallback path (deterministic, no network/LLM)
    for every test in this file unless a test explicitly re-enables an API."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)


def _run_full_pipeline(raw_text: str) -> ProductIntelligenceRecord:
    """Mirrors the exact call order used by app/main.py's /api/enrich route:
    extract -> validate -> enrich missing fields."""
    record = process_raw_product_text(raw_text=raw_text, input_mode="manual")
    record = validate_and_enrich_record(record, raw_text)
    record = enrich_missing_fields(record, raw_text)
    return record


def _blank_field(name: str, label: str) -> ExtractedField:
    return ExtractedField(name=name, label=label, value=None, source_type="manual_input",
                           source_origin="Manual Text Input", confidence_score=0.0,
                           validation_status="missing")


def _blank_record(raw_text: str) -> ProductIntelligenceRecord:
    """A minimal, otherwise-empty record for unit-testing enricher functions
    in isolation, without going through the full extractor."""
    names = ["product_name", "category", "voltage_rating", "current_rating", "ip_rating",
             "connector_type", "operating_temperature_min", "operating_temperature_max",
             "material", "certifications", "mounting_type"]
    fields = {n: _blank_field(n, n.replace("_", " ").title()) for n in names}
    return ProductIntelligenceRecord(
        id="test_prod", raw_input=raw_text, input_mode="manual",
        fetch_metadata=FetchMetadata(), **fields,
    )


# ---------------------------------------------------------------------------
# A. Source says: "Material: Brass" -> may be accepted as source-grounded.
# ---------------------------------------------------------------------------
def test_case_a_bare_brass_is_accepted_as_source_grounded():
    record = _run_full_pipeline("Material: Brass. Rated Voltage: 24V DC.")
    m = record.material
    assert m.value is not None
    assert "brass" in str(m.value).lower()
    assert m.validation_status == "verified"
    assert m.is_grounded is True


# ---------------------------------------------------------------------------
# B. Source says: "Material: Stainless Steel" -> may be accepted as
#    source-grounded.
# ---------------------------------------------------------------------------
def test_case_b_bare_stainless_steel_is_accepted_as_source_grounded():
    record = _run_full_pipeline("Material: Stainless Steel. Rated Voltage: 24V DC.")
    m = record.material
    assert m.value is not None
    val_l = str(m.value).lower()
    assert "stainless" in val_l and "steel" in val_l
    assert m.validation_status == "verified"
    assert m.is_grounded is True


# ---------------------------------------------------------------------------
# C. Source says only: "Nickel" -> must NOT become "Nickel-plated brass".
# ---------------------------------------------------------------------------
def test_case_c_bare_nickel_never_becomes_nickel_plated_brass():
    raw_text = ("Spec Sheet: Compact Sensor Unit. Some internal parts use Nickel. "
                "Rated for general industrial use.")
    record = _run_full_pipeline(raw_text)
    m = record.material
    assert m.value != "Nickel-plated brass"
    assert m.value is None
    assert m.validation_status != "verified"
    assert m.validation_status in ("needs_review", "missing")


# ---------------------------------------------------------------------------
# D. Source says only: "Metal" -> must NOT become "Brass" or
#    "Nickel-plated brass".
# ---------------------------------------------------------------------------
def test_case_d_bare_metal_never_becomes_brass_or_nickel_plated_brass():
    raw_text = "Spec Sheet: Compact Sensor Unit. This unit is constructed from Metal components."
    record = _run_full_pipeline(raw_text)
    m = record.material
    assert m.value not in ("Brass", "Nickel-plated brass")
    assert m.value is None
    assert m.validation_status != "verified"
    assert m.validation_status in ("needs_review", "missing")


# ---------------------------------------------------------------------------
# E. Source says only: "Chrome" -> must NOT become "Nickel-plated brass".
# ---------------------------------------------------------------------------
def test_case_e_bare_chrome_never_becomes_nickel_plated_brass():
    raw_text = "Spec Sheet: Compact Sensor Unit. Some finish details mention Chrome elements."
    record = _run_full_pipeline(raw_text)
    m = record.material
    assert m.value != "Nickel-plated brass"
    assert m.value is None
    assert m.validation_status != "verified"
    assert m.validation_status in ("needs_review", "missing")


# ---------------------------------------------------------------------------
# F. Source explicitly says: "Nickel-plated brass housing" -> may be accepted
#    as source-grounded.
# ---------------------------------------------------------------------------
def test_case_f_explicit_compound_statement_is_accepted_as_source_grounded():
    raw_text = ("Spec Sheet: Compact Sensor Unit. The housing is made of "
                "Nickel-plated brass for durability.")
    record = _run_full_pipeline(raw_text)
    m = record.material
    assert m.value is not None
    val_l = str(m.value).lower()
    assert "nickel" in val_l and "brass" in val_l
    assert m.validation_status == "verified"
    assert m.is_grounded is True


# ---------------------------------------------------------------------------
# G. AI suggests a material without sufficient evidence -> it remains
#    AI-inferred/needs-review or is withheld; it must NOT become verified.
# ---------------------------------------------------------------------------
def test_case_g_insufficient_evidence_never_becomes_verified_full_pipeline():
    raw_text = ("Spec Sheet: Compact Sensor Unit. Internal parts reference "
                "steel and alloy components generally.")
    record = _run_full_pipeline(raw_text)
    m = record.material
    assert m.value != "Nickel-plated brass"
    assert m.validation_status != "verified"
    # The record as a whole must reflect that something still needs review —
    # it must never be silently reported as fully "verified".
    assert record.overall_status != "verified"


def test_case_g_smart_domain_inference_withholds_rather_than_invents():
    """Unit-level test directly on the enrichment inference function: with
    only generic evidence, it must return a withheld/needs-review inference
    for `material`, never a fabricated specific value."""
    raw_text = "This unit's body references chrome and steel in passing."
    record = _blank_record(raw_text)
    inferences = _smart_domain_inference(record, raw_text)
    assert "material" in inferences
    mat_inf = inferences["material"]
    assert mat_inf.get("value") is None
    assert mat_inf.get("withheld") is True
    assert mat_inf.get("value") != "Nickel-plated brass"


def test_case_g_enrich_missing_fields_sets_needs_review_not_ai_enriched():
    """Direct unit test of enrich_missing_fields: generic-only evidence must
    withhold the value and use the 'needs_review' status, never silently
    promote to 'ai_enriched' with an invented specific value, and never to
    'verified'."""
    raw_text = "Contains some metal parts of unspecified type."
    record = _blank_record(raw_text)
    record = enrich_missing_fields(record, raw_text)
    m = record.material
    assert m.value is None
    assert m.validation_status == "needs_review"
    assert m.validation_status != "verified"
    assert m.source_type == "ai_inference"


def test_case_g_enricher_accepts_explicit_compound_when_present():
    """Sanity check: when the raw text DOES state the compound explicitly,
    the enrichment pass may surface it (mirrors requirement F at the
    enricher-unit level, not just the extractor level)."""
    raw_text = "This unit's body is Nickel-plated brass, chrome-finished for corrosion resistance."
    record = _blank_record(raw_text)
    inferences = _smart_domain_inference(record, raw_text)
    assert "material" in inferences
    mat_inf = inferences["material"]
    assert mat_inf.get("value") is not None
    assert "nickel-plated brass" in str(mat_inf["value"]).lower()
    assert not mat_inf.get("withheld")


# ---------------------------------------------------------------------------
# Defense-in-depth: even if an LLM extraction path tries to claim a specific
# compound material while only quoting a bare generic word as its evidence
# snippet, the deterministic code-level gate in app/extractor.py must reject
# it — it must never reach "verified" status.
# ---------------------------------------------------------------------------
def test_llm_path_hallucinated_compound_material_is_rejected(monkeypatch):
    def fake_claude_extract(raw_text, api_key):
        blank = {"value": None, "unit": None, "source_snippet": None,
                 "confidence_score": 0.0, "reasoning": None}
        data = {k: dict(blank) for k in extractor.FIELD_LABELS.keys()}
        data["product_name"] = {"value": "XJ-9000", "unit": None, "source_snippet": "XJ-9000",
                                 "confidence_score": 0.9, "reasoning": "part number"}
        # The model only ever saw the bare word "nickel" but hallucinates a
        # specific compound value anyway, with a plausible-looking confidence.
        data["material"] = {"value": "Nickel-plated brass", "unit": None,
                             "source_snippet": "nickel", "confidence_score": 0.80,
                             "reasoning": "hallucinated compound from a generic word"}
        data["extra_attributes"] = []
        return data

    monkeypatch.setattr(extractor, "extract_with_claude_api", fake_claude_extract)
    monkeypatch.setattr(extractor, "HAS_ANTHROPIC_SDK", True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-test-key-0123456789")

    raw_text = "XJ-9000 sensor. Housing contains nickel. Rated for industrial use."
    record = process_raw_product_text(raw_text=raw_text, input_mode="manual")
    record = validate_and_enrich_record(record, raw_text)
    record = enrich_missing_fields(record, raw_text)

    m = record.material
    assert m.value == "Nickel-plated brass"  # the claimed value is preserved for audit...
    assert m.is_grounded is False            # ...but never treated as grounded evidence
    assert m.validation_status != "verified"
    assert m.validation_status == "flagged_ungrounded"
    assert record.overall_status != "verified"


# ---------------------------------------------------------------------------
# Requirement 1: a value must never SILENTLY become verified. Re-running
# deterministic validation after enrichment must not promote a withheld /
# needs_review material to "verified".
# ---------------------------------------------------------------------------
def test_needs_review_material_is_never_promoted_by_revalidation():
    raw_text = "Spec Sheet: Compact Sensor Unit. Mentions brass and nickel loosely in passing text."
    record = _run_full_pipeline(raw_text)
    assert record.material.validation_status != "verified"

    # Re-run deterministic validation (simulates a second pass / re-save) —
    # it must still never promote the withheld field to "verified".
    record = validate_and_enrich_record(record, raw_text)
    assert record.material.value != "Nickel-plated brass"
    assert record.material.validation_status != "verified"
