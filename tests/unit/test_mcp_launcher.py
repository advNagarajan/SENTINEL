"""Tests for portable environment and project-root discovery in the launcher."""
import importlib.util
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LAUNCHER_PATH = PROJECT_ROOT / "scripts" / "run_sentinel_mcp.py"
SPEC = importlib.util.spec_from_file_location("sentinel_mcp_launcher", LAUNCHER_PATH)
assert SPEC and SPEC.loader
LAUNCHER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LAUNCHER)


def test_launcher_resolves_project_root_from_its_own_location() -> None:
    assert LAUNCHER.PROJECT_ROOT == PROJECT_ROOT


def test_launcher_finds_current_project_virtual_environment() -> None:
    expected = Path(sys.executable).resolve()
    if Path(sys.prefix).resolve() == Path(sys.base_prefix).resolve():
        expected = LAUNCHER._python_in(PROJECT_ROOT / ".venv")
    assert LAUNCHER.find_project_python() == expected


def test_launcher_discovers_venv_without_a_fixed_directory_name(monkeypatch, tmp_path) -> None:
    environment = tmp_path / "team-python"
    interpreter = environment / "Scripts" / "python.exe"
    interpreter.parent.mkdir(parents=True)
    interpreter.touch()
    (environment / "pyvenv.cfg").touch()

    monkeypatch.setattr(LAUNCHER, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(sys, "prefix", sys.base_prefix)
    monkeypatch.delenv("SENTINEL_PYTHON", raising=False)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)

    assert LAUNCHER.find_project_python() == interpreter.resolve()


def test_launcher_prefers_project_venv_over_external_active_venv(monkeypatch, tmp_path) -> None:
    project_environment = tmp_path / "project-env"
    project_interpreter = project_environment / "Scripts" / "python.exe"
    project_interpreter.parent.mkdir(parents=True)
    project_interpreter.touch()
    (project_environment / "pyvenv.cfg").touch()

    monkeypatch.setattr(LAUNCHER, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(sys, "prefix", sys.base_prefix)
    monkeypatch.setenv("VIRTUAL_ENV", sys.base_prefix)
    monkeypatch.delenv("SENTINEL_PYTHON", raising=False)

    assert LAUNCHER.find_project_python() == project_interpreter.resolve()