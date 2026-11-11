import httpx
import pytest

from homeagent.config.settings import Secrets, Settings
from homeagent.core.google_guard import UnsafeGoogleRequestError, ensure_safe, google_client
from homeagent.sources.gmail import GmailSource

type Call = tuple[str, str, str, dict[str, object] | None]


def _urls(settings: Settings) -> dict[str, str]:
    google = settings.sources.google
    gmail = next(c for c in settings.sources.email if c.name == "gmail")
    return {"gmail": gmail.url, "calendar": google.calendar_url, "tasks": google.tasks_url}


ALLOWED: list[Call] = [
    ("GET", "gmail", "messages/m1", None),
    ("GET", "gmail", "history", None),
    ("POST", "gmail", "messages/m1/modify", {"addLabelIds": ["Label_7"]}),
    ("POST", "gmail", "messages/m1/modify", {"addLabelIds": []}),
    ("POST", "gmail", "labels", {"name": "Kita"}),
    ("GET", "tasks", "lists/L1/tasks", None),
    ("POST", "tasks", "lists/L1/tasks", {"title": "Book childcare"}),
    ("GET", "calendar", "calendars/work/events", None),
    ("POST", "calendar", "calendars/work/events", {"summary": "Home (family)"}),
]

BLOCKED: list[Call] = [
    ("DELETE", "gmail", "messages/m1", None),
    ("POST", "gmail", "messages/m1/trash", None),
    ("POST", "gmail", "threads/t1/trash", None),
    ("DELETE", "gmail", "threads/t1", None),
    ("POST", "gmail", "messages/batchDelete", {"ids": ["m1"]}),
    ("POST", "gmail", "messages/batchModify", {"ids": ["m1"], "addLabelIds": ["Label_7"]}),
    ("POST", "gmail", "messages/m1/modify", {"addLabelIds": ["TRASH"]}),
    ("POST", "gmail", "messages/m1/modify", {"addLabelIds": ["SPAM"]}),
    ("POST", "gmail", "messages/m1/modify", {"removeLabelIds": ["INBOX"]}),
    ("POST", "gmail", "messages/m1/modify", {"addLabelIds": ["Label_7"], "ids": ["m2"]}),
    ("POST", "gmail", "threads/t1/modify", {"addLabelIds": ["Label_7"]}),
    ("DELETE", "gmail", "labels/Label_7", None),
    ("PATCH", "gmail", "labels/Label_7", {"name": "x"}),
    ("PUT", "gmail", "labels/Label_7", {"name": "x"}),
    ("DELETE", "gmail", "drafts/d1", None),
    ("POST", "gmail", "messages/import", {"raw": "x"}),
    ("DELETE", "tasks", "lists/L1/tasks/T1", None),
    ("PATCH", "tasks", "lists/L1/tasks/T1", {"deleted": True}),
    ("PUT", "tasks", "lists/L1/tasks/T1", {"title": ""}),
    ("POST", "tasks", "lists/L1/clear", None),
    ("POST", "tasks", "lists/L1/tasks/T1/move", None),
    ("DELETE", "tasks", "users/@me/lists/L1", None),
    ("PATCH", "tasks", "users/@me/lists/L1", {"title": "x"}),
    ("DELETE", "calendar", "calendars/work/events/E1", None),
    ("PATCH", "calendar", "calendars/work/events/E1", {"status": "cancelled"}),
    ("PUT", "calendar", "calendars/work/events/E1", {"summary": ""}),
    ("POST", "calendar", "calendars/work/events/E1/move", None),
    ("POST", "calendar", "calendars/work/clear", None),
    ("DELETE", "calendar", "calendars/work", None),
]


def _request(settings: Settings, call: Call) -> httpx.Request:
    method, api, path, body = call
    return httpx.Request(method, f"{_urls(settings)[api]}{path}", json=body)


@pytest.mark.parametrize("call", ALLOWED, ids=lambda c: f"{c[0]} {c[1]} {c[2]}")
def test_reads_label_adds_and_creates_pass(settings: Settings, call: Call) -> None:
    ensure_safe(_request(settings, call), settings.sources.google.token_url)


@pytest.mark.parametrize("call", BLOCKED, ids=lambda c: f"{c[0]} {c[1]} {c[2]} {c[3]}")
def test_deletes_trash_edits_and_clears_are_blocked(settings: Settings, call: Call) -> None:
    with pytest.raises(UnsafeGoogleRequestError):
        ensure_safe(_request(settings, call), settings.sources.google.token_url)


def test_token_refresh_passes_but_other_oauth_posts_do_not(settings: Settings) -> None:
    token_url = settings.sources.google.token_url
    ensure_safe(httpx.Request("POST", token_url, data={"grant_type": "x"}), token_url)
    with pytest.raises(UnsafeGoogleRequestError):
        ensure_safe(httpx.Request("POST", f"{token_url}/revoke"), token_url)


async def test_blocked_request_never_reaches_the_network(settings: Settings) -> None:
    sent: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200)

    google = settings.sources.google
    client = google_client(google.token_url, google.tasks_url, 5, httpx.MockTransport(handle))
    with pytest.raises(UnsafeGoogleRequestError):
        await client.delete("lists/L1/tasks/T1")
    await client.get("lists/L1/tasks")

    assert [r.method for r in sent] == ["GET"]


async def test_gmail_source_client_is_guarded(settings: Settings) -> None:
    cfg = next(c for c in settings.sources.email if c.name == "gmail")
    source = GmailSource(cfg, settings.sources.google, Secrets.model_validate({}))

    with pytest.raises(UnsafeGoogleRequestError):
        await source.client.post("messages/m1/trash")
