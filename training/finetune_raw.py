#!/usr/bin/env python3
"""
Full fine-tune Qwen3.5-0.8B on mc_clanker DJ Conductor data.
Using raw transformers + TRL (no Unsloth) to avoid compatibility issues.
"""

import torch
from datasets import load_from_disk
from transformers import AutoTokenizer, AutoModelForCausalLM
from trl import SFTTrainer, SFTConfig

# Check GPU
if not torch.cuda.is_available():
    raise RuntimeError("CUDA GPU required for fine-tuning!")

print(f"GPU: {torch.cuda.get_device_name(0)}")
vram_gb = torch.cuda.get_device_properties(0).total_memory / 1e9
print(f"VRAM: {vram_gb:.1f} GB")

torch.cuda.empty_cache()
torch.cuda.reset_peak_memory_stats()

# ============================================================
# CONFIGURATION
# ============================================================
MODEL_NAME = "Qwen/Qwen3.5-0.8B"
DATASET_PATH = "/training/data/unsloth_dataset"
OUTPUT_DIR = "/training/outputs/qwen3.5-0.8b-mc-clanker"

MAX_SEQ_LENGTH = 1024
NUM_EPOCHS = 1
PER_DEVICE_BATCH_SIZE = 2
GRADIENT_ACCUMULATION_STEPS = 16  # Effective batch = 32
LEARNING_RATE = 1e-5
WARMUP_STEPS = 100
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

# Load model in bfloat16 (more stable than float16 on Blackwell)
model = AutoModelForCausalLM.from_pretrained(
    MODEL_NAME,
    torch_dtype=torch.bfloat16,
    device_map="auto",
    trust_remote_code=True,
)

total_params = sum(p.numel() for p in model.parameters())
trainable_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
print(f"    Total parameters: {total_params:,}")
print(f"    Trainable parameters: {trainable_params:,}")

# ============================================================
# STEP 4: Train
# ============================================================
print("\n[5/5] Starting full fine-tune...")

use_bf16 = torch.cuda.is_bf16_supported()
print(f"    Precision: {'bf16' if use_bf16 else 'fp16'}")

trainer = SFTTrainer(
    model=model,
    train_dataset=train_ds,
    eval_dataset=eval_ds,
    processing_class=tokenizer,
    args=SFTConfig(
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
        resume_from_checkpoint=True,
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
    ),
)

trainer.train()

# ============================================================
# STEP 5: Save
# ============================================================
print(f"\nSaving full model to {OUTPUT_DIR}/final/...")
trainer.save_model(f"{OUTPUT_DIR}/final")
tokenizer.save_pretrained(f"{OUTPUT_DIR}/final")

print("\n" + "=" * 60)
print("Full fine-tune complete!")
print("=" * 60)
print(f"""
Model saved to: {OUTPUT_DIR}/final/

Effective batch size: {PER_DEVICE_BATCH_SIZE * GRADIENT_ACCUMULATION_STEPS}
Precision: {'bf16' if use_bf16 else 'fp16'}
""")