"""The ``/api/v1`` OpenAPI contract, committed as ``docs/openapi.v1.json``.

python -m app.openapi            # rewrite docs/openapi.v1.json (make openapi)
python -m app.openapi --check    # exit 1 if the committed file is out of date
"""

import argparse
import json
import sys
from pathlib import Path

from fastapi import FastAPI

from app.api_v1 import API_VERSION, PREFIX

SNAPSHOT = Path(__file__).resolve().parent.parent / "docs" / "openapi.v1.json"


def _refs(node: object) -> set[str]:
    if isinstance(node, dict):
        found = {node["$ref"].rsplit("/", 1)[-1]} if isinstance(node.get("$ref"), str) else set()
        return found.union(*(_refs(v) for v in node.values()))
    if isinstance(node, list):
        return set().union(*(_refs(v) for v in node))
    return set()


def v1_schema(app: FastAPI) -> dict:
    """The app's OpenAPI document cut to ``/api/v1`` paths and the schemas they use."""
    full = app.openapi()
    paths = {p: v for p, v in full["paths"].items() if p.startswith(PREFIX + "/")}
    schemas = full.get("components", {}).get("schemas", {})
    needed, todo = set(), _refs(paths)
    while todo:
        name = todo.pop()
        if name in needed or name not in schemas:
            continue
        needed.add(name)
        todo |= _refs(schemas[name])
    components = {"schemas": {n: schemas[n] for n in sorted(needed)}}
    schemes = full.get("components", {}).get("securitySchemes")
    if schemes:
        components["securitySchemes"] = schemes
    return {
        "openapi": full["openapi"],
        "info": {
            "title": "Discover Feed public API",
            "version": API_VERSION,
            "description": "Published stories and resumable story events. Within v1 changes "
            "are additive only; breaking changes go to /api/v2.",
        },
        "paths": paths,
        "components": components,
    }


def render(app: FastAPI) -> str:
    return json.dumps(v1_schema(app), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.openapi")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    from app.main import create_app

    text = render(create_app())
    if args.check:
        current = SNAPSHOT.read_text(encoding="utf-8") if SNAPSHOT.exists() else ""
        if current != text:
            print(f"{SNAPSHOT} is out of date; run make openapi", file=sys.stderr)
            return 1
        return 0
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {SNAPSHOT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
