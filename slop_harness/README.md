# slop-harness

Dataset generation harness for fine-tuning the mc-clanker **Conductor** LLM.

Generates 100,000+ Conductor prompt/response pairs by simulating diverse DJ sessions at every stage of the lifecycle. Output is in HuggingFace `DatasetDict` JSONL format, ready for SFT fine-tuning with `training/finetune_qwen.py`.

---

## Quick Start

```bash
# Install
pip install -e slop_harness/

# Run (target your LM Studio or Ollama)
export LLM_BASE_URL=http://192.168.0.203:1234/v1
export LLM_MODEL=your-model
export TOTAL_INTERACTIONS=100000
export OUTPUT_DIR=./training/data

python -m slop_harness.harness
```

Resume after interruption — checkpoint is saved automatically.

---

## Docker

```bash
# Build
cd docker
podman build -f Dockerfile.harness -t mcclanker/harness:latest ..

# Run
podman run --rm \
  -e LLM_BASE_URL=http://host.docker.internal:1234/v1 \
  -e LLM_MODEL=your-model \
  -e TOTAL_INTERACTIONS=100000 \
  -e BATCH_SIZE=1000 \
  -e CONCURRENT_REQUESTS=20 \
  -v $(pwd)/../training/data:/harness/data \
  mcclanker/harness:latest
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

Each batch writes a JSONL file (`slop_batch_00000.jsonl`, etc.) containing records:

```jsonl
{"messages": [
  {"role": "system", "content": "<fixed Conductor system prompt>"},
  {"role": "user",   "content": "<musical state prompt>"},
  {"role": "assistant", "content": "<raw JSON from LLM>"}
]}
```

**Checkpoint:** `training/data/checkpoint.json` — tracks progress for safe resumption.

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

- **Deterministic seeds** — Same `(batch_id, interaction_id)` always produces the same musical state. Safe to extend or resume datasets.
- **Async concurrency** — Up to 20 simultaneous LLM calls.
- **Exponential backoff** — On 429/503: 1s, 2s, 4s. On other errors: 3 retries.
- **Crash-safe** — Checkpoint written atomically after each batch.

---

## Components

| File | Purpose |
|------|---------|
| `harness.py` | CLI entrypoint, batch orchestration |
| `state_generator.py` | Deterministic musical state (BPM, key, stems, history) |
| `prompt_builder.py` | Builds system/user/assistant message structure |
| `vibe_prompt_bank.py` | Rare vibe override prompts |
| `llm_client.py` | Async OpenAI-compatible LLM client with retry |
| `dataset_writer.py` | Batched JSONL writer with crash safety |
| `checkpoint.py` | Tracks batch_id + total count for resume |
| `models.py` | Constants: BPM ranges, keys, timbre tags, etc. |

---

## Deterministic Seeds

Each interaction is derived from `(batch_id, interaction_id)` via SHA256 hash, producing a reproducible `Random` instance. This means:

- **Resumable** — interrupted runs can safely resume without duplicate data
- **Reproducible** — same seeds always produce same prompts, useful for debugging
- **Parallelizable** — any batch can be re-run independently

---

## From Dataset to SFT Fine-tune

```bash
# 1. Generate the dataset (100K interactions → ~197K examples)
python -m slop_harness.harness

# 2. Convert to HuggingFace dataset format
python training/convert_to_unsloth_dataset.py \
    --input-dir training/data \
    --output-dir training/data/unsloth_dataset

# 3. Fine-tune with unsloth
podman run --gpus all -v $(pwd)/training:/training:rw mcclanker/training:latest \
    python finetune_qwen.py
```

See `training/README.md` for the full SFT + DPO pipeline.

---

## Extending for Custom Schemas

### Add new corruption strategies for DPO (in `training/dpo_pipeline.py`)

```python
CORRUPTION_STRATEGIES = {
    "missing_field": lambda json: ...,
    "invalid_enum": lambda json: ...,
    # Add your own:
    "my_corruption": lambda json: modify(json),
}
```

### Use with a different Conductor schema

Edit `prompt_builder.py` → `SYSTEM_INSTRUCTION` and `state_generator.py` → `FOUNDATION_1_MODEL` to match your action schema.
