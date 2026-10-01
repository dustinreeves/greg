FROM python:3.12-slim

# curl does all the downloading
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates tini \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY setup.py MANIFEST.in README.md COPYING ./
COPY greg ./greg
RUN pip install --no-cache-dir ".[socks,tagging]" && rm -rf /src

# Any uid can run the image (compose sets `user:`); HOME must be writable.
ENV HOME=/home/greg \
    PYTHONUNBUFFERED=1
RUN mkdir -p /home/greg/.config/greg /home/greg/.local/share/greg /downloads \
 && chmod -R 1777 /home/greg /downloads

COPY docker/entrypoint.py /usr/local/bin/greg-entrypoint
COPY docker/healthcheck.py /usr/local/bin/greg-healthcheck
RUN chmod 755 /usr/local/bin/greg-entrypoint /usr/local/bin/greg-healthcheck

VOLUME ["/home/greg/.config/greg", "/home/greg/.local/share/greg", "/downloads"]
EXPOSE 8787

HEALTHCHECK --interval=60s --timeout=10s --start-period=20s \
  CMD ["python", "/usr/local/bin/greg-healthcheck"]

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/greg-entrypoint"]
CMD ["greg", "web"]
