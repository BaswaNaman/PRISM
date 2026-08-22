# Project Summary — PRISM Product Intelligence & Commerce Content Engine

**Challenge:** UniHack — AI-Powered Product Intelligence for Industrial Commerce (organiser: Unilog)

---

## 1. Problem

Industrial distributors hold product data scattered across manufacturer websites,
PDF datasheets and legacy ERP rows, and almost none of it is sellable as written. A
real catalogue row looks like `3/8 CPLG BRS 150#`. To put that on a web store you
need to know it is a coupling, that the size is three-eighths of an inch and not
three inches, that BRS means brass, that 150# is a pressure class rather than a
size — and then you need five different descriptions of it at five different
character limits. Done by hand this is hundreds of hours of work, and the errors it
produces are expensive, because a wrong specification sells the wrong part.

---

## 2. Solution

PRISM has two halves and a gate between them. The gate is the design decision the
whole project rests on.

**Half one — get the data, and know how much to trust it.** A URL, a PDF or pasted
text comes in. Sourcing is checked *before* the fetch, HTML is reduced to its main
content region, spec tables are flattened to `Label: Value` lines, and an AI chain
(Gemini → Claude → deterministic regex) extracts attributes. Every value carries a
verbatim `source_snippet`, and that snippet is checked back against the source text.
If the quote is not in the source, the value is marked `flagged_ungrounded`.

**Half two — turn trusted data into sellable content.** Attributes are normalised
(decimal ↔ fraction in 64ths, approved UOM abbreviations with the mandatory
number-unit space), brands are canonicalised, item type is classified, values are
checked against the LOV controlled vocabulary, and five description formats are
generated to their character limits.

**The gate.** Only values that survived verification may reach a description. An
ungrounded value, an AI-inferred value, or a value below the 0.65 confidence
threshold is withheld, with a human-readable reason attached and shown in the UI.
The reasoning is simple: an invented specification scores zero against ground truth,
so publishing nothing beats publishing a guess. Nothing is silently dropped — every
refusal is reportable.

---

## 3. Technology

Python 3.13, FastAPI, Uvicorn, Pydantic v2, SQLite. `httpx` + `beautifulsoup4` +
`pypdf` for ingestion. Gemini `gemini-2.0-flash` then Claude
`claude-3-5-sonnet-latest` for extraction, backed by a deterministic regex layer
that runs when no key is present — so the app works end to end offline. Validation
is pure Python (`app/validator.py`, `app/enricher.py`). The frontend is a vanilla
HTML/CSS/JS single-page app with five tabs and no build step.

---

## 4. What makes it different

| | Typical AI extraction tool | PRISM |
|---|---|---|
| **Sources** | Fetches whatever it is given | Manufacturer-only rule enforced **before** the HTTP request |
| **Evidence** | Confidence score only | Verbatim snippet re-checked against the source; failures marked ungrounded |
| **Bad values** | Published with a low score | **Withheld from generated content, with a stated reason** |
| **Output** | A JSON blob | Five commerce description formats, scored against their character limits |
| **Vocabulary** | Free text | LOV controlled-vocabulary validation, with corrections adopted back |
| **Unknown item type** | Guessed | Marked `needs_review` rather than guessed |
| **Missing vocabulary** | Would report 100% | Reports `UNAVAILABLE`, with the reason |

That last row matters more than it looks. A compliance percentage computed against
an empty vocabulary is not a score, and quoting one would be the most misleading
number in the report.

---

## 5. Deliverables

A working FastAPI backend and five-tab UI, including a **Commerce Output &
Compliance** tab that renders the full compliance report, a live sourcing-policy
checker, and every attribute the trust gate withheld with its reason. A CLI
(`run_unilog_pipeline.py --demo`) that exercises the compliance half with no API key
and no server. Two test suites — 26 assertions over the extraction guards, 95 over
the trust gate, classifier, attribute parser, description formats and sourcing
policy — both passing. `README.md` describes the system as built; `README_CHANGES.md`
documents every change with reasoning and what remains.

---

## 6. Honest status

The compliance half is complete and tested. LOV validation is invoked on every run
but has nothing to validate against until the supplied workbooks are dropped into
`data/`, and it reports that rather than inventing a number.

The known structural limitation: the extractor's eleven typed fields are electrical
(voltage, current, IP rating, connector type, temperature range), while the
ground-truth categories are faucets, fittings and appliances. The generic path —
`unilog/attrparse.py` plus the LOV attribute set for a classpath — covers those
categories, but the typed fields do not, so raw extraction scores on plumbing items
will be lower than the trust layer's quality suggests. That is a coverage gap rather
than a correctness one, and it is the first thing to widen.
