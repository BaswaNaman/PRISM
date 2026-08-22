"""
Sparse product discovery for PRISM.

Purpose:
    Sparse organizer row (MPN + manufacturer + optional brand/description)
    -> Tavily Search proposes candidate URLs
    -> existing PRISM sourcing policy checks each URL
    -> existing SSRF-safe fetcher independently fetches it
    -> requested MPN and manufacturer/brand are corroborated
    -> only then reuse the existing extraction/validation/enrichment pipeline

Web-search output is discovery metadata only. It is never product-spec evidence.
"""

import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from . import sourcing
from .ingestion import fetch_and_clean_url
from .extractor import process_raw_product_text
from .validator import validate_and_enrich_record
from .enricher import enrich_missing_fields
from .database import save_product


MAX_CANDIDATES = 10
MIN_EXTRACTION_TEXT_CHARS = 120
DEFAULT_SEARCH_DEPTH = "advanced"
TAVILY_SEARCH_URL = "https://api.tavily.com/search"

_PLACEHOLDER_VALUES = {
    "", "unbranded", "no brand", "no unilog brand", "no dib brand",
    "n/a", "na", "none", "null", "unknown", "-", "--",
}

_COMPANY_SUFFIXES = {
    "inc", "incorporated", "ltd", "limited", "llc", "corp", "corporation",
    "company", "co", "plc", "gmbh", "ag", "sa", "srl", "pvt", "private",
}


def _meaningful(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    cleaned = re.sub(r"\s+", " ", str(value)).strip(" -\t\r\n")
    if cleaned.lower() in _PLACEHOLDER_VALUES:
        return None
    return cleaned or None


def _normalize_mpn(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def mpn_found_on_page(mfg_part_num: str, page_text: str) -> bool:
    """Boundary-aware, punctuation-tolerant MPN verification.

    Accepts e.g. DCB518ASTS06G <-> DCB518-ASTS06G, but rejects a requested
    ABC123 when it appears only inside XYZABC123999.
    """
    target = _normalize_mpn(mfg_part_num)
    if not target or not page_text:
        return False

    sep = r"[\s._/\-–—]*"
    body = sep.join(re.escape(ch) for ch in target)
    pattern = rf"(?<![A-Za-z0-9]){body}(?![A-Za-z0-9])"
    return re.search(pattern, page_text, flags=re.IGNORECASE) is not None


def _company_tokens(value: Optional[str]) -> List[str]:
    text = _meaningful(value)
    if not text:
        return []
    words = re.findall(r"[a-z0-9]+", text.lower())
    return [
        w for w in words
        if w not in _COMPANY_SUFFIXES and (len(w) >= 3 or any(c.isdigit() for c in w))
    ]


def manufacturer_corrobated(
    manufacturer: Optional[str],
    brand: Optional[str],
    page_text: str,
    page_title: str = "",
    url: str = "",
) -> bool:
    """Lightweight manufacturer/brand corroboration.

    MPN presence is checked separately and remains mandatory. This check requires
    at least one meaningful manufacturer or brand token in page text/title/domain.
    Company suffixes such as Inc/Ltd/LLC are ignored.
    """
    wanted = list(dict.fromkeys(_company_tokens(manufacturer) + _company_tokens(brand)))
    if not wanted:
        return True

    host = (urlparse(url).hostname or "").lower()
    evidence = " ".join([page_title or "", host, page_text or ""]).lower()
    evidence_compact = re.sub(r"[^a-z0-9]", "", evidence)
    host_compact = re.sub(r"[^a-z0-9]", "", host)

    for token in wanted:
        if re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", evidence):
            return True
        if token in host_compact:
            return True
        compact = re.sub(r"[^a-z0-9]", "", token)
        if len(compact) >= 4 and compact in evidence_compact:
            return True
    return False


def build_search_query(
    mfg_part_num: str,
    manufacturer: str,
    brand: Optional[str] = None,
    part_desc: Optional[str] = None,
) -> str:
    """Build a concise search query from sparse organizer fields."""
    parts: List[str] = []

    mpn = _meaningful(mfg_part_num)
    if mpn:
        parts.append(f'"{mpn}"')

    manuf = _meaningful(manufacturer)
    if manuf:
        manuf = re.sub(r"\s*\(\d+\)\s*$", "", manuf).strip()
        parts.append(f'"{manuf}"')

    brand_clean = _meaningful(brand)
    if brand_clean:
        parts.append(f'"{brand_clean}"')

    desc = _meaningful(part_desc)
    if desc:
        if mpn:
            desc = re.sub(re.escape(mpn), " ", desc, flags=re.IGNORECASE)
        desc = re.sub(r"[_|]+", " ", desc)
        desc = re.sub(r"\s+", " ", desc).strip(" -")
        if desc:
            words = desc.split()
            desc = " ".join(words[:10])
            if len(desc) > 100:
                desc = desc[:100].rsplit(" ", 1)[0]
            if desc:
                parts.append(desc)

    parts.append("product")
    return " ".join(parts).strip()


def _extract_candidate_urls(payload: Any) -> List[str]:
    """Extract and de-duplicate candidate URLs from a Tavily Search response."""
    if not isinstance(payload, dict):
        return []

    urls: List[str] = []
    for item in payload.get("results") or []:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if not isinstance(url, str):
            continue
        url = url.strip()
        if url.startswith(("http://", "https://")):
            urls.append(url)

    deduped: List[str] = []
    seen = set()
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        deduped.append(url)
        if len(deduped) >= MAX_CANDIDATES:
            break
    return deduped


def search_candidate_urls(query: str) -> Dict[str, Any]:
    """Use Tavily Search for candidate-source discovery only.

    Tavily results are never treated as product-spec evidence. The selected URL
    must still pass PRISM's source policy, SSRF-safe fetch, exact MPN check, and
    manufacturer/brand corroboration before the extraction pipeline sees it.
    """
    api_key = (os.environ.get("TAVILY_API_KEY") or "").strip()
    if len(api_key) < 10:
        return {
            "urls": [],
            "available": False,
            "error": "TAVILY_API_KEY is not configured; product discovery search is unavailable.",
        }

    search_depth = (
        os.environ.get("PRISM_DISCOVERY_SEARCH_DEPTH") or DEFAULT_SEARCH_DEPTH
    ).strip().lower()
    if search_depth not in {"basic", "advanced", "fast", "ultra-fast"}:
        search_depth = DEFAULT_SEARCH_DEPTH

    try:
        import httpx

        response = httpx.post(
            TAVILY_SEARCH_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "query": query,
                "search_depth": search_depth,
                "topic": "general",
                "max_results": MAX_CANDIDATES,
                "include_answer": False,
                "include_raw_content": False,
                "include_images": False,
            },
            timeout=20.0,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        return {
            "urls": [],
            "available": False,
            "error": f"Tavily Search call failed: {exc}",
        }

    return {
        "urls": _extract_candidate_urls(payload),
        "available": True,
        "error": None,
    }


def _fetch_is_extraction_ready(fetch_res: Dict[str, Any]) -> tuple[bool, str]:
    """Return whether a fetched page is rich enough to enter the extraction pipeline.

    Identity verification and extraction readiness are deliberately separate. A
    JavaScript-rendered manufacturer page may prove the exact MPN/brand from its
    title while still exposing too little specification text for safe enrichment.
    In that case discovery should continue to later candidates instead of claiming
    that the product record itself is verified.
    """
    if not fetch_res.get("success"):
        return False, fetch_res.get("error") or "fetch failed"

    text = (fetch_res.get("text") or "").strip()
    error = (fetch_res.get("error") or "").strip()
    error_lower = error.lower()

    sparse_markers = (
        "sparse content",
        "insufficient product specification text",
        "insufficient content",
        "client-side by javascript",
    )
    if any(marker in error_lower for marker in sparse_markers):
        return False, error or "Fetched page was too sparse for reliable extraction."

    if len(text) < MIN_EXTRACTION_TEXT_CHARS:
        return False, (
            f"Fetched page exposed only {len(text)} characters; at least "
            f"{MIN_EXTRACTION_TEXT_CHARS} are required before extraction."
        )

    return True, ""


def _record_status(record: Any) -> Optional[str]:
    """Read ProductIntelligenceRecord.overall_status from Pydantic or dict records."""
    if isinstance(record, dict):
        value = record.get("overall_status")
    else:
        value = getattr(record, "overall_status", None)
    return str(value).strip().lower() if value is not None else None


_CATEGORY_RANK = {"manufacturer": 0, "unknown": 1, "distributor": 2}


def _rank_candidates(urls: List[str]) -> List[str]:
    def rank_key(url: str):
        try:
            verdict = sourcing.evaluate_url(url)
            return _CATEGORY_RANK.get(verdict.category, 3)
        except Exception:
            return 4
    return sorted(urls, key=rank_key)


_SPEC_ROW_RE = re.compile(
    r"(?m)^\s*[A-Za-z][A-Za-z0-9 /_()#%&.+-]{1,58}\s*:\s*\S.{0,220}$"
)

_RICHNESS_KEYWORDS = (
    "specifications", "technical data", "technical specifications",
    "manufacturer part number", "model", "sku", "upc", "gtin",
    "weight", "dimensions", "dimension", "length", "width", "height",
    "diameter", "size", "material", "backing", "grit", "pack quantity",
    "country of origin", "voltage", "current", "pressure", "flow",
    "temperature", "capacity", "thread", "connection", "rating",
    "certification", "certifications",
)

_STOREFRONT_NOISE = (
    "add to cart", "your cart", "continue shopping", "free shipping",
    "secure checkout", "wishlist", "sale price", "regular price",
    "unit price", "sign in", "create account",
)


def _content_richness_score(fetch_res: Dict[str, Any]) -> tuple[float, Dict[str, Any]]:
    """Score *fetched source text* for useful product-detail density.

    This is deterministic and never uses Tavily snippets or LLM output as
    evidence. It exists only to choose among sources that have already passed
    sourcing policy, secure fetching, exact-MPN verification, manufacturer/
    brand corroboration, and extraction-readiness checks.

    The score favors explicit specification rows and technical/product
    attributes, while giving only a small bounded reward for raw text length so
    long navigation/storefront pages cannot win merely by being verbose.
    """
    text = (fetch_res.get("text") or "").strip()
    lower = text.lower()

    spec_rows = len(_SPEC_ROW_RE.findall(text))
    keyword_hits = sum(1 for kw in _RICHNESS_KEYWORDS if kw in lower)
    noise_hits = sum(1 for kw in _STOREFRONT_NOISE if kw in lower)

    has_extracted_table = "=== extracted specification tables ===" in lower
    has_spec_heading = any(
        heading in lower
        for heading in ("specifications", "technical data", "technical specifications")
    )

    # Length matters only weakly and is capped. Structured/product-specific
    # content is intentionally worth much more.
    length_points = min(len(text), 3000) / 150.0          # 0 .. 20
    row_points = min(spec_rows, 15) * 5.0                # 0 .. 75
    keyword_points = min(keyword_hits, 15) * 2.0         # 0 .. 30
    structure_points = (12.0 if has_extracted_table else 0.0) + (
        6.0 if has_spec_heading else 0.0
    )
    noise_penalty = min(noise_hits, 10) * 1.5            # 0 .. -15

    score = round(
        length_points + row_points + keyword_points + structure_points - noise_penalty,
        3,
    )

    return score, {
        "score": score,
        "content_length": len(text),
        "spec_rows": spec_rows,
        "keyword_hits": keyword_hits,
        "noise_hits": noise_hits,
        "has_extracted_spec_table": has_extracted_table,
    }


def _choose_best_verified_candidate(candidates: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Choose the richest source among candidates that already passed trust checks.

    Source category is only a tie-breaker. This preserves the sourcing gate:
    a disallowed URL can never reach this function.
    """
    if not candidates:
        return None

    def key(item: Dict[str, Any]):
        category = getattr(item["verdict"], "category", "unknown")
        category_rank = _CATEGORY_RANK.get(category, 3)
        # Higher richness wins; for an exact tie prefer the more trusted source
        # category and then the earlier ranked candidate.
        return (
            item["richness_score"],
            -category_rank,
            -item["candidate_rank"],
        )

    return max(candidates, key=key)


@dataclass
class DiscoveryTrace:
    search_query: str = ""
    candidate_urls_considered: List[str] = field(default_factory=list)
    rejections: List[Dict[str, Any]] = field(default_factory=list)
    selected_source_url: Optional[str] = None
    mpn_found_on_selected_source: bool = False
    manufacturer_corrobated_on_selected_source: bool = False
    search_grounding_available: bool = True
    search_error: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "search_query": self.search_query,
            "candidate_urls_considered": self.candidate_urls_considered,
            "rejections": self.rejections,
            "selected_source_url": self.selected_source_url,
            "mpn_found_on_selected_source": self.mpn_found_on_selected_source,
            "manufacturer_corrobated_on_selected_source": self.manufacturer_corrobated_on_selected_source,
            "search_grounding_available": self.search_grounding_available,
            "search_error": self.search_error,
        }


def _needs_review(reason: str, trace: DiscoveryTrace) -> Dict[str, Any]:
    return {
        "status": "needs_review",
        "reason": reason,
        "record": None,
        "source_url": None,
        "source_type": None,
        "trace": trace.as_dict(),
    }


def discover_and_enrich(
    mfg_part_num: str,
    part_desc: str,
    manufacturer: str,
    brand: Optional[str] = None,
) -> Dict[str, Any]:
    """Discover, independently verify, then enrich one sparse product row."""
    mpn = _meaningful(mfg_part_num)
    if not mpn:
        return _needs_review(
            "A manufacturer part number is required for safe product discovery.",
            DiscoveryTrace(),
        )

    trace = DiscoveryTrace()
    trace.search_query = build_search_query(mpn, manufacturer, brand, part_desc)

    search_result = search_candidate_urls(trace.search_query)
    trace.search_grounding_available = bool(search_result.get("available"))
    trace.search_error = search_result.get("error")

    if not trace.search_grounding_available:
        return _needs_review(trace.search_error or "Tavily Search is unavailable.", trace)

    candidates = list(search_result.get("urls") or [])[:MAX_CANDIDATES]
    trace.candidate_urls_considered = candidates

    if not candidates:
        return _needs_review(
            "Tavily Search returned no candidate product URLs for this part.", trace
        )

    # Do not stop at the first acceptable page. Several candidates can prove
    # the same product identity while exposing very different amounts of usable
    # product content. First apply every existing trust/safety gate to every
    # candidate; only then choose the richest verified source.
    verified_candidates: List[Dict[str, Any]] = []

    for candidate_rank, url in enumerate(_rank_candidates(candidates)):
        try:
            verdict = sourcing.evaluate_url(url)
        except Exception as exc:
            trace.rejections.append({
                "url": url, "stage": "sourcing", "reason": f"source evaluation failed: {exc}"
            })
            continue

        if not verdict.allowed:
            trace.rejections.append({
                "url": url, "stage": "sourcing", "reason": f"sourcing policy: {verdict.reason}"
            })
            continue

        try:
            fetch_res = fetch_and_clean_url(url)
        except Exception as exc:
            trace.rejections.append({
                "url": url, "stage": "fetch", "reason": f"secure fetch rejected/failed: {exc}"
            })
            continue

        page_text = fetch_res.get("text", "") or ""
        page_title = fetch_res.get("title", "") or ""

        if not fetch_res.get("success") and not page_text.strip():
            trace.rejections.append({
                "url": url,
                "stage": "fetch",
                "reason": fetch_res.get("error") or "fetch failed",
            })
            continue

        if not mpn_found_on_page(mpn, page_text):
            trace.rejections.append({
                "url": url,
                "stage": "identity",
                "mpn_matched": False,
                "manufacturer_corrobated": False,
                "reason": "requested manufacturer part number was not found as a complete identifier on the fetched page",
            })
            continue

        corroborated = manufacturer_corrobated(
            manufacturer=manufacturer,
            brand=brand,
            page_text=page_text,
            page_title=page_title,
            url=url,
        )
        if not corroborated:
            trace.rejections.append({
                "url": url,
                "stage": "identity",
                "mpn_matched": True,
                "manufacturer_corrobated": False,
                "reason": "MPN was present, but supplied manufacturer/brand could not be corroborated from page text, title, or domain",
            })
            continue

        extraction_ready, content_reason = _fetch_is_extraction_ready(fetch_res)
        if not extraction_ready:
            trace.rejections.append({
                "url": url,
                "stage": "content",
                "mpn_matched": True,
                "manufacturer_corrobated": True,
                "reason": (
                    "Product identity was corroborated, but the fetched page was not "
                    f"rich enough for reliable extraction: {content_reason}"
                ),
            })
            continue

        richness_score, richness = _content_richness_score(fetch_res)
        verified_candidates.append({
            "url": url,
            "verdict": verdict,
            "fetch_res": fetch_res,
            "page_text": page_text,
            "page_title": page_title,
            "richness_score": richness_score,
            "richness": richness,
            "candidate_rank": candidate_rank,
        })

    selected = _choose_best_verified_candidate(verified_candidates)
    if selected is None:
        return _needs_review(
            "No candidate source could be independently verified. Candidates were rejected by "
            "source policy, secure fetching, MPN verification, manufacturer/brand corroboration, "
            "or extraction-readiness checks.",
            trace,
        )

    url = selected["url"]
    verdict = selected["verdict"]
    fetch_res = selected["fetch_res"]
    page_text = selected["page_text"]
    page_title = selected["page_title"]

    trace.selected_source_url = url
    trace.mpn_found_on_selected_source = True
    trace.manufacturer_corrobated_on_selected_source = True

    record = process_raw_product_text(
        raw_text=page_text,
        product_name_hint=None,
        category_hint=None,
        input_mode="url",
        source_type=fetch_res["source_type"],
        source_origin=fetch_res["source_origin"],
        fetch_success=bool(fetch_res.get("success")),
        http_status=fetch_res["http_status"],
        error_message=fetch_res.get("error"),
        page_title=page_title,
    )
    record = validate_and_enrich_record(record, page_text)
    record = enrich_missing_fields(record, page_text)
    save_product(record)

    overall_status = _record_status(record)
    source_and_record_verified = overall_status in (None, "verified")

    return {
        "status": "verified" if source_and_record_verified else "needs_review",
        "reason": (
            None if source_and_record_verified else
            "The product source and identity were verified, but the resulting "
            "product intelligence record still requires review."
        ),
        "record": record,
        "source_url": url,
        "source_type": verdict.category,
        "trace": trace.as_dict(),
    }

