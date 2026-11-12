import json
import time

import httpx
from pydantic import BaseModel

from homeagent.config.settings import LlmBackendCfg
from homeagent.llm.base import Completion, LlmBackend, Usage, register_llm_backend

JSON_ONLY = "Answer with the JSON object only. No explanation."


class _Message(BaseModel):
    content: str | None = None


class _Choice(BaseModel):
    message: _Message


class _Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0


class _ChatResponse(BaseModel):
    choices: list[_Choice]
    usage: _Usage = _Usage()


@register_llm_backend("openai")
class OpenAICompatBackend(LlmBackend):
    def __init__(self, name: str, cfg: LlmBackendCfg, api_key: str) -> None:
        self.name = name
        self.model = cfg.model
        self.cloud = cfg.cloud
        self.json_mode = cfg.json_mode
        self.sends_temperature = cfg.sends_temperature
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.client = httpx.AsyncClient(base_url=cfg.url, timeout=cfg.timeout_s, headers=headers)

    async def complete[T: BaseModel](
        self, prompt: str, schema: type[T], *, system: str, temperature: float, task_class: str
    ) -> Completion[T]:
        json_schema = schema.model_json_schema()
        if self.json_mode == "schema":
            json_rule = JSON_ONLY
            response_format: dict[str, object] = {
                "type": "json_schema",
                "json_schema": {"name": schema.__name__, "schema": json_schema},
            }
        else:
            json_rule = f"{JSON_ONLY} JSON schema: {json.dumps(json_schema)}"
            response_format = {"type": "json_object"}
        body: dict[str, object] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "\n\n".join(p for p in (system, json_rule) if p)},
                {"role": "user", "content": prompt},
            ],
            "response_format": response_format,
        }
        if self.sends_temperature:
            body["temperature"] = temperature
        started = time.perf_counter()
        response = await self.client.post("chat/completions", json=body)
        response.raise_for_status()
        latency_ms = int((time.perf_counter() - started) * 1000)
        chat = _ChatResponse.model_validate_json(response.content)
        value = schema.model_validate_json(chat.choices[0].message.content or "")
        usage = Usage(
            prompt_tokens=chat.usage.prompt_tokens,
            completion_tokens=chat.usage.completion_tokens,
            latency_ms=latency_ms,
        )
        return Completion(value, usage, self.model, self.name)

    async def healthy(self) -> bool:
        try:
            response = await self.client.get("models")
        except httpx.HTTPError:
            return False
        return response.is_success
