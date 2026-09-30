"""
A curses front-end for greg: manage feeds, sync, and browse/download episodes.
"""
import contextlib
import curses
import os.path
import threading
import time

import greg.aux_functions as aux
import greg.classes as c
import greg.commands as commands


class LogBuffer:
    """File-like object that collects output from commands as log lines."""
    def __init__(self):
        self.lines = []
        self.lock = threading.Lock()
        self._partial = ""

    def write(self, text):
        with self.lock:
            text = self._partial + text.replace("\r", "")
            *done, self._partial = text.split("\n")
            self.lines.extend(done)
        return len(text)

    def flush(self):
        pass

    def add(self, line):
        with self.lock:
            self.lines.append(line)

    def tail(self, n):
        with self.lock:
            return self.lines[-n:] if n > 0 else []


class Job:
    """Runs a callable in a thread with stdout/stderr captured into the log."""
    def __init__(self, log, label, func):
        self.log, self.label = log, label
        self.error = None
        self.thread = threading.Thread(target=self._run, args=(func,),
                                       daemon=True)
        self.thread.start()

    def _run(self, func):
        self.log.add("> " + self.label)
        try:
            with contextlib.redirect_stdout(self.log), \
                    contextlib.redirect_stderr(self.log):
                func()
        except SystemExit as e:
            if e.code:
                self.log.add(str(e.code))
        except Exception as e:  # keep the UI alive whatever a feed does
            self.log.add("Error: {}: {}".format(type(e).__name__, e))
        self.log.add("Done: " + self.label)

    @property
    def running(self):
        return self.thread.is_alive()


class App:
    def __init__(self, stdscr, args):
        self.scr = stdscr
        self.args = {k: v for k, v in args.items() if k != "func"}
        self.log = LogBuffer()
        self.job = None
        self.status = ""
        self.sel = 0
        self.top = 0
        self.feeds = []
        self.reload()
        curses.curs_set(0)
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_CYAN, -1)
        curses.init_pair(2, curses.COLOR_YELLOW, -1)
        curses.init_pair(3, curses.COLOR_GREEN, -1)
        stdscr.timeout(150)
        stdscr.keypad(True)

    # -- helpers ----------------------------------------------------------

    def cmd_args(self, **extra):
        a = dict(self.args)
        a.update(extra)
        return a

    def reload(self):
        session = c.Session(self.cmd_args())
        self.feeds = session.list_feeds()
        self.urls = {f: session.feeds[f].get("url", "") for f in self.feeds}
        self.sel = min(self.sel, max(len(self.feeds) - 1, 0))

    def current(self):
        return self.feeds[self.sel] if self.feeds else None

    def put(self, y, x, text, attr=0, width=None):
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return
        room = (w - x) if width is None else min(width, w - x)
        if y == h - 1:
            room -= 1  # avoid curses error writing the bottom-right cell
        try:
            self.scr.addstr(y, x, text[:max(room, 0)], attr)
        except curses.error:
            pass

    def prompt(self, label, initial=""):
        """Single-line input on the bottom row. Returns None on Esc."""
        buf = list(initial)
        curses.curs_set(1)
        self.scr.timeout(-1)
        try:
            while True:
                h, w = self.scr.getmaxyx()
                self.scr.move(h - 1, 0)
                self.scr.clrtoeol()
                shown = label + "".join(buf)
                self.put(h - 1, 0, shown[-(w - 2):], curses.A_BOLD)
                self.scr.move(h - 1, min(len(shown), w - 2))
                ch = self.scr.get_wch()
                if ch in ("\n", "\r") or ch == curses.KEY_ENTER:
                    return "".join(buf).strip()
                if ch == "\x1b":
                    return None
                if ch in ("\x7f", "\b", curses.KEY_BACKSPACE):
                    if buf:
                        buf.pop()
                elif ch == "\x15":  # ctrl-u
                    buf.clear()
                elif isinstance(ch, str) and ch.isprintable():
                    buf.append(ch)
        finally:
            curses.curs_set(0)
            self.scr.timeout(150)

    def confirm(self, text):
        answer = self.prompt(text + " (y/N) ")
        return bool(answer) and answer.lower().startswith("y")

    def start(self, label, func):
        if self.job and self.job.running:
            self.status = "A job is already running."
            return
        self.job = Job(self.log, label, func)

    # -- drawing ----------------------------------------------------------

    def draw_frame(self, title, footer):
        self.scr.erase()
        h, w = self.scr.getmaxyx()
        self.put(0, 0, " " + title + " " * w, curses.A_REVERSE)
        busy = self.job and self.job.running
        if busy:
            spin = "|/-\\"[int(time.time() * 6) % 4]
            self.put(0, w - len(self.job.label) - 6,
                     " {} {} ".format(spin, self.job.label), curses.A_REVERSE)
        return h, w

    def draw_log(self, top, bottom):
        h, w = self.scr.getmaxyx()
        rows = bottom - top - 1
        if rows < 1:
            return
        self.put(top, 0, "-- log " + "-" * w, curses.color_pair(1))
        for i, line in enumerate(self.log.tail(rows)):
            self.put(top + 1 + i, 1, line)

    def draw_footer(self, text):
        h, w = self.scr.getmaxyx()
        self.put(h - 1, 0, self.status or text,
                 curses.color_pair(2) if self.status else curses.A_DIM)

    def draw_main(self):
        h, w = self.draw_frame("greg - podcast manager", "")
        list_w = max(min(w // 3, 32), 16)
        log_h = max(min(h // 3, 10), 4)
        body_bottom = h - log_h - 1
        rows = body_bottom - 2
        if self.sel < self.top:
            self.top = self.sel
        if self.sel >= self.top + rows:
            self.top = self.sel - rows + 1
        self.put(1, 1, "Feeds ({})".format(len(self.feeds)), curses.A_BOLD)
        if not self.feeds:
            self.put(3, 2, "No feeds yet. Press 'a' to add one.")
        for i in range(rows - 1):
            idx = self.top + i
            if idx >= len(self.feeds):
                break
            attr = curses.A_REVERSE if idx == self.sel else 0
            self.put(2 + i, 1, (" " + self.feeds[idx]).ljust(list_w - 2), attr,
                     list_w - 2)
        for y in range(1, body_bottom):
            self.put(y, list_w, "|", curses.color_pair(1))
        name = self.current()
        if name:
            self.put(1, list_w + 2, name, curses.A_BOLD)
            self.put(3, list_w + 2, "url: " + self.urls.get(name, ""))
            self.put(4, list_w + 2, "last download: " + self.last_date(name))
            self.put(6, list_w + 2, "Enter  browse/download episodes",
                     curses.A_DIM)
        self.draw_log(body_bottom, h - 1)
        self.draw_footer("a add  d remove  e edit url  s sync  S sync all  "
                         "Enter episodes  r reload  q quit")
        self.scr.refresh()

    def last_date(self, name):
        session = c.Session(self.cmd_args())
        _, dates = aux.parse_feed_info(os.path.join(session.data_dir, name))
        if not dates:
            return "never"
        try:
            return time.strftime("%d %b %Y %H:%M", tuple(max(dates)))
        except (TypeError, ValueError):
            return "unknown"

    # -- actions ----------------------------------------------------------

    def do_add(self):
        name = self.prompt("New feed name: ")
        if not name:
            return
        url = self.prompt("Feed url: ")
        if not url:
            return
        if not url.startswith(("http://", "https://")):
            self.status = "Url must start with http:// or https://"
            return
        self.start("add " + name, lambda: commands.add(
            self.cmd_args(name=name, url=url, downloadfrom=None)))
        self.job.thread.join()
        self.reload()
        if name in self.feeds:
            self.sel = self.feeds.index(name)

    def do_remove(self):
        name = self.current()
        if name and self.confirm("Remove '{}'?".format(name)):
            self.start("remove " + name, lambda: commands.remove(
                self.cmd_args(name=name, force=True)))
            self.job.thread.join()
            self.reload()

    def do_edit(self):
        name = self.current()
        if not name:
            return
        url = self.prompt("New url: ", self.urls.get(name, ""))
        if url and url.startswith(("http://", "https://")):
            self.start("edit " + name, lambda: commands.edit(
                self.cmd_args(name=name, url=url)))
            self.job.thread.join()
            self.reload()

    def do_sync(self, names):
        if not names:
            return
        self.start("sync " + (names[0] if len(names) == 1 else "all"),
                   lambda: commands.sync(self.cmd_args(names=names)))

    def browse(self):
        name = self.current()
        if not name:
            return
        self.status = "Fetching {}...".format(name)
        self.draw_main()
        self.status = ""
        session = c.Session(self.cmd_args())
        podcast = aux.parse_podcast(session.feeds[name]["url"])
        if not podcast.entries:
            self.status = "No episodes found (or feed unreachable)."
            return
        EpisodeView(self, session, name, podcast).run()

    def run(self):
        while True:
            self.draw_main()
            try:
                ch = self.scr.get_wch()
            except curses.error:
                continue
            except KeyboardInterrupt:
                return
            if ch != curses.KEY_RESIZE:
                self.status = ""
            if ch in ("q", "Q"):
                if self.job and self.job.running and not self.confirm(
                        "A job is running. Quit anyway?"):
                    continue
                return
            elif ch in (curses.KEY_UP, "k"):
                self.sel = max(self.sel - 1, 0)
            elif ch in (curses.KEY_DOWN, "j"):
                self.sel = min(self.sel + 1, max(len(self.feeds) - 1, 0))
            elif ch == curses.KEY_HOME:
                self.sel = 0
            elif ch == curses.KEY_END:
                self.sel = max(len(self.feeds) - 1, 0)
            elif ch == "a":
                self.do_add()
            elif ch == "d":
                self.do_remove()
            elif ch == "e":
                self.do_edit()
            elif ch == "s" and self.current():
                self.do_sync([self.current()])
            elif ch == "S":
                self.do_sync(["all"])
            elif ch == "r":
                self.reload()
            elif ch in ("\n", "\r", curses.KEY_ENTER, curses.KEY_RIGHT):
                self.browse()


class EpisodeView:
    def __init__(self, app, session, name, podcast):
        self.app, self.session, self.name = app, session, name
        self.podcast = podcast
        self.entries = podcast.entries
        self.marked = set()
        self.sel = 0
        self.top = 0

    @staticmethod
    def label(entry):
        title = entry.get("title") or entry.get("link") or "(untitled)"
        date = entry.get("updated") or entry.get("published") or ""
        return title, date[:16]

    def download(self, indices):
        feed = c.Feed(self.session, self.name, self.podcast)

        def work():
            for i in indices:
                entry = self.entries[i]
                feed.info = []  # don't touch the sync history
                feed.entrylinks = []
                feed.fix_linkdate(entry)
                feed.download_entry(entry)
        self.app.start("download {} ({})".format(self.name, len(indices)),
                       work)

    def draw(self):
        app = self.app
        h, w = app.draw_frame("{} - {} episodes".format(
            self.name, len(self.entries)), "")
        log_h = max(min(h // 4, 8), 4)
        rows = h - log_h - 2
        if self.sel < self.top:
            self.top = self.sel
        if self.sel >= self.top + rows:
            self.top = self.sel - rows + 1
        for i in range(rows):
            idx = self.top + i
            if idx >= len(self.entries):
                break
            title, date = self.label(self.entries[idx])
            mark = "[x]" if idx in self.marked else "[ ]"
            line = "{} {:>3} {}".format(mark, idx, title)
            attr = curses.A_REVERSE if idx == self.sel else 0
            if idx in self.marked and idx != self.sel:
                attr = curses.color_pair(3)
            app.put(1 + i, 0, line.ljust(w), attr, w)
            if w > 60:
                app.put(1 + i, w - 17, date, attr)
        app.draw_log(h - log_h - 1, h - 1)
        app.draw_footer("Space mark  a mark all  Enter download marked/current"
                        "  Esc back")
        app.scr.refresh()

    def run(self):
        app = self.app
        while True:
            self.draw()
            try:
                ch = app.scr.get_wch()
            except curses.error:
                continue
            except KeyboardInterrupt:
                return
            app.status = ""
            last = len(self.entries) - 1
            if ch in ("q", "\x1b", curses.KEY_LEFT):
                return
            elif ch in (curses.KEY_UP, "k"):
                self.sel = max(self.sel - 1, 0)
            elif ch in (curses.KEY_DOWN, "j"):
                self.sel = min(self.sel + 1, last)
            elif ch == curses.KEY_PPAGE:
                self.sel = max(self.sel - 10, 0)
            elif ch == curses.KEY_NPAGE:
                self.sel = min(self.sel + 10, last)
            elif ch == curses.KEY_HOME:
                self.sel = 0
            elif ch == curses.KEY_END:
                self.sel = last
            elif ch == " ":
                self.marked ^= {self.sel}
                self.sel = min(self.sel + 1, last)
            elif ch == "a":
                if len(self.marked) == len(self.entries):
                    self.marked.clear()
                else:
                    self.marked = set(range(len(self.entries)))
            elif ch in ("\n", "\r", curses.KEY_ENTER):
                todo = sorted(self.marked) or [self.sel]
                self.download(todo)
                self.marked.clear()


def run(args):
    """Entry point for `greg tui`."""
    def main(stdscr):
        App(stdscr, args).run()
    curses.wrapper(main)
