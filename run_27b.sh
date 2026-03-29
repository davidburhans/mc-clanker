#!/usr/bin/env bash
set -euo pipefail

# 27B AWQ at 16 concurrent (~26 req/min, ~64 hours for 100k)
python -m slop_harness.harness \
  --base-url "http://127.0.0.1:1235/v1" \
  --model "cyankiwi/Qwen3.5-27B-AWQ-4bit" \
  --batch-size 1000 \
  --total 100000 \
  --output-dir ./data \
  --concurrent 16 \
  --vibe-prob 0.05
