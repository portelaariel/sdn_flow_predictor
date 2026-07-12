# Simple Switch REST
FROM python:3.10-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /usr/src/app

# System deps (certs for outbound requests)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl && \
    rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY requirements.txt ./
RUN pip install --no-cache-dir \
    "pip<24" \
    "setuptools==57.5.0" \
    wheel

RUN pip install --no-cache-dir -r requirements.txt
# COPY requirements.txt ./
# RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
# If you have local packet parsing helpers under ./lib, copy them too.
COPY lib ./lib
COPY Simpleswitch_cnsm.py .

# Defaults (override in compose)
ENV RYU_BASE_URL=http://127.0.0.1:8080 \
    PORT=8090

EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s CMD sh -c 'curl -fsS http://localhost:${PORT}/ || exit 1'
#HEALTHCHECK --interval=30s --timeout=5s --start-period=10s CMD curl -fsS http://localhost:8090/ || exit 1

CMD ["sh", "-c", "python ./Simpleswitch_cnsm.py --host=0.0.0.0 --port=$PORT"]
