"""
sourcing.py — where a specification may legitimately come from.
==============================================================

The Unilog content guidelines are explicit that enrichment content must come
from the **manufacturer**, and that marketplaces and distributors are not
acceptable sources. Until now nothing in this codebase enforced that: the app
would happily fetch a Wikipedia article or an Amazon listing and treat the
result as source-of-truth. That produced the single most embarrassing class of
bug in this project — a "current rating" parsed out of an encyclopedia citation
date.

There are two reasons the rule matters beyond compliance:

* **Marketplace listings are seller-authored.** The specs are copy-paste, often
  wrong, and frequently describe a different variant of the part. A grounded
  extraction from a bad source is still bad data — snippet grounding proves the
  text said it, not that it is true.
* **Distributor pages are usually derived** from the manufacturer's data, with
  the units and options quietly changed to fit the distributor's own schema.
  They are the second-best source, so they are blocked by default but can be
  enabled deliberately rather than by accident.

Policies
--------
    manufacturer_only  (default) — block marketplaces, distributors, encyclopedias
                                   and community/UGC sites
    allow_distributors           — as above but permit known distributors, flagged
    warn_only                    — permit everything, attach the classification
    allow_all                    — permit everything, no annotation

Set via `PRISM_SOURCING_POLICY` in `.env`, or pass `policy=` explicitly.

A strict allow-list is also supported: drop one domain per line into
`data/approved_domains.txt` and only those domains will be fetched. That is the
right configuration for a graded run, where the source set should be decided
in advance rather than by whatever a demo happens to paste in.
"""

import os
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import List, Optional, Set

from . import urlguard

# --------------------------------------------------------------------------
# Domain classification. Matched on the registrable domain, so
# "www.amazon.co.uk" and "smile.amazon.com" both resolve to amazon.
# --------------------------------------------------------------------------
MARKETPLACES: Set[str] = {
    "amazon", "ebay", "alibaba", "aliexpress", "indiamart", "tradeindia",
    "made-in-china", "walmart", "flipkart", "etsy", "wish", "temu",
    "mercadolibre", "rakuten", "shopee", "lazada", "snapdeal", "olx",
    "exportersindia", "dhgate", "globalsources", "thomasnet",
}

DISTRIBUTORS: Set[str] = {
    # industrial / MRO
    "grainger", "mscdirect", "fastenal", "zoro", "motionindustries",
    "applied", "wesco", "rexel", "graybar", "ferguson", "supplyhouse",
    "pexsupply", "plumbersstock", "webstaurantstore", "globalindustrial",
    "automationdirect", "galco", "statesupply", "buyheatpumps",
    # electronics
    "digikey", "mouser", "rs-online", "rsdelivers", "farnell", "newark",
    "arrow", "avnet", "alliedelec", "element14", "tme", "verical",
    "onlinecomponents", "sager", "heilind",
    # big-box retail (retailers, not manufacturers)
    "homedepot", "lowes", "menards", "acehardware", "harborfreight",
    "northerntool", "tractorsupply", "build", "wayfair", "overstock",
    "fergusonhome", "faucetdirect", "efaucets", "qualitybath",
}

ENCYCLOPEDIC: Set[str] = {
    "wikipedia", "wikimedia", "wiktionary", "britannica", "fandom",
    "everything2", "citizendium", "dbpedia", "wikiwand",
}

COMMUNITY: Set[str] = {
    "reddit", "quora", "stackexchange", "stackoverflow", "medium",
    "blogspot", "wordpress", "tumblr", "facebook", "twitter", "x",
    "instagram", "linkedin", "pinterest", "youtube", "tiktok",
    "eng-tips", "practicalmachinist", "plbg", "diychatroom",
}

# A seed of known manufacturer domains, so a positive identification is possible
# rather than everything unrecognised being lumped together. This is a seed, not
# an authority — the 27k-row UniCat manufacturer list is the real source, and
# `load_approved_domains()` is how you supply a definitive list.
KNOWN_MANUFACTURERS: Set[str] = {
    # industrial sensing / automation
    "turck", "banner-engineering", "bannerengineering", "omron", "keyence",
    "ifm", "pepperl-fuchs", "sick", "balluff", "festo", "smcworld", "smcusa",
    "parker", "siemens", "schneider-electric", "rockwellautomation", "abb",
    "phoenixcontact", "weidmueller", "harting", "lapp", "binder-connector",
    "amphenol", "te", "molex", "hirschmann", "belden", "eaton", "wago",
    "murrelektronik", "baumer", "wenglor", "leuze", "contrinex", "autonics",
    # plumbing / fittings / faucets
    "anvilintl", "anvil-emea", "victaulic", "swagelok", "parkerhannifin",
    "nibco", "watts", "zurn", "viega", "uponor", "charlottepipe",
    "mueller-industries", "wardmfg", "smithcooper", "bonney-forge",
    "moen", "kohler", "delta", "deltafaucet", "grohe", "americanstandard",
    "americanstandard-us", "pfister", "hansgrohe", "toto", "elkay",
    "chicagofaucets", "symmons", "sloan", "gerberonline", "brizo",
    # appliances
    "frigidaire", "whirlpool", "electrolux", "ge", "geappliances", "bosch",
    "bosch-home", "lg", "samsung", "maytag", "kitchenaid", "rheem",
    "aosmith", "bradfordwhite", "navieninc", "rinnai",
}

POLICIES = ("manufacturer_only", "allow_distributors", "warn_only", "allow_all")
DEFAULT_POLICY = "manufacturer_only"

APPROVED_DOMAINS_FILE = os.path.join("data", "approved_domains.txt")

# Second-level domains that are not the registrable name (co.uk, com.au, ...).
_SLD_HINTS = {"co", "com", "net", "org", "gov", "edu", "ac", "or", "ne"}


@dataclass
class SourceVerdict:
    """The sourcing decision for one URL, with the reason attached."""
    url: str
    domain: str
    registrable: str
    category: str                      # manufacturer | distributor | marketplace |
                                       # encyclopedia | community | unknown
    allowed: bool
    policy: str
    reason: str
    needs_review: bool = False
    notes: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "domain": self.domain,
            "registrable_domain": self.registrable,
            "category": self.category,
            "allowed": self.allowed,
            "policy": self.policy,
            "reason": self.reason,
            "needs_review": self.needs_review,
            "notes": self.notes,
        }


def _registrable(domain: str) -> str:
    """'www.amazon.co.uk' -> 'amazon'; 'shop.turck.com' -> 'turck'.

    Deliberately simple: this is a classification aid, not a PSL implementation.
    """
    host = (domain or "").lower().strip().rstrip(".")
    host = re.sub(r"^www\d?\.", "", host)
    parts = [p for p in host.split(".") if p]
    if not parts:
        return ""
    if len(parts) >= 3 and parts[-2] in _SLD_HINTS:
        return parts[-3]
    if len(parts) >= 2:
        return parts[-2]
    return parts[0]


def classify_domain(domain: str) -> str:
    """Bucket a domain. Unrecognised domains are 'unknown', never 'manufacturer'."""
    reg = _registrable(domain)
    if not reg:
        return "unknown"
    if reg in MARKETPLACES:
        return "marketplace"
    if reg in DISTRIBUTORS:
        return "distributor"
    if reg in ENCYCLOPEDIC:
        return "encyclopedia"
    if reg in COMMUNITY:
        return "community"
    if reg in KNOWN_MANUFACTURERS:
        return "manufacturer"
    return "unknown"


def load_approved_domains(path: Optional[str] = None) -> Set[str]:
    """Read a strict allow-list, one domain per line. `#` starts a comment.

    Returns an empty set when the file is absent, which means "no allow-list
    configured" — not "nothing is allowed".
    """
    target = path or APPROVED_DOMAINS_FILE
    if not os.path.exists(target):
        return set()
    out: Set[str] = set()
    try:
        with open(target, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.split("#", 1)[0].strip().lower()
                if line:
                    out.add(_registrable(line) or line)
    except OSError:
        return set()
    return out


def active_policy(policy: Optional[str] = None) -> str:
    """Resolve the policy from the argument, then the environment, then default."""
    chosen = (policy or os.getenv("PRISM_SOURCING_POLICY") or DEFAULT_POLICY).strip().lower()
    return chosen if chosen in POLICIES else DEFAULT_POLICY


# Human-readable rationale per blocked category.
_BLOCK_REASONS = {
    "marketplace": ("Marketplace listings are seller-authored and routinely describe a "
                    "different variant of the part, so they are excluded as an enrichment "
                    "source by the content guidelines."),
    "distributor": ("Distributor pages are derived from manufacturer data and often "
                    "re-map units and options into the distributor's own schema. The "
                    "guidelines require manufacturer sources."),
    "encyclopedia": ("Encyclopedia articles describe a product category, not a purchasable "
                     "part, so any number on the page is illustrative rather than a "
                     "specification for a specific MPN."),
    "community": ("Forum, blog and social content is unattributed and unversioned; it "
                  "cannot be cited as a specification source."),
}


def evaluate_url(url: str,
                 policy: Optional[str] = None,
                 approved_domains: Optional[Set[str]] = None) -> SourceVerdict:
    """Decide whether `url` may be used as an enrichment source."""
    pol = active_policy(policy)
    parsed = urllib.parse.urlparse(url if "://" in url else "https://" + url.strip())
    domain = parsed.netloc or "unknown"

    # ---- protocol safety, before anything else ---------------------------
    # Whether a source is *fetchable at all* is a different question from
    # whether it is an acceptable source, and it has to be settled first.
    # `ingestion.fetch_and_clean_url` already runs this guard, but
    # `evaluate_url` is a public entry point (the /api/sourcing/check endpoint
    # calls it to classify a URL without fetching), so the rule is enforced
    # here too rather than assumed. Delegated to `urlguard` so the scheme and
    # credential rules live in exactly one place.
    #
    # Deliberately above the allow-list override and above the warn_only /
    # allow_all escape hatches: no sourcing policy should be able to permit a
    # file:// URL or a request aimed at the loopback interface.
    try:
        urlguard.validate_url(url, resolve=False)
    except urlguard.SafeFetchError as exc:
        return SourceVerdict(
            url, domain, _registrable(domain), "unfetchable", False, pol,
            f"Not a fetchable web source: {exc}",
            needs_review=True, notes=["rejected by URL safety guard"])
    reg = _registrable(domain)
    category = classify_domain(domain)
    notes: List[str] = []

    allow_list = approved_domains if approved_domains is not None else load_approved_domains()
    # Normalise whatever the caller handed us. `load_approved_domains()` already
    # reduces file entries to registrable names, but a caller passing a set
    # directly will naturally write "moen.com" — comparing that against the
    # registrable "moen" would reject an explicitly approved domain.
    if allow_list:
        allow_list = {_registrable(d) or str(d).strip().lower() for d in allow_list}

    # ---- a configured allow-list overrides everything --------------------
    if allow_list:
        if reg in allow_list:
            return SourceVerdict(url, domain, reg, category, True, pol,
                                 f"'{reg}' is on the configured approved-domain list.",
                                 needs_review=False,
                                 notes=["allow-list match"])
        if pol in ("warn_only", "allow_all"):
            notes.append("not on the approved-domain list")
        else:
            return SourceVerdict(
                url, domain, reg, category, False, pol,
                f"'{domain}' is not on the approved-domain list in "
                f"{APPROVED_DOMAINS_FILE}. Add it there to permit this source.",
                needs_review=True, notes=["allow-list miss"])

    # ---- policy escape hatches ------------------------------------------
    if pol == "allow_all":
        return SourceVerdict(url, domain, reg, category, True, pol,
                             "Sourcing policy is allow_all; no restriction applied.",
                             needs_review=False, notes=notes)

    if pol == "warn_only":
        flag = category in _BLOCK_REASONS
        return SourceVerdict(
            url, domain, reg, category, True, pol,
            (f"Classified as {category}. Permitted because the policy is warn_only, "
             f"but the source does not meet the manufacturer-only guideline.")
            if flag else f"Classified as {category}; permitted.",
            needs_review=flag, notes=notes)

    # ---- manufacturer_only / allow_distributors --------------------------
    if category == "manufacturer":
        return SourceVerdict(url, domain, reg, category, True, pol,
                             f"'{reg}' is a recognised manufacturer domain.",
                             needs_review=False, notes=notes)

    if category == "distributor" and pol == "allow_distributors":
        notes.append("distributor permitted by policy")
        return SourceVerdict(
            url, domain, reg, category, True, pol,
            ("Distributor source permitted by the allow_distributors policy. Values "
             "are flagged for review because distributor data is second-hand."),
            needs_review=True, notes=notes)

    if category in _BLOCK_REASONS:
        return SourceVerdict(url, domain, reg, category, False, pol,
                             _BLOCK_REASONS[category], needs_review=True, notes=notes)

    # Unknown domain: most manufacturer sites are unknown to a seed list, so
    # blocking here would make the tool useless. Permit, but say plainly that the
    # source was not verified as a manufacturer.
    notes.append("domain not verified against a manufacturer list")
    return SourceVerdict(
        url, domain, reg, "unknown", True, pol,
        (f"'{domain}' is not a known marketplace, distributor, encyclopedia or forum, "
         f"so it is permitted — but it was not positively verified as the "
         f"manufacturer either. Add it to {APPROVED_DOMAINS_FILE} to make this explicit."),
        needs_review=True, notes=notes)


def describe_policy(policy: Optional[str] = None) -> str:
    """One-line description, for logs and the API report."""
    pol = active_policy(policy)
    return {
        "manufacturer_only": "manufacturer_only — marketplaces, distributors, "
                             "encyclopedias and forums are rejected",
        "allow_distributors": "allow_distributors — distributors permitted but flagged; "
                              "marketplaces, encyclopedias and forums rejected",
        "warn_only": "warn_only — everything fetched, non-compliant sources flagged",
        "allow_all": "allow_all — no sourcing restriction (not guideline-compliant)",
    }[pol]
