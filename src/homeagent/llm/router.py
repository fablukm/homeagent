from pydantic import BaseModel

from homeagent.config.settings import Llm, Secrets
from homeagent.llm.base import LLM_BACKENDS, Completion, LlmBackend


class NoBackendError(RuntimeError):
    pass


class LlmRouter:
    def __init__(self, cfg: Llm, secrets: Secrets) -> None:
        self.cfg = cfg
        self.backends: dict[str, LlmBackend] = {}
        for name, backend_cfg in cfg.backends.items():
            if backend_cfg.type != "laya":
                backend_cls = LLM_BACKENDS.get(backend_cfg.type)
                api_key = secrets.llm_key(backend_cfg.secret)
                self.backends[name] = backend_cls(name, backend_cfg, api_key)
        self._healthy: dict[str, bool] = dict.fromkeys(self.backends, True)

    async def refresh_health(self) -> None:
        for name, backend in self.backends.items():
            self._healthy[name] = await backend.healthy()

    def candidates(self, task_class: str) -> list[LlmBackend]:
        route = self.cfg.classes[task_class]
        chosen: list[LlmBackend] = []
        for name in route.backends:
            backend = self.backends[name]
            if backend.cloud and not route.allow_cloud:
                continue
            if self._healthy.get(name, False):
                chosen.append(backend)
        return chosen

    async def complete[T: BaseModel](
        self, task_class: str, prompt: str, schema: type[T], *, system: str
    ) -> Completion[T]:
        route = self.cfg.classes[task_class]
        last: Exception | None = None
        for backend in self.candidates(task_class):
            try:
                return await backend.complete(
                    prompt,
                    schema,
                    system=system,
                    temperature=route.temperature,
                    task_class=task_class,
                )
            except Exception as exc:
                last = exc
                self._healthy[backend.name] = False
        raise NoBackendError(f"no backend answered for {task_class!r}") from last
