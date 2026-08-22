"""
attrparse.py — pull attributes out of a terse catalogue description.
===================================================================

The input rows in this challenge look like `3/8 CPLG BRS 150#`. Everything a
buyer needs is in there, but compressed: size, item type, material and pressure
class in four tokens. This module recovers the attributes so that

  * `LOVRegistry.validate()` has real values to check (before this existed the
    pipeline validated an empty dictionary, which is how a 100% compliance score
    can be completely meaningless), and
  * the description builders have attributes to render, instead of producing a
    title that is just a brand and a part number.

Design rule, same as everywhere else in this codebase: **only what is written
is returned.** There is no "typical pressure class for a brass coupling"
inference here. Every parsed attribute carries the substring it came from, so a
reviewer can check it against the source in one glance, and anything not
recognised is left out rather than guessed.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
import re

# --------------------------------------------------------------------------
# Materials. Longest first, so "stainless steel" is not read as "steel", and
# "cast iron" is not read as "iron".
# --------------------------------------------------------------------------
MATERIALS: List[Tuple[str, str]] = [
    ("stainless steel 316l", "Stainless Steel 316L"),
    ("stainless steel 316", "Stainless Steel 316"),
    ("stainless steel 304", "Stainless Steel 304"),
    ("stainless steel", "Stainless Steel"),
    ("carbon steel", "Carbon Steel"),
    ("malleable iron", "Malleable Iron"),
    ("ductile iron", "Ductile Iron"),
    ("cast iron", "Cast Iron"),
    ("forged steel", "Forged Steel"),
    ("galvanized steel", "Galvanized Steel"),
    ("galvanized", "Galvanized"),
    ("polyethylene", "Polyethylene"),
    ("polypropylene", "Polypropylene"),
    ("aluminum", "Aluminum"), ("aluminium", "Aluminum"),
    ("brass", "Brass"), ("bronze", "Bronze"), ("copper", "Copper"),
    ("nylon", "Nylon"), ("cpvc", "CPVC"), ("pvc", "PVC"),
    ("pex", "PEX"), ("abs", "ABS"), ("zinc", "Zinc"),
    ("rubber", "Rubber"), ("ceramic", "Ceramic"),
    ("steel", "Steel"), ("iron", "Iron"), ("plastic", "Plastic"),
]

FINISHES: List[Tuple[str, str]] = [
    ("polished chrome", "Polished Chrome"),
    ("brushed nickel", "Brushed Nickel"),
    ("satin nickel", "Satin Nickel"),
    ("oil rubbed bronze", "Oil Rubbed Bronze"),
    ("matte black", "Matte Black"),
    ("stainless finish", "Stainless"),
    ("chrome plated", "Chrome Plated"),
    ("nickel plated", "Nickel Plated"),
    ("chrome", "Chrome"),
    ("brushed", "Brushed"),
    ("polished", "Polished"),
]

CONNECTIONS: List[Tuple[str, str]] = [
    ("male to female threaded", "Male x Female Threaded"),
    ("female threaded npt", "Female NPT"),
    ("male threaded npt", "Male NPT"),
    ("female threaded", "Female Threaded"),
    ("male threaded", "Male Threaded"),
    ("threaded npt", "NPT Threaded"),
    ("socket weld", "Socket Weld"),
    ("butt weld", "Butt Weld"),
    ("compression", "Compression"),
    ("push to connect", "Push To Connect"),
    ("threaded", "Threaded"),
    ("grooved", "Grooved"),
    ("flanged", "Flanged"),
    ("soldered", "Soldered"),
    ("sweat", "Sweat"),
    ("barbed", "Barbed"),
    ("slip", "Slip"),
]

# A size token: 1-1/2, 3/8, 24.25, 24.
#
# ORDER MATTERS. Python's alternation is leftmost-first, not longest-match, so
# a plain `\d+` branch placed before `\d+/\d+` would match only the "3" of "3/8"
# and silently report a 3-inch coupling as the size of a 3/8-inch one. Mixed
# numbers first, then fractions, then decimals, then bare integers.
_SIZE_TOKEN = r"\d+\s*-\s*\d+/\d+|\d+/\d+|\d+\.\d+|\d+"

SIZE_RE = re.compile(
    r"(?<![A-Za-z0-9])(" + _SIZE_TOKEN + r")\s*"
    r"(\"|''|in\b|inch(?:es)?\b|mm\b|cm\b)?",
    re.IGNORECASE,
)
# Fraction and mixed-number forms are self-evidently trade sizes ("3/8",
# "1-1/2"). A bare integer is not: in "LAV FCT 2-HNDL CHR" the 2 is a handle
# count, and in "DW AD 511 M8 PROX SNSR" the 511 is part of a model number.
# So a bare integer or decimal is only accepted when an explicit unit marker
# follows it. Reporting a 511-inch dishwasher would be worse than reporting no
# size at all, since a reviewer has no way to know the number was invented.
_FRACTIONAL_SIZE = re.compile(r"^\d+-\d+/\d+$|^\d+/\d+$")
# Contexts in which a nearby number means something other than a size. These are
# directional on purpose. A blunt "any keyword within N characters" window would
# reject the perfectly good 2" in `2" SCH 40 PVC PIPE`, because SCH happens to
# sit four characters away — the keyword has to be attached to *this* number.
# Keywords that precede the number: SCH 40, CLASS 150, CL150.
_SIZE_PREFIX_BLOCK = re.compile(r"(?:sch(?:ed(?:ule)?)?|class|cl)\s*\.?\s*$", re.IGNORECASE)
# Keywords that follow it: 150#, 150 LB, 2-HNDL, 5 CYCLES, 1.5 GPM, 47 dBA.
_SIZE_SUFFIX_BLOCK = re.compile(
    r"^[-\s]*(?:#|lbs?\b|psi\b|hndl|handle|cycles?\b|gpm\b|dba\b|rpm\b|"
    r"watts?\b|volts?\b|amps?\b)",
    re.IGNORECASE,
)
# "3/8 x 1/4" — reducing fittings carry two sizes.
SIZE_PAIR_RE = re.compile(
    r"(?<![A-Za-z0-9])(" + _SIZE_TOKEN + r")\s*"
    r"(?:\"|''|in\b)?\s*[xX×]\s*"
    r"(" + _SIZE_TOKEN + r")\s*(?:\"|''|in\b)?",
    re.IGNORECASE,
)
# Pressure class: 150#, CL150, CLASS 300, 150 LB
PRESSURE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:cl(?:ass)?\s*)?(\d{2,5})\s*(?:#|lb\b|lbs\b|psi\b|class\b)"
    r"|(?<![A-Za-z0-9])cl(?:ass)?\s*(\d{2,5})(?![A-Za-z0-9])",
    re.IGNORECASE,
)
SCHEDULE_RE = re.compile(r"(?<![A-Za-z0-9])sch(?:ed(?:ule)?)?\s*\.?\s*(\d{1,3}s?)", re.IGNORECASE)
HANDLE_RE = re.compile(r"(?<![A-Za-z0-9])(\d)\s*[- ]?\s*handle", re.IGNORECASE)
VOLT_RE = re.compile(r"(?<![A-Za-z0-9])(\d{1,4}(?:\.\d+)?)\s*(?:v|volts?|vac|vdc)(?![A-Za-z])",
                     re.IGNORECASE)
AMP_RE = re.compile(r"(?<![A-Za-z0-9])(\d{1,4}(?:\.\d+)?)\s*(?:a|amps?)(?![A-Za-z])",
                    re.IGNORECASE)


@dataclass
class ParsedAttribute:
    """One attribute recovered from the description, with its evidence."""
    label: str
    value: str
    unit: Optional[str] = None
    evidence: Optional[str] = None
    is_key: bool = True

    def as_dict(self) -> Dict:
        return {"label": self.label, "value": self.value, "unit": self.unit,
                "evidence": self.evidence}


def _first(patterns: List[Tuple[str, str]], low: str) -> Optional[Tuple[str, str]]:
    """First (canonical, matched_text) whose phrase appears as whole words."""
    for needle, canonical in patterns:
        if re.search(r"(?<![a-z])" + re.escape(needle) + r"(?![a-z])", low):
            return canonical, needle
    return None


def parse_attributes(raw_desc: Optional[str],
                     expanded: Optional[str] = None) -> List[ParsedAttribute]:
    """Recover attributes from a terse description.

    `expanded` is the abbreviation-expanded form from `classify.expand_abbreviations`.
    Passing it improves recall considerably (`BRS` only looks like brass after
    expansion); when omitted, expansion is done here.
    """
    if not raw_desc or not str(raw_desc).strip():
        return []

    if expanded is None:
        from . import classify as classify_mod
        expanded = classify_mod.expand_abbreviations(str(raw_desc))

    text = re.sub(r"\s+", " ", str(expanded)).strip()
    low = text.lower()
    out: List[ParsedAttribute] = []

    # --- size, and the second size on reducing fittings -------------------
    pair = SIZE_PAIR_RE.search(text)
    if pair:
        ev = pair.group(0).strip()
        out.append(ParsedAttribute("Size", pair.group(1), "in", ev))
        out.append(ParsedAttribute("Second Size", pair.group(2), "in", ev))
    else:
        # Scan every numeric candidate rather than only the first: in
        # "DW AD 511 M8 PROX SNSR 2 IN" the leading 511 is a model number and
        # the real size comes later, so stopping at the first match would
        # either invent a size or lose the true one.
        for m in SIZE_RE.finditer(text):
            token = (m.group(1) or "").replace(" ", "")
            if not token:
                continue
            mark = (m.group(2) or "").strip().lower()
            before = text[max(0, m.start() - 12): m.start()]
            after = text[m.end(): m.end() + 10]
            if _SIZE_PREFIX_BLOCK.search(before) or _SIZE_SUFFIX_BLOCK.search(after):
                continue
            # A bare integer/decimal needs an explicit unit to count as a size.
            if not (_FRACTIONAL_SIZE.match(token) or mark):
                continue
            unit = "mm" if mark == "mm" else "cm" if mark == "cm" else "in"
            out.append(ParsedAttribute("Size", token, unit, m.group(0).strip()))
            break

    # --- material ---------------------------------------------------------
    hit = _first(MATERIALS, low)
    if hit:
        out.append(ParsedAttribute("Material", hit[0], None, hit[1]))

    # --- finish (kept separate from material: a chrome-plated brass faucet
    #     has both, and conflating them loses a filterable attribute) ------
    hit = _first(FINISHES, low)
    if hit:
        out.append(ParsedAttribute("Finish", hit[0], None, hit[1]))

    # --- connection type --------------------------------------------------
    hit = _first(CONNECTIONS, low)
    if hit:
        out.append(ParsedAttribute("Connection Type", hit[0], None, hit[1]))

    # --- pressure class ---------------------------------------------------
    m = PRESSURE_RE.search(text)
    if m:
        val = m.group(1) or m.group(2)
        if val:
            out.append(ParsedAttribute("Pressure Class", val, None, m.group(0).strip()))

    # --- schedule ---------------------------------------------------------
    m = SCHEDULE_RE.search(text)
    if m:
        out.append(ParsedAttribute("Schedule", m.group(1).upper(), None, m.group(0).strip()))

    # --- handle count (faucets) ------------------------------------------
    m = HANDLE_RE.search(text)
    if m:
        out.append(ParsedAttribute("Handle Count", m.group(1), None, m.group(0).strip()))

    # --- electrical -------------------------------------------------------
    m = VOLT_RE.search(text)
    if m:
        out.append(ParsedAttribute("Voltage", m.group(1), "volts", m.group(0).strip()))
    m = AMP_RE.search(text)
    if m:
        out.append(ParsedAttribute("Current", m.group(1), "amps", m.group(0).strip()))

    # De-duplicate by label, keeping the first (most specific) hit.
    seen = set()
    unique: List[ParsedAttribute] = []
    for a in out:
        if a.label in seen:
            continue
        seen.add(a.label)
        unique.append(a)
    return unique


def to_dict(attrs: List[ParsedAttribute]) -> Dict[str, str]:
    """`{label: value}` — the shape `LOVRegistry.validate()` expects."""
    return {a.label: a.value for a in attrs}
