import io
import re
import urllib.parse
from typing import Any, Dict, List, Optional

import httpx
from bs4 import BeautifulSoup
import pypdf

from . import sourcing, urlguard

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive"
}

MIN_CONTENT_LENGTH_THRESHOLD = 200

# Containers that typically hold the real product content. Checked in priority
# order; the first one with enough text wins. Isolating the main region before
# text extraction is the single biggest accuracy lever for URL ingestion, because
# it removes the navigation/marketing/footer noise that produces stray numbers.
MAIN_CONTENT_SELECTORS = [
    "main", "article", '[role="main"]', "#main", "#content", "#mainContent",
    "#main-content", ".main-content", ".product-detail", ".product-details",
    ".product-info", ".pdp", ".pdp-content", "#productDetails", ".specifications",
    ".specs", "#specs", ".tech-specs", ".product-specs", "#mw-content-text",
]

# Words that mark a table/section as containing specifications.
SPEC_SECTION_HINT = re.compile(
    r"spec|attribute|parameter|technical|character|rating|dimension|property|detail",
    re.IGNORECASE,
)

# Granular per-phase timeouts, sourced from the guard so there is one place to
# tune them. A single total timeout is not enough: a server that trickles one
# byte at a time never trips a connect timeout.
HTTP_TIMEOUT = httpx.Timeout(
    connect=urlguard.CONNECT_TIMEOUT,
    read=urlguard.READ_TIMEOUT,
    write=urlguard.WRITE_TIMEOUT,
    pool=urlguard.POOL_TIMEOUT,
)


def _tables_to_text(soup) -> str:
    """Flatten HTML tables and definition lists into 'Label: Value' lines.

    Spec sheets on the web are overwhelmingly two-column tables. Rendering them as
    'Label: Value' gives the downstream extractor the label/number adjacency its
    context guards depend on — a plain get_text() would separate a spec's name from
    its value with arbitrary whitespace and defeat those guards.
    """
    out = []
    for table in soup.find_all("table"):
        try:
            caption_bits = []
            if table.caption and table.caption.get_text(strip=True):
                caption_bits.append(table.caption.get_text(strip=True))
            ident = " ".join(table.get("class", []) or []) + " " + str(table.get("id", "") or "")
            if caption_bits:
                out.append(f"[{caption_bits[0]}]")
            elif SPEC_SECTION_HINT.search(ident):
                out.append("[Specifications]")

            for row in table.find_all("tr"):
                cells = row.find_all(["th", "td"])
                texts = [c.get_text(" ", strip=True) for c in cells]
                texts = [t for t in texts if t]
                if len(texts) >= 2:
                    label = texts[0].rstrip(":").strip()
                    value = " ".join(texts[1:]).strip()
                    if label and value and len(label) <= 80:
                        out.append(f"{label}: {value}")
                elif len(texts) == 1 and len(texts[0]) <= 120:
                    out.append(texts[0])
        except Exception:
            continue

    # Definition lists are the other common spec layout.
    for dl in soup.find_all("dl"):
        try:
            terms = dl.find_all("dt")
            for dt in terms:
                dd = dt.find_next_sibling("dd")
                if dd is None:
                    continue
                label = dt.get_text(" ", strip=True).rstrip(":").strip()
                value = dd.get_text(" ", strip=True)
                if label and value and len(label) <= 80:
                    out.append(f"{label}: {value}")
        except Exception:
            continue

    return "\n".join(out)


def _select_main_region(soup):
    """Return the densest plausible main-content node, or None to use the whole doc."""
    best = None
    best_len = 0
    for sel in MAIN_CONTENT_SELECTORS:
        try:
            for node in soup.select(sel):
                text_len = len(node.get_text(" ", strip=True))
                if text_len > best_len:
                    best, best_len = node, text_len
        except Exception:
            continue
    # Only trust the isolated region if it carries real substance; otherwise the
    # page may put its content outside any recognised container.
    if best is not None and best_len >= MIN_CONTENT_LENGTH_THRESHOLD:
        return best
    return None


def _failure(
    *,
    url: str,
    domain: str,
    status: int,
    error: str,
    text: str,
    verdict_dict: Optional[dict] = None,
    notes: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Build the standard failure payload.

    Every fetch refusal returns the same key set the success path returns, so a
    security rejection is an ordinary application-level result the API can render
    — not an exception that reaches the user as a 500 and a traceback.
    """
    return {
        "success": False,
        "http_status": status,
        "error": error,
        "text": text,
        "content_length": 0,
        "title": domain,
        "source_type": "url_ingestion",
        "source_origin": f"URL: {domain} ({url})",
        "sourcing": verdict_dict,
        "cleaning_notes": notes or [],
    }


def _safe_domain(url: str) -> str:
    """Best-effort host for logging/labelling, never raising."""
    try:
        split = urllib.parse.urlsplit(url)
        if split.netloc:
            return split.netloc
        return urllib.parse.urlsplit("https://" + url).netloc or "web_source"
    except Exception:
        return "web_source"


def _fetch_validated(
    client: "httpx.Client",
    start_url: str,
    sourcing_policy: Optional[str],
    resolver=None,
) -> Dict[str, Any]:
    """Walk the redirect chain manually, validating every hop before requesting it.

    `follow_redirects` is deliberately off. Letting httpx follow redirects would
    mean only the first URL was ever checked, and a public page that 302s to
    `http://169.254.169.254/` is the standard way past a validate-once fetcher.

    Returns a dict holding either `html`, or `status` for a non-200, or `failure`
    with a ready-to-return payload.
    """
    current = start_url
    chain: List[str] = []

    for _ in range(urlguard.MAX_REDIRECTS + 1):
        with client.stream("GET", current) as resp:
            if resp.is_redirect:
                location = resp.headers.get("location", "")
                target = urlguard.resolve_redirect_target(current, location)

                # The whole point of the manual loop: the new target gets the
                # same treatment the original URL did, before we request it.
                validated = urlguard.validate_url(target, resolver=resolver)
                hop_domain = validated.netloc

                hop_verdict = sourcing.evaluate_url(validated.url, policy=sourcing_policy)
                if not hop_verdict.allowed:
                    print(f"[Sourcing] BLOCKED redirect target {hop_domain} "
                          f"({hop_verdict.category}) under policy {hop_verdict.policy}")
                    return {"failure": _failure(
                        url=validated.url, domain=hop_domain, status=403,
                        error=(f"Redirect rejected: {current} redirected to "
                               f"{hop_domain}, which is classified as a "
                               f"{hop_verdict.category}. {hop_verdict.reason}"),
                        text=f"Fetch Error: redirect target rejected ({hop_verdict.category}) — {hop_domain}",
                        verdict_dict=hop_verdict.as_dict(),
                        notes=[f"redirect blocked by sourcing policy: {hop_verdict.policy}"],
                    )}

                chain.append(validated.url)
                current = validated.url
                continue

            status_code = resp.status_code
            if status_code != 200:
                return {"status": status_code, "chain": chain}

            # Two-stage size cap: the declared length short-circuits before any
            # body is read, then the byte counter enforces the real limit,
            # because Content-Length is optional and can lie.
            urlguard.check_declared_length(resp.headers.get("content-length"))
            body = urlguard.read_capped(resp.iter_bytes(), urlguard.MAX_RESPONSE_BYTES)

            encoding = resp.encoding or "utf-8"
            try:
                html = body.decode(encoding, errors="replace")
            except LookupError:
                html = body.decode("utf-8", errors="replace")

            return {"html": html, "final_url": current, "chain": chain}

    raise urlguard.TooManyRedirectsError(
        f"Redirect chain exceeded {urlguard.MAX_REDIRECTS} hops "
        f"(last target: {current}); refusing to continue."
    )


def fetch_and_clean_url(url: str, sourcing_policy: Optional[str] = None) -> Dict[str, Any]:
    raw_input_url = (url or "").strip()

    # ---- SSRF gate, stage 1: parse-only ----------------------------------
    # Scheme, embedded credentials, port and any IP literal are settled before
    # anything touches the network — not even a DNS lookup. A `file://` URL or a
    # `http://127.0.0.1` costs zero network activity.
    try:
        pre = urlguard.validate_url(raw_input_url, resolve=False)
    except urlguard.SafeFetchError as exc:
        domain = _safe_domain(raw_input_url)
        print(f"[URL Guard] REJECTED {raw_input_url!r}: {exc}")
        return _failure(
            url=raw_input_url, domain=domain, status=400,
            error=f"URL rejected by security policy: {exc}",
            text=f"Fetch Error: URL rejected by security policy — {exc}",
            notes=["rejected by SSRF guard (pre-resolution)"],
        )

    url_clean = pre.url
    domain = urllib.parse.urlsplit(url_clean).netloc or "web_source"

    # ---- sourcing gate ---------------------------------------------------
    # Checked before the request is made, not after. The guidelines require
    # manufacturer sources; fetching a marketplace page and then deciding not to
    # trust it wastes a request and risks the text leaking into the record.
    # It also runs before DNS, so a policy-rejected source triggers no lookup.
    verdict = sourcing.evaluate_url(url_clean, policy=sourcing_policy)
    if not verdict.allowed:
        print(f"[Sourcing] BLOCKED {domain} ({verdict.category}) under policy {verdict.policy}")
        return {
            "success": False,
            "http_status": 403,
            "error": (f"Source rejected: {domain} is classified as a "
                      f"{verdict.category}. {verdict.reason}"),
            "text": f"Fetch Error: source rejected ({verdict.category}) — {domain}",
            "content_length": 0,
            "title": domain,
            "source_type": "url_ingestion",
            "source_origin": f"URL: {domain} ({url_clean})",
            "sourcing": verdict.as_dict(),
            "cleaning_notes": [f"blocked by sourcing policy: {verdict.policy}"],
        }

    # ---- SSRF gate, stage 2: resolve and check every address -------------
    # A hostname is not a security boundary. `internal.example.com` can answer
    # with 10.0.0.5, so the name is resolved and every address it returns is
    # checked. Fails closed when the host cannot be resolved.
    try:
        validated = urlguard.validate_url(url_clean)
    except urlguard.SafeFetchError as exc:
        print(f"[URL Guard] REJECTED {url_clean!r}: {exc}")
        return _failure(
            url=url_clean, domain=domain, status=400,
            error=f"URL rejected by security policy: {exc}",
            text=f"Fetch Error: URL rejected by security policy — {exc}",
            verdict_dict=verdict.as_dict(),
            notes=["rejected by SSRF guard (post-resolution)"],
        )

    redirect_chain: List[str] = []
    try:
        with httpx.Client(
            timeout=HTTP_TIMEOUT,
            follow_redirects=False,   # hops are validated one at a time instead
            headers=BROWSER_HEADERS,
        ) as client:
            outcome = _fetch_validated(client, validated.url, sourcing_policy)

        if "failure" in outcome:
            return outcome["failure"]

        redirect_chain = outcome.get("chain", []) or []

        if "status" in outcome:
            status_code = outcome["status"]
            err_msg = f"Could not fetch this page (HTTP {status_code})."
            print(f"[URL Fetcher Error] {domain} returned HTTP {status_code}")
            return {
                "success": False, "http_status": status_code, "error": err_msg,
                "text": f"Fetch Error: HTTP {status_code} on {domain}",
                "content_length": 0, "title": domain, "source_type": "url_ingestion",
                "source_origin": f"URL: {domain} ({url_clean})",
                "sourcing": verdict.as_dict()
            }

        html_content = outcome["html"]

    except urlguard.ResponseTooLargeError as e:
        print(f"[URL Guard] SIZE LIMIT {url_clean}: {e}")
        return _failure(
            url=url_clean, domain=domain, status=413,
            error=f"Response too large: {e}",
            text=f"Fetch Error: response too large — {e}",
            verdict_dict=verdict.as_dict(),
            notes=["aborted by response size cap"],
        )

    except urlguard.SafeFetchError as e:
        # Covers an unsafe redirect target and an over-long redirect chain.
        print(f"[URL Guard] REJECTED during fetch {url_clean}: {e}")
        return _failure(
            url=url_clean, domain=domain, status=400,
            error=f"Request rejected by security policy: {e}",
            text=f"Fetch Error: request rejected by security policy — {e}",
            verdict_dict=verdict.as_dict(),
            notes=["rejected by SSRF guard (redirect chain)"],
        )

    except Exception as e:
        err_msg = f"Network or SSL Error accessing {domain}: {str(e)}"
        print(f"[URL Fetcher Exception] {url_clean}: {e}")
        return {
            "success": False, "http_status": 500, "error": err_msg,
            "text": f"Fetch Error: {str(e)}", "content_length": 0, "title": domain,
            "source_type": "url_ingestion", "source_origin": f"URL: {domain} ({url_clean})",
            "sourcing": verdict.as_dict()
        }

    soup = BeautifulSoup(html_content, "html.parser")

    page_title = domain
    if soup.title and soup.title.string:
        page_title = soup.title.string.strip()
    else:
        meta_og = soup.find("meta", property="og:title")
        h1_tag = soup.find("h1")
        if meta_og and hasattr(meta_og, "get") and meta_og.get("content"):
            page_title = meta_og.get("content", "").strip()
        elif h1_tag and hasattr(h1_tag, "get_text"):
            page_title = h1_tag.get_text().strip()

    # Pull spec tables BEFORE the noise-stripping pass, because some sites wrap
    # their spec tables in containers whose class names look like navigation.
    spec_table_text = _tables_to_text(soup)

    for element in soup(["script", "style", "nav", "footer", "header", "noscript", "iframe", "aside", "form", "svg", "canvas", "select", "option", "button"]):
        element.decompose()

    NOISE_PATTERN = re.compile(
        r"nav(bar|igation)?|menu|footer|breadcrumb|cookie|sidebar|social|masthead|"
        r"header|dropdown|modal|popup|tooltip|newsletter|subscribe|country.?select|"
        r"related|recommend|upsell|cross.?sell|review|rating.?stars|share|print|"
        r"copyright|legal|disclaimer|advert|banner|promo|carousel|reference|citation|"
        r"catlinks|navbox|infobox.?hidden|edit.?section|mw-jump|siteSub|toc",
        re.IGNORECASE
    )

    # BULLETPROOF HTML PARSING
    for element in soup.find_all(["div", "ul", "ol", "section", "span"]):
        try:
            if not element.attrs:
                continue
            class_list = element.get("class", [])
            if not isinstance(class_list, list):
                class_list = [str(class_list)]

            id_val = element.get("id", "")
            identifiers = " ".join(class_list) + " " + str(id_val)

            if identifiers.strip() and NOISE_PATTERN.search(identifiers):
                element.decompose()
        except Exception:
            # Ignore completely malformed HTML tags to prevent crashes
            continue

    # Narrow to the main content region when one is identifiable. Everything
    # outside it (chrome, promos, footers) is discarded before text extraction.
    region = _select_main_region(soup) or soup
    isolated_region = region is not soup

    lines = [line.strip() for line in region.get_text().splitlines()]
    chunks = [phrase.strip() for line in lines for phrase in line.split("  ")]
    chunks = [c for c in chunks if c]

    deduped = []
    short_run = []

    def flush_short_run():
        if len(short_run) > 15:
            deduped.extend(short_run[:3])
            deduped.append("[...omitted repetitive list content...]")
        else:
            deduped.extend(short_run)
        short_run.clear()

    for chunk in chunks:
        if len(chunk) <= 25 and chunk.count(" ") <= 3:
            short_run.append(chunk)
        else:
            flush_short_run()
            deduped.append(chunk)
    flush_short_run()

    cleaned_text = "\n".join(deduped)

    # Prepend the flattened spec tables: they are the highest-value text on the
    # page and putting them first means the extractor's first regex match is far
    # more likely to be a real specification than a stray number in prose.
    if spec_table_text.strip():
        cleaned_text = (
            "=== EXTRACTED SPECIFICATION TABLES ===\n"
            + spec_table_text.strip()
            + "\n\n=== PAGE CONTENT ===\n"
            + cleaned_text
        )

    content_len = len(cleaned_text)

    notes = []
    if isolated_region:
        notes.append("main content region isolated")
    if spec_table_text.strip():
        table_lines = len([l for l in spec_table_text.splitlines() if l.strip()])
        notes.append(f"{table_lines} spec-table rows recovered")
    notes.append(f"source classified as {verdict.category} (policy: {verdict.policy})")
    if redirect_chain:
        notes.append(f"{len(redirect_chain)} redirect(s) followed, each revalidated: "
                     + " -> ".join(redirect_chain))

    # An advisory that travels with the record: the fetch succeeded, but the
    # source was not positively confirmed to be the manufacturer.
    sourcing_advisory = None
    if verdict.needs_review:
        sourcing_advisory = f"Source advisory: {verdict.reason}"
        notes.append("source not verified as manufacturer — values flagged for review")

    if content_len < MIN_CONTENT_LENGTH_THRESHOLD:
        # Sparse is a *warning*, not a hard failure: the page was reachable, it
        # simply did not expose product specs to a server-side fetch (usually
        # because the specs are rendered client-side by JavaScript). We still hand
        # the text downstream so extraction can try, and the record lands in the
        # human review queue rather than being reported as a dead fetch.
        sparse_msg = (
            "Sparse content: the page was reachable (HTTP 200) but exposed insufficient "
            "product specification text to a server-side fetch. This usually means the "
            "specifications are rendered client-side by JavaScript. Extraction proceeded "
            "on a best-effort basis — treat results as needing human review."
        )
        if sourcing_advisory:
            sparse_msg += " " + sourcing_advisory
        return {
            "success": True,
            "http_status": 200,
            "error": sparse_msg,
            "text": f"Sparse Content Warning: {page_title}\n\n{cleaned_text}",
            "content_length": content_len,
            "title": page_title,
            "source_type": "url_ingestion",
            "source_origin": f"URL: {domain} ({url_clean})",
            "sparse": True,
            "cleaning_notes": notes,
            "sourcing": verdict.as_dict(),
        }

    return {
        "success": True, "http_status": 200, "error": sourcing_advisory, "text": cleaned_text,
        "content_length": content_len, "title": page_title, "source_type": "url_ingestion",
        "source_origin": f"URL: {domain} ({url_clean})",
        "sparse": False,
        "cleaning_notes": notes,
        "sourcing": verdict.as_dict(),
    }

def extract_text_from_pdf(file_bytes: bytes, filename: str) -> Dict[str, Any]:
    try:
        pdf_file = io.BytesIO(file_bytes)
        reader = pypdf.PdfReader(pdf_file)
        num_pages = len(reader.pages)
        if num_pages == 0:
            return {"success": False, "http_status": 400, "error": "0 pages.", "text": "", "content_length": 0, "title": filename, "source_type": "pdf_upload", "source_origin": f"PDF: {filename}"}

        extracted_pages = []
        for i, page in enumerate(reader.pages):
            page_text = page.extract_text() or ""
            if page_text.strip():
                extracted_pages.append(f"--- [Page {i+1}] ---\n{page_text.strip()}")

        full_text = "\n\n".join(extracted_pages)
        return {
            "success": True, "http_status": 200, "error": None, "text": full_text,
            "content_length": len(full_text), "title": f"PDF: {filename}", "source_type": "pdf_upload",
            "source_origin": f"PDF: {filename}"
        }
    except Exception as e:
        return {"success": False, "http_status": 500, "error": str(e), "text": "", "content_length": 0, "title": filename, "source_type": "pdf_upload", "source_origin": f"PDF: {filename}"}