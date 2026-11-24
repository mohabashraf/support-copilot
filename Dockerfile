# One image for both jobs: the KB indexer (copilot kb index) and the server (python -m copilot.main).
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.10.12 /uv /bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH" \
    HF_HOME=/cache/huggingface

# 1. Dependencies first (cached layer: only rebuilt when pyproject.toml / uv.lock change).
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --no-dev --no-install-project --no-cache

# 2. Our code + the raw KB pages (rebuilt on every code change, takes seconds).
COPY src ./src
COPY knowledge_base ./knowledge_base
# Risk classifier training examples + thresholds (D12, D14): loaded by the API and the worker.
COPY classifier ./classifier
RUN uv sync --locked --no-dev --no-cache

# No .env inside the image (it may hold secrets): docker-compose passes settings as env vars.
EXPOSE 8000
CMD ["python", "-m", "copilot.main"]
