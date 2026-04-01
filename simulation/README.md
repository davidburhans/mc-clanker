# simulation

**Stateful DJ session simulation** — generates multi-turn Conductor conversations by running thousands of concurrent virtual jockeys against the production ConductorLLMAsync.

Unlike `slop_harness` (which generates stateless single-interaction pairs), `simulation` produces **multi-loop sessions** where each jockey maintains persistent state across 96–256 loops. This captures realistic DJ decision-making: stems get retained, removed, or replaced over time, and vibe overrides persist across loops.

---

## Why Stateful Sessions?

Single-interaction pairs (from slop_harness) teach the Conductor to respond correctly to *one* snapshot of state. But real DJ decisions are **sequential** — the Conductor needs to know:

- Which stems were playing in the *previous* loop (via `history`)
- How stems have aged over time (via `_age`)
- What the persistent vibe override has been set to

`simulation` produces that sequential data: each jockey runs 96–256 loops with a persistent vibe, and the Conductor's response at each loop is conditioned on the evolving state.

---

## Quick Start

```bash
# Install dependencies
pip install -e slop_harness/

# Run 2048 concurrent jockeys, 8 performances each
export LLM_BASE_URL=http://192.168.0.203:1234/v1
export LLM_MODEL=your-model
python -m simulation.cli

# With custom settings
python -m simulation.cli \
    --jockeys 2048 \
    --performances 8 \
    --min-loops 96 \
    --max-loops 256 \
    --vibe-prob 0.15 \
    --output-dir ./simulation/sim_data
```

Resume after interruption — all checkpoints are crash-safe.

---

## CLI Arguments

| Argument | Default | Description |
|----------|---------|-------------|
| `--jockeys` | `2048` | Number of concurrent jockey sessions |
| `--performances` | `8` | Separate sessions per jockey (each is an independent run) |
| `--min-loops` | `96` | Minimum loops per performance |
| `--max-loops` | `256` | Maximum loops per performance |
| `--vibe-prob` | `0.15` | Probability per loop of setting a persistent vibe override |
| `--vibe-clear-prob` | `0.05` | Probability per loop of clearing the current vibe |
| `--batch-size` | `1000` | Records per output batch file |
| `--concurrent` | `128` | Max concurrent LLM calls across all jockeys |
| `--run-seed` | random | Reproduce a run exactly by passing the logged seed |

---

## Output

Same format as `slop_harness`:

```jsonl
{"messages": [
  {"role": "system", "content": "You are an AI DJ..."},
  {"role": "user",   "content": "Current State: Master BPM: 128..."},
  {"role": "assistant", "content": "{\"master_bpm\": 128, \"actions\": [...]}"}
]}
```

Output: `simulation/sim_data/slop_batch_*.jsonl`

Checkpoints:
- `run_seed.txt` — seed for reproducibility
- `sessions_completed.txt` — resume point
- `total_records.txt` — total records written

---

## Architecture

```
cli.py
  └── SlopJockey (per session)
        ├── SessionState (persistent BPM, key, stems, history)
        ├── ConductorLLMAsync (production LLM calls)
        └── apply_actions (evolves state each loop)

  Concurrent: 2048 jockeys × 8 performances × 96-256 loops
  LLM calls:  up to 128 concurrent (semaphore)
  Records:    sessions × loops → JSONL batches
```

### Files

| File | Purpose |
|------|---------|
| `cli.py` | Entry point, orchestrates concurrent jockeys |
| `jockey.py` | `SlopJockey` — runs N loops, calls ConductorLLMAsync, returns records |
| `session_state.py` | `SessionState` — isolated mutable DJ state (BPM, key, stems, history) |

---

## Jockey vs Harness

| | `slop_harness` | `simulation` |
|---|---|---|
| Interactions | 1 per record | 96–256 per jockey |
| State | Stateless | Stateful (persists across loops) |
| Vibe override | ~5% chance per interaction | ~15% chance per loop, **persists** |
| History | None | Up to 8 previous stem sets |
| Use case | Fast, bulk dataset | Realistic multi-turn sessions |

For the **best training dataset**, run both and combine — harness provides breadth, simulation provides depth.

---

## Extending

### Add a new vibe category

Edit `slop_harness/vibe_prompt_bank.py` — the same VibePromptBank is shared by both harness and simulation.

### Change session dynamics

In `cli.py`: adjust `--min-loops`, `--max-loops`, `--vibe-prob`, `--vibe-clear-prob`.

In `session_state.py`: `_select_instruments_for_jockey()` controls how 75% of jockeys get full instrument sets vs restricted subsets (mimics real DJ limitations).
