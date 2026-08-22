# PRISM — Demo Script (≈3 minutes)

The order below is deliberate: it opens on the problem, shows the gate refusing bad
data, and ends on the commerce output, because that is what the challenge is
actually scored on.

Before you start: `cd claude && python run.py`, then open `http://127.0.0.1:8000`.
Have a second terminal ready with `python run_unilog_pipeline.py --demo`.

---

## 0:00 – 0:25 | The problem, in one line of data

**Say:**
> "This is a real catalogue row: `3/8 CPLG BRS 150#`. To sell that online you need
> to know it's a coupling, that the size is three-eighths of an inch and *not* three
> inches, that BRS is brass, and that 150# is a pressure class and not a size. Then
> you need five different descriptions of it at five different character limits.
> PRISM does that — and, more importantly, it refuses to guess."

---

## 0:25 – 0:55 | The sourcing gate (tab 1)

**Click:** the third sample chip, **AutomationDirect 8mm Proximity Sensor**.

**Say:**
> "The guidelines require manufacturer sources. Watch what happens with a
> distributor listing."

It is rejected, with the reason: distributor pages are derived from manufacturer
data and re-map units into the distributor's own schema. Point out that the check
runs **before** the HTTP request, so the page is never fetched and its text can
never leak into a record.

**Then click** the first chip, **Phoenix Contact M12 Connector**, and run the
pipeline. That one is a manufacturer domain, so it proceeds.

> "The same rule caught our worst bug during development — we were parsing a
> 'current rating' out of a Wikipedia citation date. Wikipedia is now blocked as a
> source outright."

---

## 0:55 – 1:35 | Extraction, and evidence that gets checked (tab 1)

Point at the enriched card: voltage, current, connector type, material, temperature
range — each with a confidence score and a status.

**Click "Trace"** on any field to open the evidence drawer.

**Say:**
> "Every value carries the verbatim quote it came from. The important part is that
> we then check that quote back against the source text. If the quote isn't actually
> in the page, the value is marked `flagged_ungrounded` — because a
> confident-sounding value with invented evidence is the most expensive failure in
> catalogue work. Snippet grounding proves the page said it; it doesn't prove it's
> true, which is why sourcing is a separate check."

---

## 1:35 – 2:35 | The gate, and the commerce output (tab 4)

**Click:** tab **4. Commerce Output & Compliance**, then **Refresh Report**.

Walk the four KPI tiles: character-limit compliance, LOV compliance, item-type
resolution, trust-gate pass rate.

**Say:**
> "Here are the five formats — invoice at forty upper-case characters for the ERP,
> mobile at sixty to eighty, product title, long description, and the web
> description — each scored pass or fail against its limit."

Then scroll to a **withheld** block and land the main point:

> "This is the part I'd point at. These attributes were extracted, but they did
> *not* reach any description, and each one says why: evidence couldn't be located
> in the source, or confidence below the 0.65 publication threshold, or the value
> was inferred rather than read. An invented specification scores zero against
> ground truth, so publishing nothing beats publishing a guess — and nothing is
> silently dropped, every refusal is reportable."

If LOV shows **N/A**, say so plainly:

> "We don't have the LOV workbook loaded, so we report LOV compliance as
> unavailable with the reason, rather than as 100%. A percentage against an empty
> vocabulary isn't a score."

---

## 2:35 – 3:00 | It's tested, and here's what's missing

**Switch to the terminal:**

```bash
python test_unilog_modules.py      # 95 passed
python test_url_accuracy.py        # ALL CHECKS PASSED
```

**Say:**
> "Ninety-five assertions over the trust gate, the classifier, the attribute parser
> and the sourcing policy. Writing them found three real bugs, including one where a
> regex matched only the '3' of '3/8' and reported a three-eighths coupling as three
> inches.
>
> And to be straight about the gap: our eleven typed extraction fields are
> electrical, while the ground-truth set is faucets and fittings. The generic path
> handles those categories, but the typed fields don't, so raw extraction scores on
> plumbing items will be lower than the trust layer's quality suggests. That's the
> first thing we'd widen."

---

## If asked

**"Why not RAG?"** — Retrieval helps when the answer is spread across a corpus.
Here the answer is one value on one page, and the hard part is deciding whether to
trust it. RAG would add a retrieval step without adding a verification step.

**"What if the item type is wrong?"** — A wrong item type corrupts all five
descriptions at once, so below the score threshold, or too close to the runner-up,
the classifier returns `needs_review` instead of a guess. Try `MISC HARDWARE ITEM`.

**"Does it work without API keys?"** — Yes. The extractor falls back to its
deterministic regex layer, and the compliance half needs no keys at all — that's the
`--demo` command.

**"Where does the 0.65 threshold come from?"** — It's a judgement call, not a
measured optimum. It's one constant (`MIN_CONFIDENCE` in `unilog/bridge.py`), and
calibrating it against the ground-truth set is on the list.
