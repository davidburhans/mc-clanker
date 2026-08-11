#!/usr/bin/env python3
"""
Generate DPO preference pairs from SFT dataset.

This creates chosen/rejected pairs by:
- chosen: original valid Conductor JSON
- rejected: intentionally corrupted version (missing fields, invalid enums, etc.)

Run this BEFORE finetune_dpo.py to prepare the DPO dataset.
"""

import argparse
from datasets import load_from_disk, Dataset

import sys

sys.path.insert(0, "/training")
from dpo_pipeline import generate_preference_pairs


def main():
    parser = argparse.ArgumentParser(description="Generate DPO dataset from SFT data")
    parser.add_argument("--sft-path", default="/training/data/unsloth_dataset", help="Path to SFT dataset")
    parser.add_argument("--output-path", default="/training/data/dpo_dataset", help="Output path for DPO dataset")
    parser.add_argument(
        "--corruption-types",
        nargs="+",
        default=["missing_field", "invalid_enum", "invalid_bpm"],
        help="Corruption strategies to apply",
    )
    parser.add_argument("--num-corruptions", type=int, default=1, help="Number of corruption pairs per sample")
    parser.add_argument("--max-samples", type=int, default=None, help="Max samples to process (None = all)")
    args = parser.parse_args()

    print(f"[1/4] Loading SFT dataset from {args.sft_path}...")
    dataset = load_from_disk(args.sft_path)
    train_ds = dataset["train"]
    eval_ds = dataset["test"]

    if args.max_samples:
        train_ds = train_ds.select(range(min(args.max_samples, len(train_ds))))

    print(f"    Train samples: {len(train_ds):,}")
    print(f"    Eval samples: {len(eval_ds):,}")

    print(f"\n[2/4] Generating train pairs...")
    train_pairs = generate_preference_pairs(
        list(train_ds), corruption_types=args.corruption_types, num_corruptions_per_sample=args.num_corruptions
    )
    print(f"    Generated {len(train_pairs):,} train pairs")

    print(f"\n[3/4] Generating eval pairs...")
    eval_pairs = generate_preference_pairs(
        list(eval_ds), corruption_types=args.corruption_types, num_corruptions_per_sample=args.num_corruptions
    )
    print(f"    Generated {len(eval_pairs):,} eval pairs")

    print(f"\n[4/4] Saving DPO dataset to {args.output_path}...")

    # Convert to DPO format (HuggingFace DPO expects 'chosen' and 'rejected' keys)
    def pairs_to_dpo_dataset(pairs):
        return Dataset.from_list([{"chosen": chosen, "rejected": rejected} for chosen, rejected in pairs])

    train_dpo = pairs_to_dpo_dataset(train_pairs)
    eval_dpo = pairs_to_dpo_dataset(eval_pairs)

    dpo_dataset = {
        "train": train_dpo,
        "test": eval_dpo,
    }

    # Save as Arrow format
    from datasets import DatasetDict

    dpo_dict = DatasetDict(dpo_dataset)
    dpo_dict.save_to_disk(args.output_path)

    print(f"\n    Saved to {args.output_path}/")
    print(f"    Train: {len(train_dpo):,} pairs")
    print(f"    Test:  {len(eval_dpo):,} pairs")

    # Show example
    print("\n" + "=" * 60)
    print("Example DPO pair:")
    print("=" * 60)
    example = train_dpo[0]
    print(f"\n[CHOSEN - valid]:\n{json.dumps(json.loads(example['chosen']), indent=2)[:500]}...")
    print(f"\n[REJECTED - corrupted]:\n{json.dumps(json.loads(example['rejected']), indent=2)[:500]}...")


if __name__ == "__main__":
    import json

    main()
