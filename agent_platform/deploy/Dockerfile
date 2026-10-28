FROM python:3.13-slim-bookworm AS runtime-base
COPY --from=ghcr.io/astral-sh/uv:0.11.16 /uv /uvx /bin/
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH"
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY chat_models/ ./chat_models/
COPY agent_platform/ ./agent_platform/
RUN useradd --create-home --uid 10001 platform && \
    mkdir -p /app/agent_platform/.data/exports /app/agent_platform/.tombstones && \
    chown -R platform:platform /app/agent_platform/.data /app/agent_platform/.tombstones && \
    chmod 700 /app/agent_platform/.data /app/agent_platform/.tombstones
USER platform

FROM runtime-base AS worker
CMD ["python", "-m", "agent_platform", "worker"]

FROM runtime-base AS gateway-sync
CMD ["python", "-m", "agent_platform", "gateway-sync"]

FROM runtime-base AS maintenance
CMD ["python", "-m", "agent_platform", "maintenance"]

FROM runtime-base AS migration
CMD ["alembic", "-c", "agent_platform/alembic.ini", "upgrade", "head"]

# API remains the default target. Backend images do not build or bundle the UI;
# its independent web target/release proxies API requests at the public origin.
FROM runtime-base AS api
EXPOSE 8000
CMD ["python", "-m", "agent_platform", "serve", "--host", "0.0.0.0", "--port", "8000"]
