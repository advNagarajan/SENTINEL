"""Neo4j graph database client for the Navigator microservice.

Handles all Cypher queries: MERGE-based upserts for deduplication,
prediction queries, neighbor traversal, and shortest path computation.
"""
import structlog
from neo4j import AsyncGraphDatabase, AsyncDriver
from typing import Any, Optional

from services.navigator.config import config

logger = structlog.get_logger(__name__)


class GraphClient:
    """Async Neo4j driver wrapper with MERGE-based deduplication."""

    def __init__(
        self,
        uri: str = config.NEO4J_URI,
        user: str = config.NEO4J_USER,
        password: str = config.NEO4J_PASSWORD,
        database: str = config.NEO4J_DATABASE,
    ) -> None:
        self.uri = uri
        self.user = user
        self.password = password
        self.database = database
        self._driver: Optional[AsyncDriver] = None

    async def connect(self) -> None:
        """Initialize the Neo4j async driver and create schema constraints."""
        self._driver = AsyncGraphDatabase.driver(
            self.uri, auth=(self.user, self.password)
        )
        await self._ensure_constraints()
        logger.info("neo4j_connected", uri=self.uri)

    async def close(self) -> None:
        """Close the Neo4j driver."""
        if self._driver:
            await self._driver.close()
            self._driver = None

    async def _ensure_constraints(self) -> None:
        """Create uniqueness constraint on Screen.screen_hash if not exists."""
        query = (
            "CREATE CONSTRAINT screen_unique IF NOT EXISTS "
            "FOR (s:Screen) REQUIRE s.screen_hash IS UNIQUE"
        )
        async with self._driver.session(database=self.database) as session:
            try:
                await session.run(query)
                logger.info("neo4j_constraint_ensured", constraint="screen_unique")
            except Exception as e:
                # Constraint may already exist or edition may not support it
                logger.warning("neo4j_constraint_skipped", error=str(e))

    async def record_transition(
        self,
        src_hash: str,
        src_structural_hash: str,
        src_title: Optional[str],
        src_field_ids: list[str],
        tgt_hash: str,
        tgt_structural_hash: str,
        tgt_title: Optional[str],
        tgt_field_ids: list[str],
        action_signature: str,
        tool_name: str,
        field_id: Optional[str],
        action_id: Optional[str],
        value: Optional[str],
        target_name: str,
        latency_ms: float,
    ) -> dict[str, Any]:
        """Record a state transition using MERGE to deduplicate nodes and edges.

        - If the source/target Screen nodes already exist, updates visit_count and last_seen.
        - If the edge (action_signature) between the same two nodes exists, increments
          traversal_count and appends the value to values_seen (capped).
        - Otherwise, creates new nodes/edges.
        """
        query = """
        MERGE (src:Screen {screen_hash: $src_hash})
        ON CREATE SET
            src.structural_hash = $src_structural_hash,
            src.title = $src_title,
            src.target_name = $target_name,
            src.field_ids = $src_field_ids,
            src.first_seen = datetime(),
            src.last_seen = datetime(),
            src.visit_count = 1
        ON MATCH SET
            src.last_seen = datetime(),
            src.visit_count = src.visit_count + 1,
            src.title = COALESCE($src_title, src.title),
            src.structural_hash = $src_structural_hash,
            src.field_ids = $src_field_ids

        MERGE (tgt:Screen {screen_hash: $tgt_hash})
        ON CREATE SET
            tgt.structural_hash = $tgt_structural_hash,
            tgt.title = $tgt_title,
            tgt.target_name = $target_name,
            tgt.field_ids = $tgt_field_ids,
            tgt.first_seen = datetime(),
            tgt.last_seen = datetime(),
            tgt.visit_count = 1
        ON MATCH SET
            tgt.last_seen = datetime(),
            tgt.visit_count = tgt.visit_count + 1,
            tgt.title = COALESCE($tgt_title, tgt.title),
            tgt.structural_hash = $tgt_structural_hash,
            tgt.field_ids = $tgt_field_ids

        MERGE (src)-[r:TRANSITION {action_signature: $action_signature}]->(tgt)
        ON CREATE SET
            r.tool_name = $tool_name,
            r.field_id = $field_id,
            r.action_id = $action_id,
            r.traversal_count = 1,
            r.avg_latency_ms = $latency_ms,
            r.first_seen = datetime(),
            r.last_seen = datetime(),
            r.values_seen = CASE WHEN $value IS NOT NULL THEN [$value] ELSE [] END
        ON MATCH SET
            r.traversal_count = r.traversal_count + 1,
            r.last_seen = datetime(),
            r.avg_latency_ms = (r.avg_latency_ms * (r.traversal_count - 1) + $latency_ms) / r.traversal_count,
            r.values_seen = CASE
                WHEN $value IS NOT NULL AND NOT $value IN r.values_seen AND size(r.values_seen) < $max_values
                THEN r.values_seen + [$value]
                ELSE r.values_seen
            END

        RETURN
            src.screen_hash AS src_hash,
            tgt.screen_hash AS tgt_hash,
            r.traversal_count AS traversal_count,
            src.visit_count AS src_visits,
            tgt.visit_count AS tgt_visits
        """
        params = {
            "src_hash": src_hash,
            "src_structural_hash": src_structural_hash,
            "src_title": src_title,
            "src_field_ids": src_field_ids,
            "tgt_hash": tgt_hash,
            "tgt_structural_hash": tgt_structural_hash,
            "tgt_title": tgt_title,
            "tgt_field_ids": tgt_field_ids,
            "action_signature": action_signature,
            "tool_name": tool_name,
            "field_id": field_id,
            "action_id": action_id,
            "value": value,
            "target_name": target_name,
            "latency_ms": latency_ms,
            "max_values": config.MAX_VALUES_PER_EDGE,
        }

        async with self._driver.session(database=self.database) as session:
            result = await session.run(query, params)
            record = await result.single()
            logger.info(
                "transition_recorded",
                src=src_hash[:12],
                tgt=tgt_hash[:12],
                action=action_signature,
                traversals=record["traversal_count"] if record else 0,
            )
            return dict(record) if record else {}

    async def get_predictions(self, screen_hash: str) -> dict[str, Any]:
        """Return all outgoing transitions from a screen, ranked by traversal count.

        Each transition includes a probability (traversal_count / total_outgoing).
        """
        query = """
        MATCH (src:Screen {screen_hash: $screen_hash})-[r:TRANSITION]->(tgt:Screen)
        WITH src, r, tgt
        ORDER BY r.traversal_count DESC
        WITH src,
             collect({
                 target_screen_hash: tgt.screen_hash,
                 target_structural_hash: tgt.structural_hash,
                 target_title: tgt.title,
                 action_signature: r.action_signature,
                 tool_name: r.tool_name,
                 field_id: r.field_id,
                 action_id: r.action_id,
                 traversal_count: r.traversal_count,
                 avg_latency_ms: r.avg_latency_ms,
                 values_seen: r.values_seen
             }) AS transitions,
             sum(r.traversal_count) AS total_outgoing
        RETURN
            src.screen_hash AS screen_hash,
            src.title AS title,
            total_outgoing,
            transitions
        """
        async with self._driver.session(database=self.database) as session:
            result = await session.run(query, {"screen_hash": screen_hash})
            record = await result.single()

            if not record:
                return {
                    "screen_hash": screen_hash,
                    "title": None,
                    "total_outgoing": 0,
                    "transitions": [],
                }

            transitions = record["transitions"]
            total = record["total_outgoing"]

            # Calculate probability for each transition
            for t in transitions:
                t["probability"] = round(t["traversal_count"] / total, 4) if total > 0 else 0.0

            return {
                "screen_hash": record["screen_hash"],
                "title": record["title"],
                "total_outgoing": total,
                "transitions": transitions,
            }

    async def get_neighbors(self, screen_hash: str, depth: int = 3) -> dict[str, Any]:
        """Return N-hop subgraph around a screen node."""
        depth = min(depth, 10)  # Safety cap
        query = """
        MATCH path = (start:Screen {screen_hash: $screen_hash})-[:TRANSITION*1..""" + str(depth) + """]->(neighbor:Screen)
        WITH nodes(path) AS ns, relationships(path) AS rs
        UNWIND ns AS n
        WITH DISTINCT n, rs
        UNWIND rs AS r
        WITH
            collect(DISTINCT {
                screen_hash: n.screen_hash,
                structural_hash: n.structural_hash,
                title: n.title,
                visit_count: n.visit_count
            }) AS nodes,
            collect(DISTINCT {
                source_hash: startNode(r).screen_hash,
                target_hash: endNode(r).screen_hash,
                action_signature: r.action_signature,
                traversal_count: r.traversal_count
            }) AS edges
        RETURN nodes, edges
        """
        async with self._driver.session(database=self.database) as session:
            result = await session.run(query, {"screen_hash": screen_hash})
            record = await result.single()

            if not record:
                return {
                    "center_hash": screen_hash,
                    "depth": depth,
                    "nodes": [],
                    "edges": [],
                }

            return {
                "center_hash": screen_hash,
                "depth": depth,
                "nodes": record["nodes"],
                "edges": record["edges"],
            }

    async def find_path(self, from_hash: str, to_hash: str) -> dict[str, Any]:
        """Find shortest path between two screen nodes."""
        query = """
        MATCH (start:Screen {screen_hash: $from_hash}),
              (end:Screen {screen_hash: $to_hash}),
              path = shortestPath((start)-[:TRANSITION*..20]->(end))
        WITH nodes(path) AS ns, relationships(path) AS rs
        RETURN
            [n IN ns | {screen_hash: n.screen_hash, title: n.title}] AS steps,
            [r IN rs | r.action_signature] AS actions,
            length(path) AS path_length
        """
        async with self._driver.session(database=self.database) as session:
            result = await session.run(
                query, {"from_hash": from_hash, "to_hash": to_hash}
            )
            record = await result.single()

            if not record:
                return {
                    "from_hash": from_hash,
                    "to_hash": to_hash,
                    "found": False,
                    "path_length": 0,
                    "steps": [],
                }

            # Interleave steps with actions
            steps_raw = record["steps"]
            actions_raw = record["actions"]
            steps = []
            for i, node in enumerate(steps_raw):
                step = {
                    "screen_hash": node["screen_hash"],
                    "title": node["title"],
                    "action_to_next": actions_raw[i] if i < len(actions_raw) else None,
                }
                steps.append(step)

            return {
                "from_hash": from_hash,
                "to_hash": to_hash,
                "found": True,
                "path_length": record["path_length"],
                "steps": steps,
            }

    async def get_stats(self) -> dict[str, Any]:
        """Return global graph statistics."""
        query = """
        MATCH (s:Screen)
        WITH count(s) AS total_screens
        OPTIONAL MATCH ()-[r:TRANSITION]->()
        WITH total_screens, count(r) AS total_transitions, sum(r.traversal_count) AS total_traversals
        RETURN total_screens, total_transitions, total_traversals
        """
        top_screens_query = """
        MATCH (s:Screen)
        RETURN s.screen_hash AS screen_hash, s.title AS title, s.visit_count AS visit_count
        ORDER BY s.visit_count DESC
        LIMIT 10
        """
        top_edges_query = """
        MATCH (src:Screen)-[r:TRANSITION]->(tgt:Screen)
        RETURN
            src.screen_hash AS source,
            tgt.screen_hash AS target,
            r.action_signature AS action,
            r.traversal_count AS traversals
        ORDER BY r.traversal_count DESC
        LIMIT 10
        """

        async with self._driver.session(database=self.database) as session:
            stats_result = await session.run(query)
            stats = await stats_result.single()

            screens_result = await session.run(top_screens_query)
            top_screens = [dict(r) async for r in screens_result]

            edges_result = await session.run(top_edges_query)
            top_edges = [dict(r) async for r in edges_result]

            return {
                "total_screens": stats["total_screens"] if stats else 0,
                "total_transitions": stats["total_transitions"] if stats else 0,
                "total_traversals": stats["total_traversals"] if stats else 0,
                "most_visited_screens": top_screens,
                "most_traversed_edges": top_edges,
            }

    async def search_screens(self, query: Optional[str] = None, limit: int = 10) -> dict[str, Any]:
        """Search discovered screens by title, hash, structural hash, or field names."""
        limit = min(max(1, limit), 50)
        cypher = """
        MATCH (s:Screen)
        WHERE $query IS NULL OR $query = ""
           OR toLower(COALESCE(s.title, "")) CONTAINS toLower($query)
           OR s.screen_hash STARTS WITH $query
           OR s.structural_hash STARTS WITH $query
           OR any(f IN s.field_ids WHERE toLower(f) CONTAINS toLower($query))
        RETURN s.screen_hash AS screen_hash,
               s.structural_hash AS structural_hash,
               s.title AS title,
               s.visit_count AS visit_count,
               COALESCE(s.field_ids, []) AS field_ids
        ORDER BY s.visit_count DESC
        LIMIT $limit
        """
        async with self._driver.session(database=self.database) as session:
            result = await session.run(cypher, {"query": query, "limit": limit})
            screens = [dict(r) async for r in result]
            return {
                "query": query,
                "total_found": len(screens),
                "screens": screens,
            }

    async def run_read_query(self, query: str, parameters: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Execute a read-only Cypher query with safety validation.

        Rejects mutating operations (CREATE, MERGE, SET, DELETE, REMOVE, DROP, ALTER).
        """
        import re
        cleaned = query.strip()
        forbidden = {"CREATE", "MERGE", "SET", "DELETE", "REMOVE", "DROP", "ALTER"}
        tokens = set(re.findall(r"\b[A-Za-z]+\b", cleaned.upper()))
        intersection = tokens.intersection(forbidden)
        if intersection:
            raise ValueError(
                f"Forbidden Cypher operation(s): {', '.join(sorted(intersection))}. Only read-only queries are permitted."
            )

        first_word = cleaned.split()[0].upper() if cleaned.split() else ""
        if first_word not in {"MATCH", "RETURN", "WITH", "UNWIND"}:
            raise ValueError(
                f"Query must start with MATCH, RETURN, WITH, or UNWIND (got '{first_word}')."
            )

        async with self._driver.session(database=self.database) as session:
            result = await session.run(query, parameters or {})
            raw_keys = result.keys()
            import inspect
            if inspect.isawaitable(raw_keys):
                keys = list(await raw_keys)
            else:
                keys = list(raw_keys)
            rows = []
            count = 0

            async for record in result:
                if count < 100:
                    row_dict = {}
                    for k in keys:
                        val = record[k]
                        if hasattr(val, "_properties"):
                            val = dict(val._properties)
                        elif hasattr(val, "items"):
                            val = dict(val)
                        row_dict[k] = val
                    rows.append(row_dict)
                count += 1
            return {
                "columns": keys,
                "rows": rows,
                "count": count,
            }

    async def health_check(self) -> bool:
        """Verify Neo4j connectivity."""
        try:
            async with self._driver.session(database=self.database) as session:
                result = await session.run("RETURN 1 AS ok")
                record = await result.single()
                return record["ok"] == 1
        except Exception:
            return False

