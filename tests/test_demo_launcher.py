import importlib.util
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("demo_launcher", ROOT / "scripts" / "demo.py")
launcher = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(launcher)


def test_venv_python_matches_platform_layout(tmp_path: Path) -> None:
    expected = ("Scripts", "python.exe") if os.name == "nt" else ("bin", "python")
    assert launcher.venv_python(tmp_path).parts[-2:] == expected


def test_reinstalls_only_when_pyproject_changes(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "x"\n', encoding="utf-8")
    stamp = launcher.install_stamp(pyproject)
    assert launcher.needs_install(tmp_path, stamp)

    (tmp_path / launcher.MARKER_NAME).write_text(stamp, encoding="utf-8")
    assert not launcher.needs_install(tmp_path, stamp)

    pyproject.write_text('[project]\nname = "x"\ndependencies = ["httpx"]\n', encoding="utf-8")
    assert launcher.needs_install(tmp_path, launcher.install_stamp(pyproject))


def test_demo_cmd_uses_crlf_and_launches_python_script() -> None:
    raw = (ROOT / "demo.cmd").read_bytes()
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")
    assert b"scripts\\demo.py %*" in raw
