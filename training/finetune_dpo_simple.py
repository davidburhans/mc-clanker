#!/usr/bin/env python3
"""
Simple DPO Training - Custom implementation without trl dependencies.
"""

import torch
from datasets import load_from_disk
from transformers import AutoTokenizer, AutoModelForCausalLM
from torch.utils.data import DataLoader
import json
import os

# Config
MODEL_NAME = "Qwen/Qwen3.5-0.8B"
SFT_MODEL_PATH = "/training/outputs/qwen3.5-0.8b-mc-clanker/final"
DPO_TRAIN_DATA = "/training/data/dpo_dataset/train"
DPO_TEST_DATA = "/training/data/dpo_dataset/test"
OUTPUT_DIR = "/training/outputs/qwen3.5-0.8b-mc-clanker-dpo"

MAX_SEQ_LENGTH = 512
NUM_EPOCHS = 1
PER_DEVICE_BATCH_SIZE = 2
GRADIENT_ACCUMULATION_STEPS = 16
LEARNING_RATE = 1e-6
DPO_BETA = 0.1
USE_GRADIENT_CHECKPOINTING = False  # Disable for max compute

def compute_dpo_loss(policy_logps, ref_logps):
    """Compute DPO loss given log probabilities."""
    logits = policy_logps - ref_logps
    loss = -torch.nn.functional.logsigmoid(DPO_BETA * logits).mean()
    return loss

def main():
    print(f"GPU: {torch.cuda.get_device_name(0)}")
    vram_gb = torch.cuda.get_device_properties(0).total_memory / 1e9
    print(f"VRAM: {vram_gb:.1f} GB")

    torch.cuda.empty_cache()

    # Load tokenizer
    print("\n[1/5] Loading tokenizer...")
    tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)

    # Load DPO dataset
    print("\n[2/5] Loading DPO dataset...")
    train_ds = load_from_disk(DPO_TRAIN_DATA)
    eval_ds = load_from_disk(DPO_TEST_DATA)
    print(f"    Train pairs: {len(train_ds):,}")
    print(f"    Eval pairs:  {len(eval_ds):,}")

    # Load model
    print("\n[3/5] Loading model from SFT checkpoint...")
    model = AutoModelForCausalLM.from_pretrained(
        SFT_MODEL_PATH,
        torch_dtype=torch.bfloat16,
        device_map="auto",
        trust_remote_code=True,
    )
    if USE_GRADIENT_CHECKPOINTING:
        model.gradient_checkpointing_enable()
        print("    Gradient checkpointing enabled (memory-efficient)")
    else:
        print("    Gradient checkpointing disabled (max compute)")

    # Reference model on same GPU (frozen, no optimizer state, no grads)
    ref_model = AutoModelForCausalLM.from_pretrained(
        SFT_MODEL_PATH,
        torch_dtype=torch.bfloat16,
        device_map="auto",
        trust_remote_code=True,
    )
    ref_model.eval()
    print("    Reference model on GPU (frozen, no gradients)")

    total_params = sum(p.numel() for p in model.parameters())
    print(f"    Total parameters: {total_params:,}")

    # Prepare data collator
    def collate_fn(batch):
        chosen_ids = tokenizer([b['chosen'] for b in batch], return_tensors='pt', padding=True, truncation=True, max_length=MAX_SEQ_LENGTH)['input_ids']
        rejected_ids = tokenizer([b['rejected'] for b in batch], return_tensors='pt', padding=True, truncation=True, max_length=MAX_SEQ_LENGTH)['input_ids']
        return {
            'chosen_ids': chosen_ids,
            'rejected_ids': rejected_ids,
            'chosen_mask': (chosen_ids != tokenizer.pad_token_id).float(),
            'rejected_mask': (rejected_ids != tokenizer.pad_token_id).float(),
        }

    train_loader = DataLoader(train_ds, batch_size=PER_DEVICE_BATCH_SIZE, shuffle=True, collate_fn=collate_fn)

    # Optimizer
    optimizer = torch.optim.AdamW(model.parameters(), lr=LEARNING_RATE, weight_decay=0.1)

    # Training loop
    print("\n[4/5] Starting DPO training...")
    use_bf16 = torch.cuda.is_bf16_supported()
    print(f"    Precision: {'bf16' if use_bf16 else 'fp16'}")
    print(f"    Batch size: {PER_DEVICE_BATCH_SIZE} x {GRADIENT_ACCUMULATION_STEPS} = {PER_DEVICE_BATCH_SIZE * GRADIENT_ACCUMULATION_STEPS}")

    model.train()
    global_step = 0
    for epoch in range(NUM_EPOCHS):
        for batch_idx, batch in enumerate(train_loader):
            chosen_ids = batch['chosen_ids'].cuda()
            rejected_ids = batch['rejected_ids'].cuda()
            chosen_mask = batch['chosen_mask'].cuda()
            rejected_mask = batch['rejected_mask'].cuda()

            # Compute policy logps
            policy_chosen = model(input_ids=chosen_ids, attention_mask=chosen_mask)
            policy_rejected = model(input_ids=rejected_ids, attention_mask=rejected_mask)

            # Use last token logits only
            def get_seq_logps(logits, input_ids, attention_mask):
                # Shift for next-token prediction
                shift_logits = logits[:, :-1, :].contiguous()
                shift_labels = input_ids[:, 1:].contiguous()
                shift_mask = attention_mask[:, 1:].contiguous()
                log_probs = torch.log_softmax(shift_logits, dim=-1)
                token_logps = torch.gather(log_probs, 2, shift_labels.unsqueeze(-1)).squeeze(-1)
                return (token_logps * shift_mask).sum(dim=-1)

            policy_chosen_logps = get_seq_logps(policy_chosen.logits, chosen_ids, chosen_mask)
            policy_rejected_logps = get_seq_logps(policy_rejected.logits, rejected_ids, rejected_mask)

            # Compute reference logps (no grad, on CPU to save VRAM)
            with torch.no_grad():
                ref_chosen = ref_model(input_ids=chosen_ids, attention_mask=chosen_mask)
                ref_rejected = ref_model(input_ids=rejected_ids, attention_mask=rejected_mask)
                ref_chosen_logps = get_seq_logps(ref_chosen.logits, chosen_ids, chosen_mask)
                ref_rejected_logps = get_seq_logps(ref_rejected.logits, rejected_ids, rejected_mask)

            # DPO loss
            chosen_logps = policy_chosen_logps - ref_chosen_logps
            rejected_logps = policy_rejected_logps - ref_rejected_logps
            loss = -torch.nn.functional.logsigmoid(DPO_BETA * (chosen_logps - rejected_logps)).mean()

            # Backward
            loss.backward()

            if (batch_idx + 1) % GRADIENT_ACCUMULATION_STEPS == 0:
                torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                optimizer.step()
                optimizer.zero_grad()
                global_step += 1

                if global_step % 10 == 0:
                    print(f"  Step {global_step}: loss={loss.item():.4f}")

    # Save
    print(f"\n[5/5] Saving DPO model to {OUTPUT_DIR}/final...")
    model.save_pretrained(f"{OUTPUT_DIR}/final")
    tokenizer.save_pretrained(f"{OUTPUT_DIR}/final")
    print("\nDPO training complete!")

if __name__ == "__main__":
    main()
