"""
Worker entry points used by the web UI. Each job runs in its own process
(`python -m greg.jobs ...`), so the web server can stream its output and run
it under a different network group if needed.

    python -m greg.jobs sync NAME [NAME ...]
    python -m greg.jobs download NAME        (JSON list of entry ids on stdin)
"""
import json
import os
import sys
from urllib.parse import urlparse

import greg.aux_functions as aux
import greg.classes as c
import greg.commands as commands


def entry_id(entry):
    """A stable identifier for a feed entry."""
    return (entry.get("id") or entry.get("link") or
            "{}|{}".format(entry.get("title", ""), entry.get("published", "")))


def record_manual(session, name, entry):
    """
    Remember episodes fetched by hand in a separate ledger. They are kept out
    of the sync history on purpose: a manual download of a newer episode would
    otherwise move the sync watermark forward and skip older ones.
    """
    path = os.path.join(session.data_dir, name + ".manual")
    files = [os.path.basename(urlparse(x.get("href", "")).path)
             for x in entry.get("enclosures", [])]
    with open(path, "a") as f:
        f.write(json.dumps({"id": entry_id(entry), "files": files}) + "\n")


def parse_common(argv):
    """Pull out --configfile/--datadirectory, return (args, rest)."""
    args = {"configfile": None, "datadirectory": None}
    rest = []
    it = iter(argv)
    for a in it:
        if a == "--configfile":
            args["configfile"] = next(it)
        elif a == "--datadirectory":
            args["datadirectory"] = next(it)
        else:
            rest.append(a)
    return args, rest


def do_sync(args, names):
    commands.sync(dict(args, names=names))


def do_download(args, name):
    wanted = json.load(sys.stdin)
    session = c.Session(args)
    if name not in session.feeds:
        sys.exit("You don't have a feed called {}.".format(name))
    print("Fetching feed {}...".format(name), flush=True)
    podcast = aux.parse_podcast(session.feeds[name]["url"])
    feed = c.Feed(session, name, podcast)
    by_id = {entry_id(e): e for e in podcast.entries}
    failed = 0
    for wid in wanted:
        entry = by_id.get(wid)
        if entry is None:
            print("Entry not found in feed any more: {}".format(wid),
                  file=sys.stderr, flush=True)
            failed += 1
            continue
        try:
            # like `greg download`: don't touch the sync history
            feed.info = []
            feed.entrylinks = []
            feed.fix_linkdate(entry)
            if feed.download_entry(entry):
                record_manual(session, name, entry)
        except Exception as e:
            failed += 1
            print("Problem downloading {}: {}: {}".format(
                entry.get("title", wid), type(e).__name__, e),
                file=sys.stderr, flush=True)
    print("Done" + ("" if not failed else " ({} problems)".format(failed)),
          flush=True)
    if failed:
        sys.exit(1)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] not in ("sync", "download"):
        sys.exit(__doc__)
    args, rest = parse_common(argv[1:])
    if argv[0] == "sync":
        if not rest:
            sys.exit("sync needs at least one feed name")
        do_sync(args, rest)
    else:
        if len(rest) != 1:
            sys.exit("download needs exactly one feed name")
        do_download(args, rest[0])


if __name__ == "__main__":
    main()
