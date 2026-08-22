from app.enricher import enrich_missing_fields
from app.schema import ExtractedField, ProductIntelligenceRecord, FetchMetadata


def _field(name, label, value=None, status="missing", confidence=0.0):
    return ExtractedField(
        name=name,
        label=label,
        value=value,
        confidence_score=confidence,
        validation_status=status,
    )


def test_enricher_preserves_not_applicable_material_and_summary():
    record = ProductIntelligenceRecord(
        id="test",
        raw_input="Sanding belt for wood and metal",
        fetch_metadata=FetchMetadata(),
        product_name=_field("product_name", "Product Name", "Test Sanding Belt", "verified", 0.9),
        category=_field("category", "Category", "Sanding Belt", "verified", 0.92),
        voltage_rating=_field("voltage_rating", "Voltage Rating", status="not_applicable"),
        current_rating=_field("current_rating", "Current Rating", status="not_applicable"),
        ip_rating=_field("ip_rating", "IP Rating", status="not_applicable"),
        connector_type=_field("connector_type", "Connector Type", status="not_applicable"),
        operating_temperature_min=_field("operating_temperature_min", "Operating Temp (Min)", status="not_applicable"),
        operating_temperature_max=_field("operating_temperature_max", "Operating Temp (Max)", status="not_applicable"),
        material=_field("material", "Housing / Body Material", status="not_applicable"),
        certifications=_field("certifications", "Certifications", status="not_applicable"),
        mounting_type=_field("mounting_type", "Mounting Type", status="not_applicable"),
        extra_attributes={
            "grit": _field("grit", "Grit", "80", "verified", 0.94),
            "backing": _field("backing", "Backing", "Cloth", "verified", 0.94),
        },
    )

    result = enrich_missing_fields(record, record.raw_input)

    assert result.material.value is None
    assert result.material.validation_status == "not_applicable"
    assert result.material.source_type != "ai_inference"

    # 2 core applicable fields + 2 verified dynamic attributes.
    assert result.total_fields == 4
    assert result.verified_fields_count == 4
    assert result.flagged_fields_count == 0
    assert result.missing_fields_count == 0
    assert result.overall_status == "verified"
