FROM ubuntu:24.04
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ca-certificates curl git ripgrep python3 python3-venv coreutils \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /workspace
