# Goodreads Cookie Refresher

Companion tool for ShelfSync (`storygraph.koplugin`), a KOReader plugin. Goodreads sits behind an AWS WAF bot-challenge that occasionally
rejects the plugin's saved session cookie (see the WAF-challenge comments at
the top of `shelfsync/lib/goodreads/api.lua` in the plugin repo). Normally
that just means repasting a fresh cookie by hand once in a while. This runs a
real, logged-in Chrome session on your home network instead, and hands the
plugin a fresh cookie automatically whenever it hits that wall.

It's two containers:
- **selenium** -- a real Chrome browser (`selenium/standalone-chrome`), so
  the AWS WAF challenge gets solved by an actual browser, not simulated.
  Exposes a **noVNC** view on port 7900 -- open it in any normal browser tab,
  no VNC client needed.
- **refresher** -- a small Python service that keeps one persistent
  WebDriver session pointed at goodreads.com, and serves the resulting
  cookie over HTTP for the plugin to pull.

## Setup

1. `cp .env.example .env` and set a real `VNC_PASSWORD`. Also set
   `REFRESHER_PORT` there if 5080 is already taken on this machine.
2. `docker compose pull && docker compose up -d` (pulls the published image instead of building locally; use `docker compose up -d --build` instead if you're working on the `refresher` source)
3. Open `http://<this-machine's-LAN-IP>:7900` in a browser on any device on
   your network, enter the VNC password, and you'll see a live Chrome
   window already pointed at goodreads.com. Log in normally -- this is a
   real interactive browser session, so your usual Amazon login/2FA works
   exactly as it would anywhere else.
4. Once logged in, leave the tab -- you don't need to keep it open. The
   container keeps that same browser/profile alive and reuses it going
   forward, refreshing itself every `REFRESH_INTERVAL_SECONDS` (default 20
   min) to keep the WAF token from going stale.
5. In KOReader: **Goodreads menu > Settings > Account (Cookie) > Cookie
   Auto-Refresh URL**, set it to `http://<this-machine's-LAN-IP>:<REFRESHER_PORT>`
   (`5080` unless you changed it in `.env`) -- just the base address, the
   plugin appends the `/refresh` path itself. If you set `REFRESHER_AUTH_TOKEN`
   in `.env`, also fill in the separate **Cookie Auto-Refresh Token** field
   right below it with the same value.

That's it -- when the plugin hits a WAF challenge, it calls that URL, gets a
fresh cookie, saves it, and retries automatically. The same URL also covers
first-time setup: if the "Goodreads Cookie" field is still empty (e.g. a
fresh install, or `shelfsync_config.lua` was never filled in), the plugin
pulls whatever cookie the refresher already has cached instead of just
failing until one is pasted in by hand. The manual "Goodreads Cookie" field
still exists and still works as a fallback if this isn't configured or the
refresher is unreachable.

You'll need to repeat step 3 any time the underlying session dies outright
(e.g. logged out on Goodreads' end, or the profile volume is removed) --
everything else is automatic.

## Endpoints

- `POST /refresh` -- forces an immediate re-visit + cookie capture, returns
  the fresh `Cookie` header value as plain text. This is what the plugin calls.
- `GET /cookie` -- returns the last captured cookie without forcing a new
  browser round-trip.
- `GET /health` -- `{"logged_in": bool, "cookie_count": int, "last_refresh_seconds_ago": float}`

If `REFRESHER_AUTH_TOKEN` is set in `.env`, all of the above require either
`?token=...` or an `X-Auth-Token` header matching it.

## Security notes

This container holds a live, logged-in Goodreads/Amazon session. Treat it
accordingly:
- Keep ports 7900 and 5080 (or your `REFRESHER_PORT`) LAN-only -- don't
  forward either past your router.
- Always set a real `VNC_PASSWORD`; anyone who can reach port 7900 can see
  and drive that session.
- Set `REFRESHER_AUTH_TOKEN` if your network isn't fully trusted (e.g.
  shared wifi).
- The `refresher` service's own HTTP server is a plain Flask dev server --
  fine for trusted-LAN use, not something to expose to the internet.

## Why it's built this way

- **Real browser, not a scripted "solve the challenge" hack**: the WAF
  challenge is JS-based; nothing short of an actual browser engine solves it
  reliably.
- **noVNC instead of a VNC client**: renders into a plain browser tab over
  websockets, so login doesn't need any extra software installed anywhere.
- **One persistent WebDriver session, not a fresh one per refresh**: reusing
  the same browser/profile continuously looks far more like a normal
  returning user than repeatedly spinning up "new" sessions, and it's what
  lets the noVNC login carry forward without needing to happen again on
  every refresh.
- **The named volume is mounted over `Downloads`, not a fresh path**: Docker
  seeds a brand-new named volume from whatever already exists at that path
  in the image, including ownership. `Downloads` already exists there,
  correctly owned by the image's unprivileged `seluser`; a path with nothing
  there yet would come up root-owned, and Chrome (which never runs as root
  in this image) wouldn't be able to write its profile into it.
