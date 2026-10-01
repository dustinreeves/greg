# Sending some feeds through a VPN or proxy

Some feeds (Patreon is the usual one) refuse connections from datacenter
addresses, such as a VPS. greg can send chosen feeds through a proxy, so the
rest of your traffic (and your other feeds) stays on the normal connection.

greg does not manage a VPN itself. It talks to a **proxy** that something else
provides, and anything that offers a local HTTP or SOCKS proxy will do.

## The setting

In `greg.conf`:

```ini
[DEFAULT]
proxy = http://127.0.0.1:8888      # or socks5h://127.0.0.1:1080
proxy_hosts = patreon.com          # optional: only feeds on these hosts use it
```

* With `proxy_hosts`, only feeds whose URL is on those hosts (or their
  subdomains) use the `[DEFAULT]` proxy. Without it, every feed does.
* A feed section can set its own: `[myfeed]` + `proxy = http://...`. An empty
  `proxy =` there forces that feed to connect directly.
* The proxy is used for fetching the feed and for greg's downloader, which is
  curl (`http://` and `socks5h://` proxies both work). A custom `downloadhandler`
  (yt-dlp, for example) gets it in the environment (`http_proxy`, `https_proxy`,
  `all_proxy`) and as `{proxy}`.
* If the proxy is down, the feed fails. greg never falls back to a direct
  connection.
* `socks5://` proxies need the `socks` extra: `pip install "greg[socks]"`.

Check it works with:

```
greg proxytest
```

It prints your public address directly and through each configured proxy; they
must differ.

## Option 1: Docker + gluetun (easiest, any OS)

See the `docker-compose.yml` in this repository. [gluetun](https://github.com/qdm12/gluetun)
runs the VPN (many providers, or your own WireGuard) and offers an HTTP proxy on
port 8888 inside the compose network. greg stays on the normal network and is
told to use `http://gluetun:8888` for the hosts you list.

```
cp .env.example .env                 # your WireGuard details
echo 'a long password' > greg_web_password.txt
mkdir -p config data downloads gluetun
docker compose up -d
```

Notes:

* For a custom WireGuard server gluetun needs the endpoint as an **IP address**,
  not a hostname.
* Commercial VPN exit addresses are shared and sometimes blocked by the same
  sites. An exit you control (your own WireGuard server, a home connection)
  works most reliably.
* Do not use `network_mode: service:gluetun` for greg unless you want *all* of
  greg's traffic (including the web UI) inside the VPN.

## Option 2: wireproxy (no root, one binary)

[wireproxy](https://github.com/pufferffish/wireproxy) is a userspace WireGuard
client that exposes a local proxy. Append to your WireGuard config:

```ini
[http]
BindAddress = 127.0.0.1:8888
```

Run `wireproxy -c wg.conf`, and set `proxy = http://127.0.0.1:8888`.

## Option 3: an SSH tunnel

If you can SSH to a machine with the connection you want:

```
ssh -N -D 127.0.0.1:1080 user@home.example.org
```

then `proxy = socks5h://127.0.0.1:1080` (and `pip install "greg[socks]"`). Use
`autossh` or a systemd unit to keep it up.

## Option 4: Tailscale

Run `tailscaled --tun=userspace-networking --socks5-server=127.0.0.1:1080`, pick
an exit node, and use `proxy = socks5h://127.0.0.1:1080`.

## Advanced (Linux, root): route by group, no proxy

If you would rather not run a proxy, you can route one Unix group's traffic
through a kernel WireGuard interface. greg's web UI can then run the affected
feeds through a command wrapper (`vpn_prefix` in `web.json`, for example
`["sudo","-n","-u","me","-g","gregvpn","env","HOME=/home/me"]`).

```ini
# /etc/wireguard/wgvpn.conf
[Interface]
PrivateKey = ...
Address = 10.64.0.2/32
Table = off                      # do NOT install a default route
PostUp = sysctl -w net.ipv4.conf.%i.rp_filter=2     # replies are dropped without this
PostUp = ip rule del fwmark 0x51 table 51820 priority 100 2>/dev/null || true
PostUp = ip rule add fwmark 0x51 table 51820 priority 100
PostUp = ip route add default dev %i table 51820
PostUp = iptables -t mangle -A OUTPUT -m owner --gid-owner gregvpn -j MARK --set-mark 0x51
PostUp = iptables -t nat -A POSTROUTING -o %i -j MASQUERADE
PostDown = iptables -t nat -D POSTROUTING -o %i -j MASQUERADE || true
PostDown = iptables -t mangle -D OUTPUT -m owner --gid-owner gregvpn -j MARK --set-mark 0x51 || true
PostDown = ip route del default dev %i table 51820 || true
PostDown = ip rule del fwmark 0x51 table 51820 priority 100 || true

[Peer]
PublicKey = ...
AllowedIPs = 0.0.0.0/0
Endpoint = vpn.example.org:51820
```

Create the group (`groupadd gregvpn`, add yourself), and run greg with
`sudo -u you -g gregvpn greg sync FEED`. Things that will bite you:

* Replies are silently dropped unless `rp_filter` is relaxed on the interface.
* If the download folder is a FUSE mount (rclone, sshfs), mount it with
  `--allow-other`, or the changed group cannot read it.
* `sg` can hang waiting for a group password in scripts; use `sudo -g`.
