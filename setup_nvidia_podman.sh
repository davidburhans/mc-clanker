#!/bin/bash
# Setup NVIDIA Container Toolkit for Podman on Windows

set -e

echo "=== NVIDIA Container Toolkit Setup for Podman ==="

# Step 1: Access the podman-machine and install the toolkit
echo "[1/4] Installing NVIDIA Container Toolkit in podman-machine..."

wsl.exe -d podman-machine-default << 'WSL_COMMANDS'
set -e

echo "  Adding NVIDIA package repository..."
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit.gpg 2>/dev/null || true

ARCH=$(dpkg --print-architecture)
echo "deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit.gpg] https://nvidia.github.io/libnvidia-container/stable/deb/${ARCH} /" | tee /etc/apt/sources.list.d/nvidia-container-toolkit.list > /dev/null

echo "  Installing nvidia-container-toolkit..."
apt-get update -qq
apt-get install -y -qq nvidia-container-toolkit > /dev/null 2>&1

echo "  Configuring NVIDIA runtime for Podman..."
nvidia-ctk runtime configure --runtime=podman --runtime-default-file=/etc/containers/containers.conf

echo "  Verifying installation..."
nvidia-ctk --version

echo "  CDI devices available:"
nvidia-ctk cdi list

echo "  Installation complete!"
WSL_COMMANDS

echo "[2/4] Stopping podman machine..."
podman machine stop

echo "[3/4] Starting podman machine..."
podman machine start

echo "[4/4] Verifying GPU access..."
if podman run --rm --gpus all nvidia/cuda:12.0-base-ubuntu22.04 nvidia-smi; then
    echo ""
    echo "=== SUCCESS: GPU is accessible via Podman ==="
    echo "You can now run: podman compose up -d"
else
    echo ""
    echo "=== GPU access failed. You may need to restart your computer. ==="
fi
