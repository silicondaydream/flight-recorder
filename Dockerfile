# Watchdog: the Devin importer, the supervisor and its dashboard in one process.
FROM python:3.12-slim

RUN useradd --create-home --uid 10001 watchdog && mkdir /data && chown watchdog /data
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[judge]"

USER watchdog
# Logs, importer state and Watchdog's state all live on the volume.
ENV XDG_DATA_HOME=/data XDG_STATE_HOME=/data/state WATCHDOG_DRY_RUN=1
VOLUME /data
EXPOSE 8765
ENTRYPOINT ["watchdog"]
# 0.0.0.0 so the port can be published; set WATCHDOG_APPROVAL_TOKEN when you do.
CMD ["run", "--dashboard", "0.0.0.0:8765"]
