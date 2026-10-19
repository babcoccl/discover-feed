# Quick start

Runs the full stack with Docker Compose. Everything below also works in
PowerShell on Windows with Docker Desktop.

## 1. Configure

Copy `config/profiles.example.yaml` to `config/profiles.yaml` and set each
profile's `llm.summarizer` endpoint — or put `LOCAL_LLM_BASE_URL` /
`LOCAL_LLM_API_KEY` in `.env` (copy `.env.example`).

```yaml
llm:
  summarizer:
    base_url: ${LOCAL_LLM_BASE_URL:-http://localhost:8080/v1}
    api_key: ${LOCAL_LLM_API_KEY:-}
    model: ${LOCAL_LLM_MODEL:-local}
```

Any OpenAI-compatible chat-completions endpoint works (llama.cpp `llama-server`,
vLLM, Ollama, OpenAI). See [LLM endpoints](../configuration/llm.md).

!!! warning "Config file encoding"
    `profiles.yaml` must be saved as **UTF-8** and copied to the server with
    `cp`/`scp` — not PowerShell `Get-Content`/`Set-Content`, which can corrupt
    non-ASCII text and cause `UnicodeDecodeError`.

## 2. Start

```bash
docker compose up --build -d
```

The log shows the LLM health check (`GET {base_url}/models`). A `401` there is
reported but tolerated.

## 3. Watch the first stories arrive

Open <http://localhost:8000>. Until the first story is fully generated the feed
says "Preparing your first stories..." with live counts and any blocker
(endpoint not set, unreachable, HTTP 401, paused). Stories appear one at a time
as they publish ("N new stories — click to load").

Settings > Pipeline shows every story still in progress.

## 4. Start over

```bash
docker compose down -v   # drops the database volume too
```

## Useful commands

```bash
curl -X POST localhost:8000/api/admin/refresh   # fetch all feeds now
docker compose logs -f app                     # watch ingestion and generation
docker compose down                            # stop (keep the database)
```

Next: [first-run checklist](first-run.md) ·
[deploy on a NAS](nas.md) ·
[configuration](../configuration/profiles.md)
