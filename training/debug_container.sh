#!/bin/bash
podman run --rm localhost/mcclanker/training:latest ls -la /usr/local/lib/python3.11/dist-packages/ 2>&1 | head -50
