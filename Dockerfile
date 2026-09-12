# Multi-stage Dockerfile for SparkPlug

# Stage 1: Build virtual environment with dependencies
FROM python:3.12-slim AS builder

WORKDIR /build

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Create virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install dependencies
COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir .

# Stage 2: Production runtime image
FROM python:3.12-slim AS runtime

LABEL maintainer="SparkPlug Team"
LABEL description="Wake-on-LAN and SSH shutdown daemon for Caddy reverse proxy"

WORKDIR /app

# Install runtime dependencies (OpenSSH client for remote shutdown, ca-certificates)
RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-client \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV SPARKPLUG_CONFIG_PATH=/etc/sparkplug/config.yaml

# Create directories (and sparkplug user for optional non-root execution)
RUN groupadd -r sparkplug && useradd -r -g sparkplug -d /app sparkplug \
    && mkdir -p /etc/sparkplug/keys /var/run/caddy /app \
    && chmod 777 /var/run/caddy

# Copy application source code
COPY src/ /app/src/

EXPOSE 8080

ENTRYPOINT ["python", "-m", "src.main"]

