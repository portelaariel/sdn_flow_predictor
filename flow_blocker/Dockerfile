# FlowBlocker
FROM python:3.10-slim

# Prevent Python from buffering stdout/stderr
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# System deps (optional but useful for SSL/certs in requests)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl && \
    rm -rf /var/lib/apt/lists/*

# Copy requirements and install
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Copy app
COPY flow_blocker_cnsm.py /app/

# Default envs (override in compose)
ENV RYU_BASE_URL=http://127.0.0.1:8080 \
    PORT=7070 \
    ETCD_ENDPOINTS=127.0.0.1:2379 \
    CONTROLLER_ID=default_cid

EXPOSE 7070

# (Optional) basic healthcheck hits the index route
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s CMD sh -c 'curl -fsS http://localhost:${PORT}/ || exit 1'
#HEALTHCHECK --interval=30s --timeout=5s --start-period=10s CMD curl -fsS http://localhost:7070/ || exit 1

CMD ["python", "flow_blocker_cnsm.py"]
