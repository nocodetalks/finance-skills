"""Connect QuickBooks with the person's own Intuit app keys, kept in a .env file the model never reads.

1. `connect --environment production` reads QUICKBOOKS_CLIENT_ID and QUICKBOOKS_CLIENT_SECRET from
   <work folder>/.env. If the file is missing it creates a blank one (mode 600) and stops, so the person can paste
   the keys in. Then it opens Intuit's sign-in page in the browser.
2. The person signs in, picks the company and clicks Connect. Intuit sends the browser to the redirect address with a
   one-time code, which is useless without the secret and expires within minutes. They paste that address in chat.
3. `connect --finish '<address>'` swaps the code for tokens, reads the company's name (one GET) and saves
   ~/.config/finance-skills/quickbooks-<profile>.json (mode 600).

The Client Secret never appears in chat or in this command's output. The Client ID isn't secret (it's in the
sign-in link), but it's only printed when the browser can't be opened for the person.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
import subprocess
import sys
import urllib.parse
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import qbo
from .util import StopRun

AUTH_URL = "https://appcenter.intuit.com/connect/oauth2"
SCOPE = "com.intuit.quickbooks.accounting"
# Intuit only allows https redirects for real companies, so a local callback is out. Intuit's own OAuth Playground
# address is https, belongs to Intuit, and leaves the code in the address bar for the person to copy.
DEFAULT_REDIRECT = "https://developer.intuit.com/v2/OAuth2Playground/RedirectUrl"
ID_KEY, SECRET_KEY = "QUICKBOOKS_CLIENT_ID", "QUICKBOOKS_CLIENT_SECRET"
PENDING_MINUTES = 30

TEMPLATE = f"""# QuickBooks keys for finance-skills. Paste each value right after its = sign, then save.
# Find them at developer.intuit.com > My Hub > App dashboard > your app > Keys and credentials
# (Production for your real company, Development for a test company).
# Never paste these into the chat. The assistant never opens this file; only `mer.py connect` reads it.
{ID_KEY}=
{SECRET_KEY}=

# Optional: only if you use Stripe and want it read directly. A restricted key with Read access (rk_live_...).
STRIPE_API_KEY=
"""

ENV_LABEL = {"production": "Production", "sandbox": "Development"}


# ---------------------------------------------------------------- the .env file

def env_path(root: Path, env_file: str | None) -> Path:
    return Path(env_file).expanduser().resolve() if env_file else root / ".env"


def read_env(p: Path) -> dict[str, str]:
    out = {}
    for line in p.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip().removeprefix("export ").strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1].strip()
        out[k] = v
    return out


def make_private(p: Path) -> bool:
    """Tighten a file other users can read to mode 600. Returns True if it changed."""
    if os.name != "posix":
        return False
    if p.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        os.chmod(p, 0o600)
        return True
    return False


def open_for_editing(p: Path) -> bool:
    try:
        if sys.platform == "darwin":
            cmd = ["open", "-e", str(p)]          # TextEdit, which shows a file starting with "."
        elif os.name == "nt":
            cmd = ["notepad", str(p)]
        else:
            cmd = ["xdg-open", str(p)]
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError:
        return False


def create_template(p: Path, open_file: bool) -> str:
    new_root = not p.parent.exists()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(TEMPLATE)
    if new_root:
        # A folder this command just made holds keys and, later, the books: keep all of it out of git.
        (p.parent / ".gitignore").write_text("# Holds QuickBooks keys and financial data. Never commit it.\n*\n")
    opened = open_for_editing(p) if open_file else False
    return (f"Created {p} for your QuickBooks keys{' and opened it' if opened else ''}. Paste the Client ID and "
            f"Client Secret right after their = signs, save the file, then run connect again. "
            "Don't paste the keys into the chat.")


def load_keys(p: Path, open_file: bool = False) -> tuple[str, str]:
    if not p.exists():
        raise StopRun(create_template(p, open_file))
    tightened = make_private(p)
    vals = read_env(p)
    missing = [k for k in (ID_KEY, SECRET_KEY) if not vals.get(k)]
    if missing:
        raise StopRun(f"{p} has no value for {' or '.join(missing)}. Paste "
                      f"{'it' if len(missing) == 1 else 'them'} right after the = sign, save, and run connect again."
                      + (" (The file could be read by other users, so it is now private to you.)" if tightened else ""))
    for k in (ID_KEY, SECRET_KEY):
        if not re.fullmatch(r"[A-Za-z0-9._-]{16,}", vals[k]):
            raise StopRun(f"{k} in {p} doesn't look like an Intuit key: it should be one long run of letters and "
                          "numbers, with no spaces. Copy it again from Keys and credentials.")
    if vals[ID_KEY] == vals[SECRET_KEY]:
        raise StopRun(f"{p} has the same value for the Client ID and the Client Secret. Copy each one again.")
    return vals[ID_KEY], vals[SECRET_KEY]


# ---------------------------------------------------------------- sign-in

def pending_path(profile: str) -> Path:
    return qbo.cred_path(profile).with_name(f"quickbooks-{profile}.pending.json")


def start(root: Path, profile: str, environment: str, env_file: str | None = None,
          redirect_uri: str = DEFAULT_REDIRECT, replace: bool = False, open_browser: bool = True) -> str:
    p = env_path(root, env_file)
    client_id, _ = load_keys(p, open_file=open_browser)
    if environment not in qbo.API_BASE:
        raise StopRun("Say which QuickBooks to connect: --environment production (your real company) or "
                      "--environment sandbox (an Intuit test company).")
    if not redirect_uri.startswith("https://") and environment == "production":
        raise StopRun("Intuit only accepts https:// redirect addresses for real companies.")
    state = secrets.token_urlsafe(24)
    qbo.write_private(pending_path(profile), {
        "state": state, "environment": environment, "redirect_uri": redirect_uri, "env_file": str(p),
        "replace": replace, "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    url = AUTH_URL + "?" + urllib.parse.urlencode({"client_id": client_id, "response_type": "code", "scope": SCOPE,
                                                   "redirect_uri": redirect_uri, "state": state})
    opened = webbrowser.open(url) if open_browser else False
    lines = [("Opened Intuit's sign-in page in your browser." if opened
              else f"Open this link in your browser to sign in to QuickBooks:\n  {url}"),
             f"  1. Sign in and choose the company to review ({environment}), then click Connect.",
             "  2. The browser lands on an Intuit page: the OAuth Playground showing an Authorization Code and Realm "
             "ID, or an Intuit sign-in page. Don't click Get tokens or sign in there; that would use up the code.",
             "  3. Copy the whole address from the browser's address bar (or the Authorization Code and Realm ID shown "
             "on the page) and paste it into the chat within a few minutes. The code works only once, only with "
             "your Client Secret, which stays in .env.",
             f"Then run: mer.py connect --profile {profile} --finish '<the address>'"]
    return "\n".join(lines)


def parse_redirect(text: str) -> dict:
    """Pull code, realmId, state and error out of what the person pasted.

    Usually the address of Intuit's redirect page (the OAuth Playground), or, when they aren't signed in to Intuit's
    developer site, the sign-in page it bounces to, which carries the redirect (and the code) URL-encoded inside it.
    Failing that, the Authorization Code and Realm ID copied off the Playground page (no state to check then).
    """
    found: dict = {}
    t = (text or "").strip()
    for _ in range(4):
        for key, field, pat in (("code", "code", r"[A-Za-z0-9._~-]+"), ("realmId", "realm", r"\d+"),
                                ("state", "state", r"[A-Za-z0-9._~-]+"), ("error", "error", r"[A-Za-z0-9_.~-]+")):
            m = re.search(rf"(?:^|[?&#\s]){key}=({pat})", t)
            if m and field not in found:
                found[field] = m.group(1)
        decoded = urllib.parse.unquote(t)
        if decoded == t:
            break
        t = decoded
    if "code" not in found and "://" not in text:
        code = re.search(r"\b[A-Za-z][A-Za-z0-9]{29,}\b", text)
        realm = re.search(r"\b\d{9,20}\b", text)
        if code and realm:
            found.update(code=code.group(0), realm=realm.group(0))
    return found


def _load_pending(profile: str) -> dict:
    pp = pending_path(profile)
    if not pp.exists():
        raise StopRun("There's no sign-in in progress for this profile. Start with "
                      "`mer.py connect --environment production` (or sandbox) first.")
    pending = json.loads(pp.read_text())
    started = datetime.fromisoformat(pending["started_at"])
    if datetime.now(timezone.utc) - started > timedelta(minutes=PENDING_MINUTES):
        pp.unlink()
        raise StopRun(f"That sign-in was started more than {PENDING_MINUTES} minutes ago. Run connect again.")
    return pending


def finish(profile: str, pasted: str) -> str:
    pending = _load_pending(profile)
    env = pending["environment"]
    got = parse_redirect(pasted)
    if got.get("error"):
        raise StopRun(f"Intuit returned \"{got['error']}\" instead of a sign-in code (access_denied means Connect "
                      "wasn't clicked, or this login can't connect apps to the company). Nothing was connected.")
    if not got.get("code") or not got.get("realm"):
        if "appcenter.intuit.com" in pasted:
            raise StopRun("That's Intuit's company-picker page, from before Connect, so it has no sign-in code yet. "
                          "Choose the company, click Connect, then copy the address of the page that comes next.")
        raise StopRun("That has no sign-in code and company ID in it. After clicking Connect, copy the whole "
                      "address of the page the browser lands on, or the Authorization Code and Realm ID it shows.")
    if got.get("state") and got["state"] != pending["state"]:
        raise StopRun("That address comes from a different sign-in. Run connect again and paste the newest address.")
    realm = got["realm"]

    cp = qbo.cred_path(profile)
    if cp.exists() and not pending.get("replace"):
        try:
            old = json.loads(cp.read_text())
        except json.JSONDecodeError:
            old = {}
        if old.get("realm_id") and (old.get("realm_id") != realm or old.get("environment") != env):
            raise StopRun(
                f"Profile '{profile}' is already connected to another company (ID {old['realm_id']}, "
                f"{old.get('environment')}). Nothing was changed. To keep both, start again with "
                "`--profile <a new name>`; to replace it, start again with `--replace`.")

    client_id, client_secret = load_keys(Path(pending["env_file"]))
    try:
        tok = qbo.token_request(client_id, client_secret, {
            "grant_type": "authorization_code", "code": got["code"], "redirect_uri": pending["redirect_uri"]})
    except qbo.TokenError as e:
        if e.error == "invalid_client" or e.status == 401:
            raise StopRun(f"Intuit didn't accept the Client ID and Secret in {pending['env_file']}. Check they are "
                          f"both from the {ENV_LABEL[env]} tab of Keys and credentials for the same app, with "
                          "nothing extra copied. Nothing was connected.") from e
        if e.error == "invalid_grant":
            raise StopRun("Intuit says the sign-in code has expired or was already used (clicking Get tokens on "
                          "the Playground page uses it up). Run connect again and paste the address within a few "
                          "minutes, without clicking anything on that page. Nothing was connected.") from e
        raise StopRun(f"Intuit refused the sign-in code ({e}). Nothing was connected.") from e

    creds = {"client_id": client_id, "client_secret": client_secret, "refresh_token": tok["refresh_token"],
             "realm_id": realm, "environment": env,
             "connected_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    c = qbo.Client(profile, creds=creds)
    c.token = tok["access_token"]
    try:
        info = c.get(f"companyinfo/{realm}", {}).get("CompanyInfo") or {}
    except qbo.QBOError as e:
        raise StopRun(f"Signed in, but reading the company failed ({e.code}). If you chose --environment {env}, "
                      f"the keys in .env must be the {ENV_LABEL[env]} keys. Nothing was saved.") from e
    qbo.save_creds(profile, creds)
    pending_path(profile).unlink(missing_ok=True)
    return (f"Connected to {info.get('CompanyName') or '(a company with no name)'} ({env}, company ID {realm}).\n"
            f"Saved to {cp}, readable only by you. Check this is the company you want to review.")


def check(profile: str) -> str:
    """Confirm the saved connection still works, with one read. Shows no keys or tokens."""
    c = qbo.Client(profile)
    info = c.get(f"companyinfo/{c.realm}", {}).get("CompanyInfo") or {}
    return (f"QuickBooks is connected: {info.get('CompanyName') or '(no name)'} ({c.creds['environment']}, "
            f"company ID {c.realm}, profile '{profile}').")
