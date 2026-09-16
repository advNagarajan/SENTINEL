"""Async HTTP emitter for the Navigator microservice.

Used by L4 harness to fire-and-forget transition events to the navigator.
If the navigator is unreachable, errors are logged but never block the pipeline.
"""
import hashlib
import json
import structlog
from typing import Any, Optional

logger = structlog.get_logger(__name__)

# httpx is optional — only imported when the emitter is actually used
try:
    import httpx
    _HTTPX_AVAILABLE = True
except ImportError:
    _HTTPX_AVAILABLE = False


def compute_structural_hash(title: Optional[str], fields: dict[str, Any]) -> str:
    """Compute a fuzzy identity hash from screen title and field structure.

    This ignores volatile content (field values, timestamps) and hashes only
    the structural skeleton: title + sorted field IDs + field metadata (row, col, length, protected).
    Two screens with the same layout but different data will produce the same structural_hash.
    """
    parts = [title or ""]
    for key in sorted(fields.keys()):
        f = fields[key]
        # Include structural properties only, not the value
        if hasattr(f, "row"):
            parts.append(f"{key}:{f.row}:{f.col}:{f.length}:{f.protected}")
        elif isinstance(f, dict):
            parts.append(f"{key}:{f.get('row', 0)}:{f.get('col', 0)}:{f.get('length', 0)}:{f.get('protected', False)}")
        else:
            parts.append(key)

    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_action_signature(tool_name: str, field_id: Optional[str], action_id: Optional[str]) -> str:
    """Build a canonical action signature string for edge identity.

    Examples:
        set_field_and_submit:fld_userid:ENTER
        trigger_action::PF3
        fill_form_and_submit:multi:ENTER
    """
    return f"{tool_name}:{field_id or ''}:{action_id or ''}"


class NavigatorEmitter:
    """Fire-and-forget async HTTP client for emitting transitions to the Navigator service.

    State Capture Strategy:
    -----------------------
    Before an action executes, L4 calls `capture_pre_state()` to snapshot the current
    screen identity (hash, title, fields). After the action succeeds and the screen
    transitions, L4 calls `emit_transition()` with the new state. The emitter then
    POSTs the (old → new) transition to the navigator service.

    If the navigator is unreachable, the error is logged and the pipeline continues
    unaffected. The captured pre-state is cleared after emission (successful or not).
    """

    def __init__(self, base_url: str = "http://localhost:8100", target_name: str = "unknown") -> None:
        self.base_url = base_url.rstrip("/")
        self.target_name = target_name
        self._pre_state: Optional[dict[str, Any]] = None
        self._client: Optional[Any] = None

        if not _HTTPX_AVAILABLE:
            logger.warning("navigator_emitter_disabled", reason="httpx not installed")

    async def _get_client(self) -> Any:
        """Lazy-initialize the httpx async client."""
        if not _HTTPX_AVAILABLE:
            return None
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=5.0)
        return self._client

    def capture_pre_state(self, state: Any) -> None:
        """Snapshot the current screen state before an action is executed.

        Called by L4 harness immediately before dispatching an action tool.
        The snapshot is held in memory until emit_transition() consumes it.
        """
        self._pre_state = {
            "screen_hash": state.screen_hash,
            "structural_hash": compute_structural_hash(state.title, state.fields),
            "title": state.title,
            "field_ids": [f.field_id for f in state.fields.values()],
        }

    async def emit_transition(
        self,
        new_state: Any,
        tool_name: str,
        arguments: dict[str, Any],
        runtime_id: str,
        latency_ms: float,
    ) -> None:
        """Emit a transition event (pre_state → new_state) to the Navigator service.

        Fire-and-forget: errors are logged but never raised to the caller.
        The captured pre-state is always cleared after this call.
        """
        if not _HTTPX_AVAILABLE or self._pre_state is None:
            return

        pre = self._pre_state
        self._pre_state = None  # Always clear, regardless of success

        # Don't emit self-transitions (screen didn't change)
        if pre["screen_hash"] == new_state.screen_hash:
            logger.debug("navigator_skip_self_transition", hash=pre["screen_hash"][:12])
            return

        field_id = arguments.get("field_id")
        action_id = arguments.get("action_id") or arguments.get("action", "ENTER")
        value = arguments.get("value")

        # For multi-field fills, combine field info
        if "fields" in arguments and arguments["fields"]:
            field_id = "multi"
            value = json.dumps(arguments["fields"])

        payload = {
            "src_screen_hash": pre["screen_hash"],
            "src_structural_hash": pre["structural_hash"],
            "src_title": pre["title"],
            "src_field_ids": pre["field_ids"],
            "tgt_screen_hash": new_state.screen_hash,
            "tgt_structural_hash": compute_structural_hash(new_state.title, new_state.fields),
            "tgt_title": new_state.title,
            "tgt_field_ids": [f.field_id for f in new_state.fields.values()],
            "tool_name": tool_name,
            "field_id": field_id,
            "action_id": action_id,
            "value": value,
            "action_signature": build_action_signature(tool_name, field_id, action_id),
            "runtime_id": runtime_id,
            "target_name": self.target_name,
            "latency_ms": latency_ms,
        }

        try:
            client = await self._get_client()
            if client is None:
                return
            response = await client.post(f"{self.base_url}/transitions", json=payload)
            if response.status_code == 201:
                logger.info(
                    "navigator_transition_emitted",
                    src=pre["screen_hash"][:12],
                    tgt=new_state.screen_hash[:12],
                    action=payload["action_signature"],
                )
            else:
                logger.warning(
                    "navigator_emit_non_201",
                    status=response.status_code,
                    body=response.text[:200],
                )
        except Exception as e:
            logger.warning("navigator_emit_failed", error=str(e))

    async def get_predictions(self, screen_hash: str) -> Optional[dict[str, Any]]:
        """Query the navigator for predictions from a given screen hash.

        Returns the prediction response dict, or None if the service is unreachable.
        """
        if not _HTTPX_AVAILABLE:
            return None

        try:
            client = await self._get_client()
            if client is None:
                return None
            response = await client.get(f"{self.base_url}/predict/{screen_hash}")
            if response.status_code == 200:
                return response.json()
            else:
                logger.warning("navigator_predict_non_200", status=response.status_code)
                return None
        except Exception as e:
            logger.warning("navigator_predict_failed", error=str(e))
            return None

    async def get_neighbors(self, screen_hash: str, depth: int = 1) -> Optional[dict[str, Any]]:
        """Query N-hop neighborhood around a screen node."""
        if not _HTTPX_AVAILABLE:
            return None

        try:
            client = await self._get_client()
            if client is None:
                return None
            response = await client.get(f"{self.base_url}/graph/neighbors/{screen_hash}", params={"depth": depth})
            if response.status_code == 200:
                return response.json()
            else:
                logger.warning("navigator_neighbors_non_200", status=response.status_code)
                return None
        except Exception as e:
            logger.warning("navigator_neighbors_failed", error=str(e))
            return None

    async def find_path(self, from_hash: str, to_hash: str) -> Optional[dict[str, Any]]:
        """Find shortest path between two screen nodes."""
        if not _HTTPX_AVAILABLE:
            return None

        try:
            client = await self._get_client()
            if client is None:
                return None
            response = await client.get(
                f"{self.base_url}/graph/paths",
                params={"from_hash": from_hash, "to_hash": to_hash},
            )
            if response.status_code == 200:
                return response.json()
            else:
                logger.warning("navigator_path_non_200", status=response.status_code)
                return None
        except Exception as e:
            logger.warning("navigator_path_failed", error=str(e))
            return None

    async def get_stats(self) -> Optional[dict[str, Any]]:
        """Return global graph statistics."""
        if not _HTTPX_AVAILABLE:
            return None

        try:
            client = await self._get_client()
            if client is None:
                return None
            response = await client.get(f"{self.base_url}/graph/stats")
            if response.status_code == 200:
                return response.json()
            else:
                logger.warning("navigator_stats_non_200", status=response.status_code)
                return None
        except Exception as e:
            logger.warning("navigator_stats_failed", error=str(e))
            return None

    async def search_screens(self, query: Optional[str] = None, limit: int = 10) -> Optional[dict[str, Any]]:
        """Search screens in the navigation graph by keyword, hash, or field names."""
        if not _HTTPX_AVAILABLE:
            return None

        try:
            client = await self._get_client()
            if client is None:
                return None
            params = {"limit": limit}
            if query:
                params["query"] = query
            response = await client.get(f"{self.base_url}/graph/screens", params=params)
            if response.status_code == 200:
                return response.json()
            else:
                logger.warning("navigator_search_non_200", status=response.status_code)
                return None
        except Exception as e:
            logger.warning("navigator_search_failed", error=str(e))
            return None

    async def query_cypher(self, query: str, parameters: Optional[dict[str, Any]] = None) -> Optional[dict[str, Any]]:
        """Execute a read-only Cypher query against the navigation graph."""
        if not _HTTPX_AVAILABLE:
            return None

        try:
            client = await self._get_client()
            if client is None:
                return None
            payload = {"query": query, "parameters": parameters}
            response = await client.post(f"{self.base_url}/graph/query", json=payload)
            if response.status_code == 200:
                return response.json()
            else:
                logger.warning("navigator_cypher_non_200", status=response.status_code, body=response.text[:200])
                return {"error": response.json().get("detail", response.text)}
        except Exception as e:
            logger.warning("navigator_cypher_failed", error=str(e))
            return None

    async def close(self) -> None:
        """Close the httpx client."""
        if self._client:
            await self._client.aclose()
            self._client = None

