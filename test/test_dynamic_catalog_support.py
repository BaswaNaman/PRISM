"""Regression tests for mixed-category organizer catalog support."""

from app.extractor import _category_from_source, _parse_spec_table_attributes, process_raw_product_text
from app.validator import validate_and_enrich_record

SANDING_SOURCE = '''=== EXTRACTED SPECIFICATION TABLES ===
Sku: DCB518ASTS06G
Brand: Diablo
Pack Quantity: 6
Item UPC: 008925172550
Item Quantity: 1
Item Weight (lb): 0.13
Country of Origin: United States
Grit: 50/80/120
Grit Blend: Zirconium Blend
Grit Description: Multi-Grade
Backing: Cloth
Assorted Pack: Y
Product Type: Sanding
Cutting Materials: Wood; Metal; Plastic

=== PAGE CONTENT ===
Diablo DCB518ASTS06G 1/2" x 18" Detail File Sanding Belt Assorted Pack (6-Pieces)
Specifications
Sku
DCB518ASTS06G
'''

def test_source_category_recovers_sanding_belt():
    category = _category_from_source(SANDING_SOURCE)
    assert category is not None
    assert category[0] == "Sanding Belt"
    assert category[1] >= 0.90

def test_spec_table_rows_become_dynamic_attributes():
    rows = _parse_spec_table_attributes(SANDING_SOURCE)
    by_name = {row["attribute_name"]: row for row in rows}
    assert by_name["Manufacturer Part Number"]["value"] == "DCB518ASTS06G"
    assert by_name["Brand"]["value"] == "Diablo"
    assert by_name["Pack Quantity"]["value"] == "6"
    assert by_name["UPC"]["value"] == "008925172550"
    assert by_name["Item Weight"]["value"] == "0.13"
    assert by_name["Item Weight"]["unit"] == "lb"
    assert by_name["Country of Origin"]["value"] == "United States"
    assert by_name["Grit"]["value"] == "50/80/120"
    assert by_name["Backing"]["value"] == "Cloth"
    assert by_name["Cutting Materials"]["value"] == "Wood; Metal; Plastic"

def test_sanding_belt_irrelevant_sensor_fields_are_not_applicable(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    record = process_raw_product_text(
        SANDING_SOURCE,
        input_mode="url",
        source_type="url_ingestion",
        source_origin="URL: example.test/product",
        page_title='Diablo DCB518ASTS06G 1/2" x 18" Sanding Belt',
    )
    record = validate_and_enrich_record(record, SANDING_SOURCE)
    assert record.category.value == "Sanding Belt"
    assert record.ip_rating.validation_status == "not_applicable"
    assert record.voltage_rating.validation_status == "not_applicable"
    assert record.current_rating.validation_status == "not_applicable"
    assert record.connector_type.validation_status == "not_applicable"
    assert record.material.validation_status == "not_applicable"
    assert "pack_quantity" in record.extra_attributes
    assert "grit" in record.extra_attributes
    assert "backing" in record.extra_attributes
    assert record.extra_attributes["backing"].value == "Cloth"
    assert record.missing_fields_count == 0

def test_sensor_behavior_is_not_weakened(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    source = "PRX-M18-30 Inductive Proximity Sensor. Supply voltage 10-30 VDC."
    record = process_raw_product_text(source)
    record = validate_and_enrich_record(record, source)
    assert record.category.value == "Industrial Sensor"
    assert record.ip_rating.validation_status != "not_applicable"
