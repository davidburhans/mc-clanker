"""simulation CLI — orchestrates concurrent SlopJockey simulations.

Mirrors slop_harness/harness.py CLI interface. Each jockey reuses
production ConductorLLMAsync. No DB, no audio generation.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import random
import sys
from datetime import datetime

from slop_harness.dataset_writer import DatasetWriter

from simulation.jockey import SlopJockey


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Simulate stateful Slop Jockey sessions (reuses production ConductorLLMAsync)"
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
        "--jockeys",
        type=int,
        default=int(os.environ.get("JOCKEYS", "2048")),
        help="Number of concurrent jockeys (default 2048)",
    )
    parser.add_argument(
        "--performances",
        type=int,
        default=int(os.environ.get("PERFORMANCES", "8")),
        help="Number of separate sessions each jockey runs (default 8)",
    )
    parser.add_argument(
        "--run-seed",
        type=int,
        default=None,
        help="Seed for loop count per jockey. If omitted, a random seed is "
        "generated and logged so the run can be reproduced exactly.",
    )
    parser.add_argument(
        "--min-loops",
        type=int,
        default=int(os.environ.get("MIN_LOOPS", "96")),
        help="Minimum loops per performance (default 96)",
    )
    parser.add_argument(
        "--max-loops",
        type=int,
        default=int(os.environ.get("MAX_LOOPS", "256")),
        help="Maximum loops per performance (default 256)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=int(os.environ.get("BATCH_SIZE", "1000")),
        help="Records per output batch file",
    )
    parser.add_argument(
        "--output-dir",
        default=os.environ.get("OUTPUT_DIR", "./sim_data"),
        help="Output directory",
    )
    parser.add_argument(
        "--concurrent",
        type=int,
        default=int(os.environ.get("CONCURRENT_REQUESTS", "128")),
        help="Max concurrent LLM calls across all jockeys",
    )
    parser.add_argument(
        "--vibe-prob",
        type=float,
        default=float(os.environ.get("VIBE_PROB", "0.15")),
        help="Probability per loop of setting a persistent vibe override",
    )
    parser.add_argument(
        "--vibe-clear-prob",
        type=float,
        default=float(os.environ.get("VIBE_CLEAR_PROB", "0.05")),
        help="Probability per loop of clearing the current vibe override",
    )
    parser.add_argument(
        "--enable-thinking",
        action="store_true",
        default=False,
        help="Pass enable_thinking=True to the model (Qwen3.5-27B only)",
    )
    return parser.parse_args()


async def run_jockey(
    jockey_id: int,
    perf_id: int,
    run_seed: int,
    semaphore: asyncio.Semaphore,
    min_loops: int,
    max_loops: int,
    vibe_prob: float,
    vibe_clear_prob: float,
    llm_base_url: str,
    llm_model: str,
    enable_thinking: bool,
) -> list[dict]:
    jockey = SlopJockey(
        jockey_id=jockey_id,
        perf_id=perf_id,
        run_seed=run_seed,
        min_loops=min_loops,
        max_loops=max_loops,
        vibe_prob=vibe_prob,
        vibe_clear_prob=vibe_clear_prob,
        llm_base_url=llm_base_url,
        llm_model=llm_model,
        enable_thinking=enable_thinking,
    )
    return await jockey.run(semaphore)


async def main_async(args: argparse.Namespace) -> None:
    logger.info(f"Starting jockey simulation (production ConductorLLMAsync)")
    logger.info(f"  Jockeys:    {args.jockeys}")
    logger.info(f"  Loops:      {args.min_loops}–{args.max_loops} per jockey")
    logger.info(f"  Base URL:  {args.base_url}")
    logger.info(f"  Model:     {args.model}")
    logger.info(f"  Concurrent: {args.concurrent}")
    logger.info(f"  Vibe prob: {args.vibe_prob}")
    logger.info(f"  Vibe clear prob: {args.vibe_clear_prob}")
    logger.info(f"  Output:    {args.output_dir}")

    os.makedirs(args.output_dir, exist_ok=True)
    writer = DatasetWriter(args.output_dir, batch_size=args.batch_size)

    # Resolve and persist run_seed (try resume first, else generate)
    run_seed_path = os.path.join(args.output_dir, "run_seed.txt")
    saved_run_seed = None
    if os.path.exists(run_seed_path):
        with open(run_seed_path) as f:
            saved_run_seed = int(f.read().strip())
    if args.run_seed is None and saved_run_seed is not None:
        args.run_seed = saved_run_seed
    elif args.run_seed is None:
        args.run_seed = random.randint(0, 2**31 - 1)
    with open(run_seed_path, "w") as f:
        f.write(str(args.run_seed))
    logger.info(f"  Run seed:  {args.run_seed}  (use --run-seed to reproduce)")

    # Session checkpoint: tracks completed sessions and total records for crash-safe resume
    sessions_completed_path = os.path.join(args.output_dir, "sessions_completed.txt")
    total_records_path = os.path.join(args.output_dir, "total_records.txt")
    sessions_completed = 0
    total_done = 0
    if os.path.exists(sessions_completed_path):
        with open(sessions_completed_path) as f:
            sessions_completed = int(f.read().strip())
    if os.path.exists(total_records_path):
        with open(total_records_path) as f:
            total_done = int(f.read().strip())

    start_session = sessions_completed

    logger.info(f"  Performances: {args.performances} per jockey")
    logger.info(f"  Total sessions: {args.jockeys * args.performances}")
    logger.info(f"  Resuming from session {start_session}")

    semaphore = asyncio.Semaphore(args.concurrent)

    # Flatten sessions: each jockey runs `performances` separate sessions.
    # Order: all jockeys' performance 0, then all jockeys' performance 1, etc.
    # This gives a good distribution of jockey tastes running concurrently.
    performances = args.performances
    total_sessions = args.jockeys * performances

    pending_tasks: dict[asyncio.Task, tuple[int, int]] = {}  # task → (jockey_id, perf_id)
    next_session = start_session
    sessions_finished = sessions_completed  # Resume from checkpoint
    batch_start_time = datetime.now()
    last_log_time = datetime.now()

    def session_from_counter(counter: int) -> tuple[int, int]:
        jockey_id = counter // performances
        perf_id = counter % performances
        return jockey_id, perf_id

    # Seed initial batch — fill all concurrent slots
    for _ in range(min(args.concurrent, total_sessions - start_session)):
        jockey_id, perf_id = session_from_counter(next_session)
        next_session += 1
        task = asyncio.create_task(
            run_jockey(
                jockey_id=jockey_id,
                perf_id=perf_id,
                run_seed=args.run_seed,
                semaphore=semaphore,
                min_loops=args.min_loops,
                max_loops=args.max_loops,
                vibe_prob=args.vibe_prob,
                vibe_clear_prob=args.vibe_clear_prob,
                llm_base_url=args.base_url,
                llm_model=args.model,
                enable_thinking=args.enable_thinking,
            )
        )
        pending_tasks[task] = (jockey_id, perf_id)

    # Process completions — dynamic reaping keeps 128 running until all done
    while pending_tasks:
        done, still_pending = await asyncio.wait(list(pending_tasks.keys()), return_when=asyncio.FIRST_COMPLETED)
        # Save metadata for done tasks BEFORE rebuilding pending_tasks
        done_metadata = {task: pending_tasks[task] for task in done}
        # Rebuild pending_tasks dict from still_pending set
        pending_tasks = {t: pending_tasks[t] for t in still_pending}

        for task in done:
            jockey_id, perf_id = done_metadata[task]
            all_records: list[dict] = []
            try:
                records = task.result()
                all_records = records
            except Exception as e:
                logger.error(f"Session jockey={jockey_id} perf={perf_id} raised: {e}")

            for record in all_records:
                writer.write(record)
            total_done += len(all_records)
            sessions_finished += 1

            # Persist progress for crash-safe resume
            with open(sessions_completed_path, "w") as f:
                f.write(str(sessions_finished))
            with open(total_records_path, "w") as f:
                f.write(str(total_done))

            # Spawn replacement if more sessions remain
            if next_session < total_sessions:
                jockey_id_new, perf_id_new = session_from_counter(next_session)
                next_session += 1
                new_task = asyncio.create_task(
                    run_jockey(
                        jockey_id=jockey_id_new,
                        perf_id=perf_id_new,
                        run_seed=args.run_seed,
                        semaphore=semaphore,
                        min_loops=args.min_loops,
                        max_loops=args.max_loops,
                        vibe_prob=args.vibe_prob,
                        vibe_clear_prob=args.vibe_clear_prob,
                        llm_base_url=args.base_url,
                        llm_model=args.model,
                        enable_thinking=args.enable_thinking,
                    )
                )
                pending_tasks[new_task] = (jockey_id_new, perf_id_new)

            # Progress log every ~30 seconds
            now = datetime.now()
            if (now - last_log_time).total_seconds() >= 30:
                elapsed = (now - batch_start_time).total_seconds()
                rate = total_done / elapsed if elapsed > 0 else 0
                pct = (sessions_finished / total_sessions) * 100
                logger.info(
                    f"Progress: {sessions_finished}/{total_sessions} sessions done "
                    f"({pct:.0f}%, {total_done} records, {rate:.1f} rec/s)"
                )
                last_log_time = now

    writer.close()
    logger.info(f"Complete! {total_done} total records written to {args.output_dir}")


def main() -> None:
    args = parse_args()
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        logger.info("Interrupted — checkpoint saved, will resume on next run.")
        sys.exit(0)


if __name__ == "__main__":
    main()
