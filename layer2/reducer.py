"""Stage 2c: State Reducer for producing canonical RuntimeState and computing ScreenDeltas."""
import hashlib
import time
from typing import Optional
import structlog

logger = structlog.get_logger(__name__)


from layer2.base import StateReducer
from layer2.builder import ScreenObjectBuilder
from layer2.parser import TN3270StreamParser
from schemas.pipeline import Decoded3270Frame, ScreenObjectModel, TransportFrame
from schemas.state import FieldEntry, RuntimeState, ScreenDelta, ScreenType, StabilityReport


class TN3270StateReducer(StateReducer):
    """Full implementation of Layer 2 State Reducer pipeline."""

    def __init__(self, rows: int = 24, cols: int = 80):
        self.rows = rows
        self.cols = cols
        self.parser = TN3270StreamParser()
        self.builder = ScreenObjectBuilder(rows=rows, cols=cols)

    def parse_frame(self, frame: TransportFrame) -> Decoded3270Frame:
        return self.parser.parse_frame(frame)

    def build_object_model(self, decoded: Decoded3270Frame) -> ScreenObjectModel:
        return self.builder.build_object_model(decoded)

    def reduce_state(
        self,
        som: ScreenObjectModel,
        runtime_id: str,
        generation: int,
        previous_state: Optional[RuntimeState] = None,
    ) -> tuple[RuntimeState, Optional[ScreenDelta]]:
        # 1. Build raw 80x24 text grid lines
        raw_grid: list[str] = ["".join(row_chars) for row_chars in som.grid_matrix]

        # 2. Compute SHA-256 screen hash for fast screen equality/caching
        grid_blob = "\n".join(raw_grid).encode("utf-8")
        screen_hash = hashlib.sha256(grid_blob).hexdigest()

        # 3. Build canonical FieldEntry dict
        fields_dict: dict[str, FieldEntry] = {}
        for idx, f in enumerate(som.fields):
            key_name = f.label if f.label else f"field_{f.start_row}_{f.start_col}"
            # Ensure unique key names
            if key_name in fields_dict:
                key_name = f"{key_name}_{idx}"

            fields_dict[key_name] = FieldEntry(
                label=key_name,
                value=f.value,
                row=f.start_row,
                col=f.start_col,
                length=f.length,
                protected=f.protected,
                hidden=f.hidden,
                numeric=f.numeric,
            )

        # 4. Infer screen title (first non-empty protected text line) and status line (bottom line)
        title: Optional[str] = raw_grid[0].strip() if raw_grid and raw_grid[0].strip() else None
        status_line: Optional[str] = raw_grid[-1].strip() if raw_grid and raw_grid[-1].strip() else None

        # 5. Default initial stability report (overwritten by Stability Engine)
        stability_report = StabilityReport(
            is_stable=(som.oia_status == "READY"),
            method="oia_byte",
            confidence=1.0 if (som.oia_status == "READY") else 0.5,
            delta_value=0.0,
            iterations=1,
            details={"oia_status": som.oia_status},
        )

        state = RuntimeState(
            runtime_id=runtime_id,
            screen_type=ScreenType.TEXT_GRID,
            is_stable=(som.oia_status == "READY"),
            confidence_score=1.0 if (som.oia_status == "READY") else 0.5,
            raw_grid=raw_grid,
            title=title,
            fields=fields_dict,
            status_line=status_line,
            stability_report=stability_report,
            screen_hash=screen_hash,
            generation=generation,
            timestamp=som.timestamp,
            cursor={"row": som.cursor[0], "col": som.cursor[1]},
            screen_size={"rows": self.rows, "cols": self.cols},
            text_grid=raw_grid,
            metadata={"oia_status": som.oia_status},
        )

        logger.info(
            "l2_state_reduced",
            layer="layer2",
            runtime_id=runtime_id,
            generation=generation,
            screen_hash=screen_hash,
            fields_count=len(fields_dict),
            title=title,
        )


        # 6. Compute ScreenDelta if previous_state exists
        delta: Optional[ScreenDelta] = None
        if previous_state is not None:
            changed_fields: dict[str, dict[str, str]] = {}
            for name, field_entry in fields_dict.items():
                prev_field = previous_state.fields.get(name)
                if prev_field and prev_field.value != field_entry.value:
                    changed_fields[name] = {"old": prev_field.value, "new": field_entry.value}
                elif not prev_field:
                    changed_fields[name] = {"old": "", "new": field_entry.value}

            cursor_moved = (
                previous_state.cursor["row"] != state.cursor["row"]
                or previous_state.cursor["col"] != state.cursor["col"]
            )
            text_changed = (previous_state.screen_hash != state.screen_hash)

            delta = ScreenDelta(
                generation_from=previous_state.generation,
                generation_to=state.generation,
                screen_hash_from=previous_state.screen_hash,
                screen_hash_to=state.screen_hash,
                changed_fields=changed_fields,
                cursor_moved=cursor_moved,
                cursor_from=(previous_state.cursor["row"], previous_state.cursor["col"]),
                cursor_to=(state.cursor["row"], state.cursor["col"]),
                text_changed=text_changed,
                timestamp=time.time(),
            )

        return state, delta
