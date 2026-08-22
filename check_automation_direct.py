import httpx
from bs4 import BeautifulSoup
from app.ingestion import BROWSER_HEADERS

url = "https://www.automationdirect.com/adc/shopping/catalog/sensors_-z-_encoders/inductive_proximity_sensors/8mm_tubular/dw-ad-511-m8"
with httpx.Client(follow_redirects=True, headers=BROWSER_HEADERS, timeout=15.0) as client:
    resp = client.get(url)
    soup = BeautifulSoup(resp.text, "html.parser")
    for el in soup(["script", "style", "nav", "footer", "header", "noscript"]):
        el.decompose()
    text = soup.get_text()

print("--- SEARCHING SPECS IN AUTOMATIONDIRECT TEXT ---")
lines = [line.strip() for line in text.splitlines() if line.strip()]
for line in lines:
    l_lower = line.lower()
    if any(k in l_lower for k in ["volt", "ip", "brass", "chrome", "temp", "ma", "amp", "sensing", "pnp", "npn"]):
        print("  MATCH LINE:", line)
