#!/usr/bin/env python3
"""
GPU Utilization Benchmark for Qwen3.5-0.8B Fine-tuning
Tests different batch sizes and gradient accumulation to find optimal config.
"""

import gc
import time
import torch
from datasets import load_from_disk
from transformers import AutoTokenizer, AutoModelForCausalLM
from trl import SFTTrainer, SFTConfig

# Check GPU
if not torch.cuda.is_available():
    raise RuntimeError("CUDA GPU required!")

print(f"GPU: {torch.cuda.get_device_name(0)}")
total_vram = torch.cuda.get_device_properties(0).total_memory / 1e9
print(f"Total VRAM: {total_vram:.1f} GB")
print(f"Usable VRAM: ~30 GB (monitor overhead)\n")

torch.cuda.empty_cache()

# ============================================================
# Load tokenizer
# ============================================================
MODEL_NAME = "Qwen/Qwen3.5-0.8B"
DATASET_PATH = "/training/data/unsloth_dataset"

print("[1/3] Loading tokenizer...")
tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)

print("[2/3] Loading dataset...")
dataset = load_from_disk(DATASET_PATH)
train_ds = dataset["train"]


# Format a small subset for benchmarking
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
# Use only first 500 samples for quick benchmark
train_ds = train_ds.select(range(500))

print(f"    Benchmark dataset: {len(train_ds)} samples\n")

# ============================================================
# Configurations to test
# ============================================================
configs = [
    # Batch size experiments (baseline - same as current)
    {
        "name": "bs=2, grad_acc=16 (baseline)",
        "per_device_batch_size": 2,
        "gradient_accumulation_steps": 16,
        "gradient_checkpointing": False,
    },
    # Increase batch size, reduce accumulation
    {
        "name": "bs=4, grad_acc=8",
        "per_device_batch_size": 4,
        "gradient_accumulation_steps": 8,
        "gradient_checkpointing": False,
    },
    {
        "name": "bs=8, grad_acc=4",
        "per_device_batch_size": 8,
        "gradient_accumulation_steps": 4,
        "gradient_checkpointing": False,
    },
    {
        "name": "bs=16, grad_acc=2",
        "per_device_batch_size": 16,
        "gradient_accumulation_steps": 2,
        "gradient_checkpointing": False,
    },
    {
        "name": "bs=32, grad_acc=1",
        "per_device_batch_size": 32,
        "gradient_accumulation_steps": 1,
        "gradient_checkpointing": False,
    },
    # With gradient checkpointing
    {
        "name": "bs=8, grad_acc=4, gc=True",
        "per_device_batch_size": 8,
        "gradient_accumulation_steps": 4,
        "gradient_checkpointing": True,
    },
    {
        "name": "bs=16, grad_acc=2, gc=True",
        "per_device_batch_size": 16,
        "gradient_accumulation_steps": 2,
        "gradient_checkpointing": True,
    },
    {
        "name": "bs=32, grad_acc=1, gc=True",
        "per_device_batch_size": 32,
        "gradient_accumulation_steps": 1,
        "gradient_checkpointing": True,
    },
]

print("[3/3] Running benchmarks...\n")
print("=" * 85)
print(f"{'Config':<50} {'VRAM':>8} {'Time/Step':>10} {'Eff Batch':>10}")
print("=" * 85)

results = []

for i, cfg in enumerate(configs):
    config_name = cfg["name"]
    print(f"\n[{i + 1}/{len(configs)}] Testing: {config_name}")

    # Clean up previous model
    if "model" in dir():
        del model
    torch.cuda.empty_cache()
    gc.collect()

    # Load model
    model = AutoModelForCausalLM.from_pretrained(
        MODEL_NAME,
        torch_dtype=torch.bfloat16,
        device_map="auto",
        trust_remote_code=True,
    )

    # Get VRAM usage after loading
    vram_allocated = torch.cuda.memory_allocated() / 1e9
    print(f"    Model loaded: {vram_allocated:.1f}GB allocated")

    # Create trainer
    trainer = SFTTrainer(
        model=model,
        train_dataset=train_ds,
        processing_class=tokenizer,
        args=SFTConfig(
            output_dir="/tmp/bench_output",
            per_device_train_batch_size=cfg["per_device_batch_size"],
            gradient_accumulation_steps=cfg["gradient_accumulation_steps"],
            num_train_epochs=1,
            learning_rate=1e-5,
            warmup_steps=10,
            logging_steps=10,
            save_steps=500,
            eval_strategy="no",
            optim="adamw_torch",
            bf16=True,
            gradient_checkpointing=cfg["gradient_checkpointing"],
            dataloader_pin_memory=True,
            report_to="none",
        ),
    )

    # Warmup
    print(f"    Warming up (3 steps)...")
    for _ in range(3):
        trainer.training_step(model, trainer.train_dataset[0])

    # Benchmark
    steps_to_measure = 10
    print(f"    Benchmarking ({steps_to_measure} steps)...")
    start_time = time.time()
    for _ in range(steps_to_measure):
        trainer.training_step(model, trainer.train_dataset[0])
    elapsed = time.time() - start_time
    time_per_step = elapsed / steps_to_measure

    # Get peak VRAM
    peak_vram = torch.cuda.max_memory_allocated() / 1e9
    torch.cuda.reset_peak_memory_stats()

    effective_batch = cfg["per_device_batch_size"] * cfg["gradient_accumulation_steps"]
    print(f"    Results: {time_per_step:.3f}s/step, Peak VRAM: {peak_vram:.1f}GB, Eff Batch: {effective_batch}")

    results.append(
        {
            "name": config_name,
            "vram_gb": peak_vram,
            "time_per_step": time_per_step,
            "per_device_batch_size": cfg["per_device_batch_size"],
            "gradient_accumulation_steps": cfg["gradient_accumulation_steps"],
            "gradient_checkpointing": cfg["gradient_checkpointing"],
            "effective_batch": effective_batch,
        }
    )

    print("-" * 85)

# ============================================================
# Summary
# ============================================================
print("\n\n" + "=" * 85)
print("BENCHMARK RESULTS (sorted by throughput - best first)")
print("=" * 85)
print(f"{'Config':<50} {'VRAM':>8} {'Time/Step':>10} {'Eff Batch':>10} {'Throughput':>12}")
print("-" * 85)

# Calculate throughput for each
for r in results:
    r["throughput"] = r["effective_batch"] / r["time_per_step"]

results.sort(key=lambda x: x["throughput"], reverse=True)

for r in results:
    vram_str = f"{r['vram_gb']:.1f}GB"
    time_str = f"{r['time_per_step']:.3f}s"
    throughput_str = f"{r['throughput']:.1f} samples/s"
    print(f"{r['name']:<50} {vram_str:>8} {time_str:>10} {r['effective_batch']:>10} {throughput_str:>12}")

# Check which configs fit in 30GB
print("\n\n" + "=" * 85)
print("CONFIGS THAT FIT IN ~30GB VRAM (sorted by throughput)")
print("=" * 85)
fit_results = [r for r in results if r["vram_gb"] <= 30.5]

if fit_results:
    print(f"{'Config':<50} {'VRAM':>8} {'Time/Step':>10} {'Eff Batch':>10} {'Throughput':>12}")
    print("-" * 85)
    for r in fit_results:
        vram_str = f"{r['vram_gb']:.1f}GB"
        time_str = f"{r['time_per_step']:.3f}s"
        throughput_str = f"{r['throughput']:.1f} samples/s"
        print(f"{r['name']:<50} {vram_str:>8} {time_str:>10} {r['effective_batch']:>10} {throughput_str:>12}")

    best = fit_results[0]
    print(f"\n\n*** BEST CONFIG: {best['name']} ***")
    print(f"    VRAM Usage: {best['vram_gb']:.1f}GB")
    print(f"    Time per step: {best['time_per_step']:.3f}s")
    print(f"    Effective batch size: {best['effective_batch']}")
    print(f"    Throughput: {best['throughput']:.1f} samples/s")
    print(f"\nRecommended finetune_raw.py settings:")
    print(f"    PER_DEVICE_BATCH_SIZE = {best['per_device_batch_size']}")
    print(f"    GRADIENT_ACCUMULATION_STEPS = {best['gradient_accumulation_steps']}")
    print(f"    gradient_checkpointing = {best['gradient_checkpointing']}")
else:
    print("No configs fit in 30GB VRAM - need to reduce batch size")
