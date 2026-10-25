# tradebot image. Secrets are NOT baked in: they come from .env at runtime (compose env_file).
FROM python:3.12-slim-bookworm

ARG EXTRAS=""
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy \
    TZ=UTC PATH="/app/.venv/bin:$PATH"

COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /usr/local/bin/uv
RUN useradd --create-home --uid 10001 bot
WORKDIR /app

# dependencies first (cached layer), then the project itself
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project ${EXTRAS:+--extra $EXTRAS}
COPY src ./src
COPY config/default.yaml config/default.yaml
COPY config/calendars config/calendars
RUN uv sync --frozen --no-dev ${EXTRAS:+--extra $EXTRAS} \
 && mkdir -p data logs reports backups && chown -R bot:bot /app

USER bot
HEALTHCHECK --interval=60s --timeout=30s --start-period=180s --retries=3 CMD ["tradebot", "health"]
CMD ["tradebot", "run"]
