"""End-to-end run in Docker: fake LLM + local fixture feeds on a fresh volume (stdlib only).

    python scripts/docker_e2e.py [--timeout 900] [--keep] [--no-assert] [--out timeline.txt]

Works on Windows and Linux (needs Docker with `docker compose`). Starts the app with
docker-compose.fake-llm.yml on a fresh volume, polls the API once a second, prints a timeline
of when stories appeared, got a brief and a final report, checks that no unpublished story
ever appears in a feed, and tears everything down (`down -v`) unless --keep is given.
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROJECT = "discover-e2e"
COMPOSE = [
    "docker",
    "compose",
    "-p",
    PROJECT,
    "-f",
    "docker-compose.yml",
    "-f",
    "docker-compose.fake-llm.yml",
]
FINAL_REPORT = {"ok", "ready", "skipped", "failed"}


def compose(*args: str, check: bool = True) -> int:
    print("$", " ".join(COMPOSE + list(args)), flush=True)
    return subprocess.run(COMPOSE + list(args), cwd=ROOT, check=check).returncode


def get(base: str, path: str) -> dict | list | None:
    try:
        with urllib.request.urlopen(base + path, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, ConnectionError):
        return None


class Timeline:
    def __init__(self) -> None:
        self.t0 = time.monotonic()
        self.events: list[tuple[float, str]] = []
        self.stories: dict[str, dict] = {}
        self.violations: list[str] = []
        self.snippet_cards = 0
        self.cards = 0

    def now(self) -> float:
        return round(time.monotonic() - self.t0, 1)

    def event(self, text: str) -> None:
        self.events.append((self.now(), text))
        print(f"[{self.now():7.1f}s] {text}", flush=True)

    def story(self, key: str, title: str) -> dict:
        if key not in self.stories:
            self.stories[key] = {"title": title, "card": self.now()}
        return self.stories[key]

    def mark(self, row: dict, field: str, value: object = None) -> None:
        if field not in row:
            row[field] = self.now() if value is None else value


def report_state(report: dict | None) -> str | None:
    if not report:
        return None
    return report.get("state") or report.get("status")


def poll(base: str, tl: Timeline, assert_published: bool) -> None:
    articles = get(base, "/api/articles?limit=500") or []
    if articles and not any(e[1].startswith("first article") for e in tl.events):
        tl.event(f"first article ingested ({len(articles)} so far)")
    for profile in get(base, "/api/profiles") or []:
        slug = profile.get("slug") or profile.get("id")
        feed = get(base, f"/api/profiles/{slug}/feed?group=stories&limit=100") or {}
        for item in feed.get("items", []):
            tl.cards += 1
            key = str(item["id"]) if item.get("id") is not None else "a:" + item["title"]
            row = tl.story(key, item["title"])
            if item.get("id") is None:
                tl.snippet_cards += 1
                msg = f"unclustered article in {slug} feed: {item['title'][:60]}"
                if msg not in tl.violations:
                    tl.violations.append(msg)
                continue
            tl.mark(row, "clustered")
            if item.get("published_at", True) is None:
                tl.violations.append(f"unpublished story {key} in {slug} feed")
            if not item.get("brief"):
                tl.snippet_cards += 1
                msg = f"story {key} shown without a brief in {slug}"
                if msg not in tl.violations:
                    tl.violations.append(msg)
            else:
                tl.mark(row, "brief")
                if not any(e[1].startswith("first story published") for e in tl.events):
                    tl.event(f"first story published: {item['title'][:60]}")
            state = report_state(item.get("report"))
            if state is None and "brief" in row and "report" not in row:
                story = get(base, f"/api/stories/{key}") or {}
                state = report_state(story.get("report"))
            if state in FINAL_REPORT or (state or "").startswith(("skipped", "failed")):
                tl.mark(row, "report")
                tl.mark(row, "report_state", state)
    if not assert_published:
        return


def settled(tl: Timeline) -> bool:
    """At least 5 published stories with a final report, and every shown card has a brief."""
    rows = [r for r in tl.stories.values() if "clustered" in r]
    final = [r for r in rows if "report" in r]
    return len(final) >= 5 and all("brief" in r for r in rows)


def render(tl: Timeline) -> str:
    lines = ["Events:"] + [f"  {t:7.1f}s  {text}" for t, text in tl.events]
    lines += ["", f"{'card':>7} {'story':>7} {'brief':>7} {'report':>7}  state     title"]
    for row in sorted(tl.stories.values(), key=lambda r: r["card"]):
        cells = [f"{row.get(k, '-'):>7}" for k in ("card", "clustered", "brief", "report")]
        lines.append(" ".join(cells) + f"  {row.get('report_state', '-')!s:9} {row['title'][:60]}")
    lines += [
        "",
        f"cards polled: {tl.cards}, shown without a brief: {tl.snippet_cards}",
        f"publish-gate violations: {len(tl.violations)}",
    ]
    lines += [f"  - {v}" for v in tl.violations[:20]]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="http://localhost:8000")
    parser.add_argument("--timeout", type=float, default=900, help="seconds to wait to settle")
    parser.add_argument("--keep", action="store_true", help="leave the stack running")
    parser.add_argument("--no-assert", action="store_true", help="record only, never fail")
    parser.add_argument("--out", type=Path, help="also write the timeline to this file")
    args = parser.parse_args()

    compose("down", "-v", "--remove-orphans", check=False)
    tl = Timeline()
    try:
        compose("up", "-d", "--build", "--wait", "--wait-timeout", "180")
        tl.event("stack healthy")
        while tl.now() < args.timeout:
            poll(args.base, tl, not args.no_assert)
            if settled(tl):
                done = sum("report" in r for r in tl.stories.values())
                tl.event(f"settled: {done} stories published with a final report")
                break
            time.sleep(1)
        else:
            tl.event(f"timed out after {args.timeout:.0f}s")
    finally:
        text = render(tl)
        print("\n" + text)
        if args.out:
            args.out.write_text(text + "\n", encoding="utf-8")
        if not args.keep:
            compose("logs", "--no-color", "--tail", "200", "app", check=False)
            compose("down", "-v", "--remove-orphans", check=False)
    if args.no_assert:
        return 0
    ok = settled(tl) and not tl.violations
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
