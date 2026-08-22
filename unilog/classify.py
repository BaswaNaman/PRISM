"""
classify.py — item-type / classpath classification.
===================================================

This replaces the twenty hardcoded nouns that used to live in the pipeline
runner. Item type is the single highest-leverage field in this challenge: it
decides the classpath, the classpath decides which attributes are legal, and the
item type appears in all five description formats. Get it wrong and five fields
are wrong at once.

How it decides
--------------
1. **Abbreviation expansion — unambiguous only.** Catalogue descriptions are
   written in trade shorthand — `3/8 CPLG BRS 150#`. Nothing can be matched
   until `CPLG` is known to mean `Coupling`, so expansion happens first and
   the expanded text is what everything else sees. But not every abbreviation
   has one meaning: `DW` is trade shorthand for "dishwasher" in some catalogs
   and means nothing of the sort in others (`DW AD 511 M8 PROX SNSR` is a
   proximity sensor with a "DW" model prefix, not a dishwasher). Abbreviations
   like that live in `AMBIGUOUS_ABBREVIATIONS`, not `ABBREVIATIONS`, and are
   never blindly substituted into the text — see step 4.

2. **LOV taxonomy match (preferred).** When a `LOVRegistry` is loaded, the
   candidate set *is* the client's own taxonomy: every classpath leaf becomes a
   candidate, so a match returns a real classpath that `registry.validate()` can
   then use. This is the difference between guessing a noun and classifying into
   the customer's tree.

3. **Built-in fallback taxonomy.** With no workbook loaded the classifier falls
   back to a curated list covering the two categories the brief specifies end to
   end (fittings and faucets) plus the appliance and sensing types that appear in
   the samples. Candidates found this way carry no classpath, and say so.

4. **Scored token overlap, not substring matching.** The old stub did
   `if "cap" in text.lower()`, which fires on "capacity", "capillary" and
   "escape". Matching is over whole tokens, weighted by inverse document
   frequency across the candidate set, so a distinctive token like `coupling`
   counts for much more than a token like `pipe` that appears in dozens of
   candidate names.

5. **Ambiguous abbreviations, used only as a last resort.** `classify()` first
   scores candidates using only strong (unambiguous) evidence. Only if that
   fails to clear the confidence bar does it re-score with an ambiguous
   abbreviation's meaning folded in — and only for abbreviations that have an
   independent corroborating token elsewhere in the description. A match that
   depended on this step is capped below the review threshold, so it always
   comes back `needs_review`. An ambiguous abbreviation with no corroboration
   at all contributes nothing and cannot, by itself, pick a category.

Why confidence is reported
--------------------------
An unresolved classification is useful information; a confidently wrong one is
not. `Classification.needs_review` is set whenever the margin over the runner-up
is thin or the absolute score is weak, so borderline items can be routed to a
human rather than silently propagated into five description fields.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple
import math
import re

# --------------------------------------------------------------------------
# Trade shorthand -> words. Keys are matched as whole tokens, case-insensitively.
# --------------------------------------------------------------------------
ABBREVIATIONS: Dict[str, str] = {
    # fittings
    "cplg": "coupling", "cplgs": "coupling", "coup": "coupling",
    "elb": "elbow", "ell": "elbow", "el": "elbow",
    "nip": "nipple", "nippl": "nipple",
    "adpt": "adapter", "adap": "adapter", "adptr": "adapter",
    "bush": "bushing", "bushg": "bushing", "bshg": "bushing",
    "flg": "flange", "flng": "flange",
    "un": "union", "unn": "union",
    "redu": "reducer", "red": "reducer", "rdcr": "reducer",
    "tee": "tee", "crs": "cross",
    "plg": "plug", "cp": "cap",
    "nut": "nut", "wshr": "washer",
    "hex": "hexagon", "sq": "square",
    "str": "strainer", "swg": "swage",
    # valves
    "vlv": "valve", "vv": "valve",
    "bv": "ball valve", "gv": "gate valve", "ckv": "check valve",
    "chk": "check", "bfly": "butterfly", "bttrfly": "butterfly",
    "gl": "globe", "rlf": "relief", "prv": "pressure relief valve",
    # faucets / plumbing
    "fct": "faucet", "fcet": "faucet", "fauc": "faucet",
    "lav": "lavatory", "kit": "kitchen", "shwr": "shower",
    "sprd": "spread", "wdspr": "widespread", "ctrst": "centerset",
    "cart": "cartridge", "aer": "aerator", "sp": "spout",
    "hndl": "handle", "hdl": "handle", "trm": "trim",
    "dvtr": "diverter", "tub": "tub", "sink": "sink",
    "sply": "supply", "drn": "drain", "trp": "trap",
    # materials / construction (expanded so they do not pollute type matching)
    "brs": "brass", "br": "brass", "brz": "bronze",
    "sst": "stainless steel", "ss": "stainless steel",
    "galv": "galvanized", "gal": "galvanized",
    "ci": "cast iron", "di": "ductile iron", "mi": "malleable iron",
    "cu": "copper", "pvc": "pvc", "cpvc": "cpvc", "poly": "polyethylene",
    "chr": "chrome", "cp2": "chrome plated", "ni": "nickel",
    "pol": "polished", "brn": "brushed",
    # connections
    "thd": "threaded", "thrd": "threaded", "npt": "threaded npt",
    "fnpt": "female threaded", "mnpt": "male threaded",
    "swt": "sweat", "slip": "slip", "cmp": "compression",
    "grv": "grooved", "sw": "socket weld", "bw": "butt weld",
    "fem": "female", "mal": "male", "mxf": "male to female",
    # appliances / electrical
    # NOTE: "dw" is deliberately absent from this table. It is genuinely
    # ambiguous trade shorthand (dishwasher, but also seen for "domestic
    # water", drawing/model-number prefixes, etc.) and blindly expanding it
    # is exactly the bug this module now guards against — see
    # AMBIGUOUS_ABBREVIATIONS below.
    "dshwshr": "dishwasher",
    "refrig": "refrigerator", "ref": "refrigerator",
    "wshm": "washing machine", "dryr": "dryer",
    "mtr": "motor", "pmp": "pump", "snsr": "sensor", "prox": "proximity",
    "photo": "photoelectric", "temp": "temperature", "pres": "pressure",
    "conn": "connector", "cbl": "cable", "recept": "receptacle",
    "sw2": "switch", "swtch": "switch", "rly": "relay",
    "xfmr": "transformer", "brkr": "breaker",
    # generic
    "assy": "assembly", "kt": "kit", "repl": "replacement",
    "std": "standard", "hd": "heavy duty", "lt": "light",
    "dia": "diameter", "od": "outside diameter", "id": "inside diameter",
    "lg": "long", "sht": "short", "w": "with", "wo": "without",
}

# --------------------------------------------------------------------------
# Ambiguous abbreviations: short forms with more than one live meaning in
# trade shorthand, where the wrong pick actively corrupts classification
# (unlike ABBREVIATIONS above, which are safe to substitute unconditionally).
#
# These are intentionally NOT part of ABBREVIATIONS and are NEVER substituted
# by `expand_abbreviations`. Text passed through `expand_abbreviations` keeps
# "DW" as the literal token "dw" — no downstream consumer (attribute parsing,
# description building) ever sees an invented "dishwasher" that the source
# text did not actually support.
#
# The classifier resolves an ambiguous abbreviation only when the rest of the
# description independently corroborates that meaning (one of `corroborators`
# is also present). Absent corroboration, the abbreviation is left as an inert
# token: it cannot match any candidate and therefore cannot, by itself, decide
# the product type. This is what keeps "DW AD 511 M8 PROX SNSR" from becoming
# a Dishwasher and what still lets a genuine "DW BUILT-IN RACK DETERGENT ..."
# resolve correctly.
#
# Deliberately a short, curated table (not a taxonomy): add an entry only for
# an abbreviation that has actually been observed to misfire.
# --------------------------------------------------------------------------
AMBIGUOUS_ABBREVIATIONS: Dict[str, Dict[str, object]] = {
    "dw": {
        "expansion": "dishwasher",
        "corroborators": {
            "dishwasher", "cycle", "cycles", "rack", "detergent", "wash",
            "washing", "cabinet", "undercounter", "builtin", "built",
            "appliance", "tub", "portable", "door",
        },
    },
}

# Fallback taxonomy used when no LOV workbook is loaded. Weighted toward the two
# categories the brief specifies end to end.
FALLBACK_TYPES: List[str] = [
    # fittings
    "Coupling", "Reducing Coupling", "Elbow", "Street Elbow", "Tee", "Reducing Tee",
    "Cross", "Nipple", "Adapter", "Bushing", "Union", "Reducer", "Cap", "Plug",
    "Flange", "Strainer", "Swage Nipple", "Close Nipple", "Hex Bushing",
    "Pipe Nipple", "Compression Fitting", "Grooved Coupling",
    # faucets and plumbing
    "Faucet", "Kitchen Faucet", "Lavatory Faucet", "Bar Faucet", "Shower Faucet",
    "Tub Filler", "Widespread Faucet", "Centerset Faucet", "Faucet Cartridge",
    "Faucet Handle", "Faucet Aerator", "Faucet Spout", "Shower Head",
    "Shower Valve Trim", "Diverter", "Supply Line", "Drain", "P-Trap", "Sink",
    # valves
    "Valve", "Ball Valve", "Gate Valve", "Check Valve", "Butterfly Valve",
    "Globe Valve", "Pressure Relief Valve", "Solenoid Valve",
    # appliances
    "Dishwasher", "Refrigerator", "Washing Machine", "Dryer", "Range", "Oven",
    "Water Heater",
    # electrical / sensing
    "Sensor", "Proximity Sensor", "Photoelectric Sensor", "Pressure Sensor",
    "Temperature Sensor", "Connector", "Circular Connector", "Cable Assembly",
    "Receptacle", "Switch", "Limit Switch", "Relay", "Motor", "Pump",
    "Transformer", "Circuit Breaker", "Encoder", "Actuator", "Transducer",
    "Controller", "Enclosure", "Terminal Block", "Contactor",
    # abrasives / surface preparation observed in organizer-style catalog rows
    "Sanding Belt", "Sanding Disc", "Flap Disc", "Grinding Wheel",
    "Cut-Off Wheel", "Abrasive Sheet",
]

# Tokens that carry no classification signal.
STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "for", "with", "without", "to", "in",
    "on", "by", "from", "type", "series", "no", "new", "each", "per", "size",
    "assembly", "kit", "standard", "heavy", "duty", "light", "long", "short",
    "diameter", "outside", "inside", "female", "male", "left", "right",
}

# Scoring thresholds. Deliberately conservative: an unresolved item is cheaper
# than a confidently wrong one, because the wrong item type corrupts all five
# description formats at once.
MIN_SCORE = 0.34
REVIEW_MARGIN = 0.12
REVIEW_CONFIDENCE = 0.70

_TOKEN_RE = re.compile(r"[A-Za-z]+")
_NUM_UNIT_RE = re.compile(r"\d")


@dataclass
class Classification:
    """One classification decision, with the evidence behind it."""
    item_type: Optional[str] = None
    classpath: Optional[str] = None
    confidence: float = 0.0
    method: str = "unresolved"
    evidence: Optional[str] = None
    alternatives: List[Tuple[str, float]] = field(default_factory=list)
    expanded_text: Optional[str] = None

    @property
    def needs_review(self) -> bool:
        """True when the decision is too close to call to be trusted unattended."""
        if self.item_type is None:
            return True
        if self.confidence < REVIEW_CONFIDENCE:
            return True
        if self.alternatives:
            return (self.confidence - self.alternatives[0][1]) < REVIEW_MARGIN
        return False

    def as_dict(self) -> Dict:
        return {
            "item_type": self.item_type,
            "classpath": self.classpath,
            "confidence": round(self.confidence, 3),
            "method": self.method,
            "evidence": self.evidence,
            "needs_review": self.needs_review,
            "runner_up": self.alternatives[0][0] if self.alternatives else None,
        }


def expand_abbreviations(text: str) -> str:
    """`3/8 CPLG BRS 150#` -> `3/8 coupling brass 150#`.

    Whole-token replacement only. A substring replacement here would turn
    "SWEAT" into "sweatEAT" and "CAPACITY" into "capACITY".
    """
    if not text:
        return ""

    def _sub(m: "re.Match") -> str:
        tok = m.group(0)
        return ABBREVIATIONS.get(tok.lower(), tok)

    # Split on non-alphanumerics but keep them, so sizes and "150#" survive.
    return re.sub(r"[A-Za-z]+", _sub, str(text))


def tokenize(text: str) -> List[str]:
    """Lowercase alphabetic tokens, stopwords and 1-char noise removed."""
    toks = [t.lower() for t in _TOKEN_RE.findall(str(text or ""))]
    return [t for t in toks if len(t) > 1 and t not in STOPWORDS]


def resolve_ambiguous_tokens(text_tokens: "set") -> Tuple["set", List[str]]:
    """Decide which ambiguous abbreviations, if any, are safe to treat as
    evidence for this description.

    An ambiguous abbreviation contributes its mapped meaning only when at
    least one independent corroborating token is also present in
    `text_tokens`. Otherwise it is left alone — it stays exactly what it
    is (e.g. the bare token "dw"), which no candidate's tokens will match, so
    it cannot sway the classification on its own.

    Returns `(extra_tokens, notes)`: extra tokens to fold into scoring, and a
    human-readable note per abbreviation encountered (resolved or not) for
    the evidence trail.
    """
    extra: set = set()
    notes: List[str] = []
    for abbr, info in AMBIGUOUS_ABBREVIATIONS.items():
        if abbr not in text_tokens:
            continue
        corroborators = info["corroborators"]
        hit = text_tokens & corroborators
        if hit:
            extra.add(str(info["expansion"]))
            notes.append(
                f"ambiguous abbreviation '{abbr}' read as '{info['expansion']}', "
                f"corroborated by: {', '.join(sorted(hit))}"
            )
        else:
            notes.append(
                f"ambiguous abbreviation '{abbr}' left unresolved: no corroborating "
                f"context for '{info['expansion']}' (or any other reading)"
            )
    return extra, notes


def _leaf_of(classpath: str) -> str:
    """Last segment of a classpath, whichever separator the workbook uses."""
    parts = re.split(r"\s*(?:>|/|\||::|\\)\s*", str(classpath).strip())
    parts = [p for p in parts if p.strip()]
    return parts[-1] if parts else str(classpath).strip()


class ItemTypeClassifier:
    """Classifies a terse product description into an item type (and classpath).

    Pass a loaded `LOVRegistry` to classify into the client's own taxonomy; with
    no registry the built-in fallback list is used and `classpath` stays None.
    """

    def __init__(self, registry=None, extra_types: Optional[Sequence[str]] = None):
        # candidate label -> classpath (None for fallback candidates)
        self.candidates: Dict[str, Optional[str]] = {}
        self.source = "fallback"

        if registry is not None and getattr(registry, "loaded", False):
            for cp in registry.by_classpath:
                leaf = _leaf_of(cp)
                if leaf:
                    # First classpath wins for a given leaf; ambiguity is rare and
                    # the alternatives list still surfaces near-misses.
                    self.candidates.setdefault(leaf, cp)
            self.source = "lov_taxonomy"

        if not self.candidates:
            for t in FALLBACK_TYPES:
                self.candidates.setdefault(t, None)

        for t in (extra_types or []):
            self.candidates.setdefault(str(t), None)

        # Token sets + IDF weights over the candidate corpus.
        self._tokens: Dict[str, List[str]] = {
            label: tokenize(label) for label in self.candidates
        }
        self._tokens = {k: v for k, v in self._tokens.items() if v}
        self._idf = self._build_idf()

        # Longest candidates first, so "Ball Valve" is tested before "Valve".
        self._by_length = sorted(self._tokens, key=lambda s: -len(s))

    # -- internals ---------------------------------------------------------
    def _build_idf(self) -> Dict[str, float]:
        n = max(len(self._tokens), 1)
        df: Dict[str, int] = {}
        for toks in self._tokens.values():
            for t in set(toks):
                df[t] = df.get(t, 0) + 1
        # +1 smoothing keeps a token that appears in every candidate from
        # collapsing to a zero weight.
        return {t: math.log((n + 1) / (c + 1)) + 1.0 for t, c in df.items()}

    def _score(self, label: str, text_tokens: set) -> float:
        """Weighted recall of the candidate's tokens within the description."""
        toks = self._tokens.get(label) or []
        if not toks:
            return 0.0
        total = sum(self._idf.get(t, 1.0) for t in toks)
        hit = sum(self._idf.get(t, 1.0) for t in toks if t in text_tokens)
        return hit / total if total else 0.0

    def _score_all(self, text_tokens: set) -> List[Tuple[str, float]]:
        """Every candidate with nonzero overlap against `text_tokens`, sorted
        best-first. Ties favour the more specific (longer) candidate."""
        scored: List[Tuple[str, float]] = []
        for label in self._tokens:
            s = self._score(label, text_tokens)
            if s > 0:
                scored.append((label, s + 0.001 * len(self._tokens[label])))
        scored.sort(key=lambda x: -x[1])
        return scored

    def _from_scored(self, scored: List[Tuple[str, float]], expanded: str,
                      matched_tokens: set, method_suffix: str = "") -> Classification:
        label, raw = scored[0]
        conf = min(round(0.55 + 0.40 * min(raw, 1.0), 3), 0.94)
        return Classification(
            item_type=label,
            classpath=self.candidates.get(label),
            confidence=conf,
            method=f"{self.source}_token_overlap{method_suffix}",
            evidence=("matched on token(s): "
                      + ", ".join(t for t in self._tokens[label] if t in matched_tokens)),
            alternatives=[(l, round(min(s, 1.0), 3)) for l, s in scored[1:4]],
            expanded_text=expanded,
        )

    # -- public API --------------------------------------------------------
    def classify(self, text: Optional[str]) -> Classification:
        if not text or not str(text).strip():
            return Classification(method="unresolved", evidence="empty description")

        # Only unambiguous abbreviations are substituted into the text itself
        # -- see AMBIGUOUS_ABBREVIATIONS for why "dw"-style shorthand is kept
        # out of this step.
        expanded = expand_abbreviations(str(text))
        low = " " + re.sub(r"\s+", " ", expanded.lower()).strip() + " "
        text_tokens = set(tokenize(expanded))
        if not text_tokens:
            return Classification(method="unresolved", expanded_text=expanded,
                                  evidence="no usable tokens after expansion")

        # --- 1. whole-phrase match on the strong (unambiguous) text only --
        # This is the strongest possible signal, so it is never allowed to
        # depend on a guessed-at ambiguous expansion.
        for label in self._by_length:
            if len(self._tokens[label]) < 2:
                continue
            if f" {label.lower()} " in low:
                return Classification(
                    item_type=label,
                    classpath=self.candidates.get(label),
                    confidence=0.95,
                    method=f"{self.source}_phrase",
                    evidence=f"exact phrase '{label}' present in expanded description",
                    expanded_text=expanded,
                )

        # --- 2. scored token overlap, strong evidence first ----------------
        # Strong evidence (unambiguous abbreviations + literal words) always
        # gets first say. An ambiguous abbreviation is only even considered
        # once the strong-only pass has failed to clear the bar, and even
        # then only if something else in the text corroborates it.
        strong_scored = self._score_all(text_tokens)
        if strong_scored and strong_scored[0][1] >= MIN_SCORE:
            return self._from_scored(strong_scored, expanded, text_tokens)

        ambiguous_extra, ambiguous_notes = resolve_ambiguous_tokens(text_tokens)
        if ambiguous_extra:
            combined_tokens = text_tokens | ambiguous_extra
            combined_scored = self._score_all(combined_tokens)
            if combined_scored and combined_scored[0][1] >= MIN_SCORE:
                result = self._from_scored(
                    combined_scored, expanded, combined_tokens,
                    method_suffix="_ambiguous_context",
                )
                # A match that only exists because of a corroborated
                # ambiguous abbreviation is inherently less certain than one
                # built entirely from strong evidence — cap it so
                # `needs_review` is always true and route it to a human
                # rather than let it silently outrank an unresolved result.
                result.confidence = min(result.confidence, REVIEW_CONFIDENCE - 0.01)
                result.evidence = (result.evidence or "") + "; " + "; ".join(ambiguous_notes)
                return result

        # --- 3. nothing cleared the bar: unresolved rather than a guess ----
        best = strong_scored[0] if strong_scored else None
        evidence = (f"best candidate '{best[0]}' scored {best[1]:.2f}, below the "
                    f"{MIN_SCORE} threshold" if best
                    else "no candidate shared a token with the description")
        if ambiguous_notes:
            evidence += "; " + "; ".join(ambiguous_notes)
        return Classification(
            method="unresolved",
            expanded_text=expanded,
            alternatives=[(l, round(min(s, 1.0), 3)) for l, s in strong_scored[:3]],
            evidence=evidence,
        )

    def classify_many(self, texts: Sequence[Optional[str]]) -> List[Classification]:
        return [self.classify(t) for t in texts]

    @staticmethod
    def stats(results: Sequence[Classification]) -> Dict:
        """Aggregate report — resolution rate is the number worth quoting."""
        total = len(results)
        resolved = [r for r in results if r.item_type]
        review = [r for r in resolved if r.needs_review]
        methods: Dict[str, int] = {}
        for r in results:
            methods[r.method] = methods.get(r.method, 0) + 1
        mean_conf = (sum(r.confidence for r in resolved) / len(resolved)) if resolved else 0.0
        return {
            "rows": total,
            "classified": len(resolved),
            "unresolved": total - len(resolved),
            "resolution_rate_pct": round(len(resolved) / total * 100, 1) if total else 0.0,
            "needs_review": len(review),
            "mean_confidence": round(mean_conf, 3),
            "with_classpath": sum(1 for r in resolved if r.classpath),
            "methods": methods,
        }


# Convenience: a module-level default using only the fallback taxonomy, so
# callers without a registry still get sane behaviour.
DEFAULT = ItemTypeClassifier()


def guess_item_type(text: Optional[str]) -> Optional[str]:
    """Backwards-compatible one-liner replacing the old `_guess_item_type`."""
    return DEFAULT.classify(text).item_type
