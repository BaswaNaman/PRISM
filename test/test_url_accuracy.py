#!/usr/bin/env python3
"""
test_url_accuracy.py — regression suite for the URL-accuracy hardening.
=======================================================================

Every case here is a bug that was observed in the running app, plus the
"must not regress" case that proves the fix did not cost us real extractions.

Run directly:
    python test_url_accuracy.py

Run under pytest:
    pytest -q test_url_accuracy.py

Exit code is 0 when all assertions hold, 1 otherwise, so this can be wired into
CI or a pre-demo smoke check.

Bugs covered
------------
1.  "Retrieved 12 March 2008" produced current_rating = 0.012 A.  The unit
    alternation matched "Ma" inside "March", and mA->A normalisation turned it
    into a plausible-looking 12 mA.  Fixed with longest-first alternation plus a
    trailing (?![A-Za-z]) guard.
2.  "Founded in 2008 A leading maker" produced current_rating = 2008 A, because
    the "current"/"rating" prefix in the regex was optional and a bare capital A
    was accepted as an ampere symbol.  Fixed with the year guard, the plausible-
    range envelope, and the context-word requirement.
3.  A Wikipedia article about brass instruments produced material = brass.
    Fixed by requiring construction context near generic single-word metals.
4.  Any input under 40 characters was reported as "URL fetch failed" and all
    fields nulled — so a pasted one-line spec silently produced nothing.
5.  "CN-M12-5P Circular Connector" produced the name "CN-M12-5P P Circular
    Connector" because the product-type phrase was allowed to start inside the
    part number.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.extractor import process_raw_product_text, _strip_site_suffix
from app.validator import validate_and_enrich_record


def _run(text, hint=None):
    """Full pipeline: extract -> validate/enrich, exactly as the API does."""
    record = process_raw_product_text(text, product_name_hint=hint)
    return validate_and_enrich_record(record, text)


def _run_checks():
    """Runs every regression case and returns (failures, pass_count).

    All state is local to this call so importing this module has no side
    effects and repeated calls (e.g. from both direct execution and pytest)
    don't share state.
    """
    failures = []
    pass_count = 0

    def check(label, got, expected):
        nonlocal pass_count
        ok = got == expected
        print(f"    [{'PASS' if ok else 'FAIL'}] {label}: got {got!r}, expected {expected!r}")
        if ok:
            pass_count += 1
        else:
            failures.append(label)

    def header(text):
        print(f"\n{text}\n" + "-" * len(text))

    # ==========================================================================
    WIKI_NOISE = """Proximity sensor - Wikipedia
Jump to content. From Wikipedia, the free encyclopedia.
A proximity sensor is a sensor able to detect the presence of nearby objects
without any physical contact. Retrieved 12 March 2008. A study of brass
instruments in orchestras was published in 2019. See also: References.
Categories: Sensors. This page was last edited on 3 May 2024."""

    header("TEST 1  Encyclopedia noise must yield NO fabricated specs")
    rec = _run(WIKI_NOISE, "Proximity sensor - Wikipedia")
    check("product_name has no site suffix", rec.product_name.value, "Proximity sensor")
    check("current_rating not fabricated from '12 March 2008'", rec.current_rating.value, None)
    check("current_rating status", rec.current_rating.validation_status, "missing")
    check("voltage_rating not fabricated", rec.voltage_rating.value, None)
    check("material not taken from 'brass instruments'", rec.material.value, None)

    # ==========================================================================
    REAL_SPEC = """CN-M12-5P Circular Connector
Rated voltage: 250 VAC
Rated current: 4 A
Housing material: stainless steel
Protection: IP67
Operating temperature: -25 C to 85 C"""

    header("TEST 2  Real spec text must STILL extract everything (no regression)")
    rec = _run(REAL_SPEC)
    check("product_name (no duplicated token)", rec.product_name.value, "CN-M12-5P Circular Connector")
    check("current_rating", rec.current_rating.value, 4.0)
    check("voltage_rating", rec.voltage_rating.value, 250.0)
    check("material", rec.material.value, "stainless steel")
    check("ip_rating", rec.ip_rating.value, "IP67")
    check("current is grounded", rec.current_rating.is_grounded, True)
    check("current is verified", rec.current_rating.validation_status, "verified")

    # ==========================================================================
    header("TEST 3  Short inputs and spelled-out units are accepted")
    for text, field, expected in [
        ("Consumption 200 mA typical.", "current_rating", 0.2),   # mA normalised to A
        ("Output 15 amps maximum.", "current_rating", 15.0),
        ("Draws 2.5 A.", "current_rating", 2.5),
        ("Supply voltage 24 VDC nominal.", "voltage_rating", 24.0),
    ]:
        rec = _run(text)
        check(f"{text!r} -> {field}", getattr(rec, field).value, expected)

    # ==========================================================================
    header("TEST 4  Implausible or year-like numbers are rejected as MISSING")
    for text in [
        "Founded in 2008 A leading maker of industrial sensing equipment.",
        "Rated current 99999 A for the facility busbar assembly here.",
    ]:
        rec = _run(text)
        check(f"{text[:38]!r}... value", rec.current_rating.value, None)
        check(f"{text[:38]!r}... status", rec.current_rating.validation_status, "missing")

    # ==========================================================================
    header("TEST 5  Generic metals require construction context")
    rec = _run("Housing material: brass, nickel plated barrel assembly.")
    check("'Housing material: brass' -> brass", rec.material.value, "brass")
    rec = _run("The brass band played nearby in the town square that evening.")
    check("'brass band' -> None", rec.material.value, None)

    # ==========================================================================
    header("TEST 6  Site-title suffixes are stripped from product names")
    check("Wikipedia suffix", _strip_site_suffix("Proximity sensor - Wikipedia"), "Proximity sensor")
    check("DigiKey suffix", _strip_site_suffix("M12 Connector | DigiKey"), "M12 Connector")
    check("internal hyphen kept", _strip_site_suffix("Widget - Foo | Wikipedia"), "Widget - Foo")

    # ==========================================================================
    header("TEST 7  Genuinely empty input reports honestly (not as a fetch failure)")
    rec = _run("   ")
    check("product_name", rec.product_name.value, None)
    check("status", rec.product_name.validation_status, "missing")

    # ==========================================================================
    print("\n" + "=" * 70)
    if failures:
        print(f"{len(failures)} CHECK(S) FAILED:")
        for f in failures:
            print(f"  - {f}")
    else:
        print("ALL CHECKS PASSED")

    return failures, pass_count


def test_url_accuracy():
    """pytest entry point: runs every regression case and fails the test if
    any of them reported a mismatch, preserving the original pass/fail
    semantics."""
    failures, _ = _run_checks()
    assert not failures, f"{len(failures)} check(s) failed: {failures}"


def main():
    failures, _ = _run_checks()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
