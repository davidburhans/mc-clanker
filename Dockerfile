FROM nvcr.io/nvidia/pytorch:24.10-py3

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .

# Install audio libraries and ffmpeg with MP3 support
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libportaudio2 \
        alsa-utils \
        libmp3lame0 \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "setuptools<70.0.0" && \
    pip install --no-cache-dir torch==2.6.0 --index-url https://download.pytorch.org/whl/cu124 && \
    pip install --no-cache-dir gradio scipy huggingface_hub safetensors && \
    pip install --no-cache-dir stable-audio-tools && \
    pip install --no-cache-dir openai sounddevice fastapi uvicorn pydantic

# Copy all Python files and static directory
COPY app_ui.py .
COPY api_routes.py .
COPY framework_*.py .
COPY static/ ./static/

EXPOSE 7860

CMD ["python", "-u", "app_ui.py"]
