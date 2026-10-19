# Demo without an LLM

Two ways to see the app working without configuring a model: a local throwaway
demo (`make demo` / `demo.cmd`) and a full Docker end-to-end run
(`scripts/docker_e2e.py`).

## Local demo (`make demo`)

Needs Python 3.12+. Creates a throwaway database under `.demo/`, seeds the
example profiles, loads 66 fixture articles over a mock transport (no network),
extracts, clusters, and generates briefs/reports with a deterministic in-process
fake LLM. Card images are generated locally too.

=== "Linux / macOS"

    ```bash
    make demo                      # http://localhost:8000
    make demo PORT=8001            # different port
    make demo DEMO_ARGS="--fake-llm-delay 3"   # slow answers: watch generating state
    ```

=== "Windows"

    ```bat
    demo.cmd                  :: http://localhost:8000, opens your browser
    demo.cmd --port 8001 --no-open
    ```

    `demo.cmd` finds Python 3.12+ via `py`/`python`, creates `.venv`, installs
    dependencies on first run (reinstalls only when `pyproject.toml` changes),
    then starts the demo. `python scripts/demo.py` is the same launcher and works
    anywhere.

Demo extras:

- A fake alert receiver (webhook + ntfy + SMTP) at <http://localhost:8000/demo/sink>;
  market-monitor alert rules are pointed at it and a few "live" items are ingested.
- `?cards=lead_only` on a feed URL previews lead-only cards.
- `?no_image=logo|text_only|gradient` previews the no-image fallbacks.
- `--report-mode off`, `--fake-chat-mode <mode>` (`stream_error`,
  `invented_number`, `stall`, ...) exercise failure states.

Use a real model instead: set `LOCAL_LLM_BASE_URL` / `LOCAL_LLM_API_KEY` and run
`make demo DEMO_ARGS="--real-llm --model <model>"` (Windows:
`.\demo.cmd --real-llm --model <model>`). It generates briefs+reports for the
newest 10 stories at startup.

## Docker e2e (`scripts/docker_e2e.py`)

A fresh stack (own compose project `discover-e2e`,
`docker-compose.yml` + `docker-compose.fake-llm.yml`) with fixture feeds, a fake
LLM HTTP server and fake web-search providers. It waits for published stories,
asserts the publish gate holds, prints a timeline and tears down. Stdlib only;
works on Windows.

```bash
python scripts/docker_e2e.py --keep      # keep the stack running, then open :8000
# later: docker compose -p discover-e2e down -v
```

=== "Windows"

    ```powershell
    .venv\Scripts\python scripts\docker_e2e.py --keep
    ```

Options: `--timeout 900`, `--out timeline.txt`.
