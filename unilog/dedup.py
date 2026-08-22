"""
Catalogue de-duplication.
=========================

Distributor files routinely contain the same physical part several times: the
same MPN under different brand spellings, with and without punctuation, or padded
with leading zeros. Pipeline step 2 in the brief is de-duplication.

Approach: build a blocking key from the normalised manufacturer part number
(punctuation and leading zeros stripped) plus the canonical brand. Rows sharing a
key are one product. Within a group the most complete row is chosen as the
survivor, because it needs the least enrichment downstream.

Near-duplicates (same normalised MPN, *different* brand) are reported separately
rather than merged: they might be genuinely different parts, and silently
collapsing them would destroy data.
"""

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Any

_PUNCT_RE = re.compile(r"[^A-Za-z0-9]+")


def normalize_mpn(mpn) -> str:
    """Blocking key for a part number.

    'DW-AD-511-M8' / 'dw ad 511 m8' / 'DWAD511M8' all collapse to 'DWAD511M8'.
    Leading zeros are stripped from numeric runs, since suppliers pad
    inconsistently ('00123' vs '123').
    """
    if mpn is None:
        return ""
    s = _PUNCT_RE.sub("", str(mpn)).upper()
    # Strip leading zeros inside numeric runs while keeping at least one digit.
    s = re.sub(r"0+(\d)", r"\1", s)
    return s


@dataclass
class DuplicateGroup:
    key: str
    rows: List[Dict[str, Any]] = field(default_factory=list)
    survivor_index: int = 0
    reason: str = ""

    def as_dict(self) -> Dict:
        return {
            "key": self.key,
            "count": len(self.rows),
            "survivor_index": self.survivor_index,
            "reason": self.reason,
            "row_indices": [r.get("_row_index") for r in self.rows],
        }


def _completeness(row: Dict[str, Any]) -> int:
    """Count populated, non-placeholder fields — used to pick the survivor."""
    from .brands import is_placeholder
    score = 0
    for k, v in row.items():
        if k.startswith("_"):
            continue
        if v is not None and str(v).strip() and not is_placeholder(v):
            score += 1
    return score


def find_duplicates(rows: List[Dict[str, Any]],
                    mpn_field: str = "Mfg_Part_Num",
                    brand_field: Optional[str] = "Part_Manuf") -> Dict[str, Any]:
    """Group rows into exact duplicates and flag MPN-collisions across brands.

    Returns a report dict with the duplicate groups, the indices to keep, and the
    indices that are redundant — plus cross-brand collisions for human review.
    """
    from .brands import is_placeholder, normalize_key

    for i, row in enumerate(rows):
        row.setdefault("_row_index", i)

    by_full_key: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    by_mpn: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    no_mpn: List[int] = []

    for row in rows:
        mpn_key = normalize_mpn(row.get(mpn_field))
        if not mpn_key:
            no_mpn.append(row["_row_index"])
            continue
        brand_raw = row.get(brand_field) if brand_field else None
        brand_key = "" if is_placeholder(brand_raw) else normalize_key(brand_raw)
        by_full_key[(mpn_key, brand_key)].append(row)
        by_mpn[mpn_key].append(row)

    groups: List[DuplicateGroup] = []
    redundant: List[int] = []

    for (mpn_key, brand_key), grp in by_full_key.items():
        if len(grp) < 2:
            continue
        best = max(range(len(grp)), key=lambda i: _completeness(grp[i]))
        g = DuplicateGroup(
            key=f"{mpn_key}|{brand_key or '(no brand)'}",
            rows=grp,
            survivor_index=grp[best]["_row_index"],
            reason="Identical normalised part number and brand.",
        )
        groups.append(g)
        redundant.extend(r["_row_index"] for i, r in enumerate(grp) if i != best)

    # Same part number, different brand: suspicious but not automatically merged.
    collisions = []
    for mpn_key, grp in by_mpn.items():
        brand_keys = {
            ("" if is_placeholder(r.get(brand_field) if brand_field else None)
             else normalize_key(r.get(brand_field)))
            for r in grp
        }
        brand_keys.discard("")
        if len(brand_keys) > 1:
            collisions.append({
                "mpn_key": mpn_key,
                "brands": sorted(brand_keys),
                "row_indices": [r["_row_index"] for r in grp],
                "note": "Same normalised part number under different brands — not merged "
                        "automatically, as these may be genuinely different parts. Human review.",
            })

    total = len(rows)
    unique = total - len(redundant)
    return {
        "total_rows": total,
        "unique_products": unique,
        "duplicate_rows_removed": len(redundant),
        "duplicate_rate_pct": round((len(redundant) / total) * 100, 1) if total else 0.0,
        "rows_without_part_number": no_mpn,
        "groups": [g.as_dict() for g in groups],
        "redundant_row_indices": sorted(redundant),
        "cross_brand_mpn_collisions": collisions,
    }


def deduplicate(rows: List[Dict[str, Any]],
                mpn_field: str = "Mfg_Part_Num",
                brand_field: Optional[str] = "Part_Manuf") -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Return (surviving_rows, report)."""
    report = find_duplicates(rows, mpn_field, brand_field)
    drop = set(report["redundant_row_indices"])
    kept = [r for r in rows if r.get("_row_index") not in drop]
    return kept, report
