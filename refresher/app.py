"""Keeps a persistent, logged-in Chrome session alive against Goodreads via a
remote Selenium browser, and serves the resulting session cookie over a tiny
local HTTP API so the KOReader plugin can pull a fresh one when AWS WAF's
bot-challenge rejects its saved cookie.

The browser never logs in on its own -- first login (and any future
re-login, if the session dies outright) is done by a human through the
Selenium container's noVNC view. See ../README.md.
"""
import json
import logging
import os
import re
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


def _configured_accounts():
    names = [name.strip().lower() for name in os.environ.get("ACCOUNTS", "default").split(",")]
    if not names or any(not name for name in names):
        raise ValueError("ACCOUNTS must contain one or more comma-separated account names")
    if any(not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", name) for name in names):
        raise ValueError("account names may contain lowercase letters, digits, '_' and '-' (up to 32 characters)")
    if len(set(names)) != len(names):
        raise ValueError("ACCOUNTS contains duplicate account names")
    return tuple(names)


ACCOUNTS = _configured_accounts()

# Mirrors tools/support/fetch_cookies.py's GOODREADS_SKIP_COOKIES: a
# short-lived (~5 min) per-request token that's almost always already
# expired by the time it's read back out, and makes goodreads.com reject
# the whole request if replayed stale -- unlike the rest of the cookie
# bundle, which is safe to just carry forward as-is.
SKIP_COOKIES = {"jwt_token"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("refresher")

app = Flask(__name__)

# Each account owns a persistent WebDriver session and a separate Chrome
# user-data directory. The lock protects all sessions because Selenium's
# shared desktop and our refresh endpoint must not be navigated concurrently.
_driver_lock = threading.RLock()
_drivers = {}
_states = {
    account: {"cookie": "", "cookie_count": 0, "updated_at": 0.0}
    for account in ACCOUNTS
}


def _profile_dir_for(account):
    # Keep the original single-account profile path for upgrades. Additional
    # accounts use sibling directories on the same persistent Docker volume.
    if account == "default":
        return PROFILE_DIR
    return f"{PROFILE_DIR}-{account}"


def _account_title_script(account):
    label = json.dumps(f"[ShelfSync: {account}]")
    return f"""(() => {{
  const label = {label};
  const markTitle = () => {{
    const current = document.title || "Goodreads";
    const base = current.replace(/\\s*\\[ShelfSync: [a-z0-9_-]+\\]$/, "");
    const marked = base + " " + label;
    if (current !== marked) document.title = marked;
  }};
  new MutationObserver(markTitle).observe(document, {{
    subtree: true,
    childList: true,
    characterData: true,
  }});
  markTitle();
}})();"""


def _build_driver(account):
    options = Options()
    # Each account gets an isolated persistent profile. The mounted Selenium
    # volume keeps all of them across container restarts.
    options.add_argument(f"--user-data-dir={_profile_dir_for(account)}")
    options.add_argument("--profile-directory=Default")
    driver = webdriver.Remote(command_executor=SELENIUM_URL, options=options)
    driver.set_page_load_timeout(60)
    # Keep the account ID visible in Chrome's tab/window title, including
    # when Goodreads redirects through Amazon during sign-in.
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": _account_title_script(account),
    })
    return driver


def ensure_driver(account, force_new=False):
    with _driver_lock:
        driver = _drivers.get(account)
        if driver is not None and not force_new:
            return driver
        if driver is not None:
            try:
                driver.quit()
            except WebDriverException:
                pass
            _drivers.pop(account, None)

        log.info("starting persistent browser session for account %s against %s", account, SELENIUM_URL)
        last_err = None
        for attempt in range(1, 13):
            driver = None
            try:
                driver = _build_driver(account)
                _drivers[account] = driver
                driver.get(TARGET_URL)
                log.info("session ready for account %s (attempt %d)", account, attempt)
                return driver
            except Exception as e:
                # Broad on purpose: a not-yet-listening Selenium container
                # surfaces as a raw urllib3.MaxRetryError (connection
                # refused), not a WebDriverException, so narrowing this
                # would let that case skip the retry loop entirely.
                last_err = e
                log.warning("selenium session for %s not ready yet (attempt %d): %s", account, attempt, e)
                if driver is not None:
                    try:
                        driver.quit()
                    except WebDriverException:
                        pass
                _drivers.pop(account, None)
                time.sleep(5)
        raise RuntimeError(f"could not start Selenium session for {account} after repeated attempts: {last_err}")


def _cookie_header_from(driver):
    cookies = driver.get_cookies()
    parts = [f"{c['name']}={c['value']}" for c in cookies if c["name"] not in SKIP_COOKIES]
    return "; ".join(parts), len(parts)


def do_refresh(account):
    # Held for the whole navigate/recreate/read-cookies sequence so the
    # background loop and account-specific /refresh requests cannot navigate
    # a session while another request is reading its cookies.
    with _driver_lock:
        driver = ensure_driver(account)
        try:
            driver.get(TARGET_URL)
        except WebDriverException as e:
            log.warning("navigation failed for account %s (%s), recreating session", account, e)
            driver = ensure_driver(account, force_new=True)
            driver.get(TARGET_URL)

        cookie, count = _cookie_header_from(driver)
        if cookie:
            state = _states[account]
            state["cookie"] = cookie
            state["cookie_count"] = count
            state["updated_at"] = time.time()
            log.info("refreshed cookie for account %s (%d cookies)", account, count)
        else:
            log.warning("refresh for account %s produced no cookies -- probably not logged in yet; "
                         "log in via the Selenium container's noVNC view", account)
        return cookie


def _check_auth():
    if not AUTH_TOKEN:
        return
    supplied = request.args.get("token") or request.headers.get("X-Auth-Token")
    if supplied != AUTH_TOKEN:
        abort(401)


def _known_account(account):
    if account not in _states:
        abort(404, f"unknown account {account!r}; configured accounts: {', '.join(ACCOUNTS)}")
    return account


def _default_account():
    if "default" in _states:
        return "default"
    if len(ACCOUNTS) == 1:
        return ACCOUNTS[0]
    abort(400, "specify an account in the URL, for example /accounts/girlfriend")


def _account_health(account):
    state = _states[account]
    age = time.time() - state["updated_at"] if state["updated_at"] else None
    return {
        "account": account,
        "logged_in": state["cookie_count"] > 0,
        "cookie_count": state["cookie_count"],
        "last_refresh_seconds_ago": age,
    }


def _get_cookie(account):
    cookie = _states[account]["cookie"]
    if not cookie:
        abort(503, f"no cookie captured for {account} yet -- log in via the Selenium noVNC view first")
    return Response(cookie, mimetype="text/plain")


def _force_refresh(account):
    cookie = do_refresh(account)
    if not cookie:
        abort(503, f"refresh for {account} produced no cookies -- log in via the Selenium noVNC view")
    return Response(cookie, mimetype="text/plain")


@app.get("/accounts")
def list_accounts():
    _check_auth()
    return {"accounts": [_account_health(account) for account in ACCOUNTS]}


@app.get("/accounts/<account>/cookie")
def get_account_cookie(account):
    _check_auth()
    return _get_cookie(_known_account(account))


@app.post("/accounts/<account>/refresh")
def refresh_account(account):
    _check_auth()
    return _force_refresh(_known_account(account))


@app.get("/accounts/<account>/health")
def account_health(account):
    _check_auth()
    return _account_health(_known_account(account))


@app.get("/cookie")
def get_cookie():
    """Returns the default (or sole) account's cached cookie."""
    _check_auth()
    return _get_cookie(_default_account())


@app.post("/refresh")
def force_refresh():
    """Refreshes the default (or sole) account; multi-account clients use /accounts/<id>."""
    _check_auth()
    return _force_refresh(_default_account())


@app.get("/health")
def health():
    _check_auth()
    if "default" in _states or len(ACCOUNTS) == 1:
        return _account_health(_default_account())
    return {"accounts": [_account_health(account) for account in ACCOUNTS]}


def background_loop():
    # Selenium can take well over a minute to come up on a first boot (cold
    # image pull, Xvfb/Chrome startup), longer than ensure_driver()'s own
    # 12-attempt/~60s budget -- so retry that budget itself indefinitely
    # here rather than letting the whole thread die on a slow first start.
    while True:
        try:
            for account in ACCOUNTS:
                ensure_driver(account)
            break
        except Exception:
            log.warning("initial driver startup still failing, retrying")
            time.sleep(5)
    while True:
        try:
            for account in ACCOUNTS:
                try:
                    do_refresh(account)
                except Exception:
                    log.exception("background refresh failed for account %s", account)
        except Exception:
            log.exception("background refresh loop failed")
        time.sleep(REFRESH_INTERVAL)


if __name__ == "__main__":
    threading.Thread(target=background_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=HTTP_PORT)
