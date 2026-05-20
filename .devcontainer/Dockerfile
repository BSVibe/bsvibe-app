# BSVibe devcontainer — Python 3.11 + Node 20 + uv + pnpm
FROM mcr.microsoft.com/devcontainers/python:1-3.11-bookworm

# Install uv globally
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
    && mv /root/.local/bin/uv /usr/local/bin/uv \
    && mv /root/.local/bin/uvx /usr/local/bin/uvx \
    && chmod +x /usr/local/bin/uv /usr/local/bin/uvx

# pnpm via corepack (Node feature provides Node)
RUN corepack enable || true

# System libs that asyncpg / cryptography need (most already in the base, this is belt+braces)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

USER vscode
