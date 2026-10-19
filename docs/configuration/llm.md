# LLM endpoints

Each profile has two LLM roles: `summarizer` (briefs, reports, enrichment query
mode `llm`, context angles and the context judge) and `chat` (story Q&A). Any
OpenAI-compatible chat-completions server works — providers differ only in
these settings, never in code.

```yaml
llm:
  summarizer:
    base_url: ${LOCAL_LLM_BASE_URL:-http://localhost:8080/v1}
    api_key: ${LOCAL_LLM_API_KEY:-}
    model: ${LOCAL_LLM_MODEL:-local}
    temperature: 0.2            # defaults shown
    max_tokens: 600
    timeout_seconds: 120
    context_tokens: 16384       # the model's context window
    structured_output: json_schema   # json_schema | json_object | none
    disable_thinking: true      # sends chat_template_kwargs {"enable_thinking": false}
  chat:                         # optional; unset = no story Q&A
    base_url: ${LOCAL_LLM_BASE_URL:-http://localhost:8080/v1}
    api_key: ${LOCAL_LLM_API_KEY:-}
    model: ${LOCAL_LLM_MODEL:-local}
```

Which profile the generation worker uses: `DISCOVER_SUMMARIZE_PROFILE`'s
summarizer (default: the first profile). `DISCOVER_SUMMARIZE_ENABLED=false`
turns summaries off.

## llama.cpp `llama-server`

Start it so other machines can reach it, with a key, enough context for a
report's 4 articles × 600 words, and parallel slots matching
`summaries.max_inflight`:

```bash
llama-server -m model.gguf --host 0.0.0.0 --port 8080 --api-key <key> -c 16384 -np 1
```

`-c` is the **total** context, split across the `-np` slots: use `-c 32768 -np 2`
for `max_inflight: 2`. With `-np 1` and a chat role on the same endpoint, chat
requests take priority over background generation (`chat_priority`) and a
running brief/report is never aborted.

## Cloud providers

Same client, different settings — see the `market-monitor` profile in
`config/profiles.example.yaml`:

```yaml
summarizer:
  base_url: https://api.openai.com/v1
  api_key: ${OPENAI_API_KEY:-}
  model: gpt-4o-mini
  temperature: 0.0
  timeout_seconds: 60
  disable_thinking: false   # OpenAI rejects llama.cpp's chat_template_kwargs
```

## Health check

On startup the app calls `GET {base_url}/models` and logs the result; a `401`
is reported but tolerated. While an endpoint is unreachable, feeds keep
updating, generation pauses, and the feed page + Settings > Pipeline name the
blocker.

## Evaluating a model

Before trusting an endpoint, run the read-only tools:
`python -m app.summarize.smoke` (one story, prints the brief/report + checks),
`python -m app.summarize.compare` (side-by-side endpoints, quality metrics).
See [evaluation tools](../development/evaluation.md).
