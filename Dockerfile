# =============================================================================
# Stage 1: Dependencies - rebuild only when these change
# =============================================================================
FROM nvcr.io/nvidia/pytorch:24.09-py3 AS deps

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install system dependencies for audio
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libportaudio2 \
        libmp3lame0 \
        ffmpeg \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv for fast dependency management
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"

# Copy pyproject.toml and README.md for dependency installation
COPY pyproject.toml README.md .

# Install PyTorch nightly with CUDA 13.2 (matching the system's driver)
# RTX 5090 (Blackwell/compute capability 8.9) requires newer PyTorch with Blackwell support
RUN uv pip install --system --no-cache-dir --pre torch torchvision torchaudio --index-url https://download.pytorch.org/whl/nightly/cu132

# Install dependencies via uv
RUN uv pip install --system --no-cache-dir \
    stable-audio-tools==0.0.19 \
    scipy \
    sounddevice \
    fastapi \
    "uvicorn[standard]" \
    pydantic \
    huggingface_hub \
    safetensors \
    openai \
    numpy

# Fix clip/clip.py import issue with newer setuptools
RUN sed -i 's/from pkg_resources import packaging/from packaging import version as packaging/' /usr/local/lib/python3.10/dist-packages/clip/clip.py && \
    sed -i 's/packaging\.version\.parse(/packaging.parse(/g' /usr/local/lib/python3.10/dist-packages/clip/clip.py

# =============================================================================
# Stage 2: Application - rebuild only when code changes
# =============================================================================
FROM deps AS app

# Install additional dependencies for auth, database, and production
RUN uv pip install --system --no-cache-dir \
    sqlalchemy>=2.0.0 \
    psycopg2-binary>=2.9.0 \
    bcrypt>=4.0.0 \
    PyJWT>=2.8.0 \
    python-multipart \
    email-validator

# Copy application code (this layer gets cached unless these files change)
COPY app_ui.py .
COPY api_routes.py .
COPY auth.py .
COPY db.py .
COPY playback.py .
COPY framework_*.py .
COPY models/ ./models/
COPY models_config.json .
COPY static/ ./static/
COPY data/ ./data/

EXPOSE 7860

CMD ["python", "-u", "app_ui.py"]
