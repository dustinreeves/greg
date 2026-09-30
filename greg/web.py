"""
A web interface for greg: manage feeds, sync, browse/download episodes,
edit (safe) settings and see system status.

Standard library only (works on old Pythons). Start with `greg web`.
Put it behind a TLS-terminating reverse proxy if you expose it; it listens on
127.0.0.1 by default and refuses to start without a password.

Settings live in ~/.config/greg/web.json (never in the greg repo):

    {
      "host": "127.0.0.1", "port": 8787,
      "username": "greg", "password_hash": "pbkdf2$...",   # `greg web --set-password`
      "min_password_length": 12,     # enforced by --set-password
      "trust_proxy": false,          # believe X-Forwarded-For (behind Caddy)
      "allowed_hosts": [],           # optional Host header allow-list
      "vpn_prefix": [],              # command prefix for feeds that need it
      "vpn_hosts": ["patreon.com"],  # feeds whose url host matches use it
      "vpn_feeds": [],               # or name feeds explicitly
      "schedules": [],               # [{"name","feeds":["all"],"every_minutes":60}]
                                     # (or set env GREG_SYNC_EVERY_MINUTES)
      "status": {"mounts": [], "interfaces": [], "processes": []}
    }
"""
import base64
import collections
import getpass
import hashlib
import hmac
import json
import os
import re
import shutil
import socketserver
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs, unquote

import feedparser

import greg.aux_functions as aux
import greg.classes as c
from greg.jobs import entry_id

NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')
RESERVED = ("all", "DEFAULT")
PBKDF2_ITER = 200000

# greg.conf keys that may be edited from the web. `filter` and
# `downloadhandler` are deliberately absent: greg executes them, so editing
# them from a web page would be remote code execution.
EDITABLE = collections.OrderedDict([
    ("Download directory", "Where episodes are saved"),
    ("Create subdirectory", "yes / no: one folder per feed"),
    ("subdirectory_name", "Folder name template, e.g. {podcasttitle}"),
    ("firstsync", "Episodes to fetch on first sync (a number or 'all')"),
    ("mime", "Enclosure types to download, e.g. audio or audio, video"),
    ("download_filename", "File name template for greg's own downloader"),
    ("date_format", "strftime format for {date}; write % as %%"),
    ("ignoreenclosures", "yes / no"),
    ("notype", "yes / no"),
    ("Tag", "yes / no: write ID3 tags (needs eyeD3)"),
    ("file_to_tag", "File to tag, e.g. {directory}/{filename}"),
    ("tag_artist", "ID3 artist template"),
    ("tag_title", "ID3 title template"),
    ("tag_genre", "ID3 genre template"),
])
READONLY = ("filter", "downloadhandler")


# -- settings / auth ------------------------------------------------------

def settings_path(args):
    return os.path.expanduser(
        args.get("webconfig") or "~/.config/greg/web.json")


def load_settings(args):
    path = settings_path(args)
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_settings(args, settings):
    path = settings_path(args)
    aux.ensure_dir(os.path.dirname(path))
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(settings, f, indent=2)
    os.replace(tmp, path)


def hash_password(password, salt=None, iterations=PBKDF2_ITER):
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt,
                                 iterations)
    return "pbkdf2${}${}${}".format(
        iterations, base64.b64encode(salt).decode(),
        base64.b64encode(digest).decode())


def check_password(password, stored):
    try:
        _, iterations, salt, digest = stored.split("$")
        expect = base64.b64decode(digest)
        got = hashlib.pbkdf2_hmac("sha256", password.encode(),
                                  base64.b64decode(salt), int(iterations))
        return hmac.compare_digest(got, expect)
    except (ValueError, TypeError):
        return False


class Throttle:
    """Lock an address out after repeated failed logins."""
    def __init__(self, limit=8, window=300):
        self.limit, self.window = limit, window
        self.fails = collections.defaultdict(list)
        self.lock = threading.Lock()

    def blocked(self, ip):
        now = time.time()
        with self.lock:
            self.fails[ip] = [t for t in self.fails[ip]
                              if now - t < self.window]
            return len(self.fails[ip]) >= self.limit

    def fail(self, ip):
        with self.lock:
            self.fails[ip].append(time.time())


# -- feeds ----------------------------------------------------------------

class Greg:
    """Thin layer over greg's own classes for the web API."""
    def __init__(self, args, settings):
        self.args = {"configfile": args.get("configfile"),
                     "datadirectory": args.get("datadirectory")}
        self.settings = settings
        self.lock = threading.RLock()  # serialises edits to greg's files

    def session(self):
        return c.Session(dict(self.args))

    @staticmethod
    def mask(url):
        return url.split("?", 1)[0] + ("?••••"
                                        if "?" in url else "")

    def needs_wrapper(self, name, url):
        """Feeds that must run under the (advanced) vpn_prefix wrapper."""
        s = self.settings
        if not s.get("vpn_prefix"):
            return False
        host = (urlparse(url).hostname or "").lower()
        return name in s.get("vpn_feeds", []) or any(
            host == h or host.endswith("." + h)
            for h in s.get("vpn_hosts", ["patreon.com"]))

    def route(self, session, name, url):
        """How this feed reaches the internet: proxy, wrapper or direct."""
        pr = aux.proxy_for(session, name, url)
        if pr:
            return "proxy", aux.mask_proxy(pr)
        if self.needs_wrapper(name, url):
            return "wrapper", None
        return None, None

    def history(self, session, name):
        return aux.parse_feed_info(os.path.join(session.data_dir, name))

    def feeds(self):
        session = self.session()
        out = []
        for name in session.list_feeds():
            url = session.feeds[name].get("url", "")
            links, dates = self.history(session, name)
            last = None
            if dates:
                try:
                    last = time.strftime("%Y-%m-%d %H:%M",
                                         tuple(max(dates)))
                except (TypeError, ValueError):
                    last = "unknown"
            via, detail = self.route(session, name, url)
            out.append({"name": name, "url": self.mask(url),
                        "last": last, "downloaded": len(links),
                        "vpn": via is not None, "via": via,
                        "proxy": detail,
                        "date_info": session.feeds[name].get("date_info")})
        return out

    def url(self, name):
        session = self.session()
        if name not in session.feeds:
            raise KeyError(name)
        return session.feeds[name].get("url", "")

    @staticmethod
    def valid_name(name):
        return bool(NAME_RE.match(name or "")) and name not in RESERVED

    @staticmethod
    def valid_url(url):
        p = urlparse(url or "")
        return p.scheme in ("http", "https") and bool(p.netloc)

    def add(self, name, url, downloadfrom=None):
        if not self.valid_name(name):
            raise ValueError("Name must be 1-64 letters, digits, '.', '_' or "
                             "'-' (and not 'all' or 'DEFAULT').")
        if not self.valid_url(url):
            raise ValueError("That doesn't look like an http(s) url.")
        with self.lock:
            session = self.session()
            if name in session.feeds.sections():
                raise ValueError("You already have a feed with that name.")
            session.feeds[name] = {"url": url}
            session.save_feeds()
        if downloadfrom:
            self.set_downloadfrom(name, downloadfrom)

    def set_downloadfrom(self, name, datestr):
        import greg.commands as commands
        import time as _t
        try:
            value = list(_t.strptime(datestr, "%Y-%m-%d"))
        except ValueError:
            raise ValueError("Date must be YYYY-MM-DD.")
        with self.lock:
            try:
                commands.edit(dict(self.args, name=name, downloadfrom=value,
                                   url=None))
            except SystemExit:  # greg's commands exit() on bad input
                raise KeyError(name)

    def set_url(self, name, url):
        if not self.valid_url(url):
            raise ValueError("That doesn't look like an http(s) url.")
        with self.lock:
            session = self.session()
            if name not in session.feeds:
                raise KeyError(name)
            session.feeds[name]["url"] = url
            session.save_feeds()

    def rename(self, old, new):
        if not self.valid_name(new):
            raise ValueError("Invalid new name.")
        with self.lock:
            session = self.session()
            if old not in session.feeds:
                raise KeyError(old)
            if new in session.feeds.sections():
                raise ValueError("A feed called {} already exists.".format(
                    new))
            session.feeds[new] = dict(session.feeds[old])
            session.feeds.remove_section(old)
            session.save_feeds()
            for suffix in ("", ".manual"):
                oldh = os.path.join(session.data_dir, old + suffix)
                if os.path.exists(oldh):
                    os.replace(oldh, os.path.join(session.data_dir,
                                                  new + suffix))

    def remove(self, name):
        with self.lock:
            session = self.session()
            if name not in session.feeds:
                raise KeyError(name)
            session.feeds.remove_section(name)
            session.save_feeds()
            for suffix in ("", ".manual"):
                hist = os.path.join(session.data_dir, name + suffix)
                if os.path.exists(hist):  # keep it, so this can be undone
                    keep = os.path.join(session.data_dir, "removed")
                    aux.ensure_dir(keep)
                    os.replace(hist, os.path.join(keep, "{}{}.{}".format(
                        name, suffix, int(time.time()))))

    def episodes(self, name):
        session = self.session()
        if name not in session.feeds:
            raise KeyError(name)
        podcast = aux.parse_podcast(session.feeds[name]["url"])
        links, _ = self.history(session, name)
        seen = set(links)
        manual_ids = set()
        try:
            with open(os.path.join(session.data_dir, name + ".manual")) as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                        manual_ids.add(rec.get("id"))
                    except ValueError:
                        pass
        except OSError:
            pass
        out = []
        for e in podcast.entries:
            files = [os.path.basename(urlparse(x.get("href", "")).path)
                     for x in e.get("enclosures", [])]
            out.append({
                "id": entry_id(e),
                "title": e.get("title") or e.get("link") or "(untitled)",
                "date": e.get("published") or e.get("updated") or "",
                "files": files,
                "downloaded": entry_id(e) in manual_ids or (
                    bool(files) and any(f in seen for f in files)),
            })
        title = podcast.feed.get("title", name) if hasattr(
            podcast, "feed") else name
        return {"title": title, "entries": out,
                "error": str(podcast.get("bozo_exception", ""))
                if podcast.get("bozo") and not out else ""}

    # -- greg.conf ---------------------------------------------------------

    def conf_path(self):
        return self.session().config_filename_user

    def get_config(self, feed=None):
        session = self.session()
        cfg = session.config
        section = feed if feed and cfg.has_section(feed) else \
            cfg.default_section
        values = {}
        for key in list(EDITABLE) + list(READONLY):
            values[key] = cfg.get(section, key, fallback="")
        overrides = {}
        if feed and cfg.has_section(feed):
            overrides = {k: cfg.get(feed, k, fallback="")
                         for k in cfg[feed] if k not in cfg.defaults()
                         or cfg[feed][k] != cfg.defaults().get(k)}
        return {"values": values, "editable": EDITABLE,
                "readonly": list(READONLY), "overrides": overrides,
                "path": self.conf_path(), "feed": feed}

    def set_config(self, feed, changes):
        keymap = {k.lower(): k for k in EDITABLE}
        clean = {}
        for k, v in changes.items():
            if k.lower() not in keymap:
                raise ValueError("'{}' can't be edited from the web.".format(
                    k))
            if v is not None:
                v = str(v)
                if "\n" in v or "\r" in v:
                    raise ValueError("Values must be a single line.")
                v = v.replace("%%", "\0").replace("%", "%%").replace(
                    "\0", "%%")
            clean[keymap[k.lower()]] = v
        if feed is not None and not self.valid_name(feed):
            raise ValueError("Invalid feed name.")
        with self.lock:
            path = self.conf_path()
            if not os.path.exists(path):
                aux.ensure_dir(os.path.dirname(path))
                shutil.copyfile(c.config_filename_global, path)
            with open(path, encoding="utf-8") as f:
                lines = f.read().split("\n")
            for k, v in clean.items():
                lines = conf_set(lines, feed or "DEFAULT", k, v)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            os.replace(tmp, path)


def conf_set(lines, section, key, value):
    """
    Set (or, with value None, remove) `key` in `section` of an ini file given
    as a list of lines, keeping comments and everything else intact.
    """
    header = re.compile(r'^\s*\[(.+?)\]\s*$')
    keyre = re.compile(r'^\s*' + re.escape(key) + r'\s*[=:]', re.I)
    start = end = None
    for i, line in enumerate(lines):
        m = header.match(line)
        if m:
            if start is not None and end is None:
                end = i
            if m.group(1) == section:
                start = i
    if start is not None and end is None:
        end = len(lines)
    if start is None:
        if value is None:
            return lines
        trimmed = list(lines)
        while trimmed and trimmed[-1].strip() == "":
            trimmed.pop()
        return trimmed + ["", "[{}]".format(section),
                          "{} = {}".format(key, value), ""]
    for i in range(start + 1, end):
        if keyre.match(lines[i]):
            if value is None:
                return lines[:i] + lines[i + 1:]
            return lines[:i] + ["{} = {}".format(key, value)] + lines[i + 1:]
    if value is None:
        return lines
    at = end
    while at > start + 1 and (lines[at - 1].strip() == "" or
                              lines[at - 1].lstrip().startswith("#")):
        at -= 1
    return lines[:at] + ["{} = {}".format(key, value)] + lines[at:]


# -- jobs -----------------------------------------------------------------

PROGRESS_RE = re.compile(r'^\s*\d+K [. ]')


class Job:
    def __init__(self, label, steps):
        self.id = uuid.uuid4().hex[:10]
        self.label = label
        self.steps = steps  # list of (description, argv, stdin_text)
        self.status = "queued"
        self.lines = []
        self.created = time.time()
        self.started = self.finished = None
        self.proc = None
        self.cancelled = False

    def info(self, with_lines=False, offset=0):
        d = {"id": self.id, "label": self.label, "status": self.status,
             "created": self.created, "started": self.started,
             "finished": self.finished, "nlines": len(self.lines)}
        if with_lines:
            d["lines"] = self.lines[offset:]
        return d


class JobManager:
    """Runs jobs one at a time, in order, each step in its own process."""
    def __init__(self, keep=40):
        self.jobs = collections.OrderedDict()
        self.keep = keep
        self.cv = threading.Condition()
        t = threading.Thread(target=self.worker, daemon=True)
        t.start()

    def submit(self, job):
        with self.cv:
            self.jobs[job.id] = job
            while len(self.jobs) > self.keep:
                oldest = next(iter(self.jobs))
                if self.jobs[oldest].status in ("queued", "running"):
                    break
                del self.jobs[oldest]
            self.cv.notify()
        return job

    def get(self, jid):
        return self.jobs.get(jid)

    def cancel(self, jid):
        job = self.get(jid)
        if not job:
            return False
        job.cancelled = True
        if job.status == "queued":
            job.status = "cancelled"
            job.finished = time.time()
        elif job.proc and job.proc.poll() is None:
            job.proc.terminate()
        return True

    def worker(self):
        while True:
            with self.cv:
                job = None
                while job is None:
                    for j in self.jobs.values():
                        if j.status == "queued":
                            job = j
                            break
                    else:
                        self.cv.wait()
            self.run(job)

    def run(self, job):
        job.status = "running"
        job.started = time.time()
        failed = False
        for desc, argv, stdin_text in job.steps:
            if job.cancelled:
                break
            job.lines.append("> " + desc)
            try:
                job.proc = subprocess.Popen(
                    argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT)
                if stdin_text is not None:
                    job.proc.stdin.write(stdin_text.encode())
                job.proc.stdin.close()
                buf = b""
                while True:
                    chunk = os.read(job.proc.stdout.fileno(), 4096)
                    if not chunk:
                        break
                    buf += chunk
                    parts = re.split(rb"[\r\n]", buf)
                    buf = parts.pop()
                    for p in parts:
                        line = p.decode("utf-8", "replace").rstrip()
                        if line and not PROGRESS_RE.match(line):
                            job.lines.append(line)
                            if len(job.lines) > 5000:
                                del job.lines[:1000]
                if buf.strip():
                    job.lines.append(buf.decode("utf-8", "replace").rstrip())
                rc = job.proc.wait()
                if rc:
                    failed = True
                    job.lines.append("(exit code {})".format(rc))
            except Exception as e:
                failed = True
                job.lines.append("Error: {}: {}".format(type(e).__name__, e))
        job.finished = time.time()
        job.status = "cancelled" if job.cancelled else (
            "failed" if failed else "done")


class Scheduler(threading.Thread):
    """
    Runs syncs on a timer, so no cron is needed (handy in a container).
    Each schedule: {"name": "hourly", "feeds": ["all"], "every_minutes": 60}.
    A schedule is skipped while its previous run is still queued or running.
    """
    def __init__(self, greg, jobs, schedules):
        threading.Thread.__init__(self, daemon=True)
        self.greg, self.jobs = greg, jobs
        self.schedules = []
        for i, sc in enumerate(schedules):
            every = float(sc.get("every_minutes", 0))
            if every < 1:
                continue
            self.schedules.append({
                "name": str(sc.get("name") or "schedule-{}".format(i + 1)),
                "feeds": sc.get("feeds") or ["all"],
                "every": every * 60,
                "next": time.time() + (0 if sc.get("run_on_start")
                                       else every * 60),
                "job": None})

    def describe(self):
        now = time.time()
        return [{"name": sc["name"], "feeds": sc["feeds"],
                 "every_minutes": sc["every"] / 60,
                 "next_in_seconds": max(0, int(sc["next"] - now))}
                for sc in self.schedules]

    def run(self):
        while True:
            now = time.time()
            for sc in self.schedules:
                if now < sc["next"]:
                    continue
                sc["next"] = now + sc["every"]
                prev = sc["job"]
                if prev is not None and prev.status in ("queued", "running"):
                    continue
                try:
                    feeds = sc["feeds"]
                    if feeds == ["all"]:
                        feeds = self.greg.session().list_feeds()
                    if not feeds:
                        continue
                    sc["job"] = self.jobs.submit(Job(
                        "scheduled: {}".format(sc["name"]),
                        build_steps(self.greg, "sync", feeds)))
                except Exception as e:
                    sys.stderr.write("scheduler {}: {!r}\n".format(
                        sc["name"], e))
            time.sleep(15)


def schedules_from(settings):
    """web.json "schedules", or GREG_SYNC_EVERY_MINUTES for a single one."""
    schedules = list(settings.get("schedules") or [])
    env = os.environ.get("GREG_SYNC_EVERY_MINUTES")
    if env and not schedules:
        schedules = [{"name": "sync all", "feeds": ["all"],
                      "every_minutes": float(env)}]
    return schedules


def worker_argv(greg, *rest):
    argv = [sys.executable, "-m", "greg.jobs"] + list(rest)
    # pass through the same config/data location as the web server uses
    extra = []
    if greg.args.get("configfile"):
        extra += ["--configfile", greg.args["configfile"]]
    if greg.args.get("datadirectory"):
        extra += ["--datadirectory", greg.args["datadirectory"]]
    return argv[:4] + extra + argv[4:]


def build_steps(greg, action, feeds, ids=None):
    """One step per feed, wrapped in the vpn prefix for feeds that need it."""
    session = greg.session()
    steps = []
    for name in feeds:
        if name not in session.feeds:
            raise KeyError(name)
        url = session.feeds[name].get("url", "")
        via, _ = greg.route(session, name, url)
        # a proxy is applied by greg itself (from greg.conf); only the
        # advanced wrapper needs a command prefix
        prefix = greg.settings.get("vpn_prefix", [])             if via == "wrapper" else []
        note = " (via {})".format(via) if via else ""
        if action == "sync":
            argv = prefix + worker_argv(greg, "sync", name)
            steps.append(("sync {}{}".format(name, note), argv, None))
        else:
            argv = prefix + worker_argv(greg, "download", name)
            steps.append(("download {} ({} episodes){}".format(
                name, len(ids), note), argv, json.dumps(ids)))
    return steps


# -- status ---------------------------------------------------------------

def system_status(greg):
    session = greg.session()
    conf = session.config
    dl = os.path.expanduser(conf.get(conf.default_section, "Download directory",
                                     fallback="~/Podcasts"))
    out = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "checks": []}
    paths = [("Download directory", dl), ("Data directory", session.data_dir)]
    disks = []
    for label, p in paths:
        try:
            u = shutil.disk_usage(p if os.path.exists(p) else "/")
            disks.append({"label": label, "path": p, "free": u.free,
                          "total": u.total})
        except OSError:
            pass
    out["disks"] = disks
    sched = getattr(greg, "scheduler", None)
    out["schedules"] = sched.describe() if sched else []
    st = greg.settings.get("status", {})
    for m in st.get("mounts", []):
        ok = os.path.ismount(m)
        out["checks"].append({"kind": "mount", "name": m, "ok": ok,
                              "detail": "mounted" if ok else "NOT mounted"})
    for i in st.get("interfaces", []):
        ok = os.path.exists("/sys/class/net/" + i)
        out["checks"].append({"kind": "interface", "name": i, "ok": ok,
                              "detail": "up" if ok else "missing"})
    for p in st.get("processes", []):
        n = 0
        try:
            r = subprocess.Popen(["pgrep", "-c", "-x", p],
                                 stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL)
            n = int(r.communicate(timeout=5)[0].decode().strip() or 0)
        except Exception:
            n = 0
        out["checks"].append({"kind": "process", "name": p, "ok": n > 0,
                              "detail": "{} running".format(n)})
    return out


def vpn_test(greg):
    """Public address seen directly and through every configured route."""
    session = greg.session()
    routes = []
    seen = set()
    for name in session.list_feeds():
        pr = aux.proxy_for(session, name, session.feeds[name].get("url", ""))
        if pr and pr not in seen:
            seen.add(pr)
            routes.append(("proxy " + aux.mask_proxy(pr), pr, None))
    prefix = greg.settings.get("vpn_prefix")
    if prefix:
        routes.append(("wrapper", None, list(prefix)))
    if not routes:
        return {"configured": False}

    def via_wrapper(cmd):
        try:
            r = subprocess.Popen(cmd + ["curl", "-s", "--max-time", "12",
                                        "https://ifconfig.me/ip"],
                                 stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL)
            return r.communicate(timeout=20)[0].decode().strip()
        except Exception:
            return ""
    direct = aux.egress_ip()
    out = []
    for label, pr, cmd in routes:
        ip = via_wrapper(cmd) if cmd else aux.egress_ip(pr)
        ok = bool(ip) and not ip.startswith("error") and ip != direct
        out.append({"label": label, "ip": ip, "ok": ok})
    return {"configured": True, "direct": direct, "routes": out}


# -- http -----------------------------------------------------------------

UI_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "data", "webui.html")


class Server(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_handler(greg, jobs, settings):
    throttle = Throttle()
    user = settings.get("username", "greg")
    pwhash = settings["password_hash"]
    trust_proxy = bool(settings.get("trust_proxy"))
    allowed_hosts = [h.lower() for h in settings.get("allowed_hosts", [])]

    class Handler(BaseHTTPRequestHandler):
        server_version = "greg-web"
        sys_version = ""
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *a):
            sys.stderr.write("%s %s\n" % (self.client_ip(), fmt % a))

        def client_ip(self):
            if trust_proxy:
                fwd = self.headers.get("X-Forwarded-For", "")
                if fwd:
                    return fwd.split(",")[0].strip()
            return self.client_address[0]

        # -- plumbing --
        def send_json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.security_headers()
            self.end_headers()
            self.wfile.write(body)

        def security_headers(self):
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                "script-src 'self' 'unsafe-inline'; frame-ancestors 'none'")

        def fail(self, code, msg):
            self.send_json({"error": msg}, code)

        def authorised(self):
            ip = self.client_ip()
            if throttle.blocked(ip):
                self.send_response(429)
                self.send_header("Content-Length", "0")
                self.send_header("Retry-After", "300")
                self.end_headers()
                return False
            hdr = self.headers.get("Authorization", "")
            ok = False
            if hdr.startswith("Basic "):
                try:
                    u, _, p = base64.b64decode(hdr[6:]).decode().partition(
                        ":")
                    ok = hmac.compare_digest(u, user) and \
                        check_password(p, pwhash)
                except Exception:
                    ok = False
            if not ok:
                if hdr:
                    throttle.fail(ip)
                self.send_response(401)
                self.send_header("WWW-Authenticate",
                                 'Basic realm="greg", charset="UTF-8"')
                self.send_header("Content-Length", "0")
                self.end_headers()
            return ok

        def body(self):
            return json.loads(self._raw.decode() or "{}")

        def handle_any(self, method):
            # always consume the request body, so keep-alive stays in sync
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            if n > 1 << 20:
                self.close_connection = True
                return self.fail(413, "body too large")
            self._raw = self.rfile.read(n) if n else b"{}"
            if allowed_hosts and (self.headers.get("Host", "").split(":")[0]
                                  .lower() not in allowed_hosts):
                return self.fail(400, "bad host")
            if not self.authorised():
                return
            if method != "GET":
                # custom header + JSON: a cross-site form can't send these
                if self.headers.get("X-Greg-Request") != "1":
                    return self.fail(403, "missing X-Greg-Request header")
            url = urlparse(self.path)
            path = unquote(url.path)
            query = parse_qs(url.query)
            try:
                if path in ("/", "/index.html") and method == "GET":
                    return self.serve_ui()
                if path.startswith("/api/"):
                    return self.api(method, path[5:].strip("/").split("/"),
                                    query)
                self.fail(404, "not found")
            except KeyError as e:
                self.fail(404, "not found: {}".format(e))
            except ValueError as e:
                self.fail(400, str(e))
            except Exception as e:  # never leak a traceback to the client
                sys.stderr.write("error: {!r}\n".format(e))
                self.fail(500, "{}: {}".format(type(e).__name__, e))

        def serve_ui(self):
            with open(UI_FILE, "rb") as f:
                html = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.send_header("Cache-Control", "no-store")
            self.security_headers()
            self.end_headers()
            self.wfile.write(html)

        # -- api --
        def api(self, method, parts, query):
            head = parts[0]
            if head == "feeds":
                return self.api_feeds(method, parts[1:])
            if head == "jobs":
                return self.api_jobs(method, parts[1:], query)
            if head == "config":
                if method == "GET":
                    return self.send_json(greg.get_config(
                        (query.get("feed") or [None])[0]))
                if method == "PUT":
                    b = self.body()
                    greg.set_config(b.get("feed") or None,
                                    b.get("values") or {})
                    return self.send_json({"ok": True})
            if head == "status" and method == "GET":
                st = system_status(greg)
                if query.get("vpn"):
                    st["vpn"] = vpn_test(greg)
                return self.send_json(st)
            self.fail(404, "not found")

        def api_feeds(self, method, parts):
            if not parts:
                if method == "GET":
                    return self.send_json(greg.feeds())
                if method == "POST":
                    b = self.body()
                    greg.add((b.get("name") or "").strip(),
                             (b.get("url") or "").strip(),
                             (b.get("downloadfrom") or "").strip() or None)
                    return self.send_json({"ok": True}, 201)
            else:
                name = parts[0]
                sub = parts[1] if len(parts) > 1 else None
                if sub is None and method == "DELETE":
                    greg.remove(name)
                    return self.send_json({"ok": True})
                if sub is None and method == "PATCH":
                    b = self.body()
                    if b.get("url"):
                        greg.set_url(name, b["url"].strip())
                    if b.get("downloadfrom"):
                        greg.set_downloadfrom(name, b["downloadfrom"].strip())
                    if b.get("new_name") and b["new_name"] != name:
                        greg.rename(name, b["new_name"].strip())
                    return self.send_json({"ok": True})
                if sub == "url" and method == "GET":
                    return self.send_json({"url": greg.url(name)})
                if sub == "episodes" and method == "GET":
                    return self.send_json(greg.episodes(name))
            self.fail(404, "not found")

        def api_jobs(self, method, parts, query):
            if not parts:
                if method == "GET":
                    return self.send_json([j.info() for j in reversed(
                        list(jobs.jobs.values()))])
                if method == "POST":
                    b = self.body()
                    action = b.get("action")
                    feeds = b.get("feeds") or []
                    if action == "sync":
                        if feeds == ["all"] or not feeds:
                            feeds = greg.session().list_feeds()
                        if not feeds:
                            raise ValueError("No feeds to sync.")
                        label = "sync " + (feeds[0] if len(feeds) == 1
                                           else "{} feeds".format(len(feeds)))
                        steps = build_steps(greg, "sync", feeds)
                    elif action == "download":
                        ids = b.get("ids") or []
                        if len(feeds) != 1 or not ids:
                            raise ValueError("Pick one feed and some "
                                             "episodes.")
                        label = "download {} ({})".format(feeds[0], len(ids))
                        steps = build_steps(greg, "download", feeds, ids)
                    else:
                        raise ValueError("Unknown action.")
                    job = jobs.submit(Job(label, steps))
                    return self.send_json(job.info(), 201)
            else:
                job = jobs.get(parts[0])
                if job is None:
                    raise KeyError(parts[0])
                if len(parts) == 1 and method == "GET":
                    off = int((query.get("offset") or ["0"])[0])
                    return self.send_json(job.info(True, off))
                if len(parts) == 2 and parts[1] == "cancel" and \
                        method == "POST":
                    jobs.cancel(parts[0])
                    return self.send_json({"ok": True})
            self.fail(404, "not found")

        def do_GET(self):
            self.handle_any("GET")

        def do_POST(self):
            self.handle_any("POST")

        def do_PUT(self):
            self.handle_any("PUT")

        def do_PATCH(self):
            self.handle_any("PATCH")

        def do_DELETE(self):
            self.handle_any("DELETE")

    return Handler


def run(args):
    """Entry point for `greg web`."""
    settings = load_settings(args)
    if args.get("set_password"):
        minlen = int(settings.get("min_password_length", 12))
        pw = getpass.getpass("New web UI password: ")
        if len(pw) < minlen:
            sys.exit("Please use at least {} characters (see "
                     "min_password_length in the web settings).".format(
                         minlen))
        if pw != getpass.getpass("Again: "):
            sys.exit("Passwords don't match.")
        settings["password_hash"] = hash_password(pw)
        settings.setdefault("username", "greg")
        save_settings(args, settings)
        print("Saved to {} (user: {}).".format(
            settings_path(args), settings["username"]))
        return
    if not settings.get("password_hash"):
        sys.exit("No password set. Run: greg web --set-password")
    host = args.get("host") or settings.get("host", "127.0.0.1")
    port = int(args.get("port") or settings.get("port", 8787))
    greg = Greg(args, settings)
    jobs = JobManager()
    greg.scheduler = Scheduler(greg, jobs, schedules_from(settings))
    greg.scheduler.start()
    httpd = Server((host, port), make_handler(greg, jobs, settings))
    print("greg web listening on http://{}:{}".format(host, port), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
