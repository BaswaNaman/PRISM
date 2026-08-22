# PRISM — Changes Made & What Remains

**Project:** PRISM Product Intelligence Engine
**Hackathon:** UniHack — "AI-Powered Product Intelligence for Industrial Commerce" (organiser: Unilog)
**This document:** every change in this revision, file by file, followed by an honest account of what is still missing and what it would take to close each gap.

---

## Part 0 — How to run and verify

```bash
pip install -r requirements.txt
python run.py                       # the app, at http://127.0.0.1:8000

python test_url_accuracy.py         # 26-assertion regression suite for the accuracy fixes
python test_unilog_modules.py       # 95-assertion suite for the trust gate, classifier,
                                    #   attribute parser, five formats and sourcing policy
python run_unilog_pipeline.py --demo         # Unilog compliance self-test, no client data needed
python run_unilog_pipeline.py --data-dir ./data   # full scoring, once the workbooks are dropped in
```

All three test commands currently pass end to end: `test_url_accuracy.py` reports **ALL CHECKS PASSED**, `test_unilog_modules.py` reports **95 passed, 0 failed**, and the Unilog demo reports **5/5 = 100% char-limit compliance** on every demo record.

> **Read Part 5 first if you have already read an earlier copy of this document.** A later revision closed most of the weak points listed in Part 3B, and the sections below have been updated to say so rather than left to mislead.

---

## Part 1 — What was wrong, and what changed

There were two separate problems in this revision. The first was that URL ingestion was inventing specifications. The second was that the app did not touch the hackathon's actual datasets at all.

### 1A. URL accuracy — the app was fabricating specs

The symptom you reported was that URL accuracy was far worse than PDF or text. The extractor is identical for all three inputs, so the difference was never the model — it was the input. A pasted spec block is nearly all signal. A fetched web page is mostly navigation, citations, footers, related products and cookie notices, and the regex heuristics were matching numbers out of that noise and presenting them as verified specifications.

Four concrete fabrications were found and fixed.

**Bug 1 — `current_rating = 0.012 A` from a citation date.**
The source text contained "Retrieved 12 March 2008". The unit alternation was written `(Amps|Amp|A|mA)` with `re.IGNORECASE`, so `mA` matched the "Ma" at the start of **March**. The extractor read "12 mA", and `normalize_field_units` dutifully converted it to 0.012 A — a number that looks entirely credible on the page. Fixed in `app/extractor.py` by reordering the alternation longest-first and closing it with a negative lookahead:

```python
([0-9]+(?:\.[0-9]+)?)\s*(mA|Amps|Amp|A)(?![A-Za-z])
```

The same fix was applied to the voltage pattern (`VAC|VDC|Volts|Volt|mV|kV|V` plus lookahead), which had the identical latent bug — "12 Volume" would have been read as 12 V.

**Bug 2 — `current_rating = 2008 A` from a founding year.**
The regex prefix `(?:current|current\s+rating|max\s+current|rating)?` was **optional**, so a bare number beside a stray capital "A" satisfied the whole pattern. "Founded in 2008 A leading maker of sensors" became a 2008-amp current rating. Fixed with three independent guards, all new in `extractor.py`:

- `_has_spec_context(raw_text, span, keywords, window_before, window_after)` — looks in a window around the match for a domain keyword (`current`, `amperage`, `draw`, `consumption`, `rated`, …). A number next to a unit token is not proof it *is* that spec.
- `_is_year_like(value)` — rejects integers in 1900–2099 when the unit token is weak.
- `_plausible(value, key)` with `PLAUSIBLE_RANGES` — an envelope per field (current 0.0001–5000 A, voltage 0.1–40000 V) so absurd magnitudes never reach the record.

A unit is exempt from the context requirement only when it is self-evidencing: `STRONG_CURRENT_UNITS = {"ma", "amp", "amps", "milliamp", "milliamps"}` and `STRONG_VOLTAGE_UNITS = {"vac", "vdc", "volts", "mv", "kv"}`. Nobody writes "200 mA" by accident; a bare "A" is ambiguous. This is why "Consumption 200 mA typical" still extracts cleanly while "2008 A" does not.

**Bug 3 — `material = brass` from an article about brass instruments.**
Materials were split into two classes. `SPECIFIC_MATERIALS` (compound names like "stainless steel", "nickel-plated brass") stand on their own at confidence 0.90, because those phrases essentially only appear in a materials context. `GENERIC_MATERIALS` (bare metal words — brass, steel, aluminium) now require `_has_spec_context(..., MATERIAL_CONTEXT_WORDS, 60, 40)`, meaning a nearby `housing`, `body`, `barrel`, `enclosure`, `construction`, `made of`, `finish`, `plated` and so on. Accepted at 0.84 with the wider evidence span as the snippet. "The brass band played nearby" is now correctly rejected, and the rejection reason says exactly why.

**Bug 4 — `product_name = "Proximity sensor - Wikipedia"`.**
A page title is not a product name. `SITE_TITLE_SUFFIX_RE` and `_strip_site_suffix()` strip trailing site branding — Wikipedia, Amazon, eBay, IndiaMART, DigiKey, Mouser, RS Components, Farnell, Newark, AutomationDirect, Grainger, Home Depot, Walmart, and generic tails like "Official Site", "Datasheet", "Buy Online". It loops up to three times to handle stacked suffixes ("Widget - Foo | Wikipedia" → "Widget - Foo", correctly keeping the internal hyphen).

Relatedly, `_guess_product_name`'s last-resort branch — first meaningful line, when there is no part number and no product-type phrase anywhere — had its confidence dropped from 0.68 to **0.45**. That is below the 0.65 threshold, so such names now land in the review queue instead of silently becoming catalogue entries. The reasoning string explains that the page may be editorial rather than a purchasable product.

**Bug 5 — duplicated token in the product name.**
"CN-M12-5P Circular Connector" was coming out as "CN-M12-5**P P** Circular Connector". The product-type phrase regex was allowed to start mid-token, so it matched " P Circular Connector" beginning inside the part number, and that was then concatenated onto the full part number. Fixed with a `(?<![A-Za-z0-9])` lookbehind forcing the phrase to start on a real word boundary, plus a containment check before joining.

**Bug 6 — short input reported as a failed fetch.**
This one was masking the others. `process_raw_product_text` opened with:

```python
if not fetch_success or len(raw_text.strip()) < 40 or ...:
```

Any input under 40 characters short-circuited into the failure path: every field nulled, status `flagged_validation_error`, message "URL fetch failed (HTTP 200)". So pasting `Housing material: brass.` produced nothing at all, with a message that was simply untrue. The guard now distinguishes a genuine `hard_failure` (fetch actually failed, or the ingestion layer prefixed the text) from `too_short` (under 10 characters), and reports each with an accurate message. A one-line spec paste now extracts normally.

### 1B. `app/ingestion.py` — give the extractor better text to work with

Guards alone are defensive. The other half is not handing the extractor a page full of navigation in the first place.

- `_tables_to_text(soup)` flattens `<table>` rows and `<dl>` lists into `Label: Value` lines. This matters more than it sounds: spec tables are where the real data lives, and once flattened this way the label sits immediately before the number, which is precisely what the new context guards look for. The two changes are designed to work together.
- `_select_main_region(soup)` picks the densest node matching `MAIN_CONTENT_SELECTORS` (`main`, `article`, `[role=main]`, `#content`, `.product-detail`, `.specs`, `#mw-content-text`, …) with at least 200 characters, falling back to the whole document rather than risking an empty result.
- Spec tables are extracted **before** noise stripping, then prepended to the body under an `=== EXTRACTED SPECIFICATION TABLES ===` header, with the remaining page under `=== PAGE CONTENT ===`.
- `NOISE_PATTERN` expanded to strip related/recommended/upsell/cross-sell blocks, reviews and star ratings, share and print widgets, copyright, legal and disclaimer text, adverts, banners, promos, carousels, references, citations, `catlinks`, `navbox`, hidden infoboxes, edit-section links, `mw-jump`, `siteSub` and tables of contents. `ol` and `span` are now stripped as containers too.
- Sparse pages changed from `success: False` to `success: True` with an explanatory `error` string plus new `sparse` and `cleaning_notes` keys — a thin page is a diagnosis, not a crash.

### 1C. `test_url_accuracy.py` (new)

Every bug above is pinned by an assertion, plus a "must not regress" case proving a full spec block still yields all five fields verified and grounded. 26 assertions, exit code 0/1 so it can gate a demo.

---

## Part 2 — The Unilog dataset layer (new `unilog/` package)

The gap assessment was correct: the grading for this challenge is dataset-driven, and the app was not touching the datasets. This package addresses that. Every module degrades gracefully — each loader prints *why* it could not load and the callers report "not loaded" rather than silently fabricating a score.

| Module | What it does |
|---|---|
| `fractions_util.py` | Builds the full decimal↔fraction table for all 63 sixty-fourths. `decimal_to_fraction(50.25)` → `"50-1/4"`; `0.3333` → `None` (not a clean 64th, left as a decimal rather than rounded into a lie). Round-trips via `fraction_to_decimal()`. |
| `uom.py` | ~140 seed unit variants → approved abbreviation, and enforces the mandatory space between number and unit. `"24in"` → `"24 in"`, `"50-1/4IN"` → `"50-1/4 in"`. Unrecognised units are recorded in `report_unmapped()` instead of passing through as if compliant. `load_uom_table()` merges the authoritative workbook over the seed defaults when present. |
| `descriptions.py` | The five description formats: Invoice (≤40 chars, CAPS), Mobile (60–80), Product Title, Long Description. Truncation is attribute-aware — whole attributes drop from the tail, never a mid-word cut. Each returns a `BuiltDescription` carrying length, limit and a compliance verdict, so char-limit compliance is a directly reportable number. |
| `brands.py` | Brand/manufacturer canonicalisation against the 27k approved list: placeholder → exact → fuzzy-accept (≥0.88) → fuzzy-suggest-needs-review (≥0.72) → unresolved. `PLACEHOLDERS` filters `-- Unbranded --`, `-- No Unilog Brand --`, `N/A` and friends, because a placeholder is not data. |
| `lov.py` | Controlled-vocabulary validation. `AttributeSpec` holds permitted values with `canonical()` and `suggest()`; `LOVRegistry` resolves by classpath with prefix fallback; `ValidationResult` reports `lov_compliance_pct`. |
| `dedup.py` | `normalize_mpn()` strips punctuation, uppercases and drops leading zeros, so `DW-AD-511-M8`, `dw ad 511 m8` and `DWAD511M8` collapse to one product. The most complete row wins. Cross-brand MPN collisions are **flagged, not merged** — the same part number under two brands is usually two different parts. |
| `evaluate.py` | Field-level accuracy against the 200-item ground truth, reported at two strictnesses. Exact match is what the delivery format demands; normalised match ignores case and punctuation. The gap between them is `formatting_only_error_pct` — which tells you whether you have a quick fix or a modelling problem. Also reports per-field coverage and the five weakest fields. |
| `run_unilog_pipeline.py` | CLI runner. `--demo` self-tests with no client data; `--data-dir ./data` runs dedup → brand resolution → description generation → ground-truth scoring and prints the metrics the brief tells judges to look for. `--limit` and `--json` supported. |

A design decision worth stating: **nothing is invented.** Every description builder consumes an explicit `ProductRecord` and omits absent parts. A fluent sentence built from guessed values scores zero against the ground truth and is worse than a short honest one. Where the Mobile description cannot reach its 60-character floor from real data, it says so in `notes` rather than padding.

Decimal-to-fraction conversion is centralised in `Attribute.rendered()` so all five formats inherit it — that fix is why the coupling renders as `3/8 IN` rather than `0.375 IN`.

---

## Part 3 — What still needs to be done

This is the honest part. I would rather you go in knowing the gaps than be surprised by a judge finding them.

### 3A. Blocking — needs the client data pack

**Nothing in `unilog/` has been scored against real data, because the workbooks were never uploaded.** The code paths are written and the demo self-test passes, but the headline number the brief actually asks for — field-level accuracy against the 200 items — does not exist yet. Drop these into `./data/` and run `python run_unilog_pipeline.py --data-dir ./data`:

```
Unilog-Sample_200_Items-Input-vs-Output.xlsx     <- ground truth (the one that matters)
Sample-1000_Items.xlsx                           <- volume input
UniCat_Manufacturer_and_Brand_List.xlsx          <- 27k approved brands
Unicat_Lov_v1_0_Updated_With_Remarks.xlsx        <- cross-category LOV
FAUCETS_LOV.xlsx / Fittings_LOV.xlsx             <- category LOVs
Unilog_Master_UOM_Standards_Abbreviations_and_Terms.xlsx
Decimal_Fraction.xlsx
```

Expect the loaders to need adjustment on first contact. They were written against the column names described in the brief, not against the real files, and hackathon workbooks habitually have multi-row headers, merged cells and inconsistent sheet names. The loaders are deliberately defensive (they hunt for a plausible header row and print what they found) but budget an hour for this. **Do this first — everything else is guesswork until you have a baseline number.**

`openpyxl` is required for all workbook loading and is listed in `requirements.txt`.

### 3B. Known weak points in what I built

**~~Item-type classification is a stub.~~ CLOSED in Part 5.** Replaced by `unilog/classify.py`, an IDF-weighted classifier that uses the LOV taxonomy as its candidate set when a workbook is loaded.

**The UOM table is a seed, not the standard.** ~140 variants against the client's ~500 abbreviations over 89 measurement types. `load_uom_table()` will override it, but until the workbook is loaded, coverage is partial and `report_unmapped()` is how you find the holes.

**~~Attribute extraction does not feed the description builders yet.~~ CLOSED in Part 5.** `unilog/bridge.py` is that join, and it runs everything through the trust gate on the way.

**Invoice abbreviations are hand-seeded.** The list in `descriptions.py` covers the sample data. The client has an approved abbreviations list; that should replace it.

**Fuzzy brand thresholds (0.88 / 0.72) are guesses.** They were chosen to be conservative. Tune them once you can measure precision against the real brand list.

**~~Web description is not separately built.~~ CLOSED in Part 5.** `build_web_description()` is a dedicated builder with its own 120–1500 character window and sentence-level truncation.

**The trust gate's thresholds are judgement calls.** `MIN_CONFIDENCE = 0.65` in `bridge.py`, and the decision to withhold `ai_enriched` values by default, are defensible but not measured. Once you have a ground-truth baseline, sweep the threshold and pick the value that maximises scored accuracy — the right number is an empirical question, and right now it is an opinion.

### 3C. Not attempted, and why

**JavaScript-rendered spec tables cannot be read.** The ingestion layer fetches raw HTML. Where a site builds its spec table client-side, the numbers are simply not in the response, and no amount of cleaning will find them. Solving this needs a headless browser (Playwright/Selenium), which is a heavy dependency for a hackathon and slow enough to hurt a live demo. I judged it out of scope. Be ready to say this plainly if a judge asks — "we detect and report thin content rather than guessing at it" is a defensible answer, and `sparse` plus `cleaning_notes` is how the app reports it.

**No image or PDF-diagram understanding.** Text and tables only.

**~~No persistence of the Unilog layer.~~ CLOSED in Part 5.** The compliance layer is now reachable from the API (`/api/unilog/report`) and has its own UI tab, so the demo no longer depends on a terminal dump. It still does not *write* generated descriptions back into SQLite — they are computed on request, which keeps them consistent with the current extraction rather than going stale.

### 3D. If you have limited time, in this order

1. Load the ground truth and get a baseline accuracy number. Without it you are optimising blind.
2. Load the LOV workbook, so the classifier gets the real taxonomy and LOV compliance becomes a real percentage instead of `UNAVAILABLE`.
3. Load the UOM standards workbook and clear whatever `report_unmapped()` names.
4. Sweep the trust gate's `MIN_CONFIDENCE` against that baseline and set it from evidence.
5. Tune the weakest fields that `evaluate.py` names for you.
6. Extend the extractor's typed fields beyond the electrical set, or lean harder on the generic `attrparse` + LOV-attribute path, so plumbing categories score as well as the trust layer deserves.

---

## Part 4 — Where this stands against the brief

The transferable asset here is the **trust layer**, and it is genuinely strong. Snippet grounding verifies that a claimed source quotation exists verbatim in the source before any value is allowed to be called "verified". Ungrounded values are visually distinct in the UI and never silently promoted. AI-enriched values stay statistically separate from source-extracted ones. Confidence thresholds route uncertain items to a review queue. The new semantic guards mean the system now declines to answer rather than inventing a plausible number, and every rejection carries a human-readable reason.

That matters for this challenge specifically, because the brief calls out confidence scoring and needs-review flagging as valuable, and because in a real catalogue a fabricated spec is worse than a blank field. A blank field is a task; a wrong one is a return, or a safety incident.

What is still thin is the dataset-driven half. The modules exist and self-test, but they have not met the real workbooks, and the accuracy number that the brief treats as the scoreboard has not been produced. Closing 3A is what converts this from a well-engineered trust layer into a submission that can be scored.

One honest framing for the demo: lead with the fabrication cases. Show "Retrieved 12 March 2008" producing nothing, then show the same extractor pulling all five fields cleanly from a real spec block, then show the rejection reason. A system that knows what it does not know is a more interesting claim than a system that fills every box.

---

## Part 5 — This revision: closing the four gaps reviewers found

Two independent reviews of the previous revision landed on the same conclusion: the trust layer was strong, but the two halves of the codebase were not joined, and three things the brief asks for outright were absent. This revision closes them. Everything below is new or substantially rewritten.

### 5A. `unilog/bridge.py` (new) — the join, and it runs through the trust gate

This is the file that was missing. PRISM extracted attributes with evidence, confidence and a grounding verdict; `descriptions.py` built five formats from a `ProductRecord`; nothing connected them, so every description in the previous revision came from a hand-written demo record.

`to_product_record()` closes that, and deliberately does not pass everything it is given:

| Extraction status | Reaches a description? |
|---|---|
| `verified` | yes, if confidence ≥ 0.65 |
| `flagged_low_confidence` | yes, if confidence ≥ 0.65 |
| `ai_enriched` | **no** by default — inferred is not sourced |
| `flagged_ungrounded` | **never** — the quoted evidence was not in the source |
| `flagged_validation_error` | **never** — a deterministic rule rejected it |

Every refusal is recorded in `BridgeReport.withheld` with a human-readable reason (*"evidence could not be located in the source (possible hallucination)"*, *"confidence 0.55 below the 0.65 publication threshold"*), so an omission is reportable rather than silent. That is what turns the grounding work from a badge in the UI into a measurable input-quality gate on content generation. `include_enriched=True` relaxes the inference rule for demos, but those attributes stay labelled in `ai_inferred_admitted` so they can never masquerade as sourced values.

The reasoning, stated plainly because it is the argument for the whole design: an invented specification scores zero against ground truth, so publishing nothing beats publishing a guess.

### 5B. `unilog/classify.py` (new) — a real classifier, not 20 nouns

`_guess_item_type()` is deleted. The replacement expands trade abbreviations first (`CPLG` → coupling, `BRS` → brass, `FCT` → faucet, `THD` → threaded) and then scores candidates by IDF-weighted **whole-token** overlap. Expansion is done with `re.sub(r"[A-Za-z]+", ...)` rather than substring replacement, because a substring replacement turns `SWEAT` into `sweatEAT` — there is a test pinning exactly that.

When a LOV workbook is loaded the candidate set *is* the client taxonomy, so a match also yields a classpath and therefore the attribute set that classpath permits (`self.source == "lov_taxonomy"`). Without one it falls back to a built-in ~79-type taxonomy weighted toward fittings and faucets (`self.source == "fallback"`), and the report says which was used. Matches below `MIN_SCORE`, or within `REVIEW_MARGIN` of the runner-up, are marked `needs_review` instead of guessed — `MISC HARDWARE ITEM` comes back unresolved, which is correct, because a wrong item type corrupts all five descriptions at once.

### 5C. `unilog/attrparse.py` (new) — so LOV validation has something to validate

Wiring up the validator exposed a second problem: with no attributes in the record, `registry.validate()` was being handed an empty dictionary, and a 100% compliance score over zero checked values is meaningless. `attrparse.py` recovers attributes from terse rows like `3/8 CPLG BRS 150#` — size, second size on reducing fittings, material, finish, connection type, pressure class, schedule, handle count, voltage, current — each carrying the substring it came from.

The hard part is false positives, and the tests caught three of mine:

- **Leftmost-alternation bug.** `(\d+...|\d+/\d+)` matched only the `3` of `3/8`, reporting a 3-inch coupling as the size of a 3/8-inch one. Same class of bug as the `0.012 A` fabrication in Part 1; fixed by ordering the alternation longest-first (mixed numbers, then fractions, then decimals, then bare integers).
- **`Size = 2 in` from `LAV FCT 2-HNDL CHR`**, and **`Size = 511 in` from `DW AD 511 M8 PROX SNSR`.** Fractions and mixed numbers are self-evidently trade sizes; a bare integer is not. Bare integers and decimals are now accepted only when an explicit unit marker follows, which drops both false positives while keeping `1 IN`, `2"` and `24 IN`.
- **Over-broad context guard.** The first version of that guard rejected any number with `SCH`/`CLASS`/`#` within eight characters, which threw away the perfectly good `2"` in `2" SCH 40 PVC PIPE`. The guard is now directional: prefix keywords (`SCH 40`, `CL150`) are checked behind the number, suffix keywords (`150#`, `2-HNDL`, `1.5 GPM`) ahead of it. It also scans every numeric candidate rather than stopping at the first, so a leading model number no longer hides a real size later in the string.

An invented size is worse than a missing one, because a reviewer has no way to tell it was invented. That is why these are tests and not comments.

### 5D. LOV validation is now actually invoked

The previous revision built `LOVRegistry.validate()` and never called it. `run_unilog_pipeline.py` now calls it on every row, adopts the registry's corrected spellings back into both the attribute dict and the rendered attributes, and tracks checked / in-vocabulary / corrected / violations / open-vocabulary-skipped totals.

**When no vocabulary is loaded it reports LOV compliance as `UNAVAILABLE`, with the reason, rather than as 100%.** A percentage computed against an empty vocabulary is not a score, and quoting one would be the most misleading number in the report. The same rule is enforced in the API (`lov_compliance.unavailable_reason`) and surfaced verbatim in the UI.

### 5E. `app/sourcing.py` (new) — the manufacturer-only rule

The brief requires manufacturer sources and nothing enforced it. Domains are classified on their registrable name (so `smile.amazon.co.uk` and `www.amazon.com` both resolve to amazon) into `manufacturer`, `distributor`, `marketplace`, `encyclopedia`, `community` or `unknown`. An unrecognised domain is never optimistically called a manufacturer: it is permitted but `needs_review`.

The gate runs in `fetch_and_clean_url()` **before** the HTTP request, so a rejected source is never fetched and its text can never leak into a record. Four policies (`manufacturer_only` default, `allow_distributors`, `warn_only`, `allow_all`) via `PRISM_SOURCING_POLICY`. Copying `data/approved_domains.txt.example` to `data/approved_domains.txt` activates a strict allow-list that overrides the category rules entirely — the right configuration for a graded run. Every rejection carries the reason for its category, and every ingestion result now carries a `sourcing` block.

### 5F. The compliance layer is visible in the UI

New endpoints in `app/main.py`: `GET /api/unilog/report`, `GET /api/unilog/product/{id}`, `GET /api/sourcing/policy`, `POST /api/sourcing/check` (which classifies a URL without fetching it — a useful demo lever). LOV workbooks are loaded once and cached, with loader notes.

New UI tab, **4. Commerce Output & Compliance**: four KPI tiles (char-limit compliance, LOV compliance, item-type resolution, trust-gate pass rate), a report-context panel that shows the LOV-unavailable warning verbatim when applicable, a live sourcing-policy checker, and every product's five descriptions with pass/fail badges and the list of attributes the trust gate withheld, each with its reason. Rules cards 5 and 6 document the sourcing and commerce-compliance rules.

### 5G. `test_unilog_modules.py` (new) — 95 assertions

Dependency-free (it passes plain dicts where a Pydantic record would go, which `bridge` supports deliberately). Covers the trust gate's admit/withhold matrix and refusal reasons, abbreviation expansion and classifier behaviour including the unresolved case, the attribute parser including all three false-positive classes above, all five description formats and their limits, decimal-to-fraction rendering and number-unit spacing, and the sourcing policy across all four modes plus allow-list override.

It found three real bugs on first run. That is the argument for writing it.

### 5H. `README.md` rewritten

The front door still described an 11-field electrical-connector app, which is why both reviewers concluded that features which exist were missing. It now describes the current system — both halves, the gate between them, the five formats with the limits actually implemented in code, the sourcing policy, the endpoints, and an honest status section. Description limits are cross-referenced to the constants in `descriptions.py` so the document cannot drift from the code again.

### Also fixed

- `unilog/descriptions.py` gained `build_web_description()` with its own 120–1500 window, dropping whole sentences from the tail rather than truncating mid-word, and adding no adjectives or benefit claims — every clause traces to an attribute that passed the gate.
- `run_unilog_pipeline.py --demo` gained four new sections: item-type classification, attribute recovery, LOV validation (showing both the honest-unavailable path and a hand-built demo vocabulary exercising correction, violation, open-vocabulary skip and not-applicable), and the full PRISM → bridge → five-formats path with its trust numbers.
- `.env` is excluded from the distribution zip. It holds live API keys; `.env.example` ships instead.
