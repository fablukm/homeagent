import time

import httpx
from pydantic import BaseModel

from homeagent.config.settings import LlmBackendCfg
from homeagent.llm.base import (
    Answer,
    Decision,
    DecisionBackend,
    Question,
    Usage,
    register_decision_backend,
)
from homeagent.llm.router import NoBackendError

DECIDE_PATH = "v1/systemone"
HEALTH_PATH = "health"


class _Choice(BaseModel):
    choice: str
    probabilities: dict[str, float]
    answer_confidence: float


class _Usage(BaseModel):
    input_tokens: int = 0


class _Response(BaseModel):
    answers: dict[str, _Choice]
    usage: _Usage = _Usage()


@register_decision_backend("laya")
class LayaBackend(DecisionBackend):
    def __init__(self, name: str, cfg: LlmBackendCfg) -> None:
        self.name = name
        self.model = cfg.model
        self.client = httpx.AsyncClient(base_url=cfg.url, timeout=cfg.timeout_s)

    async def decide(self, state: dict[str, str], questions: list[Question]) -> Decision:
        body = {
            "model": self.model,
            "state": state,
            "questions": {
                q.id: {"type": q.type, "instructions": q.instructions, "criteria": q.criteria}
                for q in questions
            },
        }
        started = time.perf_counter()
        try:
            response = await self.client.post(DECIDE_PATH, json=body)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise NoBackendError(f"{self.name} did not decide") from exc
        latency_ms = int((time.perf_counter() - started) * 1000)
        parsed = _Response.model_validate_json(response.content)
        answers = [
            Answer(qid, a.choice, a.answer_confidence, a.probabilities)
            for qid, a in parsed.answers.items()
        ]
        usage = Usage(prompt_tokens=parsed.usage.input_tokens, latency_ms=latency_ms)
        return Decision(answers, usage, self.model, self.name)

    async def healthy(self) -> bool:
        try:
            response = await self.client.get(HEALTH_PATH)
        except httpx.HTTPError:
            return False
        return response.is_success
