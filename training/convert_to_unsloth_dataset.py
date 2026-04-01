#!/usr/bin/env python3
"""
Convert mc_clanker JSONL data to an Unsloth-compatible dataset for fine-tuning.

Usage:
    python convert_to_unsloth_dataset.py

Output:
    data/unsloth_dataset/ - HuggingFace dataset ready for Unsloth fine-tuning
"""

import json
import glob
from pathlib import Path
from datasets import Dataset


def load_jsonl_files(file_pattern: str) -> list[dict]:
    """Load all JSONL files matching the pattern."""
    all_data = []
    files = sorted(glob.glob(file_pattern))

    print(f"Found {len(files)} JSONL files")

    for file_path in files:
        with open(file_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        data = json.loads(line)
                        if 'messages' in data and len(data['messages']) > 0:
                            all_data.append(data)
                    except json.JSONDecodeError as e:
                        print(f"Warning: Skipping invalid JSON line in {file_path}: {e}")

    return all_data


def convert_to_unsloth_format(example, tokenizer=None, chat_template: str = None):
    """
    Convert an example to Unsloth format.

    Unsloth expects either:
    1. A 'messages' column with conversation messages
    2. A 'text' column with chat-template-formatted string
    """
    messages = example['messages']

    # Ensure system message exists (Unsloth requirement)
    if not messages or messages[0].get('role') != 'system':
        messages.insert(0, {"role": "system", "content": ""})

    if tokenizer is not None and chat_template is not None:
        # Apply chat template to convert messages to text
        example['text'] = tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=False
        )
    else:
        # Keep as messages format (Unsloth will apply template internally)
        example['text'] = None

    return example


def main():
    # Configuration
    input_pattern = "/training/data/slop_batch_*.jsonl"
    output_dir = Path("/training/data/unsloth_dataset")

    print("=" * 60)
    print("mc_clanker -> Unsloth Dataset Converter")
    print("=" * 60)

    # Step 1: Load all JSONL files
    print(f"\n[1/4] Loading data from {input_pattern}...")
    all_data = load_jsonl_files(input_pattern)
    print(f"      Loaded {len(all_data):,} valid examples")

    if len(all_data) == 0:
        print("Error: No valid data found!")
        return

    # Step 2: Create HuggingFace Dataset
    print("\n[2/4] Creating HuggingFace Dataset...")
    dataset = Dataset.from_list(all_data)
    print(f"      Dataset size: {len(dataset):,} examples")
    print(f"      Columns: {dataset.column_names}")

    # Step 3: Split into train/validation sets
    print("\n[3/4] Splitting into train/validation sets (95/5)...")
    split_dataset = dataset.train_test_split(test_size=0.05, shuffle=True, seed=42)
    print(f"      Train: {len(split_dataset['train']):,} examples")
    print(f"      Validation: {len(split_dataset['test']):,} examples")

    # Step 4: Save dataset
    print(f"\n[4/4] Saving to {output_dir}...")
    output_dir.mkdir(parents=True, exist_ok=True)
    split_dataset.save_to_disk(output_dir)

    print("\n" + "=" * 60)
    print("Conversion complete!")
    print("=" * 60)
    print(f"""
To use with Unsloth Studio:

1. Load the dataset:
   from datasets import load_from_disk
   dataset = load_from_disk("{output_dir}")

2. In Unsloth Studio, point to this dataset directory

3. The dataset has:
   - 'train' split: {len(split_dataset['train']):,} examples
   - 'test' split: {len(split_dataset['test']):,} examples
   - Each example contains 'messages' array with role/content pairs

4. Example message structure:
   - role: 'system' (DJ system prompt)
   - role: 'user' (Current state context)
   - role: 'assistant' (DJ actions JSON response)
""")

    # Show sample
    print("\n--- Sample Example ---")
    sample = dataset[0]
    print(f"Number of messages: {len(sample['messages'])}")
    for i, msg in enumerate(sample['messages']):
        role = msg['role']
        content_preview = msg['content'][:100] + "..." if len(msg['content']) > 100 else msg['content']
        print(f"  [{i}] {role}: {content_preview}")


if __name__ == "__main__":
    main()
