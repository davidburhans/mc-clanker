#!/bin/bash
export MSYS_NO_PATHCONV=1
podman run --rm \
  --gpus all \
  -e NVIDIA_VISIBLE_DEVICES=all \
  -e CUDA_VISIBLE_DEVICES=0 \
  -e MODEL_NAME=Qwen/Qwen3.5-0.8B \
  -e OUTPUT_DIR=/training/outputs \
  -e MAX_SEQ_LENGTH=2048 \
  -e NUM_EPOCHS=3 \
  -e PER_DEVICE_BATCH_SIZE=8 \
  -e GRADIENT_ACCUMULATION_STEPS=4 \
  -e LEARNING_RATE=1e-5 \
  -v /c/slop/mc-clanker/training/data:/training/data:ro \
  -v /c/slop/mc-clanker/training/outputs:/training/outputs:rw \
  -v /c/Users/Dave/.cache/huggingface:/root/.cache/huggingface:rw \
  --name mcclanker-training \
  localhost/mcclanker/training:latest
