"""Focused, offline tests for PRISM sparse product discovery."""

import pytest
from unittest.mock import MagicMock, patch

from app import discovery

MPN = "DCB518ASTS06G"
MANUFACTURER = "Freud Inc"
BRAND = "Diablo"
DESC = 'DCB518ASTS06G Diablo 1/2"x18" - Sanding Belt 6pc'


def _fake_record():
    return {"id": "fake-record"}


@pytest.fixture(autouse=True)
def _stub_existing_pipeline(monkeypatch):
    monkeypatch.setattr(discovery, "process_raw_product_text", lambda **kw: _fake_record())
    monkeypatch.setattr(discovery, "validate_and_enrich_record", lambda record, text: record)
    monkeypatch.setattr(discovery, "enrich_missing_fields", lambda record, text: record)
    monkeypatch.setattr(discovery, "save_product", lambda record: None)


def _search(urls, available=True, error=None):
    return {"urls": urls, "available": available, "error": error}


def _fetch(text, title="Freud Tools Product", origin="URL: freudtools.com"):
    # Keep ordinary fixtures comfortably above discovery's minimum extraction
    # content threshold. Individual sparse-content tests override this explicitly.
    rich_text = text + (" Product specifications and manufacturer information." * 4)
    return {
        "success": True,
        "http_status": 200,
        "error": None,
        "text": rich_text,
        "title": title,
        "source_type": "url_ingestion",
        "source_origin": origin,
    }


def _sparse_fetch(text, title="Diablo Tools Product", origin="URL: diablotools.com"):
    return {
        "success": True,
        "http_status": 200,
        "error": (
            "Sparse content: the page was reachable (HTTP 200) but exposed insufficient "
            "product specification text to a server-side fetch. This usually means the "
            "specifications are rendered client-side by JavaScript."
        ),
        "text": text,
        "title": title,
        "source_type": "url_ingestion",
        "source_origin": origin,
    }


def test_valid_candidate_is_accepted_and_part_desc_not_used_as_name_hint(monkeypatch):
    url = "https://www.freudtools.com/products/dcb518asts06g"
    monkeypatch.setattr(discovery, "search_candidate_urls", lambda q: _search([url]))
    monkeypatch.setattr(
        discovery,
        "fetch_and_clean_url",
        lambda u: _fetch(f"Freud Diablo sanding belt. Part number {MPN}."),
    )

    seen = {}
    def fake_extract(**kwargs):
        seen.update(kwargs)
        return _fake_record()
    monkeypatch.setattr(discovery, "process_raw_product_text", fake_extract)

    result = discovery.discover_and_enrich(MPN, DESC, MANUFACTURER, BRAND)

    assert result["status"] == "verified"
    assert result["source_url"] == url
    assert seen["product_name_hint"] is None
    assert result["trace"]["mpn_found_on_selected_source"] is True
    assert result["trace"]["manufacturer_corrobated_on_selected_source"] is True


def test_sparse_identity_candidate_does_not_stop_later_rich_candidate(monkeypatch):
    sparse_url = "https://diablotools.com/products/DCB518ASTS06G"
    rich_url = "https://www.freudtools.com/products/DCB518ASTS06G/details"
    monkeypatch.setattr(discovery, "search_candidate_urls", lambda q: _search([sparse_url, rich_url]))

    def fake_fetch(url):
        if url == sparse_url:
            return _sparse_fetch(
                f"{MPN} | Sanding Belts | Diablo Tools",
                title=f"{MPN} | Sanding Belts | Diablo Tools",
            )
        return _fetch(f"Freud Diablo product {MPN} with detailed sanding belt specifications")

    monkeypatch.setattr(discovery, "fetch_and_clean_url", fake_fetch)
    result = discovery.discover_and_enrich(MPN, DESC, MANUFACTURER, BRAND)

    assert result["status"] == "verified"
    assert result["source_url"] == rich_url
    assert any(r.get("url") == sparse_url and r.get("stage") == "content"
               for r in result["trace"]["rejections"])


def test_only_sparse_verified_identity_returns_needs_review_without_record(monkeypatch):
    url = "https://diablotools.com/products/DCB518ASTS06G"
    monkeypatch.setattr(discovery, "search_candidate_urls", lambda q: _search([url]))
    monkeypatch.setattr(
        discovery,
        "fetch_and_clean_url",
        lambda u: _sparse_fetch(
            f"{MPN} | Sanding Belts | Diablo Tools",
            title=f"{MPN} | Sanding Belts | Diablo Tools",
        ),
    )

    result = discovery.discover_and_enrich(MPN, DESC, MANUFACTURER, BRAND)

    assert result["status"] == "needs_review"
    assert result["record"] is None
    assert result["source_url"] is None
    assert any(r.get("stage") == "content" for r in result["trace"]["rejections"])


def test_record_needs_review_is_not_reported_as_top_level_verified(monkeypatch):
    url = "https://www.freudtools.com/products/dcb518asts06g"
    monkeypatch.setattr(discovery, "search_candidate_urls", lambda q: _search([url]))
    monkeypatch.setattr(
        discovery,
        "fetch_and_clean_url",
        lambda u: _fetch(f"Freud Diablo sanding belt {MPN} with product specifications"),
    )
    monkeypatch.setattr(
        discovery, "process_raw_product_text",
        lambda **kw: {"id": "fake-record", "overall_status": "needs_review"},
    )

    result = discovery.discover_and_enrich(MPN, DESC, MANUFACTURER, BRAND)

    assert result["status"] == "needs_review"
    assert result["source_url"] == url
    assert result["record"]["overall_status"] == "needs_review"
    assert "record still requires review" in result["reason"]


def test_page_without_mpn_is_rejected(monkeypatch):
    url = "https://www.freudtools.com/products/other"
    monkeypatch.setattr(discovery, "search_candidate_urls", lambda q: _search([url]))
    monkeypatch.setattr(discovery, "fetch_and_clean_url", lambda u: _fetch("Freud other sanding belt"))

    result = discovery.discover_and_enrich(MPN, DESC, MANUFACTURER, BRAND)

    assert result["status"] == "needs_review"
    assert result["record"] is None
    assert any(r.get("mpn_matched") is False for r in result["trace"]["rejections"])


def test_second_candidate_is_selected(monkeypatch):
    urls = [
        "https://www.freudtools.com/products/wrong",
        "https://www.freudtools.com/products/right",
    ]
    monkeypatch.setattr(discovery, "search_candidate_urls", lambda q: _search(urls))
    monkeypatch.setattr(
        discovery,
        "fetch_and_clean_url",
        lambda u: _fetch("Freud wrong product") if u == urls[0]
        else _fetch(f"Freud Diablo product {MPN}"),
    )

    result = discovery.discover_and_enrich(MPN, DESC, MANUFACTURER, BRAND)

    assert result["status"] == "verified"
    assert result["source_url"] == urls[1]


def test_marketplace_rejected_before_fetch(monkeypatch):
    monkeypatch.setattr(
        discovery, "search_candidate_urls",
        lambda q: _search(["https://www.amazon.com/dp/fake"]),
    )
    fetch = MagicMock()
    monkeypatch.setattr(discovery, "fetch_and_clean_url", fetch)

    result = discovery.discover_and_enrich(MPN, DESC, MANUFACTURER, BRAND)

    assert result["status"] == "needs_review"
    fetch.assert_not_called()
    assert any("sourcing policy" in r["reason"] for r in result["trace"]["rejections"])


def test_private_candidate_is_safely_rejected(monkeypatch):
    monkeypatch.setattr(
        discovery, "search_candidate_urls",
        lambda q: _search(["http://127.0.0.1/admin"]),
    )
    monkeypatch.setattr(
        discovery, "fetch_and_clean_url",
        lambda u: {"success": False, "http_status": 400, "error": "private address blocked", "text": ""},
    )
    result = discovery.discover_and_enrich(MPN, DESC, MANUFACTURER, BRAND)
    assert result["status"] == "needs_review"
    assert result["source_url"] is None


def test_nonexistent_candidate_fetch_failure_is_safe(monkeypatch):
    url = "https://www.freudtools.com/nonexistent"
    monkeypatch.setattr(discovery, "search_candidate_urls", lambda q: _search([url]))
    monkeypatch.setattr(
        discovery, "fetch_and_clean_url",
        lambda u: {"success": False, "http_status": 404, "error": "HTTP 404", "text": ""},
    )
    result = discovery.discover_and_enrich(MPN, DESC, MANUFACTURER, BRAND)
    assert result["status"] == "needs_review"
    assert result["record"] is None


def test_no_valid_source_returns_needs_review_without_fabrication(monkeypatch):
    monkeypatch.setattr(discovery, "search_candidate_urls", lambda q: _search([]))
    result = discovery.discover_and_enrich(MPN, DESC, MANUFACTURER, BRAND)
    assert result["status"] == "needs_review"
    assert result["record"] is None
    assert result["source_url"] is None


def test_tavily_search_unavailable_is_clear(monkeypatch):
    monkeypatch.setattr(
        discovery, "search_candidate_urls",
        lambda q: _search([], available=False, error="Tavily Search unavailable"),
    )
    result = discovery.discover_and_enrich(MPN, DESC, MANUFACTURER, BRAND)
    assert result["status"] == "needs_review"
    assert "Tavily" in result["reason"]


@pytest.mark.parametrize(
    "requested,page,expected",
    [
        ("DCB518ASTS06G", "Part DCB518ASTS06G", True),
        ("DCB518ASTS06G", "Part DCB518-ASTS06G", True),
        ("DCB518-ASTS06G", "Part DCB518ASTS06G", True),
        ("dcb518asts06g", "Part DCB518_ASTS06G", True),
        ("ABC123", "Part XYZABC123999", False),
        ("ABC123", "ABC1234", False),
        ("ABC123", "XABC123", False),
        ("", "ABC123", False),
    ],
)
def test_boundary_aware_mpn_matching(requested, page, expected):
    assert discovery.mpn_found_on_page(requested, page) is expected


def test_manufacturer_suffix_normalization():
    assert discovery.manufacturer_corrobated(
        "Freud Inc", None, "Official Freud product information",
        url="https://freudtools.com/product",
    )


def test_brand_can_corroborate_when_manufacturer_name_is_absent():
    assert discovery.manufacturer_corrobated(
        "Parent Holdings LLC", "Diablo", "Diablo sanding belt catalog",
        url="https://example-manufacturer.com/product",
    )


def test_unrelated_manufacturer_is_rejected(monkeypatch):
    url = "https://www.exampletools.com/product/abc"
    monkeypatch.setattr(discovery, "search_candidate_urls", lambda q: _search([url]))
    monkeypatch.setattr(
        discovery,
        "fetch_and_clean_url",
        lambda u: _fetch(
            f"Example Tools cross-reference list includes {MPN}.",
            title="Example Tools",
            origin="URL: exampletools.com",
        ),
    )
    result = discovery.discover_and_enrich(MPN, DESC, MANUFACTURER, BRAND)
    assert result["status"] == "needs_review"
    assert any("manufacturer/brand" in r["reason"] for r in result["trace"]["rejections"])


@pytest.mark.parametrize("placeholder", [
    "Unbranded", "No Unilog Brand", "No DIB Brand", "N/A", "Unknown", "--",
])
def test_placeholder_brand_is_not_in_search_query(placeholder):
    query = discovery.build_search_query(MPN, MANUFACTURER, placeholder, DESC)
    assert placeholder.lower() not in query.lower()


def test_description_contributes_to_search_query():
    query = discovery.build_search_query(MPN, MANUFACTURER, BRAND, DESC)
    assert "sanding belt" in query.lower()
    assert MPN in query
    assert "Freud" in query


def test_search_extracts_tavily_result_urls(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-fake-key-for-tests")
    monkeypatch.setenv("PRISM_DISCOVERY_SEARCH_DEPTH", "advanced")

    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "results": [
            {"url": "https://freudtools.com/product", "score": 0.99},
            {"url": "https://freudtools.com/product", "score": 0.98},
            {"url": "ftp://invalid.example/product", "score": 0.97},
        ]
    }

    with patch("httpx.post", return_value=response) as post:
        result = discovery.search_candidate_urls('"ABC" product')

    assert result["available"] is True
    assert result["urls"] == ["https://freudtools.com/product"]

    kwargs = post.call_args.kwargs
    assert kwargs["headers"]["Authorization"] == "Bearer tvly-fake-key-for-tests"
    assert kwargs["json"]["search_depth"] == "advanced"
    assert kwargs["json"]["max_results"] == discovery.MAX_CANDIDATES
    assert kwargs["json"]["include_answer"] is False
    assert kwargs["json"]["include_raw_content"] is False


def test_tavily_search_does_not_use_result_content_as_url(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-fake-key-for-tests")
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "results": [
            {
                "url": None,
                "content": "Maybe https://invented.invalid/product",
            }
        ]
    }

    with patch("httpx.post", return_value=response):
        result = discovery.search_candidate_urls('"ABC" product')

    assert result["available"] is True
    assert result["urls"] == []


def test_missing_tavily_key_is_unavailable(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    result = discovery.search_candidate_urls('"ABC" product')
    assert result["available"] is False
    assert "TAVILY_API_KEY" in result["error"]


def test_tavily_http_error_is_safe(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-fake-key-for-tests")
    with patch("httpx.post", side_effect=RuntimeError("network unavailable")):
        result = discovery.search_candidate_urls('"ABC" product')
    assert result["available"] is False
    assert result["urls"] == []
    assert "Tavily Search call failed" in result["error"]


def test_existing_enrich_route_still_registered():
    from app.main import app
    paths = {route.path for route in app.routes}
    assert "/api/enrich" in paths
    assert "/api/discover-enrich" in paths
