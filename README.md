# greg

A command-line podcast aggregator, written in python. It basically exposes some
of the functionality of the excellent
[feedparser](https://pypi.org/project/feedparser/).

Besides the command line, this version has a [terminal interface](#terminal-interface),
a [web interface](#web-interface), a [Docker setup](#docker-and-sending-some-feeds-through-a-vpn),
and can send chosen feeds [through a VPN or proxy](docs/vpn.md).

## Installation

Clone this repository and install it with [pip](https://pip.pypa.io/en/stable/):

```
git clone https://github.com/dustinreeves/greg
cd greg
pip install --user .
```

greg downloads with [curl](https://curl.se/), so it needs to be installed (it
already is on current Windows, macOS and most Linux systems).

Optional extras: `pip install --user ".[tagging]"` for ID3 tagging (EyeD3) and
`".[socks]"` if you use a SOCKS proxy. On Windows, the terminal interface uses
`windows-curses`, which is installed automatically.

## Configuration

To edit the configuration for greg, copy the system-wide
[greg.conf](greg/data/greg.conf) file to your local config folder:

```
mkdir -p ~/.config/greg && cp `greg retrieveglobalconf` ~/.config/greg/greg.conf
```

Then open and edit `~/.config/greg/greg.conf` in a text editor. The configuration file is self-explanatory.

## Usage

Let's start by adding a feed (RSS or Atom versions will do):

    greg add mssp https://feeds.megaphone.fm/GLT1158789509

The `add` command expects a name and a url of an RSS or Atom feed. You will use this name to refer to the feed whenever you interact with it.

If you were to run `greg sync` now, it would download the latest episode of the podcast to the default directory (which is `~/Podcasts`; you can change how many episodes are dowloaded in the first sync, and the download directory, in the config file; see below). But maybe we just want to check out what this podcast is all about, so we download a list of available entries:

    greg check -f mssp

(the `-f` flag means that "mssp" is the name of a feed. `greg check` also accepts urls directly, using the `-u` flag.)

This will give you the following kind of info:


    0: Ep 638 - Beef Raised Right (feat. Mason 'Bric' LaDue) (Tue, 29 Sep 2026 12:00:00 -0000)
    1: Ep 637 - Throat Wrecker (feat. Jeremiah Watkins) (Wed, 23 Sep 2026 12:00:00 -0000)
    2: Ep 636 - Mr. Chili's (feat. Sam Tallent) (Wed, 16 Sep 2026 12:00:00 -0000)
    3: Ep 635 - Podcaster's Union (feat. Kevin Ryan & H. Foley) (Thu, 10 Sep 2026 12:00:00 -0000)
    4: Ep 634 - Department of Jokes (feat. Yakov Smirnoff) (Fri, 04 Sep 2026 12:00:00 -0000)
    5: Ep 633 - Submerged in Silence (Wed, 26 Aug 2026 12:00:00 -0000)
    6: Ep 632 - R.I.P. Andy (feat. Tim Butterly) (Fri, 21 Aug 2026 12:00:00 -0000)
    7: Ep 631 - B.B.Q. (feat. Joe Derosa) (Wed, 12 Aug 2026 12:00:00 -0000)
    8: Ep 630 - Buildings (feat. Michael P. Murphy) (Mon, 10 Aug 2026 12:00:00 -0000)
    9: Ep 629 - Like and Subscribe (feat. Danny McBride & Steve Gerben) (Sat, 08 Aug 2026 11:00:00 -0000)
    ...

Interesting stuff. We'll download a couple of episodes, just to make sure that
it's really worth it:

    greg download 1, 4-5

and Greg says

    Downloading Ep 637 - Throat Wrecker (feat. Jeremiah Watkins) -- GLT5154354170.mp3
    Done
    Downloading Ep 634 - Department of Jokes (feat. Yakov Smirnoff) -- GLT3142585669.mp3
    Done
    Downloading Ep 633 - Submerged in Silence -- GLT5281904541.mp3
    Done

As you can see, `greg download` accepts a range of episodes of the kind `a, b,
c-f, h, ...`. The numbers make reference to the numbers at the beginning of
each entry provided by `greg check`. `check` creates a persistent file
(`feeddump` in the data directory, `~/.local/share/greg/data by` default, but
you can change that in the config file, or passing a different path with the
`--datadirectory` flag), so `download` will keep on working, and referring to
the last `check` ever done.

All of these podcasts will be downloaded to the default download directory for
the feed (if you used the `-f` flag) or the general default download directory
(again, `~/Podcasts` if you don't tell Greg otherwise. We'll learn how to
change that soon), inside a subdirectory named after the podcast (we can change
that default too.) After listening to them we decide that this podcast is well
worth our time, and keep it, or we decide that it's not, and

    greg remove mssp

If we keep it, we might want to start `sync`ing from, say, the 1st of September,
2026, on. So we edit the feed information

    greg edit mssp -d 2026-09-01

We may also use the `now` keyword to instruct greg to start syncing from now
on:

    greg edit mssp -d now

`-d` or `--downloadfrom` change the date after which Greg should start
downloading episodes when it syncs. Currently, the only two things one can
`edit` in a feed are the download-from date and `--url` -- but many more things
can be changed by editing the config file. `greg edit -h` will give help you
with the `edit` options and syntax -- likewise for the rest of Greg
subcommands.

All right. Let's add a second feed:

    greg add lemonparty https://feeds.megaphone.fm/TPC2913664141

If you want to keep track of the feeds you have added, you can ask Greg:

    greg info

which returns

    mssp
    ----
        url: https://feeds.megaphone.fm/GLT1158789509
        Next sync will download from: 01 Sep 2026 00:00:00.

    lemonparty
    ----------
        url: https://feeds.megaphone.fm/TPC2913664141

Some podcasts do not tag their episodes, which is bad for most portable media
players. Greg uses [EyeD3](https://github.com/nicfit/eyeD3) (as an optional
dependency, `pip install ".[tagging]"`) to tag podcasts, if one so wishes. By
default, it uses the podcast name for the *artist* tag, and the entry title for
the *title* tag. To enable tagging for lemonparty, copy the system-wide config
file locally. (see [Configuration](#configuration) above)

Then, add a section for lemonparty:

    [lemonparty]

    Tag = yes

In fact, you can fill out any tag however you see fit. For example,

    tag_genre = Comedy
    tag_comment = {date}

will fill the *genre* tag with the string "Comedy", and the *comment*
tag with the download date.

Let's add a video podcast

    greg add myvideos https://example.com/video-feed.xml

By default, Greg only donwloads audio files (in fact, files that have "audio"
as part of their type). In order to download the right file in a video feed,
you need to change that in the config file. Again, add a section:

    [myvideos]

    mime = video

You could also have a couple of types there, as in `mime = audio, video`; or
any other type, `mime = torrent`, or whatever.

Another useful thing that you can change in the config file is the download
handler. Greg by default downloads with [curl](https://curl.se/) (which must be
installed; it is on current Windows, macOS and nearly every Linux): to a
temporary `.part` file that is renamed when the download is complete, with
retries, and through your `proxy` if you set one. You can use something else if
you need to, for example

    downloadhandler = yt-dlp {link} -o "{directory}/{date}_{filename}"

in your local `greg.conf`. You can do all sorts of nice things with this. For
example, when `check`ing a podcast, you don't need to download it, but maybe
just stream it, like this:

    greg download 0 --downloadhandler "mplayer {link}"

If you want to ensure that the downloaded files are in chronological order, you
can use placeholders to add the date at the beginning, like this:

    download_filename = {date}_{filename}

One last thing: if you subscribe to a very active feed, and you are only
interested in some of the entries, you can filter the feed. For example, if you
only want the episodes of the first podcast that have a guest, you can add the
following line to the `[mssp]` section:

    filter = "feat." in "{title}"

(You need the quotes around {title} if the string you are filtering by has
spaces, for example; they are strictly unnecessary here.)

For information about the {placeholders}, take a look at
[greg.conf](greg/data/greg.conf).
In `greg.conf` you can also change the download directory, and some other
things. It should be self-explanatory.

## Terminal interface

`greg tui` opens a curses interface for managing feeds without remembering the
commands: add, edit and remove feeds, sync one or all, browse a feed's episodes
and download the ones you pick, with a live log underneath.

| Key | Action |
| --- | --- |
| `a` / `d` / `e` | add / remove / edit the url of a feed |
| `s` / `S` | sync the selected feed / sync all |
| `Enter` | browse the feed's episodes (`Space` marks, `Enter` downloads) |
| `r`, `q` | reload, quit |

## Web interface

`greg web` starts a small web UI (standard library only) for managing feeds,
syncing, browsing/downloading episodes, editing settings and seeing system
status. It listens on `127.0.0.1:8787` and will not start without a password:

    greg web --set-password
    greg web

Settings live in `~/.config/greg/web.json`. To schedule syncs, add for example
`"schedules": [{"name": "hourly", "feeds": ["all"], "every_minutes": 60}]` to
it. If your downloads live on a network or rclone mount, add
`"require_mounts": ["/mnt/podcasts"]` so scheduled syncs are skipped, and manual
jobs refused, while the mount is missing (instead of filling the local disk).
If you expose the UI, put it behind a TLS-terminating reverse proxy and set
`"trust_proxy": true`. `filter` and `downloadhandler` are shown read-only in the
UI on purpose, since greg executes them as code.

## Docker, and sending some feeds through a VPN

A [Dockerfile](Dockerfile) and a [docker-compose.yml](docker-compose.yml) are
included. The compose file runs greg (web interface plus a built-in hourly
sync) next to [gluetun](https://github.com/qdm12/gluetun), a VPN client
container, and sends only the feeds that need it (Patreon, by default) through
the VPN:

    cp .env.example .env            # your WireGuard / VPN details
    echo 'a long password' > greg_web_password.txt
    mkdir -p config data downloads gluetun
    docker compose up -d            # then open http://127.0.0.1:8787

The same `proxy` setting works without Docker, with any proxy (wireproxy, an
SSH tunnel, Tailscale...). See [docs/vpn.md](docs/vpn.md), and run
`greg proxytest` to check that a proxy is really in use.

## Credits

greg was written by Manolo Martínez and is licensed under the GPLv3 (see
[COPYING](COPYING)). This version adds the terminal and web interfaces, the
proxy setting and the Docker setup.
