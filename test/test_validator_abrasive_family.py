from app.validator import validate_and_enrich_record
from app.schema import ExtractedField, ProductIntelligenceRecord, FetchMetadata


def _field(name, label, value=None, status="missing", confidence=0.0, grounded=None):
    return ExtractedField(
        name=name,
        label=label,
        value=value,
        confidence_score=confidence,
        validation_status=status,
        is_grounded=grounded,
    )


def test_sandpaper_marks_irrelevant_fixed_fields_not_applicable():
    record = ProductIntelligenceRecord(
        id="test-sandpaper",
        raw_input="CONSUMABLE TYPE: Sandpaper",
        fetch_metadata=FetchMetadata(),
        product_name=_field("product_name", "Product Name", "Mirka Hiolit 5 in P80", "verified", 1.0, True),
        category=_field("category", "Category", "Sandpaper", "verified", 1.0, True),
        voltage_rating=_field("voltage_rating", "Voltage Rating"),
        current_rating=_field("current_rating", "Current Rating"),
        ip_rating=_field("ip_rating", "IP Rating"),
        connector_type=_field("connector_type", "Connector Type"),
        operating_temperature_min=_field("operating_temperature_min", "Operating Temp (Min)"),
        operating_temperature_max=_field("operating_temperature_max", "Operating Temp (Max)"),
        material=_field("material", "Housing / Body Material"),
        certifications=_field("certifications", "Certifications"),
        mounting_type=_field("mounting_type", "Mounting Type"),
        extra_attributes={},
    )

    result = validate_and_enrich_record(record, record.raw_input)

    for key in (
        "voltage_rating", "current_rating", "ip_rating", "connector_type",
        "operating_temperature_min", "operating_temperature_max",
        "material", "certifications", "mounting_type",
    ):
        assert getattr(result, key).validation_status == "not_applicable"

    assert result.missing_fields_count == 0
    assert result.flagged_fields_count == 0
    assert result.overall_status == "verified"
