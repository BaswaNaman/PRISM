import os
import json
import io
import csv
import traceback
import uvicorn
from contextlib import asynccontextmanager
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from typing import List, Optional
from app.discovery import discover_and_enrich
from app.schema import (
    EnrichRequest, ReviewActionRequest, ProductIntelligenceRecord, 
    StatsResponse, BatchEnrichRequest, DiscoveryRequest, DiscoveryResponse
)
from app.ingestion import fetch_and_clean_url, extract_text_from_pdf
from app.extractor import process_raw_product_text
from app.validator import validate_and_enrich_record
from app.enricher import enrich_missing_fields
from app.database import (
    init_db, save_product, get_product, list_products, 
    update_field_review, get_analytics_stats, seed_sample_products_if_empty
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: identical logic to the old `@app.on_event("startup")` handler,
    # just expressed through the non-deprecated lifespan mechanism. Database
    # initialization itself no longer *depends* on this running (see
    # app/database.py's lazy `_ensure_db_initialized()` guard) — this call is
    # kept so the DB (and sample data) are ready before the first request
    # rather than being initialized lazily on it.
    init_db()
    seed_sample_products_if_empty()
    gemini_key = os.environ.get("GEMINI_API_KEY")
    print("=" * 70)
    if gemini_key and len(gemini_key.strip()) > 10:
        print(f"✅ GEMINI_API_KEY detected (starts with '{gemini_key.strip()[:8]}...').")
    print("=" * 70)
    yield
    # No shutdown behavior is required.


app = FastAPI(title="PRISM API", version="2.2.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={"detail": f"Invalid request payload: {exc.errors()}"})

@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    print("=" * 70)
    print(f"[UNHANDLED EXCEPTION] {request.method} {request.url.path}")
    traceback.print_exc()
    print("=" * 70)
    return JSONResponse(status_code=500, content={"detail": f"Server error: {exc}"})

# ---------------------------------------------------------------------------
# TRACEBACK ENABLED BATCH WORKER
# ---------------------------------------------------------------------------
def process_batch_worker(urls: List[str]):
    for url in urls:
        print(f"[Batch Worker] Processing: {url}")
        try:
            ingest_res = fetch_and_clean_url(url)
            if not ingest_res.get("success"):
                print(f"[Batch Worker] Failed to fetch {url}")
                continue
                
            raw_text = ingest_res.get("text", "")
            record = process_raw_product_text(
                raw_text=raw_text, input_mode="url", source_type=ingest_res["source_type"],
                source_origin=ingest_res["source_origin"], fetch_success=True,
                http_status=ingest_res["http_status"], page_title=ingest_res["title"]
            )
            record = validate_and_enrich_record(record, raw_text)
            record = enrich_missing_fields(record, raw_text)
            save_product(record)
            print(f"[Batch Worker] ✅ Saved product from {url}")
            
        except Exception as e:
            print(f"\n❌ [CRITICAL ERROR ON {url}]")
            traceback.print_exc()
            print("-" * 50)

@app.post("/api/enrich-batch")
async def enrich_product_batch(payload: BatchEnrichRequest, background_tasks: BackgroundTasks):
    if not payload.urls:
        raise HTTPException(status_code=400, detail="URL list cannot be empty.")
    background_tasks.add_task(process_batch_worker, payload.urls)
    return {"status": "queued", "message": f"Queued {len(payload.urls)} URLs"}

@app.post("/api/enrich", response_model=ProductIntelligenceRecord)
def enrich_product(payload: EnrichRequest):
    raw_text = payload.raw_text or ""
    source_type, source_origin, input_mode = "manual_input", "Manual Text Input", payload.input_mode or "manual"
    fetch_success, http_status, error_msg, page_title = True, 200, None, ""

    if input_mode == "url" and payload.url:
        ingest_res = fetch_and_clean_url(payload.url)
        source_type, source_origin = ingest_res["source_type"], ingest_res["source_origin"]
        fetch_success, http_status = ingest_res["success"], ingest_res["http_status"]
        error_msg, page_title, raw_text = ingest_res.get("error"), ingest_res["title"], ingest_res["text"]

    record = process_raw_product_text(
        raw_text=raw_text, product_name_hint=payload.product_name_hint, category_hint=payload.category_hint,
        input_mode=input_mode, source_type=source_type, source_origin=source_origin, fetch_success=fetch_success,
        http_status=http_status, error_message=error_msg, page_title=page_title
    )

    enriched_record = validate_and_enrich_record(record, raw_text)
    enriched_record = enrich_missing_fields(enriched_record, raw_text)
    save_product(enriched_record)
    return enriched_record

@app.post("/api/enrich-file", response_model=ProductIntelligenceRecord)
async def enrich_product_from_pdf(
    file: UploadFile = File(...),
    product_name_hint: Optional[str] = Form(None),
    category_hint: Optional[str] = Form(None)
):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF document files (.pdf) are supported.")

    file_bytes = await file.read()
    if len(file_bytes) == 0:
        raise HTTPException(status_code=400, detail="Uploaded file is empty (0 bytes).")

    pdf_res = extract_text_from_pdf(file_bytes, file.filename)
    record = process_raw_product_text(
        raw_text=pdf_res["text"], product_name_hint=product_name_hint, category_hint=category_hint,
        input_mode="pdf", source_type=pdf_res["source_type"], source_origin=pdf_res["source_origin"],
        fetch_success=pdf_res["success"], http_status=pdf_res["http_status"], error_message=pdf_res["error"], page_title=pdf_res["title"]
    )

    enriched_record = validate_and_enrich_record(record, pdf_res["text"])
    enriched_record = enrich_missing_fields(enriched_record, pdf_res["text"])
    save_product(enriched_record)

    return enriched_record

@app.post("/api/discover-enrich", response_model=DiscoveryResponse)
def discover_enrich_product(payload: DiscoveryRequest):
    result = discover_and_enrich(
        mfg_part_num=payload.mfg_part_num,
        part_desc=payload.part_desc or "",
        manufacturer=payload.manufacturer,
        brand=payload.brand,
    )

    return DiscoveryResponse(
        status=result["status"],
        reason=result["reason"],
        source_url=result["source_url"],
        source_type=result["source_type"],
        record=result["record"],
        discovery_trace=result["trace"],
    )

@app.get("/api/products", response_model=List[ProductIntelligenceRecord])
def get_all_products():
    return list_products()

@app.get("/api/products/{product_id}", response_model=ProductIntelligenceRecord)
def get_product_by_id(product_id: str):
    product = get_product(product_id)
    if not product:
        raise HTTPException(status_code=404, detail=f"Product with ID '{product_id}' not found.")
    return product

@app.post("/api/products/{product_id}/review", response_model=ProductIntelligenceRecord)
def review_field_action(product_id: str, payload: ReviewActionRequest):
    updated = update_field_review(
        product_id=product_id, field_name=payload.field_name, action=payload.action,
        new_value=payload.new_value, new_unit=payload.new_unit
    )
    if not updated: raise HTTPException(status_code=404)
    return updated

@app.get("/api/sample-products")
def get_sample_products():
    sample_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "sample_products.json")
    if not os.path.exists(sample_path): sample_path = "sample_products.json"
    if os.path.exists(sample_path):
        with open(sample_path, "r", encoding="utf-8") as f: return json.load(f)
    return []

@app.get("/api/sample-urls")
def get_sample_urls():
    """Stable demo URLs for the allow_distributors sourcing policy.

    Manufacturer pages remain preferred. The distributor example is permitted
    but should remain visibly flagged as a distributor source.
    """
    return [
        {"title": "Contrinex M12 Proximity Sensor (DW-AD-513-M12)",
         "url": "https://www.contrinex.com/products/basic-extra-distance-series-500-m12-non-embeddable-10-mm",
         "description": "Inductive proximity sensor. Manufacturer source — preferred by the sourcing policy."},
        {"title": "Diablo Sanding Belt (DCB518ASTS06G)",
         "url": "https://diablotools.com/products/DCB518ASTS06G",
         "description": "1/2 in x 18 in sanding belt set. Manufacturer source — preferred by the sourcing policy."},
        {"title": "Indsencon Contrinex Sensor (DW-AS-513-M12)",
         "url": "https://indsencon.com/products/contrinex-dw-as-513-m12-proximity-sensor",
         "description": "Distributor source — permitted but flagged under allow_distributors."}
    ]

@app.get("/api/stats", response_model=StatsResponse)
def get_stats(): return get_analytics_stats()

@app.post("/api/reset")
def reset_database():
    db_file = "product_intelligence.db"
    if os.path.exists(db_file):
        try: os.remove(db_file)
        except Exception: pass
    init_db()
    seed_sample_products_if_empty()
    return {"message": "Database reset and sample dataset re-seeded successfully."}

@app.get("/api/export/csv")
def export_catalog_csv():
    products = list_products()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Product ID", "Product Name", "Category", "Verification Status", 
        "Confidence Score", "Voltage", "Current", "IP Rating", 
        "Connector Type", "Temp Min (C)", "Temp Max (C)", "Dynamic Attributes"
    ])
    for p in products:
        dynamic_specs = " | ".join([f"{k}: {v.value} {v.unit or ''}".strip() for k, v in p.extra_attributes.items() if v.value is not None])
        writer.writerow([
            p.id, p.product_name.value, p.category.value, p.overall_status,
            f"{int(p.overall_confidence * 100)}%", f"{p.voltage_rating.value} {p.voltage_rating.unit or ''}".strip(),
            f"{p.current_rating.value} {p.current_rating.unit or ''}".strip(), p.ip_rating.value,
            p.connector_type.value, p.operating_temperature_min.value, p.operating_temperature_max.value, dynamic_specs
        ])
    output.seek(0)
    return StreamingResponse(iter([output.getvalue()]), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=prism_catalog_export.csv"})

# ---------------------------------------------------------------------------
# UNILOG COMMERCE-COMPLIANCE REPORTING
# ---------------------------------------------------------------------------
# The unilog/ package used to be reachable only from the terminal, which meant
# the half of the work that the brief actually grades was invisible in the demo.
# These endpoints run the catalogue through the bridge and report the metrics the
# guidelines name: char-limit compliance, LOV compliance, item-type resolution,
# dedup rate, brand resolution, and how much the trust gate withheld.

_LOV_CACHE = {"loaded": False, "registry": None, "notes": []}


def _get_lov_registry():
    """Load the LOV workbooks from ./data once, and remember the outcome.

    Returns (registry, notes). A missing workbook is reported, never silently
    treated as an empty-but-valid vocabulary.
    """
    if _LOV_CACHE["loaded"]:
        return _LOV_CACHE["registry"], _LOV_CACHE["notes"]

    from unilog import lov as lov_mod
    registry = lov_mod.LOVRegistry()
    notes = []
    candidates = [
        "Unicat_Lov_v1_0_Updated_With_Remarks.xlsx",
        "FAUCETS_LOV.xlsx",
        "Fittings_LOV.xlsx",
    ]
    for name in candidates:
        path = os.path.join("data", name)
        if os.path.exists(path):
            try:
                registry = lov_mod.load_lov(path, registry)
                notes.append(f"loaded {name}")
            except Exception as exc:
                notes.append(f"failed to load {name}: {exc}")
        else:
            notes.append(f"missing {name}")

    _LOV_CACHE.update({"loaded": True, "registry": registry, "notes": notes})
    return registry, notes


@app.get("/api/unilog/report")
def unilog_compliance_report(limit: int = 100, include_enriched: bool = False):
    """Commerce-readiness report over the stored catalogue.

    `include_enriched=true` lets AI-inferred values into the descriptions. It is
    off by default: an inferred value is not a sourced one, and publishing it
    without saying so is exactly the failure mode the trust layer exists to
    prevent.
    """
    from unilog import attrparse as attr_mod
    from unilog import bridge as bridge_mod
    from unilog import classify as classify_mod
    from unilog import dedup as dedup_mod
    from unilog import descriptions as desc_mod
    from unilog import uom as uom_mod
    from app import sourcing

    products = list_products()[:max(1, limit)]
    registry, lov_notes = _get_lov_registry()
    classifier = classify_mod.ItemTypeClassifier(registry)
    nz = uom_mod.DEFAULT

    per_product = []
    desc_totals = {"built": 0, "compliant": 0}
    trust_totals = {"admitted": 0, "withheld": 0}
    lov_totals = {"checked": 0, "in_vocab": 0, "violations": 0,
                  "corrected": 0, "open_skipped": 0, "rows_validated": 0}
    classifications = []
    dedup_rows = []

    for p in products:
        name = p.product_name.value or ""
        category = p.category.value or ""
        cls = classifier.classify(f"{name} {category}".strip())
        classifications.append(cls)

        result = bridge_mod.build_from_prism(
            p, normalizer=nz,
            item_type=cls.item_type or (category or None),
            include_enriched=include_enriched,
        )

        c = result["compliance"]
        desc_totals["built"] += c["fields_built"]
        desc_totals["compliant"] += c["compliant"]
        tr = result["trust_report"]
        trust_totals["admitted"] += tr["attributes_admitted"]
        trust_totals["withheld"] += tr["attributes_withheld"]

        # LOV check on the attributes that reached the descriptions.
        lov_result = None
        if cls.classpath:
            attrs = {}
            for key, (label, _is_key) in bridge_mod.FIELD_TO_ATTRIBUTE.items():
                fobj = getattr(p, key, None)
                val = getattr(fobj, "value", None) if fobj else None
                if val is not None and str(val).strip():
                    attrs[label] = val
            if attrs:
                vres = registry.validate(cls.classpath, attrs)
                lov_totals["rows_validated"] += 1
                lov_totals["checked"] += vres.checked
                lov_totals["in_vocab"] += vres.in_vocabulary
                lov_totals["violations"] += len(vres.issues)
                lov_totals["corrected"] += len(vres.corrected)
                lov_totals["open_skipped"] += vres.skipped_open
                lov_result = vres.as_dict()

        dedup_rows.append({
            "Mfg_Part_Num": result["record"]["mpn"] or p.id,
            "Part_Manuf": result["record"]["brand"] or "",
            "Part_Desc": name,
        })

        per_product.append({
            "id": p.id,
            "product_name": name,
            "item_type": cls.item_type,
            "classpath": cls.classpath,
            "classification_confidence": round(cls.confidence, 3),
            "classification_needs_review": cls.needs_review,
            "classification_evidence": cls.evidence,
            "descriptions": result["descriptions"],
            "compliance": c,
            "trust_report": tr,
            "lov": lov_result,
        })

    kept, dd_report = dedup_mod.deduplicate(dedup_rows) if dedup_rows else ([], {})

    desc_rate = round(desc_totals["compliant"] / desc_totals["built"] * 100, 1) if desc_totals["built"] else 0.0
    lov_pct = (round(lov_totals["in_vocab"] / lov_totals["checked"] * 100, 1)
               if lov_totals["checked"] else None)
    trust_total = trust_totals["admitted"] + trust_totals["withheld"]
    trust_pct = round(trust_totals["admitted"] / trust_total * 100, 1) if trust_total else 0.0

    return {
        "products_evaluated": len(products),
        "include_enriched": include_enriched,
        "char_limit_compliance": {
            "descriptions_built": desc_totals["built"],
            "compliant": desc_totals["compliant"],
            "compliance_pct": desc_rate,
        },
        "lov_compliance": {
            "registry_loaded": registry.loaded,
            "classpaths": len(registry.by_classpath),
            "compliance_pct": lov_pct,
            "unavailable_reason": (
                None if lov_pct is not None else
                ("No LOV workbook found in ./data — a compliance percentage computed "
                 "against an empty vocabulary would be meaningless, so it is reported "
                 "as unavailable."
                 if not registry.loaded else
                 "LOV loaded, but no product resolved to a classpath present in it.")
            ),
            "loader_notes": lov_notes,
            **lov_totals,
        },
        "classification": classify_mod.ItemTypeClassifier.stats(classifications),
        "classifier_source": classifier.source,
        "trust_gate": {
            "attributes_admitted": trust_totals["admitted"],
            "attributes_withheld": trust_totals["withheld"],
            "publish_rate_pct": trust_pct,
        },
        "dedup": dd_report,
        "sourcing_policy": {
            "active": sourcing.active_policy(),
            "description": sourcing.describe_policy(),
            "approved_domain_list_active": bool(sourcing.load_approved_domains()),
        },
        "products": per_product,
    }


@app.get("/api/unilog/product/{product_id}")
def unilog_product_descriptions(product_id: str, include_enriched: bool = False):
    """The five commerce formats for one product, with the trust audit."""
    from unilog import bridge as bridge_mod
    from unilog import classify as classify_mod
    from unilog import uom as uom_mod

    record = get_product(product_id)
    if not record:
        raise HTTPException(status_code=404, detail="Product not found.")

    registry, _ = _get_lov_registry()
    classifier = classify_mod.ItemTypeClassifier(registry)
    cls = classifier.classify(f"{record.product_name.value or ''} {record.category.value or ''}")
    result = bridge_mod.build_from_prism(
        record, normalizer=uom_mod.DEFAULT,
        item_type=cls.item_type or (record.category.value or None),
        include_enriched=include_enriched,
    )
    result["classification"] = cls.as_dict()
    return result



@app.get("/api/unilog/export/{product_id}")
def export_unilog_delivery(
    product_id: str,
    format: str = "csv",
    manufacturer: Optional[str] = None,
    brand: Optional[str] = None,
    mpn: Optional[str] = None,
    manufacturer_url: Optional[str] = None,
    discovery_source_url: Optional[str] = None,
):
    """Download one already-processed PRISM record in the expected Unilog schema."""
    from app.unilog_export import export_single_record

    record = get_product(product_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"Product with ID '{product_id}' not found.")

    fmt = (format or "csv").strip().lower()
    if fmt not in {"csv", "xlsx"}:
        raise HTTPException(status_code=400, detail="format must be 'csv' or 'xlsx'.")

    result, csv_bytes, xlsx_bytes = export_single_record(
        record,
        manufacturer=manufacturer,
        brand=brand,
        mpn_override=mpn,
        manufacturer_url=manufacturer_url,
        discovery_source_url=discovery_source_url,
    )

    if fmt == "xlsx":
        if xlsx_bytes is None:
            raise HTTPException(
                status_code=500,
                detail="XLSX export is unavailable because openpyxl is not installed."
            )
        payload = xlsx_bytes
        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        filename = f"unilog_export_{product_id}.xlsx"
    else:
        payload = csv_bytes
        media_type = "text/csv; charset=utf-8"
        filename = f"unilog_export_{product_id}.csv"

    return StreamingResponse(
        io.BytesIO(payload),
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-PRISM-Export-Warnings": str(len(result.warnings)),
        },
    )

@app.get("/api/sourcing/policy")
def sourcing_policy_info():
    """What the sourcing gate is currently enforcing, and how to change it."""
    from app import sourcing
    return {
        "active_policy": sourcing.active_policy(),
        "description": sourcing.describe_policy(),
        "available_policies": list(sourcing.POLICIES),
        "approved_domain_list_active": bool(sourcing.load_approved_domains()),
        "approved_domain_file": sourcing.APPROVED_DOMAINS_FILE,
        "set_via": "PRISM_SOURCING_POLICY in .env",
    }


@app.post("/api/sourcing/check")
def sourcing_check(payload: dict):
    """Classify a URL without fetching it — useful for demoing the rule."""
    from app import sourcing
    url = (payload or {}).get("url") or ""
    if not url.strip():
        raise HTTPException(status_code=400, detail="A 'url' field is required.")
    return sourcing.evaluate_url(url).as_dict()


# ---------------------------------------------------------------------------
# FRONTEND UI MOUNTING
# ---------------------------------------------------------------------------
static_dir = os.path.join(os.getcwd(), "static")

if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/")
    def serve_frontend():
        return FileResponse(os.path.join(static_dir, "index.html"))
else:
    @app.get("/")
    def missing_frontend():
        return {
            "detail": f"Backend is running, but 'static' folder was not found at: {static_dir}",
            "fix": "Ensure you are running the server from the root 'claude' directory where the 'static' folder lives."
        }

if __name__ == "__main__":
    print("Starting Product Intelligence Application on http://127.0.0.1:8000 ...")
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=True)
