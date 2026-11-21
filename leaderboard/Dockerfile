FROM docker:cli@sha256:018edbc908e08fcc9dbf029c812c34251e9b4719e6f71ca0e5eae2a987d014ca AS docker_cli
FROM python:3.12-alpine@sha256:c5ba3deef00c7758561a0f03c26f44f10589263d40e732b8748e0d1f38f6dd89 AS web
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN addgroup -g 10001 app && adduser -D -u 10001 -G app app \
    && mkdir -p /data /results && chown app:app /data /results
WORKDIR /app
COPY starter/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY bench /app/bench
COPY leaderboard /app/leaderboard
ENV DATABASE=/data/leaderboard.sqlite3 RESULTS_DIR=/results HOME=/tmp
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8080/health",timeout=2)'
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "--threads", "4", "--access-logfile", "-", "leaderboard.app:create_app()"]

FROM web AS runner
COPY --from=docker_cli /usr/local/bin/docker /usr/local/bin/docker
