import app.extractor as extractor


def test_llm_numeric_claim_must_appear_in_cited_snippet(monkeypatch):
    """A grounded snippet must not make a different LLM-generated number verified."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-123456789")

    extracted = {
        key: {
            "value": None,
            "unit": None,
            "source_snippet": None,
            "confidence_score": 0.0,
            "reasoning": None,
        }
        for key in extractor.FIELD_LABELS
    }
    extracted["product_name"] = {
        "value": "PRX-1 Sensor", "unit": None, "source_snippet": "Sensor",
        "confidence_score": 0.9, "reasoning": "source"
    }
    extracted["category"] = {
        "value": "Industrial Sensor", "unit": None, "source_snippet": "Sensor",
        "confidence_score": 0.9, "reasoning": "source"
    }
    extracted["voltage_rating"] = {
        "value": 999, "unit": "V", "source_snippet": "Voltage rating: 10-30 V DC.",
        "confidence_score": 0.9, "reasoning": "bad model output"
    }
    extracted["ip_rating"] = {
        "value": "IP67", "unit": None, "source_snippet": "IP67",
        "confidence_score": 0.9, "reasoning": "source"
    }

    monkeypatch.setattr(extractor, "extract_with_gemini_api", lambda *args, **kwargs: extracted)
    record = extractor.process_raw_product_text(
        "Voltage rating: 10-30 V DC. IP67 rated. Sensor."
    )

    field = record.voltage_rating
    assert field.value == 999
    assert field.is_grounded is False
    assert field.validation_status == "flagged_ungrounded"


def test_failed_input_record_summary_counts_all_fixed_fields():
    """Failure records must report the real 11-field schema, not the old 10-field default."""
    record = extractor.process_raw_product_text(
        "",
        fetch_success=False,
        http_status=403,
        error_message="HTTP 403",
    )

    assert record.total_fields == 11
    assert record.flagged_fields_count == 11
    assert record.verified_fields_count == 0
    assert record.missing_fields_count == 0
    assert record.overall_status == "needs_review"
