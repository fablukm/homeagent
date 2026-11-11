import time

from ollama import AsyncClient
from pydantic import BaseModel

from homeagent.config.settings import LlmBackendCfg
from homeagent.llm.base import Completion, LlmBackend, Usage, register_llm_backend

NS_PER_MS = 1_000_000


@register_llm_backend("ollama")
class OllamaBackend(LlmBackend):
    def __init__(self, name: str, cfg: LlmBackendCfg, api_key: str) -> None:
        self.name = name
        self.model = cfg.model
        self.cloud = cfg.cloud
        self.timeout_s = cfg.timeout_s
        self.client = AsyncClient(host=cfg.url, timeout=cfg.timeout_s)

    async def complete[T: BaseModel](
        self, prompt: str, schema: type[T], *, temperature: float, task_class: str
    ) -> Completion[T]:
        started = time.perf_counter()
        response = await self.client.chat(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            format=schema.model_json_schema(),
            think=False,
            options={"temperature": temperature},
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        content = response.message.content or ""
        value = schema.model_validate_json(content)
        usage = Usage(
            prompt_tokens=response.prompt_eval_count or 0,
            completion_tokens=response.eval_count or 0,
            latency_ms=latency_ms,
            load_ms=(response.load_duration or 0) // NS_PER_MS,
            eval_ms=(response.eval_duration or 0) // NS_PER_MS,
        )
        return Completion(value, usage, self.model, self.name)

    async def healthy(self) -> bool:
        try:
            await self.client.list()
        except Exception:
            return False
        return True
