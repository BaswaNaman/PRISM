"""Offline regression tests for PRISM discovery source ranking."""

from types import SimpleNamespace

from app import discovery


MPN = "DCB518ASTS06G"
DESC = 'DCB518ASTS06G Diablo 1/2"x18" - Sanding Belt 6pc'
MANUFACTURER = "Freud Inc"
BRAND = "Diablo"


def _fetch(text, origin):
    return {
        "success": True,
        "http_status": 200,
        "error": None,
        "text": text,
        "title": "Diablo DCB518ASTS06G Sanding Belt",
        "source_type": "url_ingestion",
        "source_origin": origin,
    }


def _stub_pipeline(monkeypatch):
    monkeypatch.setattr(
        discovery,
        "process_raw_product_text",
        lambda **kw: {"id": "fake", "overall_status": "verified", "raw_text_seen": kw["raw_text"]},
    )
    monkeypatch.setattr(discovery, "validate_and_enrich_record", lambda r, text: r)
    monkeypatch.setattr(discovery, "enrich_missing_fields", lambda r, text: r)
    monkeypatch.setattr(discovery, "save_product", lambda r: None)
    monkeypatch.setattr(
        discovery.sourcing,
        "evaluate_url",
        lambda url: SimpleNamespace(allowed=True, category="unknown", reason="allowed"),
    )


def test_richer_verified_source_wins_even_when_it_appears_later(monkeypatch):
    _stub_pipeline(monkeypatch)
    thin = "https://example.com/thin"
    rich = "https://example.com/rich"

    thin_text = (
        f"Diablo {MPN} sanding belt. Regular price $11.20. "
        "Add to cart. Free shipping. Continue shopping. " * 4
    )
    rich_text = f"""=== EXTRACTED SPECIFICATION TABLES ===
Sku: {MPN}
Brand: Diablo
Pack Quantity: 6
Item UPC: 008925172550
Item Weight (lb): 0.13
Country of Origin: United States
Grit: 50/80/120
Grit Blend: Zirconium Blend
Backing: Cloth
Product Type: Sanding
Cutting Materials: Wood; Metal; Plastic
=== PAGE CONTENT ===
Diablo {MPN} Detail File Sanding Belt Assorted Pack
"""

    monkeypatch.setattr(
        discovery, "search_candidate_urls",
        lambda q: {"urls": [thin, rich], "available": True, "error": None},
    )
    monkeypatch.setattr(
        discovery, "fetch_and_clean_url",
        lambda url: _fetch(thin_text, "URL: thin") if url == thin else _fetch(rich_text, "URL: rich"),
    )

    result = discovery.discover_and_enrich(MPN, DESC, MANUFACTURER, BRAND)
    assert result["source_url"] == rich
    assert result["trace"]["selected_source_url"] == rich


def test_invalid_mpn_page_cannot_win_even_if_it_is_richer(monkeypatch):
    _stub_pipeline(monkeypatch)
    wrong = "https://example.com/wrong"
    right = "https://example.com/right"

    wrong_text = """=== EXTRACTED SPECIFICATION TABLES ===
Sku: WRONG999
Brand: Diablo
Pack Quantity: 6
Grit: 50/80/120
Backing: Cloth
Country of Origin: United States
""" * 5
    right_text = (
        f"Diablo {MPN} sanding belt product specification. "
        "Backing: Cloth. Pack Quantity: 6. Product Type: Sanding. "
        "Designed for detail file sanding applications with assorted abrasive grits."
    )

    monkeypatch.setattr(
        discovery, "search_candidate_urls",
        lambda q: {"urls": [wrong, right], "available": True, "error": None},
    )
    monkeypatch.setattr(
        discovery, "fetch_and_clean_url",
        lambda url: _fetch(wrong_text, "URL: wrong") if url == wrong else _fetch(right_text, "URL: right"),
    )

    result = discovery.discover_and_enrich(MPN, DESC, MANUFACTURER, BRAND)
    assert result["source_url"] == right
    assert any(
        r["url"] == wrong and r["stage"] == "identity" and r["mpn_matched"] is False
        for r in result["trace"]["rejections"]
    )


def test_single_valid_source_behavior_is_unchanged(monkeypatch):
    _stub_pipeline(monkeypatch)
    url = "https://example.com/only"
    text = (
        f"Diablo {MPN} sanding belt specifications. Grit: 80. Backing: Cloth. "
        "Pack Quantity: 6. Product Type: Sanding. Suitable for detail file sanding applications."
    )

    monkeypatch.setattr(
        discovery, "search_candidate_urls",
        lambda q: {"urls": [url], "available": True, "error": None},
    )
    monkeypatch.setattr(discovery, "fetch_and_clean_url", lambda u: _fetch(text, "URL: only"))

    result = discovery.discover_and_enrich(MPN, DESC, MANUFACTURER, BRAND)
    assert result["status"] == "verified"
    assert result["source_url"] == url


def test_storefront_length_alone_does_not_beat_structured_specs():
    thin_store = {
        "text": (
            "Add to cart. Free shipping. Continue shopping. Regular price. "
            "Sale price. Wishlist. Secure checkout. " * 80
        )
    }
    rich_specs = {
        "text": """=== EXTRACTED SPECIFICATION TABLES ===
Sku: ABC123
Brand: Example
Pack Quantity: 6
Weight: 0.13 lb
Country of Origin: United States
Grit: 80
Backing: Cloth
Specifications
"""
    }

    thin_score, _ = discovery._content_richness_score(thin_store)
    rich_score, _ = discovery._content_richness_score(rich_specs)
    assert rich_score > thin_score
