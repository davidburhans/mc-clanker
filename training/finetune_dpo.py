#!/usr/bin/env python3
"""
DPO (Direct Preference Optimization) Training for Conductor Schema Enforcement.

This script fine-tunes the SFT model using DPO to reinforce:
1. Valid JSON output matching the Conductor schema
2. Proper action types (retain, add, remove)
3. Correct field requirements per action type

Run AFTER SFT training completes.
Uses the same architecture as finetune_raw.py but with DPO training.
"""

import torch
from datasets import load_from_disk
from transformers import AutoTokenizer, AutoModelForCausalLM
from trl import DPOTrainer, DPOConfig

# Check GPU
if not torch.cuda.is_available():
    raise RuntimeError("CUDA GPU required for DPO training!")

print(f"GPU: {torch.cuda.get_device_name(0)}")
vram_gb = torch.cuda.get_device_properties(0).total_memory / 1e9
print(f"VRAM: {vram_gb:.1f} GB")

torch.cuda.empty_cache()
torch.cuda.reset_peak_memory_stats()

# ============================================================
# CONFIGURATION
# ============================================================
MODEL_NAME = "Qwen/Qwen3.5-0.8B"
SFT_MODEL_PATH = "/training/outputs/qwen3.5-0.8b-mc-clanker/final"
DPO_TRAIN_DATA = "/training/data/dpo_dataset/train"
DPO_TEST_DATA = "/training/data/dpo_dataset/test"
OUTPUT_DIR = "/training/outputs/qwen3.5-0.8b-mc-clanker-dpo"

MAX_SEQ_LENGTH = 1024
NUM_EPOCHS = 1
PER_DEVICE_BATCH_SIZE = 8
GRADIENT_ACCUMULATION_STEPS = 4
LEARNING_RATE = 1e-6
WARMUP_STEPS = 50
LOGGING_STEPS = 10
SAVE_STEPS = 500

# DPO-specific
DPO_BETA = 0.1  # KL penalty strength


# ============================================================
# STEP 1: Load tokenizer
# ============================================================
print("\n[1/5] Loading tokenizer...")
tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)


# ============================================================
# STEP 2: Load DPO dataset
# ============================================================
print(f"\n[2/5] Loading DPO dataset...")
train_ds = load_from_disk(DPO_TRAIN_DATA)
eval_ds = load_from_disk(DPO_TEST_DATA)

print(f"    Train pairs: {len(train_ds):,}")
print(f"    Eval pairs:  {len(eval_ds):,}")

# Inspect first sample
sample = train_ds[0]
print(f"    Sample keys: {list(sample.keys())}")


# ============================================================
# STEP 3: Load model for DPO (from SFT checkpoint)
# ============================================================
print("\n[3/5] Loading model from SFT checkpoint...")

# Load model in bfloat16
model = AutoModelForCausalLM.from_pretrained(
    SFT_MODEL_PATH,
    torch_dtype=torch.bfloat16,
    device_map="auto",
    trust_remote_code=True,
)

# Reference model for DPO (should be the same model in eval mode)
ref_model = AutoModelForCausalLM.from_pretrained(
    SFT_MODEL_PATH,
    torch_dtype=torch.bfloat16,
    device_map="auto",
    trust_remote_code=True,
)
ref_model.eval()

total_params = sum(p.numel() for p in model.parameters())
print(f"    Total parameters: {total_params:,}")


# ============================================================
# STEP 4: Train with DPO
# ============================================================
print("\n[4/5] Starting DPO training...")

use_bf16 = torch.cuda.is_bf16_supported()
print(f"    Precision: {'bf16' if use_bf16 else 'fp16'}")

dpo_trainer = DPOTrainer(
    model=model,
    ref_model=ref_model,
    train_dataset=train_ds,
    eval_dataset=eval_ds,
    processing_class=tokenizer,
    args=DPOConfig(
        output_dir=OUTPUT_DIR,
        per_device_train_batch_size=PER_DEVICE_BATCH_SIZE,
        per_device_eval_batch_size=PER_DEVICE_BATCH_SIZE,
        gradient_accumulation_steps=GRADIENT_ACCUMULATION_STEPS,
        warmup_steps=WARMUP_STEPS,
        num_train_epochs=NUM_EPOCHS,
        learning_rate=LEARNING_RATE,
        logging_steps=LOGGING_STEPS,
        save_steps=SAVE_STEPS,
        eval_strategy="steps",
        eval_steps=SAVE_STEPS,
        optim="adamw_torch",
        fp16=not use_bf16,
        bf16=use_bf16,
        weight_decay=0.1,
        lr_scheduler_type="cosine",
        seed=42,
        report_to="none",
        remove_unused_columns=False,
        gradient_checkpointing=False,
        dataloader_pin_memory=True,
        beta=DPO_BETA,
        # DPO-specific settings
        generate_during_eval=False,
        precompute_ref_log_probs=False,
    ),
)

dpo_trainer.train()


# ============================================================
# STEP 5: Save
# ============================================================
print(f"\nSaving DPO model to {OUTPUT_DIR}/final...")
dpo_trainer.save_model(f"{OUTPUT_DIR}/final")
tokenizer.save_pretrained(f"{OUTPUT_DIR}/final")

print("\n" + "=" * 60)
print("DPO training complete!")
print("=" * 60)
print(f"""
Model saved to: {OUTPUT_DIR}/final

DPO beta (KL penalty): {DPO_BETA}
""")
