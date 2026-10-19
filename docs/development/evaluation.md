# Evaluation tools

Harnesses that use fixtures or a live DB — see each feature's
*Diagnostics* section for the details.

```bash
# Clustering: fixtures or DB; sweep, near-miss cosines, extraneous pairs
python -m app.cluster evaluate --fixtures tests/fixtures/demo/stories.yaml --sweep
python -m app.cluster evaluate --db /data/discover.db --markdown eval.md

# Summaries: diagnose exact per-attempt failures; compare models offline
python -m app.summarize.diagnose --db /data/discover.db --profile personal-reader
python -m app.summarize.diagnose --db /data/discover.db --failures
python -m app.summarize.compare --fixture tests/fixtures/demo/stories.yaml \
    --model my-vllm --base-url http://localhost:8001/v1 --markdown compare.md

# Enrichment: replay candidates at current thresholds
python -m app.enrich.cli evaluate --db /data/discover.db --days 7 --markdown out.md
python -m app.enrich.cli evaluate --wait        # waits out cooldowns, full report
python -m app.enrich.cli dry-run 4242           # one story, no writes

# Context: evaluate filters/judge on candidates + judge-once on a source pair
python -m app.context.cli evaluate --db /data/discover.db --days 7 --markdown out.md
python -m app.context.cli judge-once 2345 --source-url https://... --verbose

# Summaries smoke: one request against an endpoint
python -m app.summarize.smoke --base-url http://localhost:8000/v1 --model qwen
python -m app.summarize.smoke --base-url http://localhost:8000/v1 --count 200   # soak

# Capture prompts/responses for a report-brief model
python -m app.summarize.capture run --base-url http://localhost:9000/v1 --model qwen --count 40
python -m app.summarize.capture run --from-jsonl /data/summary-capture.jsonl --endpoint reports

# Q&A: fixture answers + ask about a live story
python -m app.chat.smoke --fixture
python -m app.chat.smoke --live --db /data/discover.db --url 'https://...' --question 'What happened?'
python -m app.chat.smoke --live --chat-mode stub     # no provider needed
```

Recording captured production prompts (`summaries.capture_enabled`) into
`summaries.capture_path` gives you offline replay material.
