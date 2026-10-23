from pydantic import BaseModel
from typing import Literal, Optional

# LLM-facing: Gemini fills these via response_schema. Flat types only, no URLs, no nested models.
class SearchIntentOut(BaseModel):
    category: Literal["Sponsor", "College Delegation", "Both"]
    city: Optional[str] = None
    industries: list[str]
    audience: Optional[str] = None

# LLM-facing: Gemini fills these via response_schema. Flat types only, no URLs, no nested models.
class BrochureFactsOut(BaseModel):
    event_name: Optional[str] = None
    event_city: Optional[str] = None
    event_theme: Optional[str] = None
    target_audience: Optional[str] = None
    mentioned_orgs: list[str]

# Internal: built by code after Gemini replies. Never sent to Gemini.
class SearchPlan(BaseModel):
    source: Literal["prompt", "pdf", "both"]
    category: Literal["Sponsor", "College Delegation", "Both"]
    city: Optional[str] = None
    industries: list[str] = []
    theme: Optional[str] = None
    queries: list[str] = []
    seed_orgs: list[str] = []

from typing import Literal

class ClassifiedResult(BaseModel):
    result_id: int
    relevant: bool
    org_name: str
    category: Literal["Sponsor", "College Delegation"]
    city: str | None

class ClassifiedBatch(BaseModel):
    results: list[ClassifiedResult]

class OrgCandidate(BaseModel):
    org_name: str
    category: Literal["Sponsor", "College Delegation"]
    city: str | None
    domain: str
    homepage: str
    discovered_via: str
    relevance_verified: bool = True
    classification_status: str = "verified"
    
class NewQueries(BaseModel):
    queries: list[str]
