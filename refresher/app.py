"""Keeps a persistent, logged-in Chrome session alive against Goodreads via a
remote Selenium browser, and serves the resulting session cookie over a tiny
local HTTP API so the KOReader plugin can pull a fresh one when AWS WAF's
bot-challenge rejects its saved cookie.

The browser never logs in on its own -- first login (and any future
re-login, if the session dies outright) is done by a human through the
Selenium container's noVNC view. See ../README.md.
"""
import logging
import os
import threading
import time

from flask import Flask, Response, abort, request
from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options

SELENIUM_URL = os.environ.get("SELENIUM_URL", "http://selenium:4444")
TARGET_URL = os.environ.get("TARGET_URL", "https://www.goodreads.com/")
PROFILE_DIR = os.environ.get("PROFILE_DIR", "/home/seluser/Downloads/profile")
REFRESH_INTERVAL = int(os.environ.get("REFRESH_INTERVAL_SECONDS", "1200"))
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8080"))
AUTH_TOKEN = os.environ.get("AUTH_TOKEN", "")

# Mirrors tools/support/fetch_cookies.py's GOODREADS_SKIP_COOKIES: a
# short-lived (~5 min) per-request token that's almost always already
# expired by the time it's read back out, and makes goodreads.com reject
# the whole request if replayed stale -- unlike the rest of the cookie
# bundle, which is safe to just carry forward as-is.
SKIP_COOKIES = {"jwt_token"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("refresher")

app = Flask(__name__)

_driver = None
# Reentrant: do_refresh() holds this for its whole navigate/read critical
# section and calls back into ensure_driver() while still holding it.
_driver_lock = threading.RLock()
_state = {"cookie": "", "cookie_count": 0, "updated_at": 0.0}


def _build_driver():
    options = Options()
    # Reuses the same on-disk profile every time (see docker-compose.yml's
    # volume comment) instead of a fresh one per session, which is what
    # actually makes the login persist across container restarts.
    options.add_argument(f"--user-data-dir={PROFILE_DIR}")
    options.add_argument("--profile-directory=Default")
    driver = webdriver.Remote(command_executor=SELENIUM_URL, options=options)
    driver.set_page_load_timeout(60)
    return driver


def ensure_driver(force_new=False):
    global _driver
    with _driver_lock:
        if _driver is not None and not force_new:
            return _driver
        if _driver is not None:
            try:
                _driver.quit()
            except WebDriverException:
                pass
            _driver = None

        log.info("starting persistent browser session against %s", SELENIUM_URL)
        last_err = None
        for attempt in range(1, 13):
            try:
                _driver = _build_driver()
                _driver.get(TARGET_URL)
                log.info("session ready (attempt %d)", attempt)
                return _driver
            except Exception as e:
                # Broad on purpose: a not-yet-listening Selenium container
                # surfaces as a raw urllib3.MaxRetryError (connection
                # refused), not a WebDriverException, so narrowing this
                # would let that case skip the retry loop entirely.
                last_err = e
                log.warning("selenium not ready yet (attempt %d): %s", attempt, e)
                time.sleep(5)
        raise RuntimeError(f"could not reach Selenium after repeated attempts: {last_err}")


def _cookie_header_from(driver):
    cookies = driver.get_cookies()
    parts = [f"{c['name']}={c['value']}" for c in cookies if c["name"] not in SKIP_COOKIES]
    return "; ".join(parts), len(parts)


def do_refresh():
    # Held for the whole navigate/recreate/read-cookies sequence (not just
    # the driver swap in ensure_driver()) so a concurrent caller -- the
    # background loop and /refresh requests both land here -- can never
    # quit() the driver out from under another thread mid-navigation or
    # mid-get_cookies.
    with _driver_lock:
        driver = ensure_driver()
        try:
            driver.get(TARGET_URL)
        except WebDriverException as e:
            log.warning("navigation failed (%s), recreating session", e)
            driver = ensure_driver(force_new=True)
            driver.get(TARGET_URL)

        cookie, count = _cookie_header_from(driver)
        if cookie:
            _state["cookie"] = cookie
            _state["cookie_count"] = count
            _state["updated_at"] = time.time()
            log.info("refreshed cookie (%d cookies)", count)
        else:
            log.warning("refresh produced no cookies -- probably not logged in yet; "
                         "log in via the Selenium container's noVNC view (port 7900)")
        return cookie


def _check_auth():
    if not AUTH_TOKEN:
        return
    supplied = request.args.get("token") or request.headers.get("X-Auth-Token")
    if supplied != AUTH_TOKEN:
        abort(401)


@app.get("/cookie")
def get_cookie():
    """Returns the last captured cookie without forcing a new browser round-trip."""
    _check_auth()
    if not _state["cookie"]:
        abort(503, "no cookie captured yet -- log in via the Selenium noVNC view first")
    return Response(_state["cookie"], mimetype="text/plain")


@app.post("/refresh")
def force_refresh():
    """Forces an immediate re-visit + cookie capture. This is what the plugin calls."""
    _check_auth()
    cookie = do_refresh()
    if not cookie:
        abort(503, "refresh ran but produced no cookies -- log in via the Selenium noVNC view")
    return Response(cookie, mimetype="text/plain")


@app.get("/health")
def health():
    age = time.time() - _state["updated_at"] if _state["updated_at"] else None
    return {
        "logged_in": _state["cookie_count"] > 0,
        "cookie_count": _state["cookie_count"],
        "last_refresh_seconds_ago": age,
    }


def background_loop():
    # Selenium can take well over a minute to come up on a first boot (cold
    # image pull, Xvfb/Chrome startup), longer than ensure_driver()'s own
    # 12-attempt/~60s budget -- so retry that budget itself indefinitely
    # here rather than letting the whole thread die on a slow first start.
    while True:
        try:
            ensure_driver()
            break
        except Exception:
            log.warning("initial driver startup still failing, retrying")
            time.sleep(5)
    while True:
        try:
            do_refresh()
        except Exception:
            log.exception("background refresh failed")
        time.sleep(REFRESH_INTERVAL)


if __name__ == "__main__":
    threading.Thread(target=background_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=HTTP_PORT)
