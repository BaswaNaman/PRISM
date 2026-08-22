"""
test_fetch.py — manual diagnostic run of a raw HTTP fetch + text extraction
against a live URL.

This is a diagnostic/demo script rather than an assertion-based test (it just
prints scraped page content, which requires network access), so it is guarded
to run only when executed directly and is not collected as a pytest test.

Run directly:
    python test_fetch.py
"""

import httpx
from bs4 import BeautifulSoup

URL = ("https://www.automationdirect.com/adc/shopping/catalog/"
       "sensors_-z-_encoders/inductive_proximity_sensors/8mm_tubular/dw-ad-511-m8")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive"
}

SPEC_KEYWORDS = ["voltage", "ip6", "ip2", "temp", "housing", "current", "pnp", "npn", "m8", "range"]


def main(url: str = URL) -> int:
    with httpx.Client(follow_redirects=True, headers=HEADERS, timeout=15.0) as client:
        resp = client.get(url)
        print("HTTP STATUS:", resp.status_code)
        soup = BeautifulSoup(resp.text, "html.parser")
        title = soup.title.string.strip() if soup.title else ""

        # Remove scripts, styles, nav, footer, header
        for el in soup(["script", "style", "nav", "footer", "header", "noscript", "iframe", "aside"]):
            el.decompose()

        text = soup.get_text()
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        cleaned = "\n".join(lines)

        print(f"Title: {title}")
        print(f"Extracted Length: {len(cleaned)} chars")
        print("--- SPECS SEARCH IN CLEANED TEXT ---")
        for line in lines:
            if any(k in line.lower() for k in SPEC_KEYWORDS):
                print("  >", line[:100])

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
