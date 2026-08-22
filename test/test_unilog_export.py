"""
Focused tests for app/unilog_export.py.

These are hermetic: unilog.bridge / unilog.classify / unilog.lov / unilog.uom
and app.sourcing are monkeypatched to small fakes, so the tests check ONLY
this adapter's own responsibilities (header order, fixed-field mapping,
dynamic attribute packing, blank handling, the 50-slot cap) without also
re-testing unilog's internals or requiring network/workbook files.

Run with: pytest tests/test_unilog_export.py -v
"""
import csv
import io
import os

import pytest

from app import unilog_export as uex
from app.schema import ExtractedField, ProductIntelligenceRecord, FetchMetadata


# ---------------------------------------------------------------------------
# Fakes standing in for the real unilog/app.sourcing modules
# ---------------------------------------------------------------------------
class _FakeNormalizer:
    """Stands in for unilog.uom.DEFAULT."""
    def normalize_unit(self, unit):
        return (str(unit).strip(), None)

    def _clean_number(self, value):
        # Mirror simple int/float cleanup without pulling in real uom logic.
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value)


class _FakeUomModule:
    DEFAULT = _FakeNormalizer()


class _FakeClassification:
    def __init__(self, item_type=None, classpath=None):
        self.item_type = item_type
        self.classpath = classpath


class _FakeClassifier:
    """Stands in for unilog.classify.ItemTypeClassifier."""
    def __init__(self, registry, extra_types=None):
        self.registry = registry
        self.extra_types = extra_types or []

    def classify(self, text):
        if "sensor" in text.lower():
            return _FakeClassification(
                item_type="Inductive Proximity Sensor",
                classpath="Industrial>Sensors>Proximity Sensors",
            )
        return _FakeClassification(item_type=None, classpath=None)


class _FakeClassifyModule:
    ItemTypeClassifier = _FakeClassifier


class _FakeLOVRegistry:
    pass


class _FakeLovModule:
    LOVRegistry = _FakeLOVRegistry


class _FakeBridgeModule:
    """Stands in for unilog.bridge — controlled description output, and the
    same MPN-from-name regex behaviour as the real module (single leading
    part-number-looking token)."""
    @staticmethod
    def _extract_mpn_from_name(name):
        import re
        m = re.match(r"\s*([A-Z0-9]{2,}(?:[-/][A-Z0-9]+)+)", str(name).upper())
        return m.group(1) if m else None

    @staticmethod
    def build_from_prism(record, normalizer=None, brand=None, manufacturer=None,
                         series=None, mpn=None, item_type=None, include_enriched=False):
        return {
            "descriptions": {
                "Invoice Desc": {"text": "TEST INVOICE LINE"},
                "Mobile Desc": {"text": "Test Mobile Description Text Here"},
                "Product Title / Short Desc": {"text": "Test Product Title"},
                "Long Description": {"text": "Test long description body."},
                "Web / Online Desc": {"text": "Grounded web description prose."},
            },
            "compliance": {},
            "trust_report": {},
            "record": {"brand": brand, "mpn": mpn, "item_type": item_type, "attribute_count": 0},
        }


class _FakeVerdict:
    def __init__(self, category):
        self.category = category


class _FakeSourcingModule:
    """Stands in for app.sourcing. Default: unknown category."""
    def __init__(self, category_by_url=None):
        self._category_by_url = category_by_url or {}

    def evaluate_url(self, url):
        return _FakeVerdict(self._category_by_url.get(url, "unknown"))


@pytest.fixture(autouse=True)
def patch_unilog_modules(monkeypatch):
    monkeypatch.setattr(uex, "uom_mod", _FakeUomModule())
    monkeypatch.setattr(uex, "classify_mod", _FakeClassifyModule())
    monkeypatch.setattr(uex, "lov_mod", _FakeLovModule())
    monkeypatch.setattr(uex, "bridge_mod", _FakeBridgeModule())
    monkeypatch.setattr(uex, "sourcing", _FakeSourcingModule())
    # Force the header cache to reload from the real template on every test.
    uex._HEADERS_CACHE = None
    yield
    uex._HEADERS_CACHE = None


# ---------------------------------------------------------------------------
# Record-building helpers
# ---------------------------------------------------------------------------
def make_field(value=None, unit=None, label="Field", status="verified", confidence=0.9) -> ExtractedField:
    return ExtractedField(
        name=label.lower().replace(" ", "_"), label=label, value=value, unit=unit,
        validation_status=status, confidence_score=confidence,
    )


def make_record(
    product_name="CN-M12-5P Circular Connector",
    category="Industrial Sensor",
    certifications=None,
    voltage=(24.0, "V", "verified"),
    current=(0.2, "A", "verified"),
    ip_rating=("IP67", None, "verified"),
    connector_type=("M12", None, "verified"),
    temp_min=(-25.0, "C", "verified"),
    temp_max=(70.0, "C", "verified"),
    material=(None, None, "missing"),
    mounting_type=("Threaded", None, "verified"),
    extra_attributes=None,
) -> ProductIntelligenceRecord:
    def f(spec, label):
        value, unit, status = spec
        return make_field(value=value, unit=unit, label=label, status=status)

    return ProductIntelligenceRecord(
        id="prod_test123",
        raw_input="raw text",
        product_name=make_field(product_name, label="Product Name", status="verified"),
        category=make_field(category, label="Category", status="verified"),
        voltage_rating=f(voltage, "Voltage Rating"),
        current_rating=f(current, "Current Rating"),
        ip_rating=f(ip_rating, "IP Rating"),
        connector_type=f(connector_type, "Connector Type"),
        operating_temperature_min=f(temp_min, "Min Operating Temperature"),
        operating_temperature_max=f(temp_max, "Max Operating Temperature"),
        material=f(material, "Material"),
        certifications=make_field(certifications, label="Certifications",
                                  status="verified" if certifications else "missing"),
        mounting_type=f(mounting_type, "Mounting Type"),
        extra_attributes=extra_attributes or {},
        created_at="2026-01-01T00:00:00",
    )


# ---------------------------------------------------------------------------
# 1. Header order
# ---------------------------------------------------------------------------
def test_header_order_matches_template_exactly():
    template_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "unilog_expected_output_headers.csv",
    )
    with open(template_path, "r", encoding="utf-8-sig", newline="") as fh:
        expected = next(csv.reader(fh))
    expected = [h.strip() for h in expected]

    headers = uex.load_expected_headers()
    assert headers == expected


def test_header_order_is_preserved_in_csv_output():
    record = make_record()
    result = uex.build_export_row(record)
    csv_bytes = uex.rows_to_csv_bytes([result], result.headers)
    text = csv_bytes.decode("utf-8")
    header_line = text.splitlines()[0]
    assert header_line.split(",")[:5] == result.headers[:5]
    assert header_line.split(",") == result.headers


# ---------------------------------------------------------------------------
# 2. Fixed-field mapping
# ---------------------------------------------------------------------------
def test_product_name_maps_to_product_name_column():
    record = make_record(product_name="CN-M12-5P Circular Connector")
    result = uex.build_export_row(record)
    assert result.row["Product Name"] == "CN-M12-5P Circular Connector"


def test_certifications_maps_to_standard_approvals():
    record = make_record(certifications=["UL Listed", "CE"])
    result = uex.build_export_row(record)
    assert result.row["Standard/Approvals"] == "UL Listed | CE"


def test_mpn_reused_from_product_name_into_two_columns():
    record = make_record(product_name="CN-M12-5P Circular Connector")
    result = uex.build_export_row(record)
    assert result.row["Mfg_Part_Num"] == "CN-M12-5P"
    assert result.row["MANUFACTURER_PART_NUMBER"] == "CN-M12-5P"


def test_mpn_override_takes_precedence():
    record = make_record(product_name="CN-M12-5P Circular Connector")
    result = uex.build_export_row(record, mpn_override="OVERRIDDEN-MPN")
    assert result.row["Mfg_Part_Num"] == "OVERRIDDEN-MPN"
    assert result.row["MANUFACTURER_PART_NUMBER"] == "OVERRIDDEN-MPN"


def test_manufacturer_and_brand_only_set_when_supplied():
    record = make_record()
    result = uex.build_export_row(record)
    assert result.row["MANUFACTURER_NAME"] == ""
    assert result.row["BRAND_NAME"] == ""

    result2 = uex.build_export_row(record, manufacturer="Turck", brand="Turck®")
    assert result2.row["MANUFACTURER_NAME"] == "Turck"
    assert result2.row["BRAND_NAME"] == "Turck®"


def test_known_manufacturer_and_brand_aliases_are_normalized_conservatively():
    record = make_record()
    result = uex.build_export_row(
        record, manufacturer="Black & Decker/dewlt", brand="dewlt"
    )
    assert result.row["MANUFACTURER_NAME"] == "Stanley Black & Decker"
    assert result.row["BRAND_NAME"] == "DEWALT"
    assert any("normalized manufacturer" in warning for warning in result.warnings)

    unknown = uex.build_export_row(
        record, manufacturer="Example Industrial Co.", brand="Example"
    )
    assert unknown.row["MANUFACTURER_NAME"] == "Example Industrial Co."
    assert unknown.row["BRAND_NAME"] == "Example"


def test_all_five_descriptions_are_mapped_to_delivery_columns():
    record = make_record()
    result = uex.build_export_row(record)
    assert result.row["INVOICE_DESC"] == "TEST INVOICE LINE"
    assert result.row["MOBILE_DESC"] == "Test Mobile Description Text Here"
    assert result.row["SHORT_DESC"] == "Test Product Title"
    assert result.row["LONG_DESC1"] == "Test long description body."
    assert result.row["MARKETING_DESCRIPTION"] == "Grounded web description prose."
    assert result.row["RETAIL_DESC"] == ""


def test_classpath_splits_into_dept_class_fine():
    record = make_record(category="Industrial Sensor")
    result = uex.build_export_row(record)
    assert result.row["Classpath"] == "Industrial>Sensors>Proximity Sensors"
    assert result.row["Dept"] == "Industrial"
    assert result.row["Class"] == "Sensors"
    assert result.row["Fine"] == "Proximity Sensors"


def test_verified_category_becomes_flat_classification_without_taxonomy():
    record = make_record(product_name="DCLE34520GB", category="Cross Line Laser")
    result = uex.build_export_row(record)
    assert result.row["Classpath"] == "Cross Line Laser"
    assert result.row["Class"] == "Cross Line Laser"
    assert result.row["Dept"] == ""
    assert result.row["Fine"] == ""
    assert result.row["UNSPSC"] == ""


# ---- MFR URL priority ------------------------------------------------------
def test_mfr_url_blank_by_default():
    record = make_record()
    result = uex.build_export_row(record)
    assert result.row["MFR URL"] == ""


def test_mfr_url_uses_explicit_override_first():
    record = make_record()
    result = uex.build_export_row(
        record,
        manufacturer_url="https://www.turck.com/product/x",
        discovery_source_url="https://www.amazon.com/x",  # would fail classification anyway
    )
    assert result.row["MFR URL"] == "https://www.turck.com/product/x"


def test_mfr_url_uses_discovery_source_only_if_classified_manufacturer(monkeypatch):
    record = make_record()
    monkeypatch.setattr(
        uex, "sourcing",
        _FakeSourcingModule({"https://www.turck.com/x": "manufacturer"}),
    )
    result = uex.build_export_row(record, discovery_source_url="https://www.turck.com/x")
    assert result.row["MFR URL"] == "https://www.turck.com/x"


def test_mfr_url_rejects_discovery_source_when_not_manufacturer(monkeypatch):
    record = make_record()
    monkeypatch.setattr(
        uex, "sourcing",
        _FakeSourcingModule({"https://www.amazon.com/x": "marketplace"}),
    )
    result = uex.build_export_row(record, discovery_source_url="https://www.amazon.com/x")
    assert result.row["MFR URL"] == ""
    assert any("not used for MFR URL" in w for w in result.warnings)


def test_mfr_url_never_derived_from_source_origin():
    record = make_record()
    record.source_origin = "https://www.turck.com/should-not-be-used"
    result = uex.build_export_row(record)
    assert result.row["MFR URL"] == ""


# ---------------------------------------------------------------------------
# 3. Dynamic attribute packing (sequential, no fixed slot meaning)
# ---------------------------------------------------------------------------
def test_core_fields_pack_sequentially_starting_at_slot_1():
    record = make_record()  # material is "missing" -> skipped
    result = uex.build_export_row(record)
    assert result.row["ATTRIBUTE_LABEL 1"] == "Voltage Rating"
    assert result.row["ATTRIBUTE_VALUE 1"] == "24"
    assert result.row["ATTRIBUTE_UOM 1"] == "V"
    assert result.row["ATTRIBUTE_LABEL 2"] == "Current Rating"
    assert result.row["ATTRIBUTE_LABEL 3"] == "IP Rating"


def test_slot_number_is_not_fixed_to_a_field_identity():
    """The same field ('IP Rating') must land in DIFFERENT slot numbers
    depending on what precedes it and is exportable -- slot numbers must
    never be assumed to have a fixed meaning."""
    record_a = make_record()  # voltage, current both present -> IP Rating is slot 3
    result_a = uex.build_export_row(record_a)

    record_b = make_record(
        voltage=(None, None, "missing"),
        current=(None, None, "missing"),
    )  # voltage/current skipped -> IP Rating becomes slot 1
    result_b = uex.build_export_row(record_b)

    assert result_a.row["ATTRIBUTE_LABEL 3"] == "IP Rating"
    assert result_b.row["ATTRIBUTE_LABEL 1"] == "IP Rating"
    assert result_b.row["ATTRIBUTE_LABEL 3"] != "IP Rating"


def test_extra_attributes_pack_after_core_fields_in_dict_order():
    record = make_record(extra_attributes={
        "thread_pitch": make_field("1.5", "mm", label="Thread Pitch", status="verified"),
        "flow_rate": make_field("12", "L/min", label="Flow Rate", status="verified"),
    })
    result = uex.build_export_row(record)
    # 3 exportable core fields precede extras: Voltage(1), Current(2), IP(3),
    # ConnectorType(4), TempMin(5), TempMax(6), Mounting(7) -- material missing.
    core_labels = [result.row[f"ATTRIBUTE_LABEL {i}"] for i in range(1, 8)]
    assert "Thread Pitch" not in core_labels
    assert result.row["ATTRIBUTE_LABEL 8"] == "Thread Pitch"
    assert result.row["ATTRIBUTE_VALUE 8"] == "1.5"
    assert result.row["ATTRIBUTE_UOM 8"] == "mm"
    assert result.row["ATTRIBUTE_LABEL 9"] == "Flow Rate"


def test_logistics_alias_routes_to_fixed_column_not_a_slot():
    record = make_record(extra_attributes={
        "weight": make_field(2.5, "kg", label="Weight", status="verified"),
    })
    result = uex.build_export_row(record)
    assert result.row["WEIGHT"] == "2.5"
    assert result.row["WEIGHT_UOM"] == "kg"
    # Must not ALSO appear in a numbered slot.
    all_slot_labels = [result.row[f"ATTRIBUTE_LABEL {i}"] for i in range(1, 51)]
    assert "Weight" not in all_slot_labels


# ---------------------------------------------------------------------------
# 4. Blank handling
# ---------------------------------------------------------------------------
def test_missing_status_field_is_blank_not_written():
    record = make_record(material=(None, None, "missing"))
    result = uex.build_export_row(record)
    all_slot_labels = [result.row[f"ATTRIBUTE_LABEL {i}"] for i in range(1, 51)]
    assert "Material" not in all_slot_labels


def test_not_applicable_status_field_is_blank():
    record = make_record()
    record.mounting_type.validation_status = "not_applicable"
    record.mounting_type.value = "Threaded"  # even with a value, status wins
    result = uex.build_export_row(record)
    all_slot_labels = [result.row[f"ATTRIBUTE_LABEL {i}"] for i in range(1, 51)]
    assert "Mounting Type" not in all_slot_labels


def test_unpopulatable_columns_stay_blank_string_never_none():
    record = make_record()
    result = uex.build_export_row(record)
    for col in ("PART_NUMBER", "E1_Brand", "ITEM_FEATURES_1", "UPC", "Warranty"):
        assert result.row[col] == ""
        assert result.row[col] is not None


def test_item_features_require_verbatim_source_grounding():
    record = make_record()
    record.raw_input = "Explicit source feature"
    record.features = ["Explicit source feature", "Invented marketing claim"]
    result = uex.build_export_row(record)
    assert result.row["ITEM_FEATURES_1"] == "Explicit source feature"
    assert result.row["ITEM_FEATURES_2"] == ""
    assert any("Skipped ungrounded feature" in warning for warning in result.warnings)


def test_conflicting_runtime_features_are_withheld_for_review():
    record = make_record()
    first = "Runtime reaches 23 hours with 2 Ah battery"
    second = "Runtime reaches 24 hours with 2.0 Ah battery"
    safe = "Integrated magnetic mounting"
    record.raw_input = f"{first} {second} {safe}"
    record.features = [first, second, safe]
    result = uex.build_export_row(record)
    assert result.row["ITEM_FEATURES_1"] == safe
    assert result.row["ITEM_FEATURES_2"] == ""
    assert sum("Skipped conflicting runtime feature" in w for w in result.warnings) == 2


def test_operational_metadata_is_not_exported_as_an_attribute():
    record = make_record(extra_attributes={
        "https": make_field("//example.test/product", label="Https", status="verified"),
        "estimated_arrival_on": make_field("08/27/2026", label="Estimated Arrival On", status="verified"),
    })
    result = uex.build_export_row(record)
    labels = [result.row[f"ATTRIBUTE_LABEL {i}"] for i in range(1, 51)]
    assert "Https" not in labels
    assert "Estimated Arrival On" not in labels


def test_dynamic_unit_uses_explicit_evidence_and_avoids_label_duplication():
    length = make_field(18, "mm", label="Length", status="verified")
    length.source_snippet = "18 in L"
    grit = make_field("50, 80, 120", "Grit", label="Grit", status="verified")
    grit.source_snippet = "50, 80, 120 Grit"
    record = make_record(extra_attributes={"length": length, "grit": grit})
    result = uex.build_export_row(record)
    assert result.row["LENGTH"] == "18"
    assert result.row["LENGTH_UOM"] == "in"
    grit_slot = next(i for i in range(1, 51) if result.row[f"ATTRIBUTE_LABEL {i}"] == "Grit")
    assert result.row[f"ATTRIBUTE_UOM {grit_slot}"] == ""


def test_every_header_present_in_row_even_if_blank():
    record = make_record()
    result = uex.build_export_row(record)
    assert set(result.row.keys()) == set(result.headers)


# ---------------------------------------------------------------------------
# 5. Max 50 attributes
# ---------------------------------------------------------------------------
def test_max_fifty_attribute_slots_extras_beyond_are_dropped_not_invented():
    extras = {
        f"extra_{i}": make_field(f"value_{i}", label=f"Extra {i}", status="verified")
        for i in range(60)  # far more than fit
    }
    record = make_record(extra_attributes=extras)
    result = uex.build_export_row(record)

    assert result.slots_available == 50
    assert result.slots_used == 50
    assert result.row.get("ATTRIBUTE_LABEL 50") != ""
    assert "ATTRIBUTE_LABEL 51" not in result.row  # schema has no such column
    assert any("dropped" in w for w in result.warnings)


def test_exactly_fifty_exportable_items_fill_all_slots_with_no_warning():
    extras = {
        f"extra_{i}": make_field(f"value_{i}", label=f"Extra {i}", status="verified")
        for i in range(42)  # + 8 core fields (material missing -> 7) = need care
    }
    # 7 core fields exportable (material is "missing" by default) + 43 extras = 50
    extras[f"extra_42"] = make_field("value_42", label="Extra 42", status="verified")
    record = make_record(extra_attributes=extras)
    result = uex.build_export_row(record)
    assert result.slots_used == 50
    assert not any("dropped" in w for w in result.warnings)
