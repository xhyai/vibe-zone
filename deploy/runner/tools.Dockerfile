FROM python:3.12-alpine@sha256:c5ba3deef00c7758561a0f03c26f44f10589263d40e732b8748e0d1f38f6dd89
RUN apk add --no-cache git=2.54.1-r0 skopeo=1.20.1-r2
COPY deploy/runner/proxy.py /proxy.py
USER 1000:1000
WORKDIR /work
