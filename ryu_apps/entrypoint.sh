#!/bin/sh
# Start ryu-manager with dynamic configuration based on environment variables
exec ryu-manager --ofp-tcp-listen-port ${OFP_PORT} --wsapi-port ${WSGI_PORT} ofp_emitter_vr.py ofctl_rest.py

