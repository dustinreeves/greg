#!/usr/bin/env python3
"""
Container entrypoint: create a first-run configuration from environment
variables, then exec the real command (default: `greg web`).

Nothing already on the volumes is ever overwritten, so edit the files there
(or use the web UI) after the first start.

  GREG_WEB_PASSWORD_FILE  path to a file with the web UI password (a Docker
                          secret), or
  GREG_WEB_PASSWORD       the password itself (less safe: visible in
                          `docker inspect`)
  GREG_WEB_USER           web UI user name (default: greg)
  GREG_PROXY              proxy url for feeds, e.g. http://gluetun:8888
  GREG_PROXY_HOSTS        only feeds on these hosts use it, e.g. patreon.com
  GREG_SYNC_EVERY_MINUTES schedule a sync of all feeds (handled by greg web)
  GREG_REQUIRE_MOUNTS     comma-separated mount points that must be mounted
                          before any job runs (e.g. a NAS or rclone mount)
"""
import json
import os
import sys

HOME = os.path.expanduser("~")
CONF_DIR = os.path.join(HOME, ".config", "greg")
GREG_CONF = os.path.join(CONF_DIR, "greg.conf")
WEB_JSON = os.path.join(CONF_DIR, "web.json")
MIN_LEN = int(os.environ.get("GREG_WEB_MIN_PASSWORD", "12"))


def make_greg_conf():
    if os.path.exists(GREG_CONF):
        return
    lines = ["[DEFAULT]", "Download directory = /downloads",
             "Data directory = ~/.local/share/greg/data"]
    if os.environ.get("GREG_PROXY"):
        lines.append("proxy = " + os.environ["GREG_PROXY"].replace("%", "%%"))
    if os.environ.get("GREG_PROXY_HOSTS"):
        lines.append("proxy_hosts = " + os.environ["GREG_PROXY_HOSTS"])
    with open(GREG_CONF, "w") as f:
        f.write("\n".join(lines) + "\n")
    print("entrypoint: wrote", GREG_CONF, flush=True)


def read_password():
    path = os.environ.get("GREG_WEB_PASSWORD_FILE")
    if path:
        with open(path) as f:
            return f.read().strip()
    return os.environ.get("GREG_WEB_PASSWORD")


def make_web_json():
    if os.path.exists(WEB_JSON):
        return
    from greg.web import hash_password
    password = read_password()
    if not password:
        sys.exit("No web UI password. Set GREG_WEB_PASSWORD_FILE (preferred) "
                 "or GREG_WEB_PASSWORD, or put a web.json on the config "
                 "volume (see README).")
    if len(password) < MIN_LEN:
        sys.exit("The web UI password must be at least {} characters "
                 "(GREG_WEB_MIN_PASSWORD changes that).".format(MIN_LEN))
    settings = {
        "host": "0.0.0.0",  # inside the container; compose publishes it
        "port": 8787,       # on 127.0.0.1 only unless you change that
        "username": os.environ.get("GREG_WEB_USER", "greg"),
        "password_hash": hash_password(password),
        "min_password_length": MIN_LEN,
        "trust_proxy": os.environ.get("GREG_TRUST_PROXY", "") == "1",
        "require_mounts": [m.strip() for m in os.environ.get(
            "GREG_REQUIRE_MOUNTS", "").split(",") if m.strip()],
        "allowed_hosts": [h.strip() for h in os.environ.get(
            "GREG_ALLOWED_HOSTS", "").split(",") if h.strip()],
    }
    fd = os.open(WEB_JSON, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(settings, f, indent=2)
    print("entrypoint: wrote", WEB_JSON, flush=True)


def main():
    os.makedirs(CONF_DIR, exist_ok=True)
    make_greg_conf()
    if sys.argv[1:2] == ["greg"] and sys.argv[2:3] == ["web"]:
        make_web_json()
    if len(sys.argv) < 2:
        sys.exit("no command given")
    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    main()
