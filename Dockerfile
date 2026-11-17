# Atlas as a container image.
#   docker build -t atlas:dev .                                 the starter, as you have it
#   docker build -t atlas:solved --build-arg APPLY_SOLUTIONS=1 .   with the worked solutions (demos, CI)
#   docker run --rm -p 8000:8000 atlas:solved
# Optional OpenTelemetry exporter: --build-arg WITH_OTEL=1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first, so code changes don't reinstall them.
COPY requirements.txt requirements-otel.txt ./
ARG WITH_OTEL=0
RUN pip install -r requirements.txt \
 && if [ "$WITH_OTEL" = "1" ]; then pip install -r requirements-otel.txt; fi

COPY . .
ARG APPLY_SOLUTIONS=0
RUN if [ "$APPLY_SOLUTIONS" = "1" ]; then python tools/apply_solutions.py . ; fi

# Never run as root. The app writes nothing to disk unless ATLAS_TRACE_FILE is set (under /app/var).
RUN groupadd --system --gid 10001 atlas \
 && useradd --system --uid 10001 --gid 10001 --no-create-home atlas \
 && mkdir -p /app/var \
 && chown atlas:atlas /app/var
USER 10001:10001

EXPOSE 8000
# Liveness only: /v1/health checks the process. Orchestrators use /v1/ready to decide on traffic.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/v1/health', timeout=4)"]

CMD ["uvicorn", "atlas.app:app", "--host", "0.0.0.0", "--port", "8000"]
