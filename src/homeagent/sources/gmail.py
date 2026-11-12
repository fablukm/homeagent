from base64 import urlsafe_b64decode
from datetime import UTC, datetime
from email.utils import parseaddr
from http import HTTPStatus

from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic.alias_generators import to_camel

from homeagent.config.settings import EmailSourceCfg, GoogleCfg, Secrets
from homeagent.core.events import Event, SourceKind
from homeagent.core.google_guard import google_client
from homeagent.sources.base import EmailSource, Upstream, register_source

INBOX = "INBOX"
PLAIN = "text/plain"
MS_PER_S = 1000
CAMEL = ConfigDict(alias_generator=to_camel)


class _Token(BaseModel):
    access_token: str


class _Minimal(BaseModel):
    model_config = CAMEL

    history_id: str


class _Ref(BaseModel):
    id: str


class _MessageList(BaseModel):
    model_config = CAMEL

    messages: list[_Ref] = Field(default_factory=list[_Ref])
    next_page_token: str | None = None


class _Added(BaseModel):
    message: _Ref


class _HistoryRecord(BaseModel):
    model_config = CAMEL

    messages_added: list[_Added] = Field(default_factory=list[_Added])


class _History(BaseModel):
    model_config = CAMEL

    history: list[_HistoryRecord] = Field(default_factory=list[_HistoryRecord])
    history_id: str
    next_page_token: str | None = None


class _Header(BaseModel):
    name: str
    value: str


class _Body(BaseModel):
    data: str = ""


class _Part(BaseModel):
    model_config = CAMEL

    mime_type: str = ""
    filename: str = ""
    headers: list[_Header] = Field(default_factory=list[_Header])
    body: _Body = _Body()
    parts: list[_Part] = Field(default_factory=list["_Part"])

    def header(self, name: str) -> str:
        return next((h.value for h in self.headers if h.name.lower() == name.lower()), "")

    def plain_text(self) -> str:
        if self.mime_type == PLAIN and self.body.data:
            return urlsafe_b64decode(self.body.data).decode()
        return next((text for part in self.parts if (text := part.plain_text())), "")

    def filenames(self) -> list[str]:
        own = [self.filename] if self.filename else []
        return own + [name for part in self.parts for name in part.filenames()]


class _Message(BaseModel):
    model_config = CAMEL

    id: str
    thread_id: str = ""
    label_ids: list[str] = Field(default_factory=list[str])
    internal_date: int
    snippet: str = ""
    payload: _Part


@register_source("gmail")
class GmailSource(EmailSource):
    def __init__(self, cfg: EmailSourceCfg, google: GoogleCfg, secrets: Secrets) -> None:
        super().__init__(cfg)
        self.google = google
        self.secrets = secrets
        self.client = google_client(google.token_url, cfg.url, cfg.timeout_s)

    async def poll(self, cursor: str | None) -> tuple[list[Upstream], str | None]:
        headers = {"Authorization": f"Bearer {await self.access_token()}"}
        if cursor is None:
            ids, next_cursor = await self._backfill(headers)
        else:
            ids, next_cursor = await self._history(cursor, headers)
        raws: list[Upstream] = []
        for message_id in dict.fromkeys(ids):
            response = await self.client.get(
                f"messages/{message_id}", params={"format": "full"}, headers=headers
            )
            if response.status_code == HTTPStatus.NOT_FOUND:
                continue
            response.raise_for_status()
            raws.append(response.json())
        return raws, next_cursor

    def normalise(self, raw: Upstream) -> Event | None:
        message = _Message.model_validate(raw)
        return Event(
            source=SourceKind.MAIL,
            source_name=self.name,
            external_id=message.id,
            occurred_at=datetime.fromtimestamp(message.internal_date / MS_PER_S, UTC),
            title=message.payload.header("Subject"),
            body=message.payload.plain_text() or message.snippet,
            sender=parseaddr(message.payload.header("From"))[1].lower(),
            payload={
                "labels": message.label_ids,
                "thread": message.thread_id,
                "attachments": message.payload.filenames(),
            },
        )

    async def access_token(self) -> str:
        secrets = self.secrets
        response = await self.client.post(
            self.google.token_url,
            data={
                "grant_type": "refresh_token",
                "client_id": _secret(secrets.google_client_id),
                "client_secret": _secret(secrets.google_client_secret),
                "refresh_token": _secret(secrets.google_refresh_token),
            },
        )
        response.raise_for_status()
        return _Token.model_validate_json(response.content).access_token

    async def _backfill(self, headers: dict[str, str]) -> tuple[list[str], str | None]:
        cursor = await self._newest_history_id(headers)
        ids: list[str] = []
        params = {"labelIds": INBOX, "q": f"newer_than:{self.cfg.backfill_days}d"}
        while True:
            response = await self.client.get("messages", params=params, headers=headers)
            response.raise_for_status()
            page = _MessageList.model_validate_json(response.content)
            ids += [ref.id for ref in page.messages]
            if page.next_page_token is None:
                return ids, cursor
            params["pageToken"] = page.next_page_token

    async def _newest_history_id(self, headers: dict[str, str]) -> str | None:
        newest = await self.client.get("messages", params={"maxResults": 1}, headers=headers)
        newest.raise_for_status()
        refs = _MessageList.model_validate_json(newest.content).messages
        if not refs:
            return None
        minimal = await self.client.get(
            f"messages/{refs[0].id}", params={"format": "minimal"}, headers=headers
        )
        minimal.raise_for_status()
        return _Minimal.model_validate_json(minimal.content).history_id

    async def _history(self, cursor: str, headers: dict[str, str]) -> tuple[list[str], str]:
        ids: list[str] = []
        params = {"startHistoryId": cursor, "historyTypes": "messageAdded", "labelId": INBOX}
        while True:
            response = await self.client.get("history", params=params, headers=headers)
            response.raise_for_status()
            page = _History.model_validate_json(response.content)
            ids += [added.message.id for record in page.history for added in record.messages_added]
            if page.next_page_token is None:
                return ids, page.history_id
            params["pageToken"] = page.next_page_token


def _secret(value: SecretStr | None) -> str:
    return value.get_secret_value() if value else ""
