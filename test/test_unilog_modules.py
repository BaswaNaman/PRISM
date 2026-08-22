"""
test_unilog_modules.py — unit tests for the commerce-content half.
==================================================================

These cover the modules added after the first review: the trust gate (`bridge`),
the item-type classifier, the terse-string attribute parser, the fifth
description format, and the sourcing policy.

Deliberately dependency-free: it uses plain dicts instead of Pydantic records
(the bridge accepts either), so it runs with nothing but the standard library.

Run directly:
    python test_unilog_modules.py

Run under pytest:
    pytest -q test_unilog_modules.py
"""

from app import sourcing
from app import database
from unilog import attrparse, bridge, classify, descriptions as desc, fractions_util, uom


def _run_checks():
    """Runs every check and returns (pass_count, fail_count, failed_labels).

    All state is local to this call so importing this module (or calling this
    function more than once, e.g. from both `python test_unilog_modules.py`
    and `pytest`) has no side effects and no shared global state.
    """
    PASS = 0
    FAIL = 0
    failures = []

    def check(label, condition, detail=""):
        nonlocal PASS, FAIL
        if condition:
            PASS += 1
            print(f"  PASS  {label}")
        else:
            FAIL += 1
            failures.append(label)
            print(f"  FAIL  {label}" + (f"  --> {detail}" if detail else ""))

    def field(value, status="verified", conf=0.95, unit=None, msg=None):
        """A stand-in for one extracted PRISM field."""
        return {
            "value": value,
            "unit": unit,
            "validation_status": status,
            "confidence_score": conf,
            "validation_message": msg,
            "source_snippet": f"...{value}...",
        }

    # -----------------------------------------------------------------------
    print("\n[1] Trust gate — bridge.to_product_record")
    # -----------------------------------------------------------------------
    record = {
        "product_name": field("CN-M12-5P Circular Connector"),
        "category": field("Circular Connector"),
        "voltage_rating": field("250", unit="VAC"),
        "current_rating": field("4", unit="A"),
        # must never reach a description: the quoted evidence was not in the source
        "ip_rating": field("IP67", status="flagged_ungrounded"),
        # sourced but not confident enough to publish
        "material": field("Brass", status="flagged_low_confidence", conf=0.55),
        # inferred by the model rather than read from the source
        "mounting_type": field("Panel Mount", status="ai_enriched", conf=0.9),
        # failed a deterministic rule
        "operating_temperature_max": field("900", status="flagged_validation_error", unit="C"),
        "connector_type": field("M12 5-pin"),
    }

    rec, report = bridge.to_product_record(record)
    labels = [a.label for a in rec.attributes]

    check("verified values are admitted", "Voltage" in labels and "Current" in labels, labels)
    check("ungrounded value is withheld", "Enclosure Rating" not in labels, labels)
    check("below-threshold confidence is withheld", "Material" not in labels, labels)
    check("AI-inferred value is withheld by default", "Mounting" not in labels, labels)
    check("validation-error value is withheld", "Max Operating Temperature" not in labels, labels)
    check("withheld count is 4", report.withheld_count == 4, report.withheld)
    check("every refusal carries a reason",
          all(w.get("reason") for w in report.withheld), report.withheld)
    check("hallucination refusal names the cause",
          any("hallucination" in w["reason"] for w in report.withheld), report.withheld)
    check("confidence refusal quotes the threshold",
          any("0.65" in w["reason"] for w in report.withheld), report.withheld)
    check("pass rate is reported", 0 < report.trust_gate_pass_pct < 100,
          report.trust_gate_pass_pct)
    check("MPN recovered from the product name", rec.mpn == "CN-M12-5P", rec.mpn)
    check("item type taken from category", rec.item_type == "Circular Connector", rec.item_type)

    rec2, report2 = bridge.to_product_record(record, include_enriched=True)
    labels2 = [a.label for a in rec2.attributes]
    check("include_enriched admits the inferred value", "Mounting" in labels2, labels2)
    check("inferred admissions stay labelled",
          report2.inferred_admitted == ["Mounting"], report2.inferred_admitted)
    check("include_enriched still refuses ungrounded values",
          "Enclosure Rating" not in labels2, labels2)

    full = bridge.build_from_prism(record)
    check("build_from_prism returns five formats", len(full["descriptions"]) == 5,
          list(full["descriptions"]))
    check("build_from_prism carries the trust report",
          full["trust_report"]["attributes_withheld"] == 4, full["trust_report"])

    identity_record = dict(record)
    identity_record["extra_attributes"] = {
        "model_number": field("DCB518ASTS06G"),
        "upc": field("008925172550"),
        "manufacturer_part_number": field("7872492 UPC 008925172550"),
        "grit_options": field("50, 80 and 120"),
    }
    identity_descriptions = bridge.build_from_prism(
        identity_record, brand="Diablo", mpn="DCB518ASTS06G",
        item_type="Sanding Belt",
    )["descriptions"]
    identity_text = " ".join(v["text"] for v in identity_descriptions.values())
    check("MPN remains intact in descriptions",
          "DCB518ASTS06G" in identity_text and "DCB518ASTS06 g" not in identity_text,
          identity_text)
    check("UPC and contaminated identifiers stay out of descriptions",
          "008925172550" not in identity_text and "7872492" not in identity_text,
          identity_text)
    check("non-identity dynamic attributes remain available to descriptions",
          "50, 80 and 120" in identity_text, identity_text)

    metadata_record = dict(record)
    metadata_record["extra_attributes"] = {
        "https": field("https://example.test/product"),
        "estimated_arrival_on": field("08/27/2026"),
        "length": field("18", unit="mm"),
        "grit": field("50, 80, 120", unit="Grit"),
    }
    metadata_record["extra_attributes"]["length"]["source_snippet"] = "18 in L"
    metadata_text = " ".join(
        v["text"] for v in bridge.build_from_prism(
            metadata_record, brand="Diablo", mpn="DCB518ASTS06G",
            item_type="Sanding Belt",
        )["descriptions"].values()
    )
    check("URLs and arrival dates stay out of descriptions",
          "example.test" not in metadata_text and "08/27/2026" not in metadata_text,
          metadata_text)
    check("evidence unit corrects a mismatched extracted dimension",
          "18 in" in metadata_text and "18 mm" not in metadata_text, metadata_text)
    check("semantic labels are not duplicated as units",
          "Grit Grit" not in metadata_text, metadata_text)

    grit_record = desc.ProductRecord(
        brand="Mirka", mpn="5B-332-080", item_type="Sanding Disc",
        attributes=[desc.Attribute("Grit", "80G")],
    )
    grit_text = " ".join(
        value["text"] for value in desc.build_all_descriptions(grit_record, uom.DEFAULT).values()
    )
    check("abrasive grit suffix is not converted to grams",
          "80G" in grit_text and "80 g" not in grit_text, grit_text)

    duplicate_record = dict(record)
    duplicate_record["material"] = field("Cloth")
    duplicate_record["mounting_type"] = field("Peel & Stick")
    duplicate_record["extra_attributes"] = {
        "abrasive_backing": field("Cloth"),
        "adhesion_type": field("Peel & Stick"),
        "brand_compatibility": field("Mirka"),
    }
    deduped, _ = bridge.to_product_record(
        duplicate_record, brand="Mirka", mpn="5B-332-080",
        item_type="Sanding Disc",
    )
    deduped_values = [str(a.value) for a in deduped.attributes]
    check("equivalent dynamic description values are deduplicated",
          deduped_values.count("Cloth") == 1 and deduped_values.count("Peel & Stick") == 1,
          deduped_values)
    check("identity values are not repeated as description attributes",
          "Mirka" not in deduped_values, deduped_values)

    empty_rec, empty_report = bridge.to_product_record({"product_name": field("Widget")})
    check("absent fields are not reported as withheld", empty_report.withheld_count == 0,
          empty_report.withheld)

    # -----------------------------------------------------------------------
    print("\n[2] Item-type classification — classify.ItemTypeClassifier")
    # -----------------------------------------------------------------------
    clf = classify.ItemTypeClassifier()

    expanded = classify.expand_abbreviations("3/8 CPLG BRS 150#")
    check("CPLG expands to coupling", "coupling" in expanded.lower(), expanded)
    check("BRS expands to brass", "brass" in expanded.lower(), expanded)
    # A substring replacement would corrupt this into 'sweatEAT'.
    check("whole-token expansion leaves SWEAT intact",
          "sweateat" not in classify.expand_abbreviations("SWEAT FITTING").lower(),
          classify.expand_abbreviations("SWEAT FITTING"))

    c1 = clf.classify("3/8 CPLG BRS 150#")
    check("terse coupling string classifies", c1.item_type is not None, c1.as_dict())
    check("coupling is the resolved type", "coupling" in (c1.item_type or "").lower(),
          c1.item_type)

    c2 = clf.classify("Single Handle Pull-Down Kitchen Faucet Chrome")
    check("kitchen faucet classifies", "faucet" in (c2.item_type or "").lower(), c2.item_type)
    check("a clear match is not flagged for review", not c2.needs_review, c2.as_dict())

    c3 = clf.classify("zzqq xxyy 99")
    check("gibberish is left unresolved rather than guessed", c3.item_type is None, c3.item_type)
    check("unresolved classification needs review", c3.needs_review, c3.as_dict())

    c4 = clf.classify(None)
    check("None input is handled", c4.item_type is None and c4.confidence == 0.0, c4.as_dict())

    stats = classify.ItemTypeClassifier.stats([c1, c2, c3])
    check("stats counts rows", stats["rows"] == 3, stats)
    check("stats reports a resolution rate", stats["resolution_rate_pct"] > 0, stats)
    check("fallback taxonomy is declared as such", clf.source == "fallback", clf.source)
    check("module-level helper still works",
          classify.guess_item_type("brass ball valve") is not None)

    # -----------------------------------------------------------------------
    print("\n[2b] Ambiguity-safe classification — the 'DW' regression")
    # -----------------------------------------------------------------------
    # Regression A: "DW" must not win just because it is present. The
    # proximity-sensor evidence ("PROX SNSR") is strong and unambiguous, and
    # must take precedence over the weak, ambiguous "DW" -> dishwasher guess.
    ca = clf.classify("DW AD 511 M8 PROX SNSR")
    check("DW does not force a Dishwasher classification",
          ca.item_type != "Dishwasher", ca.as_dict())
    check("ambiguous DW input is either resolved to the sensor evidence or "
          "sent for review",
          (ca.item_type is not None and "sensor" in ca.item_type.lower()
           and not ca.needs_review) or ca.needs_review,
          ca.as_dict())
    check("'dw' is left as a literal, unexpanded token in the description "
          "text (never silently rewritten to 'dishwasher')",
          "dishwasher" not in (ca.expanded_text or "").lower(), ca.expanded_text)

    # Regression B: previously-working coupling classification is untouched.
    cb = clf.classify("3/8 CPLG BRS 150#")
    check("CPLG -> Coupling classification still works",
          "coupling" in (cb.item_type or "").lower(), cb.as_dict())
    check("regression-B classification is confident, not flagged",
          not cb.needs_review, cb.as_dict())

    # Regression C: previously-working faucet classification is untouched.
    cc = clf.classify("Single Handle Pull-Down Kitchen Faucet Chrome")
    check("faucet classification still works",
          "faucet" in (cc.item_type or "").lower(), cc.as_dict())
    check("regression-C classification is confident, not flagged",
          not cc.needs_review, cc.as_dict())

    # Regression D: an ambiguous abbreviation with nothing else in the text to
    # corroborate it must not, by itself, produce a confident classification.
    cd = clf.classify("DW UNIT XJ4521")
    check("ambiguous abbreviation alone does not yield a confident guess",
          cd.item_type is None or cd.needs_review, cd.as_dict())
    check("an uncorroborated ambiguous abbreviation is specifically not read "
          "as its guessed meaning",
          cd.item_type != "Dishwasher", cd.as_dict())

    # Regression E: gibberish / unknown text must never produce a confident,
    # unsafe classification.
    ce = clf.classify("qzx flerm 88 wobblejuice throm")
    check("unknown/gibberish text stays unresolved",
          ce.item_type is None, ce.as_dict())
    check("unresolved gibberish is flagged for review",
          ce.needs_review, ce.as_dict())

    # Bonus: the flip side of Regression D — the same ambiguous abbreviation
    # CAN resolve, but only with real corroborating context, and even then it
    # is routed for human review rather than treated as a confident answer.
    cf = clf.classify("DW UNDERCOUNTER RACK DETERGENT PORTABLE")
    check("a corroborated ambiguous abbreviation can still resolve",
          cf.item_type == "Dishwasher", cf.as_dict())
    check("a corroborated-but-ambiguous resolution is always sent for review",
          cf.needs_review, cf.as_dict())

    # -----------------------------------------------------------------------
    print("\n[3] Attribute recovery — attrparse.parse_attributes")
    # -----------------------------------------------------------------------
    attrs = attrparse.parse_attributes("3/8 CPLG BRS 150#")
    d = attrparse.to_dict(attrs)
    check("size recovered", d.get("Size") == "3/8", d)
    check("material recovered as canonical Brass", d.get("Material") == "Brass", d)
    check("pressure class recovered", "150" in str(d.get("Pressure Class", "")), d)
    check("pressure class was not mistaken for the size", d.get("Size") != "150", d)
    check("every parsed attribute keeps its evidence",
          all(a.evidence for a in attrs), [a.label for a in attrs if not a.evidence])

    pair = attrparse.to_dict(attrparse.parse_attributes("3/8 x 1/4 REDUCING CPLG SS"))
    check("reducing fitting keeps both sizes",
          pair.get("Size") == "3/8" and pair.get("Second Size") == "1/4", pair)
    check("SS resolves to Stainless Steel",
          pair.get("Material") == "Stainless Steel", pair)

    mixed = attrparse.to_dict(attrparse.parse_attributes('1-1/2" MALLEABLE IRON ELBOW'))
    check("mixed number kept whole", mixed.get("Size") == "1-1/2", mixed)
    check("longest material name wins over 'iron'",
          mixed.get("Material") == "Malleable Iron", mixed)

    sched = attrparse.to_dict(attrparse.parse_attributes('2" SCH 40 PVC PIPE'))
    check("schedule recovered", "40" in str(sched.get("Schedule", "")), sched)
    check("schedule was not mistaken for the size", sched.get("Size") == "2", sched)

    # False positives are the failure that matters here: an invented size is worse
    # than a missing one, because a reviewer cannot tell it was invented.
    noise = attrparse.to_dict(attrparse.parse_attributes("LAV FCT 2-HNDL CHR"))
    check("handle count is not read as a size", "Size" not in noise, noise)
    check("handle count is still recovered", noise.get("Handle Count") == "2", noise)

    model = attrparse.to_dict(attrparse.parse_attributes("DW AD 511 M8 PROX SNSR"))
    check("model number is not read as a size", "Size" not in model, model)

    weight = attrparse.to_dict(attrparse.parse_attributes("1/2 LB WEIGHT"))
    check("a weight is not read as a size", "Size" not in weight, weight)

    flange = attrparse.to_dict(attrparse.parse_attributes("CL150 FLANGE 4 IN"))
    check("class prefix skipped, real size found later",
          flange.get("Size") == "4" and flange.get("Pressure Class") == "150", flange)

    metric = attrparse.to_dict(attrparse.parse_attributes("COUPLING 12 mm BRASS"))
    check("metric size keeps its own unit",
          [a.unit for a in attrparse.parse_attributes("COUPLING 12 mm BRASS")
           if a.label == "Size"] == ["mm"], metric)

    check("empty input yields no attributes", attrparse.parse_attributes("") == [], "")

    # -----------------------------------------------------------------------
    print("\n[4] Descriptions — all five formats and their limits")
    # -----------------------------------------------------------------------
    pr = desc.ProductRecord(
        brand="Moen", manufacturer="Moen", mpn="7594ESRS",
        item_type="Kitchen Faucet",
        attributes=[
            desc.Attribute("Finish", "Spot Resist Stainless", is_key=True),
            desc.Attribute("Handle Type", "Single Handle", is_key=True),
            desc.Attribute("Mounting", "Deck Mount"),
            desc.Attribute("Flow Rate", "1.5", unit="GPM"),
            desc.Attribute("Spout Height", "0.375", unit="in"),
        ],
        features=["Reflex hose system", "Power Clean spray technology"],
    )

    built = desc.build_all_descriptions(pr, uom.DEFAULT)
    check("five formats are built", len(built) == 5, list(built))
    check("web format is present",
          any("web" in k.lower() for k in built), list(built))

    for name, b in built.items():
        check(f"{name}: non-empty", bool(b["text"].strip()), b)
        check(f"{name}: length matches text", b["length"] == len(b["text"]), b)

    invoice = [b for k, b in built.items() if "invoice" in k.lower()][0]
    check("invoice description fits 40 chars", invoice["length"] <= 40, invoice)

    web = [b for k, b in built.items() if "web" in k.lower()][0]
    check("web description within its window",
          desc.WEB_MIN <= web["length"] <= desc.WEB_MAX, web["length"])
    check("web description reads as sentences", web["text"].rstrip().endswith("."), web["text"][-60:])
    check("web description does not truncate mid-word", "…" not in web["text"], web["text"][-60:])
    check("web description mentions the brand", "Moen" in web["text"], web["text"][:80])

    summary = desc.compliance_summary(built)
    check("compliance summary counts all five", summary["fields_built"] == 5, summary)
    check("compliance rate is a percentage", 0 <= summary["compliance_rate_pct"] <= 100, summary)

    # Decimal inches must render as trade fractions, with the mandatory space.
    rendered = desc.Attribute("Spout Height", "0.375", unit="in").rendered(uom.DEFAULT)
    check("decimal inches render as a fraction", rendered == "3/8 in", rendered)
    check("fraction conversion is exact",
          fractions_util.decimal_to_fraction("0.375") == "3/8",
          fractions_util.decimal_to_fraction("0.375"))
    check("number and unit are separated by a space",
          uom.DEFAULT.format_measurement("1.5", "GPM").count(" ") == 1,
          uom.DEFAULT.format_measurement("1.5", "GPM"))

    # An empty record must not crash the builders.
    blank = desc.build_all_descriptions(desc.ProductRecord(), uom.DEFAULT)
    check("empty record still returns five formats", len(blank) == 5, list(blank))

    # -----------------------------------------------------------------------
    print("\n[5] Sourcing policy — app.sourcing")
    # -----------------------------------------------------------------------
    # Passing approved_domains={} explicitly keeps these tests independent of
    # whether the user has created data/approved_domains.txt.
    NO_LIST = set()

    def verdict(url, policy="manufacturer_only"):
        return sourcing.evaluate_url(url, policy=policy, approved_domains=NO_LIST)

    v = verdict("https://www.amazon.com/dp/B0001")
    check("marketplace is rejected", not v.allowed, v.as_dict())
    check("marketplace is classified correctly", v.category == "marketplace", v.category)
    check("rejection carries a reason", bool(v.reason), v.as_dict())

    check("distributor is rejected under manufacturer_only",
          not verdict("https://www.grainger.com/product/123").allowed)
    check("distributor is permitted under allow_distributors",
          verdict("https://www.grainger.com/product/123", "allow_distributors").allowed)
    check("encyclopedia is rejected",
          not verdict("https://en.wikipedia.org/wiki/Ball_valve").allowed)
    check("community site is rejected",
          not verdict("https://www.reddit.com/r/plumbing/x").allowed)

    vm = verdict("https://www.moen.com/products/7594esrs")
    check("known manufacturer is permitted", vm.allowed, vm.as_dict())
    check("known manufacturer is classified as such", vm.category == "manufacturer", vm.category)
    check("known manufacturer needs no review", not vm.needs_review, vm.as_dict())

    vu = verdict("https://some-unknown-supplier.example/product/1")
    check("unknown domain is permitted", vu.allowed, vu.as_dict())
    check("unknown domain is never called a manufacturer", vu.category == "unknown", vu.category)
    check("unknown domain is flagged for review", vu.needs_review, vu.as_dict())

    check("subdomains resolve to the registrable domain",
          verdict("https://pdb2.turck.de/en/product").category == "manufacturer")
    check("multi-part TLDs resolve correctly",
          verdict("https://www.amazon.co.uk/dp/B0001").category == "marketplace")

    check("warn_only blocks nothing",
          verdict("https://www.amazon.com/dp/B1", "warn_only").allowed)
    check("warn_only still flags for review",
          verdict("https://www.amazon.com/dp/B1", "warn_only").needs_review)
    check("allow_all disables the gate",
          verdict("https://www.ebay.com/itm/1", "allow_all").allowed)

    # An explicit allow-list overrides the category rules entirely.
    allowed = sourcing.evaluate_url("https://www.grainger.com/x",
                                    policy="manufacturer_only",
                                    approved_domains={"grainger.com"})
    check("allow-list overrides the category rules", allowed.allowed, allowed.as_dict())
    blocked = sourcing.evaluate_url("https://www.moen.com/x",
                                    policy="manufacturer_only",
                                    approved_domains={"grainger.com"})
    check("an active allow-list excludes everything else", not blocked.allowed, blocked.as_dict())

    check("policy list is exposed", "manufacturer_only" in sourcing.POLICIES, sourcing.POLICIES)
    check("policy description is human-readable", len(sourcing.describe_policy()) > 20,
          sourcing.describe_policy())

    # -----------------------------------------------------------------------
    print(f"\n{'=' * 62}")
    print(f"  {PASS} passed, {FAIL} failed")
    print(f"{'=' * 62}\n")

    return PASS, FAIL, failures


def test_unilog_modules():
    """pytest entry point: runs every custom check and fails the test if any
    of them reported FAIL, preserving the original pass/fail semantics."""
    _, fail_count, failures = _run_checks()
    assert fail_count == 0, f"{fail_count} check(s) failed: {failures}"


# ---------------------------------------------------------------------------
# DATABASE INITIALIZATION — focused tests
# ---------------------------------------------------------------------------
# These target the two problems the fix addresses: DB init used to depend on
# FastAPI startup, and it must be idempotent. Each test points `DB_PATH` at
# an isolated temp file (via monkeypatch) and resets the module's init-guard
# flag, so these tests never touch the project's real `product_intelligence.db`
# and don't interfere with each other or with other tests in the suite.

def test_fresh_database_can_be_initialized(tmp_path, monkeypatch):
    """A brand-new SQLite file, with no `products` table yet, must become a
    valid PRISM database after `init_db()` — and calling `init_db()` again
    afterward must not fail or duplicate anything (idempotency)."""
    db_file = tmp_path / "fresh_products.db"
    monkeypatch.setattr(database, "DB_PATH", str(db_file))
    monkeypatch.setattr(database, "_db_initialized", False)

    assert not db_file.exists()

    database.init_db()
    assert db_file.exists()

    # Calling it again (and again) must be a safe no-op, not an error.
    database.init_db()
    database.init_db()

    conn = database.get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='products'"
        )
        assert cursor.fetchone() is not None, "products table was not created"
    finally:
        conn.close()


def test_database_operations_work_without_explicit_init(tmp_path, monkeypatch):
    """Proves the lazy-init guard: touching the database (save / get / list)
    without ever calling `init_db()` first — the exact pattern a script, a
    pytest test, or a direct function call bypassing FastAPI startup would
    follow — still works, and existing database operations keep behaving
    correctly (round-trip save -> get -> list)."""
    db_file = tmp_path / "lazy_products.db"
    monkeypatch.setattr(database, "DB_PATH", str(db_file))
    monkeypatch.setattr(database, "_db_initialized", False)

    from app.extractor import process_raw_product_text
    from app.validator import validate_and_enrich_record
    from app.enricher import enrich_missing_fields

    raw_text = "Voltage rating: 250 VAC. Current rating: 4 A. IP67 rated. Housing material: brass."
    record = process_raw_product_text(raw_text=raw_text, product_id="test_lazy_init_001")
    record = validate_and_enrich_record(record, raw_text)
    record = enrich_missing_fields(record, raw_text)

    # No call to database.init_db() anywhere above: save_product() must
    # create the schema on demand instead of raising
    # "sqlite3.OperationalError: no such table: products".
    saved = database.save_product(record)
    assert saved.id == "test_lazy_init_001"

    fetched = database.get_product("test_lazy_init_001")
    assert fetched is not None
    assert fetched.id == "test_lazy_init_001"

    all_products = database.list_products()
    assert any(p.id == "test_lazy_init_001" for p in all_products)


def main():
    _, fail_count, _ = _run_checks()
    return 1 if fail_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
