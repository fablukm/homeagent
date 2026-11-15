from datetime import date, timedelta

from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic.alias_generators import to_camel

from homeagent.actions.base import (
    Action,
    ActionContext,
    ActionResult,
    ActionStatus,
    register_action,
)
from homeagent.config.settings import GoogleCfg, Secrets
from homeagent.core.google_guard import google_client

LISTS = "users/@me/lists"
DUE_TIME = "T00:00:00.000Z"
CAMEL = ConfigDict(alias_generator=to_camel)


class _Token(BaseModel):
    access_token: str


class _TaskList(BaseModel):
    id: str
    title: str


class _TaskLists(BaseModel):
    model_config = CAMEL

    items: list[_TaskList] = Field(default_factory=list[_TaskList])
    next_page_token: str | None = None


class _Created(BaseModel):
    id: str


class TaskListMissingError(LookupError):
    pass


@register_action("task.create")
class GoogleTasks(Action):
    type = "task.create"

    def __init__(self, google: GoogleCfg, list_title: str, secrets: Secrets) -> None:
        self.google = google
        self.list_title = list_title
        self.secrets = secrets
        self.client = google_client(google.token_url, google.tasks_url, google.timeout_s)

    async def run(self, ctx: ActionContext) -> ActionResult:
        title = ctx.render(str(getattr(ctx.spec, "title", "")))
        offset = timedelta(days=int(str(getattr(ctx.spec, "due_offset_days", 0))))
        task_id = await self.create(title, "", ctx.event.occurred_at.date() + offset)
        return ActionResult(ActionStatus.TAKEN, external_ref=task_id, detail=title)

    async def create(self, title: str, notes: str, due: date | None) -> str:
        headers = {"Authorization": f"Bearer {await self.access_token()}"}
        list_id = await self.list_id(headers)
        body = {"title": title, "notes": notes}
        if due is not None:
            body["due"] = f"{due.isoformat()}{DUE_TIME}"
        response = await self.client.post(f"lists/{list_id}/tasks", json=body, headers=headers)
        response.raise_for_status()
        return _Created.model_validate_json(response.content).id

    async def list_id(self, headers: dict[str, str]) -> str:
        params: dict[str, str] = {}
        while True:
            response = await self.client.get(LISTS, params=params, headers=headers)
            response.raise_for_status()
            page = _TaskLists.model_validate_json(response.content)
            found = next((item.id for item in page.items if item.title == self.list_title), None)
            if found is not None:
                return found
            if page.next_page_token is None:
                raise TaskListMissingError(f"no Google Tasks list named {self.list_title!r}")
            params["pageToken"] = page.next_page_token

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


def _secret(value: SecretStr | None) -> str:
    return value.get_secret_value() if value else ""
