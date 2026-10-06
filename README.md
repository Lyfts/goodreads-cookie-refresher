# Goodreads Cookie Refresher

Companion tool for [ShelfSync](https://github.com/Lyfts/ShelfSync), a KOReader plugin. Goodreads sits behind an AWS WAF bot-challenge that occasionally
rejects the plugin's saved session cookie. Normally
that just means repasting a fresh cookie by hand once in a while. This runs
one or more real, logged-in Chrome sessions on your home network instead, and
hands the plugin fresh cookies automatically whenever they hit that wall.

It's two containers:
- **selenium** -- a real Chrome browser (`selenium/standalone-chrome`), so
  the AWS WAF challenge gets solved by an actual browser, not simulated.
  Exposes a **noVNC** view on port 7900 by default -- open it in any normal
  browser tab, no VNC client needed. Multiple account windows share this
  desktop.
- **refresher** -- a small Python service that keeps one persistent
  WebDriver session per configured account pointed at goodreads.com, and
  serves each account's cookie over HTTP for the plugin to pull.

## Setup

1. `cp .env.example .env` and set a real `VNC_PASSWORD`. Also set
   `REFRESHER_PORT` there if 5080 is already taken on this machine. `VNC_PORT`
   controls the host port for the noVNC view and defaults to 7900. For multiple
   accounts, see [Multiple Goodreads accounts](#multiple-goodreads-accounts)
   before starting the containers.
2. `docker compose pull && docker compose up -d` (pulls the published image instead of building locally; use `docker compose up -d --build` instead if you're working on the `refresher` source)
3. Open `http://<this-machine's-LAN-IP>:<VNC_PORT>` in a browser on any
   device on your network, enter the VNC password, and you'll see the live
   Chrome desktop already pointed at goodreads.com. Log in normally -- this
   is a real interactive browser session, so your usual Amazon login/2FA
   works exactly as it would anywhere else.
4. Once logged in, leave the tab -- you don't need to keep it open. The
   container keeps each browser/profile alive and reuses it going forward,
   refreshing every `REFRESH_INTERVAL_SECONDS` (default 20 min) to keep the
   WAF token from going stale.
5. In KOReader: **Goodreads menu > Settings > Account (Cookie) > Cookie
   Auto-Refresh URL**, set it to `http://<this-machine's-LAN-IP>:<REFRESHER_PORT>`
   (`5080` unless you changed it in `.env`) -- for a single account, just the
   base address; the plugin appends `/refresh` itself. For multiple accounts,
   append that account's `/accounts/<id>` path. If you set
   `REFRESHER_AUTH_TOKEN` in `.env`, also fill in the separate **Cookie
   Auto-Refresh Token** field right below it with the same value.

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

## Multiple Goodreads accounts

Set `ACCOUNTS` to local labels of your choice, separated by commas. They are
not Goodreads usernames; use lowercase letters, digits, hyphens, or
underscores, with no spaces. To preserve the existing account and add a
second one, set:

```dotenv
ACCOUNTS=default,girlfriend
```

Run `docker compose pull && docker compose up -d` again to recreate the
containers with the new settings. Each account gets its own persistent Chrome
profile on the same Selenium host. The `default` account reuses the existing
profile, so its Goodreads login and existing plugin URL continue to work.

The Selenium host runs one Chrome session per account. All Chrome windows
share the single noVNC desktop; switch between them with **Alt+Tab** while
logging in. Opening the noVNC address in multiple browser tabs shows the same
desktop. Selenium's allowed concurrent session count is set automatically
from `ACCOUNTS`.
This shares one Selenium container/node, but each isolated account still
needs its own Chrome process and uses additional browser memory.
The Compose startup command removes Chromium's transient singleton and DevTools
marker files from the persistent profile volume before Selenium starts. These
files can point at an old container hostname or a socket under its removed
`/tmp` directory after an unclean shutdown; clearing them preserves the saved
browser profiles and lets Chrome start again. Mount this profile volume into
only one Selenium container at a time.
The account ID is a local label, not a Goodreads username. Chrome titles show
it as `[ShelfSync: <id>]`, so you can identify the window even during the
Amazon sign-in redirects.

On the first KOReader device, keep the existing base URL (for `default`). On
the second device, set **Cookie Auto-Refresh URL** to
`http://<this-machine's-LAN-IP>:5080/accounts/girlfriend`. The plugin appends
`/refresh` or `/cookie` to that account-specific base URL. Both devices use
the same `REFRESHER_AUTH_TOKEN` if one is configured.

## Endpoints

- `POST /refresh` and `GET /cookie` -- refresh or return the cached cookie for
  the `default` account (or the only configured account). These keep the
  single-account URL working.
- `POST /accounts/<id>/refresh` and `GET /accounts/<id>/cookie` -- account-
  specific equivalents. Set the plugin's refresh base URL to
  `http://<host>:<REFRESHER_PORT>/accounts/<id>`; it appends the endpoint path.
- `GET /accounts` -- lists configured accounts and their login/refresh status.
- `GET /health` or `GET /accounts/<id>/health` -- returns account status.

If `REFRESHER_AUTH_TOKEN` is set in `.env`, all endpoints require either
`?token=...` or an `X-Auth-Token` header matching it. With multiple accounts,
`/health` returns a status list when there is no `default` account.

## Security notes

This container holds a live, logged-in Goodreads/Amazon session. Treat it
accordingly:
- Keep the noVNC and refresher ports (7900 and 5080 by default) LAN-only --
  don't forward either past your router.
- Always set a real `VNC_PASSWORD`; anyone who can reach a noVNC port can see
  and drive that account's session.
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
- **Persistent WebDriver sessions, not fresh ones per refresh**: reusing each
  browser/profile continuously looks far more like a normal returning user
  than repeatedly spinning up new sessions, and it's what lets noVNC logins
  carry forward without repeating them on every refresh.
- **The named volume is mounted over `Downloads`, not a fresh path**: Docker
  seeds a brand-new named volume from whatever already exists at that path
  in the image, including ownership. `Downloads` already exists there,
  correctly owned by the image's unprivileged `seluser`; a path with nothing
  there yet would come up root-owned, and Chrome (which never runs as root
  in this image) wouldn't be able to write its profile into it.
