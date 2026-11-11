import time

from anthropic import APIError, AsyncAnthropic
from pydantic import BaseModel

from homeagent.config.settings import LlmBackendCfg
from homeagent.llm.base import Completion, LlmBackend, Usage, register_llm_backend

MAX_TOKENS = 16000


class NoParsedOutputError(RuntimeError):
    pass


@register_llm_backend("anthropic")
class AnthropicBackend(LlmBackend):
    def __init__(self, name: str, cfg: LlmBackendCfg, api_key: str) -> None:
        self.name = name
        self.model = cfg.model
        self.cloud = cfg.cloud
        self.client = AsyncAnthropic(api_key=api_key, timeout=cfg.timeout_s)

    async def complete[T: BaseModel](
        self, prompt: str, schema: type[T], *, temperature: float, task_class: str
    ) -> Completion[T]:
        started = time.perf_counter()
        response = await self.client.messages.parse(
            model=self.model,
            max_tokens=MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
            output_format=schema,
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        value = response.parsed_output
        if value is None:
            raise NoParsedOutputError(f"{self.name}: stop_reason {response.stop_reason}")
        usage = Usage(
            prompt_tokens=response.usage.input_tokens,
            completion_tokens=response.usage.output_tokens,
            cache_read_tokens=response.usage.cache_read_input_tokens or 0,
            cache_write_tokens=response.usage.cache_creation_input_tokens or 0,
            latency_ms=latency_ms,
        )
        return Completion(value, usage, self.model, self.name)

    async def healthy(self) -> bool:
        try:
            await self.client.models.list()
        except APIError:
            return False
        return True
