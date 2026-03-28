# slop-harness

Dataset generation harness for fine-tuning a **slop jockey** LLM — a smaller model that replicates the Conductor's DJ decision-making.

Generates 100,000+ Conductor prompt/response pairs by simulating diverse DJ sessions at every stage of the lifecycle. Output is in Unsloth Studio JSONL format.

---

## Quick Start

```bash
# Install
pip install -e ".[dev]"

# Run (target your LM Studio or Ollama)
export LLM_BASE_URL=http://192.168.0.203:1234/v1
export LLM_MODEL=your-model
export TOTAL_INTERACTIONS=100000
export OUTPUT_DIR=./data

python -m slop_harness.harness
```

Resume after interruption — checkpoint is saved automatically.

---

## Docker

```bash
docker build -t slop-harness .
docker run --rm \
  -e LLM_BASE_URL=http://host.docker.internal:1234/v1 \
  -e LLM_MODEL=your-model \
  -e TOTAL_INTERACTIONS=100000 \
  -e BATCH_SIZE=1000 \
  -e CONCURRENT_REQUESTS=20 \
  -v $(pwd)/data:/harness/data \
  slop-harness
```

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_BASE_URL` | `http://localhost:1234/v1` | OpenAI-compatible API endpoint |
| `LLM_MODEL` | `local-model` | Model name |
| `BATCH_SIZE` | `1000` | Interactions per batch file |
| `TOTAL_INTERACTIONS` | `100000` | Total interactions to generate |
| `OUTPUT_DIR` | `./data` | Output directory |
| `CONCURRENT_REQUESTS` | `20` | Max concurrent LLM calls |
| `VIBE_PROB` | `0.05` | Probability of vibe override (~5%) |

---

## Output Format

```jsonl
{"messages": [
  {"role": "system", "content": "<fixed Conductor system prompt>"},
  {"role": "user",   "content": "<musical state prompt>"},
  {"role": "assistant", "content": "<raw JSON from LLM>"}
]}
```

Output files: `data/slop_batch_00000.jsonl`, `data/slop_batch_00001.jsonl`, ...

Checkpoint: `data/checkpoint.json`

---

## Architecture

```
batch_id, interaction_id
        ↓
StateGenerator (deterministic seed → musical state)
        ↓
VibePromptBank (~5% chance — rare override)
        ↓
PromptBuilder (system + user messages)
        ↓
LLMClient (async, retry with backoff)
        ↓
DatasetWriter (JSONL, batched, crash-safe)
```

- **Deterministic seeds** — Same `(batch_id, interaction_id)` always produces the same state. Safe to extend datasets.
- **Async concurrency** — Up to 20 simultaneous LLM calls.
- **Exponential backoff** — On 429/503: 1s, 2s, 4s. On other errors: 3 retries.
- **Crash-safe** — Checkpoint written atomically after each batch.
