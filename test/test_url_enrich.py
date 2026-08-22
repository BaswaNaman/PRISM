"""
test_url_enrich.py — manual diagnostic run of the enrichment pipeline against
a live URL, plus a focused pytest test proving the same entry point works
without ever starting FastAPI.

The file has two parts:

1. `main()` / the live-URL diagnostic — a diagnostic/demo script rather than
   an assertion-based test (it just prints the extracted record for a real
   URL, which requires network access). It stays guarded to run only when
   this file is executed directly, so pytest never triggers a live network
   call.

   Run directly:
       python test_url_enrich.py

2. `test_enrich_product_without_starting_fastapi()` — a real pytest test
   (network-free, using local manual text) proving that `enrich_product()`,
   a plain FastAPI route function, can be called directly as ordinary Python
   — no TestClient, no uvicorn, no lifespan/startup event ever running — and
   still succeeds because database initialization no longer depends on
   FastAPI startup.

   Run under pytest:
       pytest -q test_url_enrich.py
"""

from app.main import enrich_product
from app.schema import EnrichRequest, ProductIntelligenceRecord
from app.database import get_product

URL = ("https://www.automationdirect.com/adc/shopping/catalog/"
       "sensors_-z-_encoders/inductive_proximity_sensors/8mm_tubular/dw-ad-511-m8")


def main(url: str = URL) -> int:
    req = EnrichRequest(input_mode="url", url=url)

    print("=" * 70)
    print(f"RUNNING ENRICHMENT PIPELINE FOR URL:\n{url}")
    print("=" * 70)

    record = enrich_product(req)

    print(f"\nPRODUCT NAME: {record.product_name.value}")
    print(f"CATEGORY: {record.category.value}")
    print(f"MOUNTING / FORM: {record.connector_type.value}")
    print(f"HOUSING MATERIAL: {record.material.value}")
    print(f"OVERALL STATUS: {record.overall_status}")
    print(f"OVERALL CONFIDENCE: {record.overall_confidence}")
    print(f"HTTP STATUS: {record.fetch_metadata.http_status}")
    print(f"FETCH LENGTH: {record.fetch_metadata.content_length} chars")
    print(f"PAGE TITLE: {record.fetch_metadata.page_title}")
    print("\n--- EXTRACTED FIELDS SUMMARY ---")
    fields = [
        record.product_name, record.category, record.voltage_rating,
        record.current_rating, record.ip_rating, record.connector_type,
        record.operating_temperature_min, record.operating_temperature_max,
        record.material, record.certifications, record.mounting_type
    ]
    for f in fields:
        print(f"  > {f.label:24} | Value: {str(f.value):25} | Unit: {str(f.unit):6} "
              f"| Conf: {int(f.confidence_score*100)}% | Status: {f.validation_status}")

    return 0


def test_enrich_product_without_starting_fastapi():
    """`enrich_product()` must work as a plain function call.

    No TestClient, no uvicorn, no ASGI lifespan — importing `app.main` and
    calling the route function directly used to be able to fail with
    `sqlite3.OperationalError: no such table: products` because the schema
    was only ever created by the FastAPI startup event. It's created lazily
    now (see `app/database.py`), so this must succeed on its own, and the
    saved record must be readable back from the database afterward.

    Uses local manual text (no `input_mode="url"`) so this test needs no
    network access and stays fast and deterministic.
    """
    raw_text = (
        "M12 5-pin circular connector. Voltage rating: 250 VAC. "
        "Current rating: 4 A. IP67 rated. Housing material: brass. "
        "Operating temperature: -25C to 85C."
    )
    req = EnrichRequest(input_mode="manual", raw_text=raw_text)

    record = enrich_product(req)

    assert isinstance(record, ProductIntelligenceRecord)
    assert record.id
    assert record.raw_input == raw_text
    assert record.overall_status in {"verified", "needs_review"}

    # enrich_product() saves the record as a side effect — confirm it is
    # actually persisted and retrievable, i.e. the database operations that
    # back this call really did work.
    persisted = get_product(record.id)
    assert persisted is not None
    assert persisted.id == record.id


if __name__ == "__main__":
    raise SystemExit(main())
