# training/Dockerfile - GPU training container for fine-tuning Qwen3.5-0.8B
# Multi-stage build: build stage installs all deps, runtime stage is minimal
# Uses CUDA 12.4 runtime. nvidia-container-toolkit ensures compatibility.

# =============================================================================
# Build stage - install all Python/ML dependencies
# =============================================================================
FROM nvidia/cuda:12.4.1-devel-ubuntu22.04 AS builder

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1

WORKDIR /build

# Install build tools and Python
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.11 python3.11-dev python3-pip git curl && \
    rm -rf /var/lib/apt/lists/* && \
    ln -sf /usr/bin/python3.11 /usr/bin/python

# Install Python dependencies (cached until requirements.txt changes)
COPY training/requirements.txt .
RUN python3.11 -m pip install --prefix=/install -r requirements.txt

# Install unsloth (heavy, but cached)
RUN python3.11 -m pip install --prefix=/install "unsloth[cu124]>=2025.3.19"

# =============================================================================
# Runtime stage - minimal image with just runtime
# =============================================================================
FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/training
ENV HF_HOME=/training/.cache/huggingface
ENV TRANSFORMERS_CACHE=/training/.cache/huggingface

WORKDIR /training

# Install only runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.11 libgomp1 && \
    rm -rf /var/lib/apt/lists/* && \
    ln -sf /usr/bin/python3.11 /usr/bin/python

# Copy installed packages from builder (must copy subdirs to avoid nested /usr/local/local)
COPY --from=builder /install/usr/local/lib /usr/local/lib
COPY --from=builder /install/usr/local/bin /usr/local/bin

# Copy training scripts (changes frequently, separate from deps)
COPY training/finetune_qwen.py .
COPY training/convert_to_unsloth_dataset.py .

# Create directories
RUN mkdir -p /training/data /training/outputs

# Create non-root user
RUN useradd -m -u 1000 trainer
USER trainer

# Default command - run fine-tuning
CMD ["python", "finetune_qwen.py"]
