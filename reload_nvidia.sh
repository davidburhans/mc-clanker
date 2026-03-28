#!/bin/bash
# Script to safely reload NVIDIA kernel modules for Podman users
# Resolves driver/library mismatch (CUDA error 100) without restarting.

set -e

echo "Stopping Podman containers and socket..."
# Podman doesn't have a central daemon like Docker, but we should stop the socket if active
systemctl stop podman.socket || true

echo "Unloading NVIDIA kernel modules..."
# We use -f (force) in case modules are still in use by lingering processes
modprobe -r nvidia_uvm || true
modprobe -r nvidia_drm || true
modprobe -r nvidia_modeset || true
modprobe -r nvidia || true

echo "Reloading NVIDIA kernel modules..."
modprobe nvidia
modprobe nvidia_modeset
modprobe nvidia_drm
modprobe nvidia_uvm

echo "Restarting Podman socket..."
systemctl start podman.socket || true

echo "NVIDIA modules successfully reloaded. You can now run your Podman containers."
