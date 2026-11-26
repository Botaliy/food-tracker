FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY scripts ./scripts

RUN mkdir -p data

# One malloc arena per thread keeps freed memory scattered where
# malloc_trim (app.images.release_memory) can't hand it back.
ENV MALLOC_ARENA_MAX=2 PYTHONUNBUFFERED=1

EXPOSE 8000

# uvicorn straight from the venv: "uv run" would sit next to it as a second process.
CMD ["/app/.venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
