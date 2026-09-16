"""FastAPI application for the Navigator microservice.

Provides REST endpoints for recording state transitions, querying predictions,
exploring the navigation graph, and computing paths between screens.
"""
import structlog
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from typing import Optional

from services.navigator.config import config
from services.navigator.graph_client import GraphClient
from services.navigator.models import (
    CypherQueryRequest,
    CypherQueryResponse,
    GraphStatsResponse,
    NeighborResponse,
    PathResponse,
    PredictionResponse,
    ScreenSearchResponse,
    TransitionEvent,
)

logger = structlog.get_logger(__name__)

graph_client = GraphClient()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Connect to Neo4j on startup, disconnect on shutdown."""
    await graph_client.connect()
    yield
    await graph_client.close()


app = FastAPI(
    title="SENTINEL Navigator",
    description="Neo4j-backed state-transition graph for mainframe screen navigation",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health():
    """Health check endpoint — verifies Neo4j connectivity."""
    ok = await graph_client.health_check()
    if not ok:
        raise HTTPException(status_code=503, detail="Neo4j unavailable")
    return {"status": "healthy", "neo4j": "connected"}


@app.post("/transitions", status_code=201)
async def record_transition(event: TransitionEvent):
    """Record a screen-to-screen state transition.

    Uses MERGE-based upserts: if the same action leads to the same screen,
    the existing edge's traversal_count is incremented — no duplicate nodes are created.
    """
    try:
        result = await graph_client.record_transition(
            src_hash=event.src_screen_hash,
            src_structural_hash=event.src_structural_hash,
            src_title=event.src_title,
            src_field_ids=event.src_field_ids,
            tgt_hash=event.tgt_screen_hash,
            tgt_structural_hash=event.tgt_structural_hash,
            tgt_title=event.tgt_title,
            tgt_field_ids=event.tgt_field_ids,
            action_signature=event.action_signature,
            tool_name=event.tool_name,
            field_id=event.field_id,
            action_id=event.action_id,
            value=event.value,
            target_name=event.target_name,
            latency_ms=event.latency_ms,
        )
        return {"recorded": True, **result}
    except Exception as e:
        logger.error("transition_record_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/predict/{screen_hash}", response_model=PredictionResponse)
async def predict_navigation(screen_hash: str):
    """Predict reachable screens from the given screen hash.

    Returns all outgoing transitions sorted by traversal count,
    with probability scores based on historical frequency.
    """
    try:
        result = await graph_client.get_predictions(screen_hash)
        return PredictionResponse(**result)
    except Exception as e:
        logger.error("prediction_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/graph/neighbors/{screen_hash}", response_model=NeighborResponse)
async def get_neighbors(screen_hash: str, depth: Optional[int] = None):
    """Return N-hop subgraph around a screen node.

    Useful for visualizing the local navigation topology.
    """
    hop_depth = depth if depth is not None else config.DEFAULT_NEIGHBOR_DEPTH
    try:
        result = await graph_client.get_neighbors(screen_hash, depth=hop_depth)
        return NeighborResponse(**result)
    except Exception as e:
        logger.error("neighbor_query_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/graph/paths", response_model=PathResponse)
async def find_path(from_hash: str, to_hash: str):
    """Find shortest path between two screen nodes.

    Enables the agent to plan multi-step navigation sequences.
    """
    try:
        result = await graph_client.find_path(from_hash, to_hash)
        return PathResponse(**result)
    except Exception as e:
        logger.error("path_query_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/graph/stats", response_model=GraphStatsResponse)
async def get_stats():
    """Return global graph statistics: node count, edge count, most visited screens."""
    try:
        result = await graph_client.get_stats()
        return GraphStatsResponse(**result)
    except Exception as e:
        logger.error("stats_query_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/graph/screens", response_model=ScreenSearchResponse)
async def search_screens(query: Optional[str] = None, limit: int = 10):
    """Search screens in the navigation graph by title, hash, structural hash, or field names."""
    try:
        result = await graph_client.search_screens(query=query, limit=limit)
        return ScreenSearchResponse(**result)
    except Exception as e:
        logger.error("screen_search_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/graph/query", response_model=CypherQueryResponse)
async def execute_query(req: CypherQueryRequest):
    """Execute a read-only Cypher query against the navigation graph.

    Permits arbitrary MATCH / RETURN queries, strictly disallowing mutations.
    """
    try:
        result = await graph_client.run_read_query(query=req.query, parameters=req.parameters)
        return CypherQueryResponse(**result)
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        logger.error("cypher_query_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))



if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=config.HOST, port=config.PORT)
