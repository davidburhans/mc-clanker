@echo off
podman run --rm --gpus all ^
  -e MODEL_NAME=Qwen/Qwen3.5-0.8B ^
  -e OUTPUT_DIR=/training/outputs ^
  -e MAX_SEQ_LENGTH=2048 ^
  -e NUM_EPOCHS=3 ^
  -e PER_DEVICE_BATCH_SIZE=8 ^
  -e GRADIENT_ACCUMULATION_STEPS=4 ^
  -e LEARNING_RATE=1e-5 ^
  -v C:\slop\mc-clanker\training\data:/training/data:rw ^
  -v C:\slop\mc-clanker\training\outputs:/training/outputs:rw ^
  -v C:\Users\Dave\.cache\huggingface:/root/.cache/huggingface:rw ^
  --name mcclanker-training ^
  localhost/mcclanker/training:latest
