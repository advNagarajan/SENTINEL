"""Pydantic request/response models for the Navigator API."""
from pydantic import BaseModel, Field
from typing import Any, Optional


class TransitionEvent(BaseModel):
    """Payload emitted by L4 harness after a successful state-changing action."""
    # Source screen
    src_screen_hash: str = Field(..., description="SHA-256 hash of the source screen grid")
    src_structural_hash: str = Field(..., description="Hash of title + field structure (fuzzy key)")
    src_title: Optional[str] = Field(None, description="Screen title at source")
    src_field_ids: list[str] = Field(default_factory=list, description="Field IDs present on source screen")

    # Target screen
    tgt_screen_hash: str = Field(..., description="SHA-256 hash of the target screen grid")
    tgt_structural_hash: str = Field(..., description="Hash of title + field structure (fuzzy key)")
    tgt_title: Optional[str] = Field(None, description="Screen title at target")
    tgt_field_ids: list[str] = Field(default_factory=list, description="Field IDs present on target screen")

    # Action details
    tool_name: str = Field(..., description="L3 tool name (e.g. set_field_and_submit, trigger_action)")
    field_id: Optional[str] = Field(None, description="Target field ID if applicable")
    action_id: Optional[str] = Field(None, description="Action key (ENTER, PF3, CLEAR, etc.)")
    value: Optional[str] = Field(None, description="Full value typed into the field")
    action_signature: str = Field(..., description="Canonical action signature for edge identity")

    # Metadata
    runtime_id: str = Field(..., description="Session runtime identifier")
    target_name: str = Field(default="unknown", description="Target system name from config")
    latency_ms: float = Field(default=0.0, description="Action execution latency in milliseconds")


class TransitionEdge(BaseModel):
    """A single outgoing transition from a screen node."""
    target_screen_hash: str
    target_structural_hash: str
    target_title: Optional[str] = None
    action_signature: str
    tool_name: str
    field_id: Optional[str] = None
    action_id: Optional[str] = None
    traversal_count: int = 1
    avg_latency_ms: float = 0.0
    values_seen: list[str] = Field(default_factory=list)
    probability: float = Field(0.0, description="Probability based on traversal count relative to total outgoing")


class PredictionResponse(BaseModel):
    """Response from the prediction endpoint."""
    screen_hash: str
    title: Optional[str] = None
    total_outgoing: int = 0
    transitions: list[TransitionEdge] = Field(default_factory=list)


class NeighborNode(BaseModel):
    """A node in the neighbor subgraph."""
    screen_hash: str
    structural_hash: str
    title: Optional[str] = None
    visit_count: int = 0
    depth: int = 0


class NeighborEdge(BaseModel):
    """An edge in the neighbor subgraph."""
    source_hash: str
    target_hash: str
    action_signature: str
    traversal_count: int = 1


class NeighborResponse(BaseModel):
    """N-hop subgraph around a screen."""
    center_hash: str
    depth: int
    nodes: list[NeighborNode] = Field(default_factory=list)
    edges: list[NeighborEdge] = Field(default_factory=list)


class PathStep(BaseModel):
    """A single step in a path between two screens."""
    screen_hash: str
    title: Optional[str] = None
    action_to_next: Optional[str] = None


class PathResponse(BaseModel):
    """Shortest path between two screens."""
    from_hash: str
    to_hash: str
    found: bool = False
    path_length: int = 0
    steps: list[PathStep] = Field(default_factory=list)


class GraphStatsResponse(BaseModel):
    """Global graph statistics."""
    total_screens: int = 0
    total_transitions: int = 0
    total_traversals: int = 0
    most_visited_screens: list[dict] = Field(default_factory=list)
    most_traversed_edges: list[dict] = Field(default_factory=list)


class ScreenSearchResult(BaseModel):
    """A discovered screen matching a search query."""
    screen_hash: str
    structural_hash: str
    title: Optional[str] = None
    visit_count: int = 0
    field_ids: list[str] = Field(default_factory=list)


class ScreenSearchResponse(BaseModel):
    """Result of searching screens in the navigation graph."""
    query: Optional[str] = None
    total_found: int = 0
    screens: list[ScreenSearchResult] = Field(default_factory=list)


class CypherQueryRequest(BaseModel):
    """Request to execute a read-only Cypher query."""
    query: str = Field(..., description="Read-only Cypher query (MATCH / RETURN)")
    parameters: Optional[dict[str, Any]] = Field(default=None, description="Optional query parameters")


class CypherQueryResponse(BaseModel):
    """Response from executing a Cypher query."""
    columns: list[str] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    count: int = 0

