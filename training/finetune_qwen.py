#!/usr/bin/env python3
"""
Full fine-tune Qwen3.5-0.8B on mc_clanker DJ Conductor data.

Usage:
    python finetune_qwen.py

Requirements:
    pip install unsloth torch trl datasets

Optimized for RTX 5090 (~32GB VRAM).
Full fine-tune: all parameters updated, no LoRA/QLORA.
"""

import torch
from datasets import load_from_disk
from transformers import AutoTokenizer
from unsloth import FastLanguageModel  # Must import before trl
from trl import SFTTrainer, SFTConfig

# Check GPU
if not torch.cuda.is_available():
    raise RuntimeError("CUDA GPU required for fine-tuning!")

print(f"GPU: {torch.cuda.get_device_name(0)}")
vram_gb = torch.cuda.get_device_properties(0).total_memory / 1e9
print(f"VRAM: {vram_gb:.1f} GB")

# Reset CUDA state to ensure clean start
torch.cuda.empty_cache()
torch.cuda.reset_peak_memory_stats()

# ============================================================
# CONFIGURATION - Full Fine-tune on RTX 5090
# ============================================================
MODEL_NAME = "Qwen/Qwen3.5-0.8B"
DATASET_PATH = "/training/data/unsloth_dataset"
OUTPUT_DIR = "/training/outputs/qwen3.5-0.8b-mc-clanker"

MAX_SEQ_LENGTH = 1024  # Reduced from 2048 to lower VRAM usage
NUM_EPOCHS = 3
PER_DEVICE_BATCH_SIZE = 4  # Reduced from 8 to be safer
GRADIENT_ACCUMULATION_STEPS = 8  # Effective batch = 32
LEARNING_RATE = 1e-5  # Lower LR for full fine-tune
WARMUP_RATIO = 0.1
LOGGING_STEPS = 10
SAVE_STEPS = 500

# ============================================================
# STEP 1: Load tokenizer
# ============================================================
print("\n[1/5] Loading tokenizer...")
tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)

# ============================================================
# STEP 2: Load and format dataset
# ============================================================
print(f"\n[2/5] Loading dataset from {DATASET_PATH}...")
dataset = load_from_disk(DATASET_PATH)
train_ds = dataset["train"]
eval_ds = dataset["test"]

print(f"    Train examples: {len(train_ds):,}")
print(f"    Eval examples:  {len(eval_ds):,}")

print("\n[3/5] Applying chat template...")


def format_conversation(example):
    messages = example["messages"]
    if not messages or messages[0].get("role") != "system":
        messages.insert(0, {"role": "system", "content": ""})
    text = tokenizer.apply_chat_template(
        messages,
        tokenize=False,
        add_generation_prompt=False,
    )
    return {"text": text}


train_ds = train_ds.map(format_conversation, remove_columns=["messages"])
eval_ds = eval_ds.map(format_conversation, remove_columns=["messages"])

print(f"    Sample length: {len(train_ds[0]['text']):,} chars")

# ============================================================
# STEP 3: Load model for FULL fine-tune (no quantization)
# ============================================================
print("\n[4/5] Loading model for full fine-tune...")

# Full fine-tune: load in 16-bit (bf16), no quantization
model, tokenizer = FastLanguageModel.from_pretrained(
    model_name=MODEL_NAME,
    max_seq_length=MAX_SEQ_LENGTH,
    load_in_4bit=False,
    load_in_8bit=False,
    load_in_16bit=False,  # Use full precision (bf16/fp16)
    full_finetuning=True,  # Enable full fine-tuning (all params)
    trust_remote_code=True,
    float32_mixed_precision=True,  # Force fp32 to avoid bf16 CUDA issues
)

total_params = sum(p.numel() for p in model.parameters())
print(f"    Total parameters: {total_params:,} (all trainable)")

# ============================================================
# STEP 4: Train
# ============================================================
print("\n[5/5] Starting full fine-tune...")

# Force fp16 to avoid potential bf16 compatibility issues on RTX 5090
use_bf16 = False
torch.cuda.empty_cache()
torch.cuda.synchronize()
print(f"    Precision: {'bf16' if use_bf16 else 'fp16'}")

trainer = SFTTrainer(
    model=model,
    train_dataset=train_ds,
    eval_dataset=eval_ds,
    args=SFTConfig(
        output_dir=OUTPUT_DIR,
        per_device_train_batch_size=PER_DEVICE_BATCH_SIZE,
        per_device_eval_batch_size=PER_DEVICE_BATCH_SIZE,
        gradient_accumulation_steps=GRADIENT_ACCUMULATION_STEPS,
        warmup_ratio=WARMUP_RATIO,
        num_train_epochs=NUM_EPOCHS,
        learning_rate=LEARNING_RATE,
        logging_steps=LOGGING_STEPS,
        save_steps=SAVE_STEPS,
        eval_strategy="steps",
        eval_steps=SAVE_STEPS,
        optim="adamw_8bit",
        fp16=not use_bf16,
        bf16=use_bf16,
        weight_decay=0.1,
        lr_scheduler_type="cosine",
        seed=42,
        report_to="none",
        remove_unused_columns=False,
        gradient_checkpointing=False,  # Disable to avoid CUDA sync issues
    ),
)

trainer.train()

# ============================================================
# STEP 5: Save
# ============================================================
print(f"\nSaving full model to {OUTPUT_DIR}/final/...")
model.save_pretrained(f"{OUTPUT_DIR}/final")
tokenizer.save_pretrained(f"{OUTPUT_DIR}/final")

print("\n" + "=" * 60)
print("Full fine-tune complete!")
print("=" * 60)
print(f"""
Model saved to: {OUTPUT_DIR}/final/

Effective batch size: {PER_DEVICE_BATCH_SIZE * GRADIENT_ACCUMULATION_STEPS}
Precision: {"bf16" if use_bf16 else "fp16"}
Full fine-tune: all {total_params:,} parameters updated
""")
