from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


ProjectMapRunStatus = Literal[
    "queued",
    "scanning",
    "extracting",
    "generating",
    "validating",
    "persisting",
    "completed",
    "failed",
    "cancelled",
    "interrupted",
    "superseded",
]


class ProjectMapGenerateRequest(BaseModel):
    model: Optional[str] = None
    effort: Optional[str] = None
    preferred_language: Literal["zh", "en"] = "zh"


class ProjectMapImpactRequest(BaseModel):
    paths: List[str] = Field(default_factory=list, min_length=1, max_length=50)
    expected_revision: Optional[int] = Field(default=None, ge=1)
    max_depth: int = Field(default=2, ge=1, le=4)
    max_results: int = Field(default=120, ge=1, le=200)
    include_low_confidence: bool = False


class ProjectMapContextPackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_ids: List[str] = Field(min_length=1, max_length=30)
    expected_revision: int = Field(ge=1)
    ttl_seconds: int = Field(default=600, ge=60, le=3600)


class ProjectMapEvidence(BaseModel):
    id: str
    path: str
    file_hash: str
    start_line: int = 1
    end_line: int = 1
    symbol_key: str = ""
    kind: str = "file"
    label: str
    excerpt: str = ""
    snippet_hash: str = ""


class ProjectMapNode(BaseModel):
    id: str
    layer: Literal["deterministic", "semantic"]
    kind: str
    title: str
    summary: str = ""
    roles: List[str] = Field(default_factory=list)
    evidence_ids: List[str] = Field(default_factory=list)
    sources: List[Dict[str, Any]] = Field(default_factory=list)
    confidence: Literal["high", "medium", "low", "unknown"] = "unknown"
    stale: bool = False
    stale_reasons: List[str] = Field(default_factory=list)


class ProjectMapRelation(BaseModel):
    id: str
    source_id: str
    target_id: str
    type: str
    provenance: Literal["parser", "llm_inferred"]
    label: str = ""
    evidence_ids: List[str] = Field(default_factory=list)
    confidence: Literal["high", "medium", "low", "unknown"] = "unknown"
    stale: bool = False


class ProjectMapDataset(BaseModel):
    manifest: Dict[str, Any]
    profile: Dict[str, Any]
    files: List[Dict[str, Any]] = Field(default_factory=list)
    evidence: List[ProjectMapEvidence] = Field(default_factory=list)
    nodes: List[ProjectMapNode] = Field(default_factory=list)
    relations: List[ProjectMapRelation] = Field(default_factory=list)


class ProjectMapRunPayload(BaseModel):
    run_id: str
    storage_key: str
    base_revision: int
    status: ProjectMapRunStatus
    phase: ProjectMapRunStatus
    progress: int = 0
    cancel_requested: bool = False
    error_category: str = ""
    error_message: str = ""
    created_at: float
    updated_at: float
