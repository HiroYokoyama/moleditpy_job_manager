# Web monitor

The **Host Monitor** can be served as a web page — the same cards, the same
meters, the same job lists — for a browser instead of a Qt window. Tick
**Serve the Host Monitor to a browser** under **Web Monitor** in
**Settings...**; **Links and Tailscale...** beside it has the link and the
commands below.

It serves whenever that is ticked, with or without the Host Monitor window open,
and from the tray process too once MoleditPy has closed. The hosts are sampled
only while someone is looking: a request starts sampling, and it stops six
minutes after the last one (longer than the page's slowest refresh). The first
load of an idle page names the hosts and says it is waiting for the first
reading; the numbers follow on the page's next refresh.

The page is **read-only**. There is no route on it that cancels, submits,
downloads or deletes anything, so a link that gets away from you costs a look
at what is running, not the run itself.

The socket binds **`127.0.0.1` and nothing else**. Reaching it from somewhere
else is [Tailscale](https://tailscale.com)'s job:

```bash
tailscale serve --bg 8770
```

which puts the same page on `https://<machine>.<tailnet>.ts.net/` behind
Tailscale's own identity check. The dialog shows that command with a **Copy**
button and a **Run** button beside it — Run executes it for you and fills in
your machine's real tailnet name, so the link underneath is one you can copy
straight to a phone rather than a placeholder to hand-edit. **Unpublish**
(`tailscale serve reset`) withdraws it again, and is offered only if this
dialog is what published it.

Keeping the exposure in Tailscale rather than in the plugin is deliberate: this
process never binds a routable address, never decides whose certificate to
trust, and never grows an authentication story beyond the token in the link.
Nothing here needs Tailscale to serve on your own machine.

> **Serve needs HTTPS enabled on your tailnet — enable it once, before Run
> will work.** Admin console ▸ **DNS** ▸ **HTTPS Certificates**
> ([how](https://tailscale.com/kb/1153/enabling-https)). It is a tailnet-wide
> setting, so it takes an admin of that tailnet, and `tailscale serve` cannot
> obtain a certificate without it. The dialog checks this first and tells you
> so; without the check, Serve simply waits, which looked like Run hanging.
> Serving on `127.0.0.1` needs none of this — only the tailnet link does.

The page carries its own controls: a **light/dark button** (it starts from the
device's own preference, then remembers what you pressed) and a **refresh
interval**. The footer names the version answering the request.

The link's token is kept in `~/.moleditpy/job_manager/web_monitor_token`, so a
link saved on a phone keeps working across restarts. It is **not** the API
token — that one grants full control, and this page is read-only. **New link...**
in the dialog replaces it, which cuts off every link and cookie already handed
out.

**What it costs.** The page polls for a fresh snapshot on a timer you set from
the page itself — **2 s to 5 min, or Paused** — and the choice is remembered in
that browser. Measured over loopback, per poll:

| Watching | Data per poll | At 4 s | Per hour |
|---|---|---|---|
| 1 host, no jobs | ~940 B | 236 B/s | 0.8 MiB |
| 1 host, 3 jobs | ~1.1 kB | 271 B/s | 0.9 MiB |
| 3 hosts, 5 jobs each | ~2.1 kB | 514 B/s | 1.8 MiB |
| 8 hosts, 10 jobs each | ~6.1 kB | 1.5 kB/s | 5.3 MiB |

The page itself is about 9 KiB, once per load. Its icon is fetched from `icon.svg`, `icon.png` and `apple-touch-icon.png`, which are the only routes served without the token — Safari ignores a `data:` favicon and iOS refuses one as a home-screen icon, and a browser fetching an icon does not send the cookie, so an inlined or a gated icon shows nothing there. HTTP headers are most of each
exchange — the JSON is only 262 B for one idle host — so raising the interval
saves nearly in proportion: 10 s costs 2.5× less than 4 s, and Paused costs
nothing. Over Tailscale, add TLS and WireGuard overhead: budget roughly 1.5–2×
these figures, so a small setup left open on 4G is around 35–50 MiB a day at
the 4 s default, or under 15 MiB at 10 s.

It is **off the first time the window opens** and remembered afterwards, so a
listening socket is never something you get by surprise, and never something
you have to ask for twice.
