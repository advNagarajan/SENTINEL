"""Stage 2b: Screen Object Builder for constructing ScreenObjectModel from decoded frames."""
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

    def __init__(self, rows: int = 24, cols: int = 80):
        self.rows = rows
        self.cols = cols

    def build_object_model(self, decoded: Decoded3270Frame) -> ScreenObjectModel:
        grid: list[list[str]] = [[" " for _ in range(self.cols)] for _ in range(self.rows)]
        attr_grid: list[list[dict]] = [[{"protected": True, "numeric": False, "hidden": False, "intense": False} for _ in range(self.cols)] for _ in range(self.rows)]
        fields: list[Field3270] = []

        curr_addr = 0
        raw_ebcdic = decoded.raw_text_ebcdic
        pos = 0

        current_attr = {"protected": True, "numeric": False, "hidden": False, "intense": False}
        field_start_addr = 0
        field_bytes = bytearray()
        field_attr = current_attr.copy()

        while pos < len(raw_ebcdic) and curr_addr < (self.rows * self.cols):
            row = curr_addr // self.cols
            col = curr_addr % self.cols

            b = raw_ebcdic[pos]
            if b == 0x1D and pos + 1 < len(raw_ebcdic):  # ORDER_SF
                # Finalize previous field if any
                if field_bytes:
                    f_start_r = field_start_addr // self.cols
                    f_start_c = field_start_addr % self.cols
                    f_end_r = (curr_addr - 1) // self.cols
                    f_end_c = (curr_addr - 1) % self.cols
                    val_str = "".join([ebcdic_to_ascii(x) for x in field_bytes]).strip()

                    fields.append(
                        Field3270(
                            start_row=f_start_r,
                            start_col=f_start_c,
                            end_row=f_end_r,
                            end_col=f_end_c,
                            length=len(field_bytes),
                            protected=field_attr["protected"],
                            numeric=field_attr["numeric"],
                            hidden=field_attr["hidden"],
                            intense=field_attr["intense"],
                            value=val_str,
                        )
                    )

                # Parse new attribute byte
                attr_byte = raw_ebcdic[pos + 1]
                is_protected = bool(attr_byte & 0x20)
                is_numeric = bool(attr_byte & 0x10)
                disp = (attr_byte & 0x0C) >> 2
                is_hidden = (disp == 0x03)
                is_intense = (disp == 0x02)

                current_attr = {
                    "protected": is_protected,
                    "numeric": is_numeric,
                    "hidden": is_hidden,
                    "intense": is_intense,
                }
                field_attr = current_attr.copy()
                field_start_addr = curr_addr + 1
                field_bytes.clear()

                grid[row][col] = " "
                attr_grid[row][col] = current_attr.copy()
                pos += 2
                curr_addr += 1
            else:
                char_str = ebcdic_to_ascii(b)
                grid[row][col] = char_str
                attr_grid[row][col] = current_attr.copy()
                field_bytes.append(b)
                pos += 1
                curr_addr += 1

        # Final field flush
        if field_bytes and curr_addr <= (self.rows * self.cols):
            f_start_r = field_start_addr // self.cols
            f_start_c = field_start_addr % self.cols
            f_end_r = (curr_addr - 1) // self.cols
            f_end_c = (curr_addr - 1) % self.cols
            val_str = "".join([ebcdic_to_ascii(x) for x in field_bytes]).strip()
            fields.append(
                Field3270(
                    start_row=f_start_r,
                    start_col=f_start_c,
                    end_row=f_end_r,
                    end_col=f_end_c,
                    length=len(field_bytes),
                    protected=field_attr["protected"],
                    numeric=field_attr["numeric"],
                    hidden=field_attr["hidden"],
                    intense=field_attr["intense"],
                    value=val_str,
                )
            )

        # Associate field labels (protected text preceding unprotected fields)
        for i, field_obj in enumerate(fields):
            if not field_obj.protected:
                # Look backwards for immediate protected label
                if i > 0 and fields[i - 1].protected and fields[i - 1].value:
                    field_obj.label = fields[i - 1].value.rstrip(":")

        cursor_row = decoded.cursor_address // self.cols
        cursor_col = decoded.cursor_address % self.cols
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
