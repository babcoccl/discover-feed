"""Example /api/v1 client: print a profile's feed, then follow the story stream and resume
after any disconnect (Last-Event-ID). Needs only httpx (``pip install httpx``).

    python examples/client.py --base http://localhost:8000 --profile personal-reader
    DISCOVER_API_KEY=... python examples/client.py      # when DISCOVER_API_KEYS is set
"""

import argparse
import json
import os
import sys
import time

import httpx


def headers(key: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"} if key else {}


def print_feed(client: httpx.Client, profile: str, limit: int) -> None:
    feed = client.get(f"/api/v1/profiles/{profile}/feed", params={"limit": limit})
    feed.raise_for_status()
    for story in feed.json()["items"]:
        lead = (story.get("brief") or {}).get("lead") or story["snippet"]
        print(f"[{story['id']} v{story['version']}] {story['title']}\n    {lead}")


def events(response: httpx.Response):
    """Parse a text/event-stream into ``(id, event, data, retry)`` tuples."""
    fields: dict[str, str] = {}
    for line in response.iter_lines():
        if not line:
            if fields:
                data = json.loads(fields["data"]) if "data" in fields else None
                yield fields.get("id"), fields.get("event", "message"), data, fields.get("retry")
            fields = {}
            continue
        if line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        fields[name] = value.removeprefix(" ")


def follow(
    client: httpx.Client,
    profile: str | None,
    include: str,
    last_id: str | None,
    max_events: int | None = None,
    stop_after_disconnects: int | None = None,
) -> None:
    retry_s, seen, disconnects = 5.0, 0, 0
    while True:
        params = {"include": include, **({"profile": profile} if profile else {})}
        extra = {"Last-Event-ID": last_id} if last_id else {}
        try:
            with client.stream("GET", "/api/v1/stream", params=params, headers=extra) as r:
                r.raise_for_status()
                for event_id, name, data, retry in events(r):
                    if retry:
                        retry_s = int(retry) / 1000
                    if data is None:  # a bare "retry:" hint
                        continue
                    if name == "hello":
                        print(
                            f"connected; latest event {data['latest_id']}, resuming after "
                            f"{data['resume_from']}",
                            flush=True,
                        )
                    elif name != "heartbeat":
                        last_id = event_id or last_id
                        story = (data or {}).get("story") or {}
                        print(
                            f"event {event_id} {name} story {data['story_id']} "
                            f"v{data['version']} {story.get('title', '')}",
                            flush=True,
                        )
                        seen += 1
                        if max_events and seen >= max_events:
                            return
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (401, 404):
                raise
            print(
                f"disconnected ({type(exc).__name__}); resuming after {last_id} in {retry_s:.0f}s",
                flush=True,
            )
        else:
            print(f"stream closed; resuming after {last_id} in {retry_s:.0f}s", flush=True)
        disconnects += 1
        if stop_after_disconnects is not None and disconnects > stop_after_disconnects:
            return
        time.sleep(retry_s)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--base", default=os.environ.get("DISCOVER_URL", "http://localhost:8000"))
    p.add_argument("--profile", default="personal-reader")
    p.add_argument("--include", choices=("id", "card", "full"), default="card")
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--since-id", help="replay stored events after this id")
    p.add_argument("--max-events", type=int, help="exit after this many story events")
    args = p.parse_args()
    key = os.environ.get("DISCOVER_API_KEY")
    timeout = httpx.Timeout(10, read=60)  # heartbeats arrive every 15 s
    with httpx.Client(base_url=args.base, headers=headers(key), timeout=timeout) as client:
        print_feed(client, args.profile, args.limit)
        follow(client, args.profile, args.include, args.since_id, args.max_events)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
