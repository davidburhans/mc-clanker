#!/usr/bin/env bash
set -euo pipefail

# 9B AWQ at max throughput (~270 req/min, ~12.3 hours for 200k)
uv run python -m slop_harness.harness \
  --base-url "http://127.0.0.1:1235/v1" \
  --model "cyankiwi/Qwen3.5-9B-AWQ-4bit" \
  --batch-size 1000 \
  --total 200000 \
  --output-dir ./data \
  --concurrent 128 \
  --vibe-prob 0.05
