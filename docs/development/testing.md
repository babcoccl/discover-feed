# Testing

```bash
python3.12 -m venv .venv && source .venv/bin/activate
make install        # pip install -e ".[dev]"
make lint           # ruff check + format --check
make test           # pytest
make run            # uvicorn :8000
```

=== "Windows (no make)"

    ```powershell
    .venv\Scripts\activate
    python -m pip install -e ".[dev]"
    python -m ruff check .
    python -m ruff format --check .
    python -m pytest
    python -m uvicorn app.main:app --reload
    python -m app.openapi    # = make openapi
    ```

## Rules

- Tests never hit the network: `httpx.MockTransport` + `tests/fixtures/*.xml`;
  `tests/conftest.py` fails any `LLMClient` built without a transport.
- The fake LLM (`app/summarize/fake.py`, `FakeLLM(mode, chat_mode=...)`)
  covers briefs, reports and chat deterministically; HTTP form:
  `python -m app.summarize.fake_server`.
- Cluster fixtures are labelled (`tests/fixtures/demo/stories.yaml`); add live
  false merges/misses there so `tests/test_cluster.py` covers them.
- API changes: `make openapi` regenerates `docs/openapi.v1.json`;
  `tests/test_api_v1.py` fails while it's stale.
- Docs changes: `make docs` runs `scripts/check_docs.py` + `mkdocs build
  --strict`.

## Docker e2e

`python scripts/docker_e2e.py [--timeout 900] [--keep] [--out timeline.txt]` —
fresh volume, fixture feed/search servers, fake LLM over HTTP; waits for
published stories and asserts none unpublished reach the feed. CI job
`docker-e2e`; works on Windows (stdlib only).
