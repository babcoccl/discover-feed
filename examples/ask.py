"""Example: ask a follow-up question about a story and print the streamed answer.
Needs only httpx (``pip install httpx``).

    python examples/ask.py --story 12 "When does it ship?"
    python examples/ask.py --story 12 --conversation <id> "Who else reported it?"
    DISCOVER_API_KEY=... python examples/ask.py ...      # when DISCOVER_API_KEYS is set
"""

import argparse
import json
import os
import sys

import httpx


def events(response: httpx.Response):
    """Parse a text/event-stream into ``(event, data)`` pairs."""
    name, data = "message", ""
    for line in response.iter_lines():
        if not line:
            if data:
                yield name, json.loads(data)
            name, data = "message", ""
        elif line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            data += line[5:].strip()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("question")
    p.add_argument("--story", type=int, required=True)
    p.add_argument("--conversation", help="continue this conversation id")
    p.add_argument("--profile", help="whose llm.chat answers (default: the worker's)")
    p.add_argument("--base", default=os.environ.get("DISCOVER_URL", "http://localhost:8000"))
    args = p.parse_args()
    key = os.environ.get("DISCOVER_API_KEY")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    params = {"profile": args.profile} if args.profile else {}
    body = {"question": args.question, "conversation_id": args.conversation}
    timeout = httpx.Timeout(10, read=300)  # the model may be busy with background work
    with httpx.Client(base_url=args.base, headers=headers, timeout=timeout) as client:
        followups = client.get(f"/api/v1/stories/{args.story}/followups", params=params)
        if followups.status_code == 200 and followups.json()["questions"]:
            print("Suggested:", " | ".join(followups.json()["questions"]))
        with client.stream(
            "POST", f"/api/v1/stories/{args.story}/ask", json=body, params=params
        ) as r:
            if r.status_code != 200:
                r.read()
                print(f"HTTP {r.status_code}: {r.text}", file=sys.stderr)
                return 1
            print(f"Q: {args.question}\nA: ", end="", flush=True)
            for name, data in events(r):
                if name == "meta":
                    conversation = data["conversation_id"]
                elif name == "waiting":
                    print(f"[{data['message']}] ", end="", flush=True)
                elif name == "token":
                    print(data["text"], end="", flush=True)
                elif name == "citations":
                    print()
                    for c in data["citations"]:
                        via = f" via {c['via']}" if c["via"] else ""
                        print(f"  [{c['index']}] {c['publisher']}{via}: {c['url']}")
                elif name == "done":
                    v = data["verification"]
                    print(
                        f"verified: {v['verified']}  first token: "
                        f"{data['time_to_first_token_ms']} ms  total: {data['latency_ms']} ms  "
                        f"tokens/sec: {data['tokens_per_second']}"
                    )
                    for check in v["checks"]:
                        if not check["ok"]:
                            print(f"  {check['level']}: {check['detail']}")
                elif name == "error":
                    print(f"\nerror: {data['reason']}: {data['message']}", file=sys.stderr)
                    return 1
    print(f"conversation: {conversation}  (--conversation {conversation} to continue)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
