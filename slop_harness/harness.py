"""Dataset generation harness — CLI entrypoint.

Bulk-generates Conductor prompt/response pairs using deterministic seeds
and async concurrent LLM calls.
"""
import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime
from random import Random

from slop_harness.checkpoint import CheckpointManager
from slop_harness.dataset_writer import DatasetWriter
from slop_harness.llm_client import LLMClient
from slop_harness.prompt_builder import PromptBuilder
from slop_harness.state_generator import StateGenerator
from slop_harness.vibe_prompt_bank import VibePromptBank


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate Conductor fine-tuning dataset"
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("LLM_BASE_URL", "http://localhost:1234/v1"),
        help="LLM API base URL",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("LLM_MODEL", "local-model"),
        help="LLM model name",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=int(os.environ.get("BATCH_SIZE", "1000")),
        help="Interactions per batch file",
    )
    parser.add_argument(
        "--total",
        type=int,
        default=int(os.environ.get("TOTAL_INTERACTIONS", "100000")),
        help="Total interactions to generate",
    )
    parser.add_argument(
        "--output-dir",
        default=os.environ.get("OUTPUT_DIR", "./data"),
        help="Output directory",
    )
    parser.add_argument(
        "--concurrent",
        type=int,
        default=int(os.environ.get("CONCURRENT_REQUESTS", "20")),
        help="Max concurrent LLM calls",
    )
    parser.add_argument(
        "--vibe-prob",
        type=float,
        default=float(os.environ.get("VIBE_PROB", "0.05")),
        help="Probability of vibe override (0.0-1.0)",
    )
    return parser.parse_args()


async def generate_one(
    client: LLMClient,
    batch_id: int,
    interaction_id: int,
    vibe_prob: float,
    semaphore: asyncio.Semaphore,
) -> dict | None:
    """Generate a single interaction. Returns the JSON dict or None on failure."""
    # Build deterministic seed
    loop_seed = (batch_id << 20) | interaction_id
    rng = Random(loop_seed)

    # Generate musical state
    state = StateGenerator(batch_id=batch_id, interaction_id=interaction_id).build()

    # Maybe add vibe override
    override = None
    if rng.random() < vibe_prob:
        override = VibePromptBank().sample(rng)

    # Build prompt
    messages = PromptBuilder().build(state, override)

    # Call LLM with semaphore limiting
    async with semaphore:
        try:
            response = await client.call(messages)
        except Exception as e:
            logger.error(f"LLM call failed for batch={batch_id} i={interaction_id}: {e}")
            return None

    return {"messages": messages, "response": response}


async def run_batch(
    batch_id: int,
    batch_size: int,
    client: LLMClient,
    writer: DatasetWriter,
    vibe_prob: float,
    concurrent: int,
) -> int:
    """Run one batch. Returns number of successfully written records."""
    semaphore = asyncio.Semaphore(concurrent)

    async def safe_generate(i: int):
        result = await generate_one(client, batch_id, i, vibe_prob, semaphore)
        return result

    tasks = [safe_generate(i) for i in range(batch_size)]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    written = 0
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            logger.error(f"Task exception batch={batch_id} i={i}: {result}")
            continue
        if not isinstance(result, dict):
            # LLM returned None or unexpected type
            continue

        # Extract the three messages + response
        messages = result["messages"]
        response = result["response"]
        # Final record format: system, user, assistant(with LLM response)
        record = {
            "messages": [
                messages[0],  # system
                messages[1],  # user
                {"role": "assistant", "content": response},
            ]
        }
        writer.write(record)
        written += 1

    return written


async def main_async(args: argparse.Namespace) -> None:
    """Main async entrypoint."""
    logger.info(f"Starting dataset generation")
    logger.info(f"  Base URL:  {args.base_url}")
    logger.info(f"  Model:     {args.model}")
    logger.info(f"  Batch size: {args.batch_size}")
    logger.info(f"  Total:     {args.total}")
    logger.info(f"  Output:    {args.output_dir}")
    logger.info(f"  Concurrent: {args.concurrent}")
    logger.info(f"  Vibe prob: {args.vibe_prob}")

    checkpoint_path = os.path.join(args.output_dir, "checkpoint.json")
    os.makedirs(args.output_dir, exist_ok=True)

    ckpt = CheckpointManager(checkpoint_path)
    writer = DatasetWriter(args.output_dir, batch_size=args.batch_size)

    initial = ckpt.load()
    start_batch = initial["batch_id"]
    total_done = initial["total"]

    logger.info(f"Resuming from batch {start_batch}, {total_done} total written")

    async with LLMClient(base_url=args.base_url, model=args.model) as client:
        current_batch = start_batch
        while total_done < args.total:
            interactions_in_batch = min(args.batch_size, args.total - total_done)
            if interactions_in_batch <= 0:
                break

            batch_start = datetime.now()
            written = await run_batch(
                batch_id=current_batch,
                batch_size=interactions_in_batch,
                client=client,
                writer=writer,
                vibe_prob=args.vibe_prob,
                concurrent=args.concurrent,
            )

            ckpt.increment(written)
            total_done += written
            current_batch += 1

            elapsed = (datetime.now() - batch_start).total_seconds()
            rate = written / elapsed if elapsed > 0 else 0
            logger.info(
                f"Batch {current_batch-1} done: {written}/{interactions_in_batch} written "
                f"({elapsed:.1f}s, {rate:.1f} int/s)"
            )

    writer.close()
    logger.info(f"Complete! {total_done} total interactions written to {args.output_dir}")


def main() -> None:
    args = parse_args()
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        logger.info("Interrupted — checkpoint saved, will resume on next run.")
        sys.exit(0)


if __name__ == "__main__":
    main()
