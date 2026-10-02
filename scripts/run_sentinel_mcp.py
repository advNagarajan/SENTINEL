"""Launch the MCP server with the virtual environment created for this checkout."""
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _python_in(environment: Path) -> Path | None:
    executable = "python.exe" if os.name == "nt" else "python"
    scripts_dir = "Scripts" if os.name == "nt" else "bin"
    candidate = environment / scripts_dir / executable
    return candidate if candidate.is_file() else None


def find_project_python() -> Path:
    explicit_python = os.environ.get("SENTINEL_PYTHON")
    if explicit_python:
        candidate = Path(explicit_python).expanduser()
        if candidate.is_file():
            return candidate.resolve()
        raise FileNotFoundError(f"SENTINEL_PYTHON does not point to a file: {candidate}")

    if sys.prefix != sys.base_prefix and _inside(Path(sys.prefix), PROJECT_ROOT):
        return Path(sys.executable).resolve()

    environments = [
        candidate
        for child in PROJECT_ROOT.iterdir()
        if child.is_dir() and (child / "pyvenv.cfg").is_file()
        if (candidate := _python_in(child)) is not None
    ]
    if len(environments) == 1:
        return environments[0].resolve()
    if len(environments) > 1:
        raise RuntimeError(
            "Multiple project virtual environments were found. Activate the intended one "
            "or set SENTINEL_PYTHON to its interpreter."
        )
    raise FileNotFoundError(
        "No Python virtual environment was found in the SENTINEL project root. "
        "Create one and install the project with 'python -m pip install -e .'."
    )


def main() -> int:
    python = find_project_python()
    return subprocess.call(
        [str(python), "-m", "layer4.mcp_server"],
        cwd=PROJECT_ROOT,
    )


if __name__ == "__main__":
    raise SystemExit(main())