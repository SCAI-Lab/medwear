FROM python:3.10-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy med API module and install
COPY medwear_med_api/ /app/medwear_med_api/
RUN cd /app/medwear_med_api && pip install -r requirements.txt && pip install -e .

# Install additional dependencies for med API
RUN pip install fastapi uvicorn python-jose httpx slowapi minio pyotp "qrcode[pil]" python-json-logger \
    pandas numpy scipy python-dateutil tqdm \
    prometheus-client prometheus-fastapi-instrumentator \
    opentelemetry-api opentelemetry-sdk opentelemetry-instrumentation-fastapi \
    opentelemetry-instrumentation-sqlalchemy opentelemetry-exporter-otlp-proto-grpc \
    pytest pytest-asyncio pytest-cov hypothesis wfdb pyEDFlib

# Set Python path
ENV PYTHONPATH=/app

# Run as non-root user
RUN useradd -m -u 1000 appuser && chown -R appuser /app
USER appuser

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=5 \
    CMD curl -f http://localhost:8080/health || exit 1

# Expose port
EXPOSE 8080

# Run the med API
CMD ["python", "medwear_med_api/scripts/run_med_api.py"]
