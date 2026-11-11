import re

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel

READ_METHODS = frozenset({"GET"})
GMAIL_MODIFY = re.compile(r"/gmail/v1/users/me/messages/[^/]+/modify$")
ALLOWED_POSTS = (
    GMAIL_MODIFY,
    re.compile(r"/gmail/v1/users/me/labels$"),
    re.compile(r"/tasks/v1/lists/[^/]+/tasks$"),
    re.compile(r"/calendar/v3/calendars/[^/]+/events$"),
)
LOSSY_LABELS = frozenset({"TRASH", "SPAM"})


class UnsafeGoogleRequestError(PermissionError):
    pass


class _Modify(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, extra="forbid")

    add_label_ids: list[str] = Field(default_factory=list[str])
    remove_label_ids: list[str] = Field(default_factory=list[str])


def ensure_safe(request: httpx.Request, token_url: str) -> None:
    if request.method in READ_METHODS:
        return
    if request.method == "POST" and str(request.url.copy_with(query=None)) == token_url:
        return
    path = request.url.path
    if request.method != "POST" or not any(p.search(path) for p in ALLOWED_POSTS):
        raise UnsafeGoogleRequestError(f"blocked {request.method} {path}")
    if GMAIL_MODIFY.search(path):
        try:
            body = _Modify.model_validate_json(request.content or b"{}")
        except ValidationError as exc:
            raise UnsafeGoogleRequestError(f"blocked label change body: {exc}") from exc
        if body.remove_label_ids or LOSSY_LABELS & set(body.add_label_ids):
            raise UnsafeGoogleRequestError(f"blocked label change {body.model_dump()}")


class GoogleGuard(httpx.AsyncBaseTransport):
    def __init__(self, token_url: str, inner: httpx.AsyncBaseTransport) -> None:
        self.token_url = token_url
        self.inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        ensure_safe(request, self.token_url)
        return await self.inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self.inner.aclose()


def google_client(
    token_url: str,
    base_url: str,
    timeout_s: int,
    inner: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    transport = GoogleGuard(token_url, inner or httpx.AsyncHTTPTransport())
    return httpx.AsyncClient(base_url=base_url, timeout=timeout_s, transport=transport)
