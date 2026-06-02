"""
QuickBooks Online OAuth 2.0 authentication.

Flow:
  1. First run: open browser → user authorizes → callback captures code → exchange for tokens
  2. Subsequent runs: load tokens from .env, refresh if expired or on 401
  3. Token refresh retries once on failure before raising

Intuit OAuth endpoints (sandbox and production share the same auth server):
  - Authorization:  https://appcenter.intuit.com/connect/oauth2
  - Token:          https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer
  - Revoke:         https://developer.api.intuit.com/v2/oauth2/tokens/revoke
"""

import os
import threading
import webbrowser
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlparse, parse_qs
from http.server import HTTPServer, BaseHTTPRequestHandler

import requests
from dotenv import load_dotenv, set_key

from logger import get_logger

load_dotenv()

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AUTH_URL = "https://appcenter.intuit.com/connect/oauth2"
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
REVOKE_URL = "https://developer.api.intuit.com/v2/oauth2/tokens/revoke"
SCOPES = "com.intuit.quickbooks.accounting"

ENV_FILE = os.path.join(os.path.dirname(__file__), ".env")

# ---------------------------------------------------------------------------
# Token persistence
# ---------------------------------------------------------------------------


def _persist_to_github_secrets(
    access_token: str, refresh_token: str, expiry_iso: str
) -> None:
    """
    In CI, write the rotated tokens back to GitHub repository secrets so the
    next scheduled run reads the current refresh token.

    Intuit rotates the refresh token on a ~24h rolling window. On an ephemeral
    runner the .env write is discarded, so without this the stored secret goes
    stale and a later run fails with invalid_grant.

    No-op unless both GH_PAT (a PAT with secrets:write) and GITHUB_REPOSITORY
    are present — i.e. only runs in CI, never locally.
    """
    pat  = os.getenv("GH_PAT", "")
    repo = os.getenv("GITHUB_REPOSITORY", "")
    if not pat or not repo:
        return

    import subprocess

    env = {**os.environ, "GH_TOKEN": pat}
    secrets = {
        "QBO_ACCESS_TOKEN":  access_token,
        "QBO_REFRESH_TOKEN": refresh_token,
        "QBO_TOKEN_EXPIRY":  expiry_iso,
    }
    for key, value in secrets.items():
        try:
            # gh reads the secret value from stdin when --body is omitted, so the
            # token never appears in the process argument list on the runner.
            # (Avoid --body-file, which older gh versions on the runner lack.)
            subprocess.run(
                ["gh", "secret", "set", key, "--repo", repo],
                input=value, env=env, check=True,
                capture_output=True, text=True, timeout=30,
            )
        except FileNotFoundError:
            log.error("gh CLI not found — cannot persist %s to GitHub Secrets.", key)
            return
        except subprocess.CalledProcessError as e:
            log.error("Failed to persist %s to GitHub Secrets: %s", key, (e.stderr or "").strip())
            return
        except subprocess.TimeoutExpired:
            log.error("Timed out persisting %s to GitHub Secrets.", key)
            return
    log.info("Rotated tokens persisted to GitHub Secrets (repo=%s).", repo)


def _save_tokens(access_token: str, refresh_token: str, expires_in: int) -> None:
    """Persist tokens and expiry to the .env file, and (in CI) to GitHub Secrets."""
    expiry = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    expiry_iso = expiry.isoformat()
    set_key(ENV_FILE, "QBO_ACCESS_TOKEN", access_token)
    set_key(ENV_FILE, "QBO_REFRESH_TOKEN", refresh_token)
    set_key(ENV_FILE, "QBO_TOKEN_EXPIRY", expiry_iso)
    os.environ["QBO_ACCESS_TOKEN"] = access_token
    os.environ["QBO_REFRESH_TOKEN"] = refresh_token
    os.environ["QBO_TOKEN_EXPIRY"] = expiry_iso
    log.info("Tokens saved — access token expires %s", expiry.strftime("%Y-%m-%d %H:%M:%S UTC"))
    _persist_to_github_secrets(access_token, refresh_token, expiry_iso)


def _is_token_expired() -> bool:
    """Return True if the access token is absent or expires within 5 minutes."""
    expiry_str = os.getenv("QBO_TOKEN_EXPIRY", "")
    if not expiry_str:
        return True
    try:
        expiry = datetime.fromisoformat(expiry_str)
        return datetime.now(timezone.utc) >= expiry - timedelta(minutes=5)
    except ValueError:
        return True


# ---------------------------------------------------------------------------
# GitHub PAT expiry monitoring
# ---------------------------------------------------------------------------

GITHUB_API_ROOT = "https://api.github.com/"
_PAT_EXPIRY_HEADER = "github-authentication-token-expiration"


def _parse_pat_expiry(raw: str) -> datetime | None:
    """
    Parse the github-authentication-token-expiration header into a tz-aware UTC
    datetime. GitHub emits e.g. '2026-06-30 23:59:59 UTC'; tolerate a numeric
    offset or ISO-8601 too. Returns None if the value can't be parsed.
    """
    raw = (raw or "").strip()
    if not raw:
        return None

    # Common form: "2026-06-30 23:59:59 UTC"
    if raw.upper().endswith("UTC"):
        try:
            naive = datetime.strptime(raw[:-3].strip(), "%Y-%m-%d %H:%M:%S")
            return naive.replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    # Numeric-offset form: "2026-06-30 23:59:59 +0000" / "-0700"
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue

    # Last resort: ISO-8601 (assume UTC if no offset given)
    try:
        dt = datetime.fromisoformat(raw)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def github_pat_expiry() -> datetime | None:
    """
    Return the GH_PAT's expiry as a tz-aware UTC datetime, read from GitHub's
    response header on an authenticated request.

    Best-effort and CI-oriented: returns None when there is no GH_PAT (local
    runs), when the request fails, when GitHub answers non-200 (e.g. the PAT is
    already invalid — the writeback path logs that separately), or when the PAT
    has no expiration (non-expiring classic PATs emit no expiry header). The PAT
    travels only in the Authorization header and is never logged. Never raises.
    """
    pat = os.getenv("GH_PAT", "")
    if not pat:
        return None

    try:
        resp = requests.get(
            GITHUB_API_ROOT,
            headers={
                "Authorization": f"Bearer {pat}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=15,
        )
    except requests.RequestException as exc:
        log.warning("Could not reach GitHub to check PAT expiry: %s", exc)
        return None

    if resp.status_code != 200:
        log.warning("PAT expiry check got HTTP %s from GitHub — skipping.", resp.status_code)
        return None

    expiry = _parse_pat_expiry(resp.headers.get(_PAT_EXPIRY_HEADER, ""))
    if expiry is None:
        log.info("No parseable PAT expiry header (PAT may be non-expiring).")
    return expiry


# ---------------------------------------------------------------------------
# Token refresh
# ---------------------------------------------------------------------------


def refresh_tokens() -> str:
    """Exchange the stored refresh token for a new access token.

    Returns the new access token. Raises RuntimeError if the refresh fails.
    """
    client_id = os.environ["QBO_CLIENT_ID"]
    client_secret = os.environ["QBO_CLIENT_SECRET"]
    refresh_token = os.environ.get("QBO_REFRESH_TOKEN", "")

    if not refresh_token:
        raise RuntimeError("No refresh token stored — run the initial OAuth flow first.")

    log.info(
        "OAuth handshake — grant_type=refresh_token endpoint=%s client_id=%s…",
        TOKEN_URL, client_id[:8],
    )
    response = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        auth=(client_id, client_secret),
        timeout=15,
    )

    log.info(
        "OAuth handshake response — status=%s intuit_tid=%s",
        response.status_code,
        response.headers.get("intuit_tid", ""),
    )

    if response.status_code != 200:
        log.error(
            "Token refresh failed — status=%s body=%s",
            response.status_code,
            response.text,
        )
        raise RuntimeError(
            f"Token refresh failed ({response.status_code}): {response.text}"
        )

    payload = response.json()
    log.info(
        "Token refresh successful — token_type=%s expires_in=%ss",
        payload.get("token_type", "bearer"),
        payload.get("expires_in", "?"),
    )
    _save_tokens(
        payload["access_token"],
        payload["refresh_token"],
        payload["expires_in"],
    )
    return payload["access_token"]


# ---------------------------------------------------------------------------
# Authenticated session (auto-refresh on 401)
# ---------------------------------------------------------------------------


class QBOSession(requests.Session):
    """A requests.Session that transparently refreshes tokens on 401 responses."""

    def request(self, method, url, **kwargs):  # noqa: D102
        if _is_token_expired():
            refresh_tokens()

        kwargs.setdefault("headers", {})
        kwargs["headers"]["Authorization"] = f"Bearer {os.environ['QBO_ACCESS_TOKEN']}"
        kwargs["headers"]["Accept"] = "application/json"

        response = super().request(method, url, **kwargs)
        intuit_tid = response.headers.get("intuit_tid", "")

        if response.status_code == 401:
            log.warning(
                "%s %s → 401 Unauthorized (intuit_tid=%s) — refreshing token and retrying",
                method, url, intuit_tid,
            )
            refresh_tokens()
            kwargs["headers"]["Authorization"] = (
                f"Bearer {os.environ['QBO_ACCESS_TOKEN']}"
            )
            response = super().request(method, url, **kwargs)
            intuit_tid = response.headers.get("intuit_tid", "")

        if response.status_code >= 400:
            log.error(
                "%s %s → %s (intuit_tid=%s) body=%s",
                method, url, response.status_code, intuit_tid, response.text[:500],
            )
        else:
            log.info(
                "%s %s → %s (intuit_tid=%s)",
                method, url, response.status_code, intuit_tid,
            )

        return response


def get_session() -> QBOSession:
    """Return an authenticated QBOSession, refreshing tokens as needed."""
    if _is_token_expired():
        refresh_tokens()
    return QBOSession()


# ---------------------------------------------------------------------------
# Initial OAuth 2.0 browser flow
# ---------------------------------------------------------------------------


class _CallbackHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler that captures the authorization code from the callback."""

    auth_code: str | None = None
    state: str | None = None

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        _CallbackHandler.auth_code = params.get("code", [None])[0]
        _CallbackHandler.state = params.get("state", [None])[0]

        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(
            b"<h2>Authorization successful!</h2>"
            b"<p>You can close this tab and return to the terminal.</p>"
        )

    def log_message(self, format, *args):  # noqa: D102, A002
        pass  # suppress default request logging


def _run_callback_server(host: str, port: int) -> None:
    server = HTTPServer((host, port), _CallbackHandler)
    server.handle_request()  # serve exactly one request then stop


def initial_auth_flow() -> str:
    """
    Open the Intuit authorization page in the user's browser, capture the
    callback, exchange the code for tokens, and return the access token.

    Call this once manually; thereafter use get_session() or refresh_tokens().
    """
    client_id = os.environ["QBO_CLIENT_ID"]
    client_secret = os.environ["QBO_CLIENT_SECRET"]
    redirect_uri = os.environ["QBO_REDIRECT_URI"]

    # The local callback server always binds to localhost.
    # When using ngrok the redirect URI hostname is the tunnel domain — we must
    # not try to bind to that. Port falls back to QBO_CALLBACK_PORT or 8080.
    host = "localhost"
    port = int(os.getenv("QBO_CALLBACK_PORT", "8080"))

    import secrets
    state = secrets.token_urlsafe(16)

    auth_params = {
        "client_id": client_id,
        "response_type": "code",
        "scope": SCOPES,
        "redirect_uri": redirect_uri,
        "state": state,
    }
    authorization_url = f"{AUTH_URL}?{urlencode(auth_params)}"

    # Start the callback server in a background thread before opening the browser.
    server_thread = threading.Thread(
        target=_run_callback_server, args=(host, port), daemon=True
    )
    server_thread.start()

    print(f"\nOpening browser for Intuit authorization…\n{authorization_url}\n")
    webbrowser.open(authorization_url)

    server_thread.join(timeout=120)

    if _CallbackHandler.auth_code is None:
        raise RuntimeError(
            "Did not receive authorization code within 120 seconds. "
            "Check that the redirect URI matches the one registered in the Intuit Developer Portal."
        )

    if _CallbackHandler.state != state:
        raise RuntimeError("OAuth state mismatch — possible CSRF attack, aborting.")

    # Exchange the authorization code for tokens.
    response = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": _CallbackHandler.auth_code,
            "redirect_uri": redirect_uri,
        },
        auth=(client_id, client_secret),
        timeout=15,
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"Token exchange failed ({response.status_code}): {response.text}"
        )

    payload = response.json()
    _save_tokens(
        payload["access_token"],
        payload["refresh_token"],
        payload["expires_in"],
    )

    print("Tokens obtained and saved to .env successfully.")
    return payload["access_token"]


# ---------------------------------------------------------------------------
# Token revocation (optional cleanup utility)
# ---------------------------------------------------------------------------


def revoke_tokens() -> None:
    """Revoke both the access and refresh tokens (e.g. on deauthorization)."""
    client_id = os.environ["QBO_CLIENT_ID"]
    client_secret = os.environ["QBO_CLIENT_SECRET"]

    for token_key in ("QBO_ACCESS_TOKEN", "QBO_REFRESH_TOKEN"):
        token = os.environ.get(token_key, "")
        if not token:
            continue
        requests.post(
            REVOKE_URL,
            data={"token": token},
            auth=(client_id, client_secret),
            timeout=10,
        )
        set_key(ENV_FILE, token_key, "")
        os.environ[token_key] = ""

    set_key(ENV_FILE, "QBO_TOKEN_EXPIRY", "")
    os.environ["QBO_TOKEN_EXPIRY"] = ""
    print("Tokens revoked.")


# ---------------------------------------------------------------------------
# CLI entry point — run `python auth.py` to kick off the first-time flow
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    if "--revoke" in sys.argv:
        revoke_tokens()
    else:
        access_token = initial_auth_flow()
        realm_id = os.getenv("QBO_REALM_ID", "<not set>")
        print(f"Realm ID in .env: {realm_id}")
        print("Run `python fetcher.py` next to pull P&L data.")
