# ── Stage 1: build / install dependencies ────────────────────────────────────
FROM python:3.13-slim AS builder
WORKDIR /app

# Install Poetry (used only to export requirements)
RUN pip install --no-cache-dir poetry==2.1.3
RUN poetry self add poetry-plugin-export
COPY pyproject.toml poetry.lock* ./

# Export runtime deps to a plain requirements file (no dev extras)
RUN poetry export --without dev --without-hashes -f requirements.txt -o requirements.txt

# ── Stage 2: runtime image ────────────────────────────────────────────────────
FROM python:3.13-slim AS runtime

LABEL org.opencontainers.image.title="ExecTunnel" \
      org.opencontainers.image.description="SOCKS5 proxy tunnel over WebSocket into Kubernetes pods" \
      org.opencontainers.image.version="1.2.1" \
      org.opencontainers.image.url="https://github.com/khodaparastan/exectunnel" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install runtime Python deps from the exported requirements
COPY --from=builder /app/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY exectunnel/ ./exectunnel/
COPY pyproject.toml README.md LICENSE ./

# Install the package itself (editable is not needed in container)
RUN pip install --no-cache-dir --no-deps .

# Create a non-root user
RUN addgroup --system exectunnel && \
    adduser  --system --ingroup exectunnel --no-create-home exectunnel

USER exectunnel

# Default SOCKS5 listen port
EXPOSE 1080

ENTRYPOINT ["exectunnel"]
CMD ["tunnel"]
