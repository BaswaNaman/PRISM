from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any

class FetchMetadata(BaseModel):
    fetch_success: bool = Field(default=True, description="Whether raw text fetch succeeded without errors")
    http_status: int = Field(default=200, description="HTTP response status code e.g. 200, 403, 404")
    content_length: int = Field(default=0, description="Length of cleaned text content in characters")
    page_title: str = Field(default="", description="Scraped page title or document title")
    error_message: Optional[str] = Field(default=None, description="Human readable fetch error or warning message")
    preview_snippet: str = Field(default="", description="First 1000 characters of raw scraped text for visual audit preview")

class ExtractedField(BaseModel):
    name: str = Field(description="System key of the field")
    label: str = Field(description="Human readable label for UI")
    value: Optional[Any] = Field(default=None, description="Enriched field value")
    unit: Optional[str] = Field(default=None, description="Normalized unit of measurement e.g. V, A, °C")
    source_snippet: Optional[str] = Field(default=None, description="Exact verbatim text snippet acting as source evidence")
    is_grounded: Optional[bool] = Field(default=None, description="True if source_snippet was located verbatim in the source text; False if the claimed evidence could not be found (potential hallucination); None for derived/inferred fields")
    source_type: str = Field(default="manual_input", description="manual_input | url_ingestion | pdf_upload | ai_inference")
    source_origin: str = Field(default="Manual Text Input", description="Origin description e.g. URL: domain.com or PDF: filename.pdf")
    confidence_score: float = Field(default=0.0, ge=0.0, le=1.0, description="AI confidence rating between 0 and 1")
    reasoning: Optional[str] = Field(default=None, description="AI reasoning explaining why this value was extracted")
    validation_status: str = Field(default="missing", description="verified | flagged_low_confidence | flagged_validation_error | missing | not_applicable | ai_enriched | needs_review (value withheld: evidence found but insufficient to justify a specific catalog value)")
    validation_message: Optional[str] = Field(default=None, description="Human readable explanation of rule validation result")
    is_reviewed: bool = Field(default=False, description="Whether human reviewer has manually acted on this field")

class ProductIntelligenceRecord(BaseModel):
    id: str
    raw_input: str
    input_mode: str = "manual"  # manual | url | pdf
    source_origin: str = "Manual Text Input"
    fetch_metadata: FetchMetadata = Field(default_factory=FetchMetadata)
    product_name: ExtractedField
    category: ExtractedField
    voltage_rating: ExtractedField
    current_rating: ExtractedField
    ip_rating: ExtractedField
    connector_type: ExtractedField
    operating_temperature_min: ExtractedField
    operating_temperature_max: ExtractedField
    material: ExtractedField
    certifications: ExtractedField
    mounting_type: ExtractedField
    
    
    # 10/10 FEATURE: Dynamic extraction escape hatch
    extra_attributes: Dict[str, ExtractedField] = Field(default_factory=dict)
    features: List[str] = Field(default_factory=list)
    
    overall_confidence: float = 0.0
    overall_status: str = "pending_review"  # verified | needs_review | rejected
    total_fields: int = 11
    verified_fields_count: int = 0
    flagged_fields_count: int = 0
    missing_fields_count: int = 0
    enriched_fields_count: int = 0
    created_at: str = ""

class EnrichRequest(BaseModel):
    raw_text: Optional[str] = ""
    input_mode: str = "manual"  # manual | url | pdf
    url: Optional[str] = None
    product_name_hint: Optional[str] = None
    category_hint: Optional[str] = None

class BatchEnrichRequest(BaseModel):
    urls: List[str] = Field(..., description="List of URLs to scrape and enrich in the background")

class ReviewActionRequest(BaseModel):
    product_id: str
    field_name: str
    action: str  # "approve", "reject", "override"
    new_value: Optional[Any] = None
    new_unit: Optional[str] = None

class StatsResponse(BaseModel):
    total_products: int
    verified_products: int
    flagged_products: int
    overall_verification_rate: float
    total_fields_processed: int
    flagged_fields_count: int
    time_saved_hours: float

from typing import Optional, Any, Dict
from pydantic import BaseModel, Field


class DiscoveryRequest(BaseModel):
    mfg_part_num: str = Field(..., min_length=1)
    part_desc: Optional[str] = None
    manufacturer: Optional[str] = None
    brand: Optional[str] = None


class DiscoveryTrace(BaseModel):
    search_query: str = ""
    candidate_urls_considered: list[str] = []
    rejections: list[Dict[str, Any]] = []
    selected_source_url: Optional[str] = None
    mpn_found_on_selected_source: bool = False
    manufacturer_corrobated_on_selected_source: bool = False
    search_grounding_available: bool = True
    search_error: Optional[str] = None


class DiscoveryResponse(BaseModel):
    status: str
    reason: Optional[str] = None
    source_url: Optional[str] = None
    source_type: Optional[str] = None
    record: Optional[ProductIntelligenceRecord] = None
    discovery_trace: DiscoveryTrace
