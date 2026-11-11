import sys
import webbrowser
from base64 import urlsafe_b64encode
from hashlib import sha256
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from secrets import token_urlsafe
from typing import override
from urllib.parse import parse_qs, quote, urlencode, urlparse

import httpx
from pydantic import SecretStr

from homeagent.config.settings import Secrets, Settings, load_settings
from homeagent.core.google_guard import ensure_safe

ENV_FILE = Path(".env")
REFRESH_KEY = "HOMEAGENT_GOOGLE_REFRESH_TOKEN"
LOOPBACK = "127.0.0.1"
DONE_PAGE = b"homeagent: login received, you can close this tab."
CALLBACK: dict[str, str] = {}


class _Callback(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        query = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        if "code" in query or "error" in query:
            CALLBACK.update(query)
        self.send_response(HTTPStatus.OK)
        self.end_headers()
        self.wfile.write(DONE_PAGE)

    @override
    def log_message(self, format: str, *args: object) -> None:
        return None


def _b64(raw: bytes) -> str:
    return urlsafe_b64encode(raw).decode().rstrip("=")


def _secret(value: SecretStr | None) -> str:
    return value.get_secret_value() if value else ""


def login(settings: Settings, secrets: Secrets) -> int:
    google = settings.sources.google
    server = HTTPServer((LOOPBACK, 0), _Callback)
    redirect = f"http://{LOOPBACK}:{server.server_port}"
    verifier = token_urlsafe(64)
    state = token_urlsafe(16)
    params = {
        "client_id": _secret(secrets.google_client_id),
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": " ".join(google.scopes),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "code_challenge": _b64(sha256(verifier.encode()).digest()),
        "code_challenge_method": "S256",
    }
    url = f"{google.auth_url}?{urlencode(params)}"
    print(f"open in the browser if it does not open by itself:\n{url}", flush=True)
    webbrowser.open(url)
    while not CALLBACK:
        server.handle_request()
    if "error" in CALLBACK or CALLBACK.get("state") != state:
        print(f"login failed: {CALLBACK.get('error', 'state mismatch')}")
        return 1
    response = httpx.post(
        google.token_url,
        data={
            "grant_type": "authorization_code",
            "code": CALLBACK["code"],
            "client_id": _secret(secrets.google_client_id),
            "client_secret": _secret(secrets.google_client_secret),
            "redirect_uri": redirect,
            "code_verifier": verifier,
        },
        timeout=google.timeout_s,
    )
    if not response.is_success:
        print(f"token exchange failed: {response.status_code} {response.text}")
        return 1
    tokens: dict[str, str] = response.json()
    if "refresh_token" not in tokens:
        print("no refresh token returned")
        return 1
    lines = [
        line
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines()
        if not line.startswith(f"{REFRESH_KEY}=")
    ]
    lines.append(f"{REFRESH_KEY}={tokens['refresh_token']}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"refresh token saved to .env\ngranted: {tokens.get('scope', '')}")
    return 0


def _report(name: str, response: httpx.Response, detail: str = "") -> bool:
    if response.is_success:
        print(f"ok    {name} {detail}".rstrip())
        return True
    message = response.text.replace("\n", " ")[:200]
    print(f"FAIL  {name}: {response.status_code} {message}")
    return False


def check(settings: Settings, secrets: Secrets) -> int:
    google = settings.sources.google
    gmail = next(c for c in settings.sources.email if c.name == "gmail")
    token = httpx.post(
        google.token_url,
        data={
            "grant_type": "refresh_token",
            "client_id": _secret(secrets.google_client_id),
            "client_secret": _secret(secrets.google_client_secret),
            "refresh_token": _secret(secrets.google_refresh_token),
        },
        timeout=google.timeout_s,
    )
    if not _report("refresh token", token):
        return 1
    grant: dict[str, str] = token.json()
    print(f"      granted: {grant.get('scope', '')}")

    def guard(request: httpx.Request) -> None:
        ensure_safe(request, google.token_url)

    client = httpx.Client(
        headers={"Authorization": f"Bearer {grant['access_token']}"},
        timeout=google.timeout_s,
        event_hooks={"request": [guard]},
    )
    results: list[bool] = []

    labels = client.get(f"{gmail.url}labels")
    names: set[str] = {str(x["name"]) for x in labels.json().get("labels", [])}
    missing = sorted(set(gmail.labels) - names) if labels.is_success else []
    detail = f"{len(names)} labels, missing from config: {missing or 'none'}"
    results.append(_report("gmail list labels", labels, detail))

    listing = client.get(f"{gmail.url}messages", params={"maxResults": 1, "labelIds": "INBOX"})
    results.append(_report("gmail list inbox", listing))
    refs: list[dict[str, str]] = listing.json().get("messages", []) if listing.is_success else []
    if refs:
        message_id = refs[0]["id"]
        full = client.get(f"{gmail.url}messages/{message_id}", params={"format": "full"})
        results.append(_report("gmail read one mail", full))
        probe = client.post(f"{gmail.url}messages/{message_id}/modify", json={"addLabelIds": []})
        allowed = probe.status_code != HTTPStatus.FORBIDDEN
        print(f"{'ok  ' if allowed else 'FAIL'}  gmail apply label to a mail: {probe.status_code}")
        results.append(allowed)

        minimal = client.get(f"{gmail.url}messages/{message_id}", params={"format": "minimal"})
        start = str(minimal.json().get("historyId", "")) if minimal.is_success else ""
        history = client.get(f"{gmail.url}history", params={"startHistoryId": start})
        results.append(_report("gmail history (cursor)", history))

    for cal in settings.sources.calendar:
        events = client.get(
            f"{google.calendar_url}calendars/{quote(cal.calendar_id, safe='')}/events",
            params={"maxResults": 1},
        )
        results.append(_report(f"calendar {cal.name} read events", events))

    lists = client.get(f"{google.tasks_url}users/@me/lists")
    count = len(lists.json().get("items", [])) if lists.is_success else 0
    results.append(_report("tasks list task lists", lists, f"{count} lists"))
    return 0 if all(results) else 1


def main(argv: list[str]) -> int:
    command = argv[0] if argv else "check"
    settings, secrets = load_settings(), Secrets()
    if command == "login":
        return login(settings, secrets)
    if command == "check":
        return check(settings, secrets)
    print("use: google_auth.py login | check")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
