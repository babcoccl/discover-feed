"""Fetch every config/source_catalog.yaml URL and check it parses as a feed (network!).

python scripts/verify_catalog.py [--out catalog-check.md]
"""

import argparse
import asyncio
import io
import sys
import time
from pathlib import Path

import feedparser
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.ingest import USER_AGENT
from app.source_admin import load_catalog


async def check(client: httpx.AsyncClient, entry) -> tuple:
    start = time.perf_counter()
    try:
        response = await client.get(entry.url)
        parsed = feedparser.parse(io.BytesIO(response.content))
        ok = response.status_code < 400 and bool(parsed.entries)
        detail = f"HTTP {response.status_code}, {len(parsed.entries)} items"
    except httpx.HTTPError as exc:
        ok, detail = False, type(exc).__name__
    return entry, ok, detail, int((time.perf_counter() - start) * 1000)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out")
    args = parser.parse_args()
    headers = {"User-Agent": USER_AGENT + " (catalog check; contact@example.com)"}
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=headers) as client:
        results = await asyncio.gather(*(check(client, e) for e in load_catalog()))
    lines = ["| Category | Source | Result | ms |", "|---|---|---|---|"]
    for entry, ok, detail, ms in results:
        mark = "ok" if ok else "FAIL"
        lines.append(f"| {entry.category} | {entry.name} | {mark}: {detail} | {ms} |")
    text = "\n".join(lines)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    return 0 if all(ok for _, ok, _, _ in results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
