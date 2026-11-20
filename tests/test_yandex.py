import json
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import parseaddr
from imaplib import IMAP4, Time2Internaldate
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from homeagent.config.settings import Secrets, Settings
from homeagent.core.events import SourceKind
from homeagent.core.ingest import Ingestor
from homeagent.db.models import Base
from homeagent.db.session import make_engine, make_sessions
from homeagent.sources.yandex import YandexSource

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "mails.json"
VALIDITY = 7
SINCE_FORMAT = "%d-%b-%Y"

type Fetched = list[bytes | tuple[bytes, bytes]]


class Mail(BaseModel):
    id: str
    sender: str
    subject: str
    date: datetime
    text: str = ""
    html_only: bool = False
    snippet: str = ""


def _mails() -> list[Mail]:
    rows: list[dict[str, object]] = json.loads(FIXTURES.read_text(encoding="utf-8"))
    return [Mail.model_validate({**row, "sender": row["from"]}) for row in rows]


def to_rfc822(mail: Mail) -> bytes:
    message = EmailMessage()
    message["From"] = mail.sender
    message["Subject"] = mail.subject
    if mail.html_only:
        message.set_content(f"<p>{mail.snippet}</p>", subtype="html")
    else:
        message.set_content(mail.text)
        message.add_alternative(f"<p>{mail.snippet}</p>", subtype="html")
    return message.as_bytes()


class FakeImap(IMAP4):
    def __init__(self, messages: dict[int, tuple[datetime, bytes]]) -> None:
        self.messages = messages
        self.validity = VALIDITY
        self.uid_next = max(messages, default=0) + 1
        self.commands: list[tuple[str, ...]] = []

    def login(self, user: str, password: str) -> tuple[Literal["OK"], list[bytes]]:
        self.commands.append(("LOGIN", user, password))
        return "OK", [b"LOGIN completed"]

    def status(self, mailbox: str, names: str) -> tuple[str, Fetched]:
        self.commands.append(("STATUS", mailbox, names))
        line = f"{mailbox} (UIDNEXT {self.uid_next} UIDVALIDITY {self.validity})"
        return "OK", [line.encode()]

    def select(
        self, mailbox: str = "INBOX", readonly: bool = False
    ) -> tuple[str, list[bytes | None]]:
        self.commands.append(("SELECT", mailbox, str(readonly)))
        return "OK", [str(len(self.messages)).encode()]

    def uid(self, command: str, *args: str) -> tuple[str, Fetched]:
        self.commands.append((command, *args))
        if command == "SEARCH":
            return "OK", [" ".join(map(str, self._search(*args))).encode()]
        uid = int(args[0])
        if uid not in self.messages:
            return "OK", [b"(nothing)"]
        date, content = self.messages[uid]
        envelope = f"1 (UID {uid} INTERNALDATE {Time2Internaldate(date)} BODY[] {{{len(content)}}}"
        return "OK", [(envelope.encode(), content), b")"]

    def logout(self) -> tuple[str, list[None]]:
        self.commands.append(("LOGOUT",))
        return "BYE", [None]

    def _search(self, key: str, value: str) -> list[int]:
        if key == "SINCE":
            since = datetime.strptime(value, SINCE_FORMAT).date()
            return [uid for uid, (date, _) in self.messages.items() if date.date() >= since]
        first = int(value.removesuffix(":*"))
        return [uid for uid in self.messages if uid >= first] or [max(self.messages)]


def _inbox(mails: list[Mail]) -> dict[int, tuple[datetime, bytes]]:
    return {uid: (mail.date, to_rfc822(mail)) for uid, mail in enumerate(mails, start=1)}


def _source(settings: Settings, imap: FakeImap) -> YandexSource:
    cfg = next(c for c in settings.sources.email if c.name == "yandex")
    source = YandexSource(cfg, Secrets.model_validate({"yandex_app_password": "app-pw"}))
    source.connect = lambda: imap
    return source


def _recent(mails: list[Mail]) -> list[Mail]:
    now = datetime.now(UTC)
    return [m.model_copy(update={"date": now - timedelta(hours=i)}) for i, m in enumerate(mails)]


def test_twenty_fixture_mails_normalise_to_events(settings: Settings) -> None:
    mails = _mails()
    imap = FakeImap(_inbox(mails))
    source = _source(settings, imap)

    raws = [source.fetch(imap, str(VALIDITY), uid) for uid in imap.messages]
    events = [source.normalise(raw.model_dump(mode="json")) for raw in raws if raw is not None]

    assert len(events) == 20
    for mail, event in zip(mails, events, strict=True):
        assert event is not None
        assert event.source == SourceKind.MAIL
        assert event.source_name == "yandex"
        assert event.title == mail.subject
        assert event.body == (mail.text or mail.snippet)
        assert parseaddr(event.payload["from"]) == parseaddr(mail.sender)
        assert event.sender in mail.sender.lower()
        assert event.occurred_at == mail.date
    assert events[0] is not None
    assert events[0].external_id == f"{VALIDITY}:1"


async def test_first_poll_reads_the_backfill_window_without_marking_mail_read(
    settings: Settings,
) -> None:
    mails = _recent(_mails()[:5])
    old = datetime.now(UTC) - timedelta(days=10)
    mails[0] = mails[0].model_copy(update={"date": old})
    imap = FakeImap(_inbox(mails))

    raws, cursor = await _source(settings, imap).poll(None)

    assert [r["subject"] for r in raws] == [m.subject for m in mails[1:]]
    assert cursor == f"{VALIDITY}:5"
    assert ("LOGIN", "you@yandex.ru", "app-pw") in imap.commands
    assert ("SELECT", "INBOX", "True") in imap.commands
    fetches = [c for c in imap.commands if c[0] == "FETCH"]
    assert fetches and all("BODY.PEEK[]" in c[2] for c in fetches)
    assert imap.commands[-1] == ("LOGOUT",)


async def test_next_poll_reads_only_new_uids_and_skips_deleted_ones(settings: Settings) -> None:
    mails = _recent(_mails()[:6])
    imap = FakeImap(_inbox(mails))
    source = _source(settings, imap)

    nothing, same = await source.poll(f"{VALIDITY}:6")
    del imap.messages[5]
    raws, cursor = await source.poll(f"{VALIDITY}:3")

    assert nothing == []
    assert same == f"{VALIDITY}:6"
    assert [r["subject"] for r in raws] == [mails[3].subject, mails[5].subject]
    assert cursor == f"{VALIDITY}:6"


async def test_a_new_uid_validity_reads_the_backfill_window_again(settings: Settings) -> None:
    mails = _recent(_mails()[:3])
    imap = FakeImap(_inbox(mails))

    raws, cursor = await _source(settings, imap).poll("6:99")

    assert len(raws) == 3
    assert cursor == f"{VALIDITY}:3"


async def test_ingest_stores_backfill_once_then_follows_new_uids(
    tmp_path: Path, settings: Settings
) -> None:
    engine = make_engine(tmp_path / "t.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    ingestor = Ingestor(make_sessions(engine))
    mails = _recent(_mails())
    imap = FakeImap(_inbox(mails[:19]))
    source = _source(settings, imap)

    first = await ingestor.run(source)
    imap.messages[20] = (mails[19].date, to_rfc822(mails[19]))
    second = await ingestor.run(source)

    assert len(first.events) == 19
    assert first.cursor == f"{VALIDITY}:19"
    assert [e.title for e in second.events] == [mails[19].subject]
    assert second.cursor == f"{VALIDITY}:20"
    await engine.dispose()


def test_body_and_attachments_are_read_as_from_gmail(settings: Settings) -> None:
    html = EmailMessage()
    html.set_content(
        "<html><head><title>x</title></head><style>p{}</style><p>Guten&nbsp;Tag</p>"
        "<div>Bitte  zahlen<br>bis 30.11.</div><p></p><p></p>"
        "<table><tr><td>CHF</td><td>120</td></tr></table>",
        subtype="html",
    )
    mixed = EmailMessage()
    mixed["From"] = "Rechnung <billing@shop.example>"
    mixed["Subject"] = "=?utf-8?q?Rechnung_f=C3=BCr_Oktober?="
    mixed.set_content("first\n")
    mixed.add_alternative("<p>ignored when plain text exists</p>", subtype="html")
    mixed.add_attachment(b"%PDF", maintype="application", subtype="pdf", filename="Rechnung.pdf")
    mixed.add_attachment("attached notes", filename="notes.txt")
    date = datetime(2026, 11, 18, 7, 12, tzinfo=UTC)
    imap = FakeImap({1: (date, html.as_bytes()), 2: (date, mixed.as_bytes())})
    source = _source(settings, imap)

    html_only = source.fetch(imap, str(VALIDITY), 1)
    plain = source.fetch(imap, str(VALIDITY), 2)

    assert html_only is not None
    assert html_only.body == "Guten Tag\n Bitte zahlen\nbis 30.11.\n\n CHF 120"
    assert plain is not None
    assert plain.body == "first"
    assert plain.subject == "Rechnung für Oktober"
    assert plain.attachments == ["Rechnung.pdf", "notes.txt"]
