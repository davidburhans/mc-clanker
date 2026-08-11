# mc-clanker Training Pipeline

Fine-tuning Qwen3.5-0.8B for the DJ Conductor schema enforcement task.

**Pipeline overview:**

```
SFT Dataset (slop harness) → SFT Fine-tune → DPO Preference Pairs → DPO Fine-tune
```

---

## Stage 0: Prerequisites

### Container Setup

```bash
# Build the training image
podman build -f training/Dockerfile -t mcclanker/training:latest .

# Run SFT training
podman run --rm --gpus all -v $(pwd)/training:/training:rw --name mcclanker-sft \
    mcclanker/training:latest python finetune_qwen.py

# Run DPO training (requires :raw image with DPO scripts copied in)
# See Stage 3 below
```

### Hardware

- **GPU**: NVIDIA RTX 5090 (32GB VRAM) — recommended
- **Alternative**: Any GPU with ≥24GB VRAM for full fine-tune
- **Minimum**: ~16GB VRAM with LoRA (not covered here)

---

## Stage 1: SFT Fine-tuning

Supervised Fine-Tuning — teaches the model to produce valid Conductor JSON.

### Input Format

The SFT dataset at `/training/data/unsloth_dataset` is a HuggingFace `DatasetDict` with a `messages` column:

```python
{
    "messages": [
        {"role": "system", "content": "You are a DJ Conductor..."},
        {"role": "user", "content": "Start a house music set"},
        {"role": "assistant", "content": '{"name": "house_set_001", "master_bpm": 124, ...}'},
    ]
}
```

The script applies Qwen's chat template to format each sample as a proper model input.

### Scripts

#### `finetune_qwen.py` — Preferred (uses unsloth for speed)

```bash
# Inside container
python finetune_qwen.py
```

Key configuration (at top of file):

```python
MODEL_NAME = "Qwen/Qwen3.5-0.8B"
DATASET_PATH = "/training/data/unsloth_dataset"
OUTPUT_DIR = "/training/outputs/qwen3.5-0.8b-mc-clanker"

MAX_SEQ_LENGTH = 1024  # Reduce to 512 if OOM
NUM_EPOCHS = 3  # 3 epochs typical for SFT
PER_DEVICE_BATCH_SIZE = 4  # Reduce to 2 if OOM
GRADIENT_ACCUMULATION_STEPS = 8  # Effective batch = 32
LEARNING_RATE = 1e-5
```

Output: `/training/outputs/qwen3.5-0.8b-mc-clanker/final/` containing:
- `model.safetensors`
- `config.json`
- `tokenizer.json`, `tokenizer_config.json`
- `generation_config.json`
- `chat_template.jinja`

#### `finetune_raw.py` — Alternative (raw transformers + trl, no unsloth)

```bash
# Inside container
python finetune_raw.py
```

Same output path. Use this if unsloth has compatibility issues with your CUDA version. Slightly slower but more predictable dependency chain.

### Expected Runtime on RTX 5090 (32GB VRAM)

| Config | VRAM | Time (1 epoch) | Time (3 epochs) |
|--------|------|----------------|-----------------|
| batch=4, seq=1024 | 32GB | ~3 hours | ~9 hours |
| batch=2, seq=1024 | ~20GB | ~5 hours | ~15 hours |
| batch=2, seq=512  | ~16GB | ~3 hours | ~9 hours |

### Reusing for Different Data

To fine-tune on your own conversational dataset:

1. Convert your data to the `{"messages": [...]}` format above
2. Save as a HuggingFace dataset:

```python
from datasets import DatasetDict, Dataset

dataset = DatasetDict(
    {
        "train": Dataset.from_list(train_samples),
        "test": Dataset.from_list(test_samples),
    }
)
dataset.save_to_disk("/training/data/my_dataset")

# Then edit finetune_qwen.py:
DATASET_PATH = "/training/data/my_dataset"
OUTPUT_DIR = "/training/outputs/my-model"
```

---

## Stage 2: Generate DPO Preference Pairs

DPO (Direct Preference Optimization) needs `chosen`/`rejected` pairs. This stage synthesizes them from the SFT dataset by corrupting valid JSON into invalid versions.

### Input

SFT dataset from Stage 1 at `/training/data/unsloth_dataset` (same format).

### Output

DPO dataset at `/training/data/dpo_dataset/` containing `train/` and `test/` arrow files with:

```python
{
    "chosen": '{"name": "house_set_001", "master_bpm": 124, ...}',  # valid
    "rejected": '{"name": "house_set_001", "master_bpm": 500, ...}',  # corrupted
}
```

### Script

```bash
python generate_dpo_dataset.py \
    --sft-path /training/data/unsloth_dataset \
    --output-path /training/data/dpo_dataset \
    --corruption-types missing_field invalid_enum invalid_bpm \
    --num-corruptions 1
```

Options:
- `--corruption-types`: Which corruption strategies to apply (see below)
- `--num-corruptions`: Pairs generated per sample (default 1)
- `--max-samples`: Limit for testing (default all)

### Corruption Strategies (`dpo_pipeline.py`)

Each corrupts a valid Conductor JSON response into an invalid one:

| Strategy | What it does |
|----------|--------------|
| `missing_field` | Removes `master_bpm`, `master_key`, `reasoning`, or `name` |
| `invalid_enum` | Replaces `action_type` with `"INVALID_ACTION"` |
| `invalid_bpm` | Sets `master_bpm` to 250, 30, 0, -10, or 500 |
| `truncated_json` | Truncates JSON string mid-parsing |
| `extra_field` | Removes required fields from an `add` action |

### Extending for Your Own Schema

The Conductor schema validation is defined in `dpo_pipeline.py`:

```python
VALID_ACTION_TYPES = {"retain", "add", "remove"}
VALID_MAJOR_FAMILIES = {"Drums", "Bass", "Synth", "Keys", ...}
VALID_BPM_RANGE = (60, 200)
REQUIRED_RESPONSE_FIELDS = {"master_bpm", "master_key", "actions", "reasoning", "name"}
```

To adapt for a different JSON schema:

1. Edit these constants to match your required fields and valid values
2. Add corruption functions in `CORRUPTION_STRATEGIES` dict
3. Re-run `generate_dpo_dataset.py`

---

## Stage 3: DPO Fine-tuning

Fine-tunes the SFT model to prefer valid schema outputs over corrupted ones.

### Memory Constraint Reality

DPO requires **two copies** of the model in memory:
- **Policy model** — being trained (all gradients)
- **Reference model** — frozen (no gradients, but still holds weights + activations)

On a 32GB VRAM GPU with Qwen3.5-0.8B (752M params @ bf16 = ~1.5GB per model):

| Config | VRAM Used | GPU Util | Notes |
|--------|-----------|----------|-------|
| batch=2, seq=1024, no GC | ~32GB+ | 88% | **Overflows** |
| batch=2, seq=1024, GC on | ~25GB | 27% | GC recomputes activations, low util |
| batch=2, seq=512, no GC | ~29GB | 50% | **Current recommended** |
| batch=1, seq=1024, no GC | ~17GB | 32% | Slow but stable |

### Script: `finetune_dpo_simple.py` (custom, no trl)

This is a standalone DPO implementation bypassing trl's dependency chain. It:
1. Loads the SFT model as the **policy** (all params trainable)
2. Loads the SFT model again as the **reference** (frozen, no optimizer state)
3. Computes DPO loss: `L = -log sigmoid(beta * (log pi(y_w) - log r(y_w) - log pi(y_l) + log r(y_l)))`

```bash
# Run with current recommended config (seq=512, batch=2, grad_accum=16)
podman run --rm --gpus all -v $(pwd)/training:/training:rw --name mcclanker-dpo \
    mcclanker/training:raw /usr/bin/python /training/finetune_dpo_simple.py
```

> **Note**: The `:raw` image has `finetune_dpo_simple.py` copied in. Build it with:
> ```bash
> podman build -f training/Dockerfile.raw -t mcclanker/training:raw .
> ```

Key configuration:

```python
SFT_MODEL_PATH = "/training/outputs/qwen3.5-0.8b-mc-clanker/final"
DPO_TRAIN_DATA = "/training/data/dpo_dataset/train"
DPO_TEST_DATA = "/training/data/dpo_dataset/test"
OUTPUT_DIR = "/training/outputs/qwen3.5-0.8b-mc-clanker-dpo"

MAX_SEQ_LENGTH = 512  # Reduce if OOM, increase for quality
PER_DEVICE_BATCH_SIZE = 2  # Reduce to 1 if OOM
GRADIENT_ACCUMULATION_STEPS = 16  # Effective batch = 32
LEARNING_RATE = 1e-6
DPO_BETA = 0.1  # KL penalty strength (higher = stay close to SFT)
USE_GRADIENT_CHECKPOINTING = False  # Enable to save memory, disable for speed
```

Output: `/training/outputs/qwen3.5-0.8b-mc-clanker-dpo/final/`

### DPO Loss Explained

The core DPO loss computation in `finetune_dpo_simple.py`:

```python
def get_seq_logps(logits, input_ids, attention_mask):
    """Sum of log probabilities for each token in the sequence."""
    shift_logits = logits[:, :-1, :]  # Next-token prediction
    shift_labels = input_ids[:, 1:]
    log_probs = torch.log_softmax(shift_logits, dim=-1)
    token_logps = torch.gather(log_probs, 2, shift_labels.unsqueeze(-1)).squeeze(-1)
    return (token_logps * shift_mask).sum(dim=-1)


# For each pair:
chosen_logps = policy_chosen_logps - ref_chosen_logps
rejected_logps = policy_rejected_logps - ref_rejected_logps
loss = -torch.nn.functional.logsigmoid(DPO_BETA * (chosen_logps - rejected_logps)).mean()
```

`DPO_BETA=0.1` means a moderate penalty for the policy diverging from the reference model. Lower = more freedom to change behavior, higher = stay closer to SFT.

### Expected Runtime on RTX 5090 (32GB VRAM)

| Config | VRAM | GPU Util | Est. Time |
|--------|------|----------|-----------|
| batch=2, seq=512, no GC | 29GB | ~50% | ~19 hours |
| batch=1, seq=1024, no GC | 17GB | ~32% | ~30 hours |
| batch=2, seq=1024, GC on | 25GB | ~27% | ~40 hours |

186,750 pairs / (batch_size × grad_accum) = total steps per epoch.

---

## ORPO: A Better Alternative for Next Time

DPO requires keeping a frozen reference model in memory alongside the policy model. On a 32GB GPU this means:
- Only ~50% GPU utilization (vs 100% possible)
- ~19 hours training time instead of ~9 hours

**ORPO (Odds Ratio Preference Optimization)** is a newer method (2024) that achieves the same goal with only **one model** — it bakes the preference learning into a single combined loss. On 32GB VRAM you can run ORPO with batch=4 at full utilization, cutting training time roughly in half.

```python
# Conceptual ORPO loss (simplified):
# L = -alpha * log P(y_w | x) + beta * log (P(y_w | x) / P(y_l | x))^2
#   = SFT loss (keep generating good outputs)
#   + odds-ratio penalty (prefer chosen over rejected)
```

For a future training run, consider ORPO instead of DPO if:
- You have ≥24GB VRAM
- You want faster training
- You're okay with a less-proven method

The mc-clanker DPO pipeline (corruption strategies, schema validation) is directly reusable for ORPO — only the training script changes.

---

## Complete Workflow

### One-time setup

```bash
# 1. Build training image
podman build -f training/Dockerfile -t mcclanker/training:latest .

# 2. If using DPO, build raw image with DPO scripts
# (Add finetune_dpo_simple.py and generate_dpo_dataset.py to Dockerfile.raw first)
podman build -f training/Dockerfile.raw -t mcclanker/training:raw .
```

### Full run

```bash
# ============================================================
# Step 1: SFT Fine-tune
# ============================================================
# Edit finetune_qwen.py → DATASET_PATH, OUTPUT_DIR, NUM_EPOCHS
podman run --rm --gpus all -v $(pwd)/training:/training:rw --name mcclanker-sft \
    mcclanker/training:latest python finetune_qwen.py

# ============================================================
# Step 2: Generate DPO pairs
# ============================================================
# Run inside the raw container (has DPO scripts)
# First copy scripts into container or bind mount:
podman run --rm --gpus all -v $(pwd)/training:/training:rw --name mcclanker-dpo-gen \
    mcclanker/training:latest python generate_dpo_dataset.py \
        --sft-path /training/data/unsloth_dataset \
        --output-path /training/data/dpo_dataset \
        --corruption-types missing_field invalid_enum invalid_bpm

# ============================================================
# Step 3: DPO Fine-tune
# ============================================================
# Edit finetune_dpo_simple.py → SFT_MODEL_PATH, DPO_TRAIN/TEST_DATA, OUTPUT_DIR
# Set MAX_SEQ_LENGTH, PER_DEVICE_BATCH_SIZE to stay within your VRAM
podman run --rm --gpus all -v $(pwd)/training:/training:rw --name mcclanker-dpo \
    mcclanker/training:raw /usr/bin/python /training/finetune_dpo_simple.py
```

### After training

Your final model is at:
```
/training/outputs/qwen3.5-0.8b-mc-clanker-dpo/final/
```

To use it in mc-clanker, point the Conductor LLM at this model directory.

---

## Troubleshooting

### OOM during SFT
- Reduce `PER_DEVICE_BATCH_SIZE` by 1
- Reduce `MAX_SEQ_LENGTH` to 512
- Enable gradient checkpointing (add `gradient_checkpointing=True` in SFTConfig)

### OOM during DPO
- Reduce `PER_DEVICE_BATCH_SIZE` from 2 to 1
- Reduce `MAX_SEQ_LENGTH` from 1024 to 512
- Enable `USE_GRADIENT_CHECKPOINTING = True` (trades 20-30% more compute for 40% less memory)

### GPU utilization is low during DPO
This is expected. DPO requires 4 forward passes per batch (policy chosen/rejected + ref chosen/rejected), and the reference model on GPU limits compute throughput even when not training it. There is no fix that doesn't involve either OOM or using a different method (ORPO needs only 1 model).

### trl import errors
Use `finetune_dpo_simple.py` (custom implementation with no trl dependency) or `finetune_raw.py` for SFT.

---

## File Reference

| File | Purpose |
|------|---------|
| `finetune_qwen.py` | SFT with unsloth (preferred) |
| `finetune_raw.py` | SFT with raw trl (fallback) |
| `generate_dpo_dataset.py` | Synthesizes chosen/rejected pairs |
| `dpo_pipeline.py` | Schema validation + corruption strategies |
| `finetune_dpo_simple.py` | Custom DPO (no trl) |
| `convert_to_unsloth_dataset.py` | Converts raw JSONL to HuggingFace dataset format |
| `requirements.txt` | Python dependencies |
| `Dockerfile` | SFT training container |
| `Dockerfile.raw` | DPO training container base |
