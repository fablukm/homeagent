import asyncio
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import parseaddr
from imaplib import IMAP4, IMAP4_SSL
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from homeagent.config.settings import EmailSourceCfg, Secrets
from homeagent.core.events import Event, SourceKind
from homeagent.sources.base import EmailSource, Upstream, register_source
from homeagent.sources.gmail import HTML, NEWLINE, PLAIN, html_text

INBOX = "INBOX"
STATUS_ITEMS = "(UIDNEXT UIDVALIDITY)"
STATUS_VALUE = re.compile(rb"(UIDNEXT|UIDVALIDITY) (\d+)")
FETCH_ITEMS = "(INTERNALDATE BODY.PEEK[])"
INTERNALDATE = re.compile(rb'INTERNALDATE "([^"]+)"')
INTERNALDATE_FORMAT = "%d-%b-%Y %H:%M:%S %z"
SINCE_FORMAT = "%d-%b-%Y"
CURSOR_SEP = ":"
IMAPS_PORT = 993


class _Mail(BaseModel):
    id: str
    received: datetime
    sender: str
    subject: str
    body: str
    attachments: list[str] = Field(default_factory=list[str])


def _texts(message: EmailMessage, mime_type: str) -> list[str]:
    return [
        str(part.get_content())
        for part in message.walk()
        if part.get_content_type() == mime_type and not part.get_filename()
    ]


def _body(message: EmailMessage) -> str:
    plain = NEWLINE.join(_texts(message, PLAIN)).strip()
    return plain or html_text(NEWLINE.join(_texts(message, HTML)))


@register_source("yandex")
class YandexSource(EmailSource):
    def __init__(self, cfg: EmailSourceCfg, secrets: Secrets) -> None:
        super().__init__(cfg)
        password = secrets.yandex_app_password
        self.password = password.get_secret_value() if password else ""
        server = urlsplit(cfg.url)
        host, port = server.hostname or "", server.port or IMAPS_PORT
        self.connect: Callable[[], IMAP4] = lambda: IMAP4_SSL(host, port, timeout=cfg.timeout_s)

    async def poll(self, cursor: str | None) -> tuple[list[Upstream], str | None]:
        return await asyncio.to_thread(self._poll, cursor)

    def normalise(self, raw: Upstream) -> Event | None:
        mail = _Mail.model_validate(raw)
        return Event(
            source=SourceKind.MAIL,
            source_name=self.name,
            external_id=mail.id,
            occurred_at=mail.received,
            title=mail.subject,
            body=mail.body,
            sender=parseaddr(mail.sender)[1].lower(),
            payload={"from": mail.sender, "attachments": mail.attachments},
        )

    def _poll(self, cursor: str | None) -> tuple[list[Upstream], str]:
        imap = self.connect()
        try:
            imap.login(self.cfg.account, self.password)
            _, status = imap.status(INBOX, STATUS_ITEMS)
            values = dict(STATUS_VALUE.findall(status[0]))
            validity = values[b"UIDVALIDITY"].decode()
            imap.select(INBOX, readonly=True)
            known, _, last = (cursor or "").partition(CURSOR_SEP)
            if known == validity:
                seen = int(last)
                uids = [uid for uid in self._search(imap, "UID", f"{seen + 1}:*") if uid > seen]
                newest = max(uids, default=seen)
            else:
                since = datetime.now(UTC) - timedelta(days=self.cfg.backfill_days)
                uids = self._search(imap, "SINCE", since.strftime(SINCE_FORMAT))
                newest = int(values[b"UIDNEXT"]) - 1
            mails = [self.fetch(imap, validity, uid) for uid in uids]
        finally:
            imap.logout()
        raws = [mail.model_dump(mode="json") for mail in mails if mail is not None]
        return raws, f"{validity}{CURSOR_SEP}{newest}"

    @staticmethod
    def _search(imap: IMAP4, *criteria: str) -> list[int]:
        _, found = imap.uid("SEARCH", *criteria)
        return [int(uid) for uid in found[0].split()]

    @staticmethod
    def fetch(imap: IMAP4, validity: str, uid: int) -> _Mail | None:
        _, data = imap.uid("FETCH", str(uid), FETCH_ITEMS)
        found: list[tuple[bytes, bytes]] = [item for item in data if isinstance(item, tuple)]
        if not found:
            return None
        [(envelope, content)] = found
        message = message_from_bytes(content, policy=policy.default)
        stamp = INTERNALDATE.findall(envelope)[0].decode().strip()
        received = datetime.strptime(stamp, INTERNALDATE_FORMAT)
        return _Mail(
            id=f"{validity}{CURSOR_SEP}{uid}",
            received=received,
            sender=str(message.get("From", "")),
            subject=str(message.get("Subject", "")),
            body=_body(message),
            attachments=[name for part in message.walk() if (name := part.get_filename())],
        )
