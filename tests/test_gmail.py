import json
from base64 import urlsafe_b64encode
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs

import httpx
from pydantic import BaseModel

from homeagent.config.settings import Secrets, Settings
from homeagent.core.events import SourceKind
from homeagent.core.google_guard import google_client
from homeagent.core.ingest import Ingestor
from homeagent.db.models import Base
from homeagent.db.session import make_engine, make_sessions
from homeagent.sources.base import Upstream
from homeagent.sources.gmail import GmailSource

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "mails.json"
TOKEN = "access-1"


class Mail(BaseModel):
    id: str
    sender: str
    subject: str
    date: datetime
    label: str
    text: str = ""
    html_only: bool = False
    snippet: str = ""


def _mails() -> list[Mail]:
    rows: list[dict[str, object]] = json.loads(FIXTURES.read_text(encoding="utf-8"))
    return [Mail.model_validate({**row, "sender": row["from"]}) for row in rows]


def _b64(text: str) -> str:
    return urlsafe_b64encode(text.encode()).decode()


def to_gmail(mail: Mail) -> Upstream:
    headers = [{"name": "From", "value": mail.sender}, {"name": "subject", "value": mail.subject}]
    html = {"mimeType": "text/html", "body": {"data": _b64(f"<p>{mail.snippet}</p>")}}
    plain = {"mimeType": "text/plain", "body": {"data": _b64(mail.text)}}
    parts = [html] if mail.html_only else [plain, html]
    return {
        "id": mail.id,
        "threadId": f"t-{mail.id}",
        "labelIds": ["INBOX", "UNREAD"],
        "internalDate": str(int(mail.date.timestamp() * 1000)),
        "snippet": mail.snippet or mail.text[:40],
        "payload": {"mimeType": "multipart/alternative", "headers": headers, "parts": parts},
    }


def _source(settings: Settings, handle: httpx.MockTransport) -> GmailSource:
    cfg = next(c for c in settings.sources.email if c.name == "gmail")
    secrets = Secrets.model_validate(
        {"google_client_id": "c", "google_client_secret": "s", "google_refresh_token": "r"}
    )
    source = GmailSource(cfg, settings.sources.google, secrets)
    source.client = google_client(settings.sources.google.token_url, cfg.url, cfg.timeout_s, handle)
    return source


class FakeGmail:
    def __init__(self, settings: Settings, mails: list[Mail]) -> None:
        self.cfg = next(c for c in settings.sources.email if c.name == "gmail")
        self.token_url = settings.sources.google.token_url
        self.messages = {m.id: to_gmail(m) for m in mails}
        self.history: list[list[str]] = []
        self.history_id = 100
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix(httpx.URL(self.cfg.url).path)
        query = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        if str(request.url) == self.token_url:
            form = parse_qs(request.content.decode())
            assert form["refresh_token"] == ["r"]
            return httpx.Response(200, json={"access_token": TOKEN, "expires_in": 3599})
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        if path == "messages" and "maxResults" in query:
            return httpx.Response(200, json={"messages": [{"id": next(iter(self.messages))}]})
        if path == "messages":
            assert query["labelIds"] == "INBOX"
            return self._page([{"id": i} for i in self.messages], query, "messages", {})
        if path == "history":
            start = int(query["startHistoryId"])
            records = [
                {"messagesAdded": [{"message": {"id": i}} for i in ids]}
                for ids in self.history[start - 100 :]
            ]
            extra = {"historyId": str(100 + len(self.history))}
            return self._page(records, query, "history", extra)
        message_id = path.removeprefix("messages/")
        if message_id not in self.messages:
            return httpx.Response(404)
        if query["format"] == "minimal":
            return httpx.Response(200, json={"id": message_id, "historyId": str(self.history_id)})
        return httpx.Response(200, json=self.messages[message_id])

    @staticmethod
    def _page(
        items: Sequence[object], query: dict[str, str], key: str, extra: dict[str, str]
    ) -> httpx.Response:
        half = len(items) // 2
        if "pageToken" in query:
            return httpx.Response(200, json={key: items[half:], **extra})
        return httpx.Response(200, json={key: items[:half], "nextPageToken": "p2", **extra})


def test_twenty_fixture_mails_normalise_to_events(settings: Settings) -> None:
    mails = _mails()
    source = _source(settings, httpx.MockTransport(lambda r: httpx.Response(500)))

    events = [source.normalise(to_gmail(m)) for m in mails]

    assert len(events) == len(mails) == 20
    for mail, event in zip(mails, events, strict=True):
        assert event is not None
        assert event.source == SourceKind.MAIL
        assert event.source_name == "gmail"
        assert event.external_id == mail.id
        assert event.title == mail.subject
        assert event.occurred_at == mail.date
        assert event.sender == mail.sender.split("<")[1].rstrip(">").lower()
        assert event.body == (mail.snippet if mail.html_only else mail.text)
        assert event.payload["thread"] == f"t-{mail.id}"


async def test_first_poll_backfills_inbox_and_takes_newest_mail_history_id(
    settings: Settings,
) -> None:
    fake = FakeGmail(settings, _mails())
    source = _source(settings, httpx.MockTransport(fake.handle))

    raws, cursor = await source.poll(None)

    assert [r["id"] for r in raws] == list(fake.messages)
    assert cursor == "100"
    backfill = [r for r in fake.requests if r.url.params.get("q")]
    assert [r.url.params["q"] for r in backfill] == ["newer_than:2d", "newer_than:2d"]
    assert not any(r.url.path.endswith("/profile") for r in fake.requests)


async def test_history_poll_dedupes_and_skips_deleted_messages(settings: Settings) -> None:
    mails = _mails()
    fake = FakeGmail(settings, mails)
    fake.history = [["m01", "gone"], ["m02", "m01"], ["m03"]]
    source = _source(settings, httpx.MockTransport(fake.handle))

    raws, cursor = await source.poll("100")

    assert [r["id"] for r in raws] == ["m01", "m02", "m03"]
    assert cursor == "103"
    history = [r for r in fake.requests if r.url.path.endswith("/history")]
    assert len(history) == 2
    assert history[0].url.params["labelId"] == "INBOX"


async def test_ingest_stores_backfill_once_then_follows_history(
    tmp_path: Path, settings: Settings
) -> None:
    engine = make_engine(tmp_path / "t.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    ingestor = Ingestor(make_sessions(engine))
    fake = FakeGmail(settings, _mails())
    source = _source(settings, httpx.MockTransport(fake.handle))

    first = await ingestor.run(source)
    fake.history = [["m05"]]
    second = await ingestor.run(source)

    assert len(first.events) == 20
    assert first.cursor == "100"
    assert second.events == []
    assert second.duplicates == 1
    assert second.cursor == "101"


def test_attachment_names_reach_the_event(settings: Settings) -> None:
    mail = _mails()[0]
    raw = to_gmail(mail)
    invoice = {"mimeType": "application/pdf", "filename": "Rechnung.pdf", "body": {"data": ""}}
    invite = {"mimeType": "text/calendar", "filename": "invite.ics", "body": {"data": ""}}
    nested = {"mimeType": "multipart/mixed", "parts": [invite]}
    raw["payload"]["parts"] += [invoice, nested]
    source = _source(settings, httpx.MockTransport(lambda r: httpx.Response(500)))

    event = source.normalise(raw)

    assert event is not None
    assert event.payload["attachments"] == ["Rechnung.pdf", "invite.ics"]
    assert event.body == mail.text
