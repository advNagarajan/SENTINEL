"""Stage 2b: Screen Object Builder for constructing ScreenObjectModel from decoded frames."""
from typing import Any
import structlog
from schemas.pipeline import Decoded3270Frame, Field3270, ScreenObjectModel

logger = structlog.get_logger(__name__)


def ebcdic_to_ascii(byte_val: int) -> str:
    """Decode single EBCDIC byte using cp037 codec with fallback to space."""
    try:
        char = bytes([byte_val]).decode("cp037")
        return char if char.isprintable() else " "
    except Exception:
        return " "


class ScreenObjectBuilder:
    """Builds semantic ScreenObjectModel (80x24 text grid, field objects, cursor) from Decoded3270Frame."""

    def __init__(self, rows: int = 24, cols: int = 80) -> None:
        self.rows = rows
        self.cols = cols

    def build_object_model(self, decoded: Decoded3270Frame) -> ScreenObjectModel:
        total_cells = self.rows * self.cols
        buf_chars: list[int] = [0x00] * total_cells
        attr_positions: dict[int, int] = {}

        raw_ebcdic = decoded.raw_text_ebcdic
        pos = 0
        buf_ptr = 0
        cursor_addr = decoded.cursor_address

        while pos < len(raw_ebcdic):
            b = raw_ebcdic[pos]

            if b == 0x11 and pos + 2 < len(raw_ebcdic):  # ORDER_SBA
                b1, b2 = raw_ebcdic[pos + 1], raw_ebcdic[pos + 2]
                buf_ptr = (((b1 & 0x3F) << 6) | (b2 & 0x3F)) % total_cells
                pos += 3
            elif b == 0x13:  # ORDER_IC
                cursor_addr = buf_ptr
                pos += 1
            elif b == 0x3C and pos + 3 < len(raw_ebcdic):  # ORDER_RA
                b1, b2, ch = raw_ebcdic[pos + 1], raw_ebcdic[pos + 2], raw_ebcdic[pos + 3]
                stop_addr = (((b1 & 0x3F) << 6) | (b2 & 0x3F)) % total_cells
                pos += 4
                while buf_ptr != stop_addr:
                    buf_chars[buf_ptr] = ch
                    buf_ptr = (buf_ptr + 1) % total_cells
            elif b == 0x12 and pos + 2 < len(raw_ebcdic):  # ORDER_EUA
                b1, b2 = raw_ebcdic[pos + 1], raw_ebcdic[pos + 2]
                stop_addr = (((b1 & 0x3F) << 6) | (b2 & 0x3F)) % total_cells
                pos += 3
                buf_ptr = stop_addr
            elif b == 0x1D and pos + 1 < len(raw_ebcdic):  # ORDER_SF
                attr_byte = raw_ebcdic[pos + 1]
                attr_positions[buf_ptr] = attr_byte
                buf_chars[buf_ptr] = 0x00
                buf_ptr = (buf_ptr + 1) % total_cells
                pos += 2
            elif b == 0x29 and pos + 1 < len(raw_ebcdic):  # ORDER_SFE
                count = raw_ebcdic[pos + 1]
                attr_byte = 0x00
                idx = pos + 2
                for _ in range(count):
                    if idx + 1 < len(raw_ebcdic):
                        if raw_ebcdic[idx] in (0xC0, 0x00):
                            attr_byte = raw_ebcdic[idx + 1]
                        idx += 2
                attr_positions[buf_ptr] = attr_byte
                buf_chars[buf_ptr] = 0x00
                buf_ptr = (buf_ptr + 1) % total_cells
                pos = idx
            elif b == 0x28 and pos + 2 < len(raw_ebcdic):  # ORDER_SA
                pos += 3
            elif b == 0x05:  # ORDER_PT
                pos += 1
            else:
                buf_chars[buf_ptr] = b
                buf_ptr = (buf_ptr + 1) % total_cells
                pos += 1

        # Reconstruct 24x80 text grid
        grid: list[list[str]] = [
            [ebcdic_to_ascii(buf_chars[r * self.cols + c]) for c in range(self.cols)]
            for r in range(self.rows)
        ]

        # Build attribute matrix & field list
        attr_grid: list[list[dict[str, Any]]] = [
            [{"protected": True, "numeric": False, "hidden": False, "intense": False} for _ in range(self.cols)]
            for _ in range(self.rows)
        ]
        fields: list[Field3270] = []

        sorted_attrs = sorted(attr_positions.keys())
        if not sorted_attrs:
            # Unformatted screen
            val = "".join([ebcdic_to_ascii(x) for x in buf_chars]).strip()
            fields.append(
                Field3270(
                    start_row=0,
                    start_col=0,
                    end_row=self.rows - 1,
                    end_col=self.cols - 1,
                    length=total_cells,
                    protected=False,
                    numeric=False,
                    hidden=False,
                    intense=False,
                    value=val,
                    label="unformatted",
                )
            )
            for r in range(self.rows):
                for c in range(self.cols):
                    attr_grid[r][c] = {"protected": False, "numeric": False, "hidden": False, "intense": False}
        else:
            for idx, a in enumerate(sorted_attrs):
                next_a = sorted_attrs[(idx + 1) % len(sorted_attrs)]
                f_start = (a + 1) % total_cells
                f_end = (next_a - 1) % total_cells
                f_len = (next_a - a - 1) % total_cells
                if f_len == 0:
                    f_len = total_cells - 1

                attr = attr_positions[a]
                is_protected = bool(attr & 0x20)
                is_numeric = bool(attr & 0x10)
                disp = (attr & 0x0C) >> 2
                is_hidden = (disp == 0x03)
                is_intense = (disp == 0x02)

                # Populate attr_grid
                curr = f_start
                chars: list[str] = []
                for _ in range(f_len):
                    cell_r = curr // self.cols
                    cell_c = curr % self.cols
                    attr_grid[cell_r][cell_c] = {
                        "protected": is_protected,
                        "numeric": is_numeric,
                        "hidden": is_hidden,
                        "intense": is_intense,
                    }
                    chars.append(ebcdic_to_ascii(buf_chars[curr]))
                    curr = (curr + 1) % total_cells

                val_str = "".join(chars).strip()
                r = f_start // self.cols
                c = f_start % self.cols
                end_r = f_end // self.cols
                end_c = f_end % self.cols

                # Anchor wrapped/circular field to cursor if cursor is at (0, 0)
                if not is_protected and (f_start > f_end or f_start == 0) and cursor_addr == 0:
                    r = 0
                    c = 0

                fields.append(
                    Field3270(
                        start_row=r,
                        start_col=c,
                        end_row=end_r,
                        end_col=end_c,
                        length=f_len,
                        protected=is_protected,
                        numeric=is_numeric,
                        hidden=is_hidden,
                        intense=is_intense,
                        value=val_str,
                    )
                )

        # Associate field labels (protected text preceding unprotected fields)
        for i, field_obj in enumerate(fields):
            if not field_obj.protected:
                if i > 0 and fields[i - 1].protected and fields[i - 1].value:
                    field_obj.label = fields[i - 1].value.rstrip(":")
                else:
                    r_pre = field_obj.start_row
                    c_pre = field_obj.start_col
                    prefix = "".join(grid[r_pre][:c_pre]).strip()
                    if prefix:
                        field_obj.label = prefix.split()[-1].rstrip(":")
                    else:
                        field_obj.label = f"field_{r_pre}_{c_pre}"

        cursor_row = cursor_addr // self.cols
        cursor_col = cursor_addr % self.cols
        oia_status = "BUSY" if (decoded.oia_byte == b"\xF1") else "READY"

        return ScreenObjectModel(
            grid_matrix=grid,
            attribute_matrix=attr_grid,
            fields=fields,
            cursor=(cursor_row, cursor_col),
            oia_status=oia_status,
            rows=self.rows,
            cols=self.cols,
            timestamp=decoded.timestamp,
        )
