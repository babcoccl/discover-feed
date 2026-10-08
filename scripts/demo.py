"""Run the demo without make: create .venv, install the app, then start ``app.demo``.

Standard library only, so it runs on any Python 3.12+ (Windows, macOS, Linux):

    python scripts/demo.py [--port 8001] [--no-open]

Dependencies are reinstalled only when ``pyproject.toml`` changes, so later runs work offline.
"""

import hashlib
import os
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / ".venv"
MARKER_NAME = ".discover-feed-installed"
MIN_PYTHON = (3, 12)


def venv_python(venv_dir: Path = VENV) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def install_stamp(pyproject: Path = ROOT / "pyproject.toml") -> str:
    return hashlib.sha256(pyproject.read_bytes()).hexdigest()


def needs_install(venv_dir: Path = VENV, stamp: str | None = None) -> bool:
    marker = venv_dir / MARKER_NAME
    stamp = stamp or install_stamp()
    return not marker.exists() or marker.read_text(encoding="utf-8").strip() != stamp


def run(cmd: list[str | Path]) -> None:
    subprocess.run([str(c) for c in cmd], cwd=ROOT, check=True)


def ensure_venv() -> Path:
    python = venv_python()
    if not python.exists():
        print(f"Creating virtual environment in {VENV} ...")
        venv.EnvBuilder(with_pip=True).create(VENV)
    version = subprocess.run(
        [str(python), "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if tuple(int(n) for n in version.split(".")) < MIN_PYTHON:
        sys.exit(f"{VENV} uses Python {version}; delete the .venv folder and run this again.")

    stamp = install_stamp()
    if needs_install(VENV, stamp):
        print("Installing discover-feed and its dependencies (first run only) ...")
        has_pip = subprocess.run([str(python), "-m", "pip", "--version"], capture_output=True)
        if has_pip.returncode != 0:
            run([python, "-m", "ensurepip", "--upgrade"])
        run([python, "-m", "pip", "install", "--disable-pip-version-check", "-q", "-e", "."])
        (VENV / MARKER_NAME).write_text(stamp, encoding="utf-8")
    return python


def main(argv: list[str]) -> int:
    if sys.version_info < MIN_PYTHON:
        need = ".".join(map(str, MIN_PYTHON))
        sys.exit(f"Python {need}+ is required (this is {sys.version.split()[0]}).")
    try:
        python = ensure_venv()
    except subprocess.CalledProcessError as exc:
        sys.exit(f"Setup failed: {exc}")
    try:
        return subprocess.call([str(python), "-m", "app.demo", "--open", *argv], cwd=ROOT)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
