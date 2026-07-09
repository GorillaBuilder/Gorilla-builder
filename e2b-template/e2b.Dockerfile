FROM node:20-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      git curl jq unzip xz-utils ca-certificates findutils procps \
      build-essential python3 python3-pip \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /home/user/app

# Ensure package.json exists in ./boilerplate or npm install will fail
COPY ./boilerplate/ /home/user/app/

RUN if [ -f package.json ]; then \
    npm install --legacy-peer-deps --no-fund --no-audit --prefer-offline \
    && npm cache clean --force; \
    fi

RUN mkdir -p /home/user/app/public/generated \
             /home/user/app/src \
             /home/user/app/.gorilla \
  && chmod -R 755 /home/user/app

# ─── preview_screenshot support (headless browser) ──────────────────────
# Adds Python Playwright + a bundled Chromium so the agent's preview_screenshot
# tool can actually render and photograph the dev server inside the sandbox.
# NOTE: this measurably increases image build time and image size (Chromium
# download + its OS-level deps via --with-deps). After changing this block,
# the E2B template must be rebuilt with `e2b template build` (an E2B CLI
# step outside this codebase) before preview_screenshot will work — this
# repo alone cannot trigger that rebuild.
# `--break-system-packages` is required because this base image is Debian
# bookworm, which marks the system Python as "externally managed" (PEP 668)
# and refuses a bare `pip install` outside a venv. It's a no-op/harmless if
# a future base image doesn't enforce PEP 668 — kept here defensively.
RUN pip3 install --no-cache-dir --break-system-packages playwright \
    && python3 -m playwright install --with-deps chromium
