FROM ghcr.io/astral-sh/uv:0.13.1-python3.12-trixie-slim

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

# /data: GPX-Dateien und Kartenseiten (Volume, gehört dem App-Nutzer)
RUN useradd -r -u 10001 app && mkdir /data && chown app /data
USER app
ENV DATA_DIR=/data

EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"
CMD ["/app/.venv/bin/transitwalter"]
