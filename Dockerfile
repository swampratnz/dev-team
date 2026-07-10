# dev-team — container image for Ubuntu-based hosts.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Python 3 and Node.js (the Agent SDK drives the Claude Code CLI).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 python3-venv python3-pip nodejs npm ca-certificates \
    && npm install -g @anthropic-ai/claude-code \
    && rm -rf /var/lib/apt/lists/*

# Unprivileged runtime user.
RUN useradd --system --create-home --home-dir /home/devteam devteam
WORKDIR /app

# Install the package into a virtualenv.
COPY pyproject.toml README.md ./
COPY src ./src
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install .
ENV PATH="/opt/venv/bin:${PATH}"

USER devteam

ENTRYPOINT ["dev-team"]
CMD ["--help"]
