# Watchdog: the importer, the supervisor, the dashboard and the public listener,
# in one process.
FROM python:3.12-slim

RUN useradd --create-home --uid 10001 watchdog && mkdir /data && chown watchdog /data
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[judge]"

USER watchdog
# Logs, importer state, settings and Watchdog's state all live on the volume.
# It starts in dry run (the default setting); turn that off in Settings.
ENV XDG_DATA_HOME=/data XDG_STATE_HOME=/data/state
VOLUME /data
# 8765: the dashboard (publish on loopback only). 8766: the public listener, for
# MCP log ingest and Teams approval links (put HTTPS in front of it).
EXPOSE 8765 8766
ENTRYPOINT ["watchdog"]
CMD ["run", "--dashboard", "0.0.0.0:8765", "--public", "0.0.0.0:8766"]
