FROM python:3.14-slim

RUN groupadd -r app && useradd -r -g app app
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
RUN uv sync --frozen --no-dev

USER app
EXPOSE 8000

# api (default): uvicorn multiprocess. worker: `worker`. beat: `worker --beat`.
# migrations + pending scripts are an explicit deploy step, never entrypoint magic:
#   uv run sg db migrate && uv run sg script run --pending
CMD ["uv", "run", "uvicorn", "src.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
