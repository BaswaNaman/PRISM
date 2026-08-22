from app.validator import validate_and_enrich_record
from app.schema import ExtractedField, ProductIntelligenceRecord, FetchMetadata


def _field(name, label, value=None, status="missing", confidence=0.0, grounded=None, unit=None):
    return ExtractedField(
        name=name,
        label=label,
        value=value,
        unit=unit,
        confidence_score=confidence,
        validation_status=status,
        is_grounded=grounded,
    )


def _base_record(voltage_value):
    return ProductIntelligenceRecord(
        id="test-voltage",
        raw_input="DEWALT 12V/20V MAX Cordless Green Cross Line 5-Spot Laser Level",
        fetch_metadata=FetchMetadata(),
        product_name=_field("product_name", "Product Name", "DEWALT Laser Level", "verified", 0.95, True),
        category=_field("category", "Category", "Laser Level", "verified", 0.95, True),
        voltage_rating=_field("voltage_rating", "Voltage Rating", voltage_value, "pending", 0.9, True, "V"),
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


def test_accepts_multiplatform_voltage():
    result = validate_and_enrich_record(_base_record("12V/20V MAX"), "DEWALT 12V/20V MAX")
    assert result.voltage_rating.validation_status == "verified"
    assert result.voltage_rating.value == "12V/20V MAX"


def test_rejects_out_of_range_multiplatform_voltage():
    result = validate_and_enrich_record(_base_record("12V/1200V MAX"), "12V/1200V MAX")
    assert result.voltage_rating.validation_status == "flagged_validation_error"
