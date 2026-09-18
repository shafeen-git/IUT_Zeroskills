# ============================================================
# Stage 1: Build & Dependency Installation
# ============================================================
FROM python:3.12-slim AS builder

WORKDIR /build

# Prevent Python from writing .pyc files and enable unbuffered logging
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Install build dependencies
RUN apt-get update && \
    apt-get install --no-install-recommends -y gcc python3-dev && \
    rm -rf /var/lib/apt/lists/*

# Create virtual environment for clean isolation
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# ============================================================
# Stage 2: Minimal Production Runtime
# ============================================================
FROM python:3.12-slim AS runner

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    PORT=8000 \
    HOST=0.0.0.0

# Copy only the compiled virtual environment from builder
COPY --from=builder /opt/venv /opt/venv

# Create a non-root system user for secure container execution
RUN groupadd -r appuser && useradd -r -g appuser -d /app -s /sbin/nologin appuser

# Copy application source code (excluding files in .dockerignore)
COPY app/ /app/app/

# Set correct ownership for security
RUN chown -R appuser:appuser /app

# Switch to non-root user
USER appuser

# Expose default API port
EXPOSE 8000

# Health check to ensure container status is actively monitored
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:' + __import__('os').getenv('PORT', '8000') + '/health')" || exit 1

# Start the FastAPI server using uvicorn
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
