FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.28 /uv /usr/local/bin/uv
WORKDIR /srv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PATH="/srv/.venv/bin:$PATH" \
    DATABASE_URL=sqlite:////data/gateway.db
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./
RUN uv sync --locked --no-dev \
    && useradd --system --uid 10001 lgw && mkdir /data && chown lgw /data
USER lgw
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"
# Migrate the SQLite file on the volume, then serve. Run the `lgw` CLI with `docker exec`.
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
