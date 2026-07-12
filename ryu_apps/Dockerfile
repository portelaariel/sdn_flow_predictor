# Ryu Controller (Emitter + optional ofctl_rest)
FROM python:3.9-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/usr/src/app

WORKDIR /usr/src/app

# System deps for Ryu & eventlet, plus curl for healthchecks (optional)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl && \
    rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY requirements.txt .
RUN pip install pip==23.0.1
RUN pip install setuptools==57.5.0
RUN pip install wheel
RUN pip install --no-cache-dir -r requirements.txt

# Copy your Ryu apps
COPY emitter_cnsm.py .
# If you maintain a local REST app for ofctl, keep it. Otherwise remove this line.
COPY ofctl_rest.py . 

# Defaults (override in compose)
ENV SIMPLESWITCH_URL=http://127.0.0.1:8090/packetin \
    FLOWBLOCKER_URL=http://127.0.0.1:7070/flowblocker/domain_table \
    CONTROLLER_ID=default_cid \
    OFP_TCP_PORT=6633 \
    WSGI_PORT=8080

# Expose Ryu REST API + OpenFlow TCP ports
EXPOSE 8080 6633 6653

# (Optional) healthcheck on the REST API port
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s CMD curl -fsS http://localhost:${WSGI_PORT}/stats/switches || exit 1
#HEALTHCHECK --interval=30s --timeout=5s --start-period=10s CMD curl -fsS http://localhost:${WSGI_PORT}/ || exit 1

# Run ryu-manager with your apps
ENTRYPOINT ["sh", "-c", "ryu-manager --ofp-tcp-listen-port=$OFP_TCP_PORT --wsapi-port=$WSGI_PORT emitter_cnsm.py ofctl_rest.py"]
