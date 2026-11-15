import json
from functools import partial
from typing import Literal

import httpx
import httpx2
import pytest
from anthropic import AsyncAnthropic
from ollama import AsyncClient
from pydantic import BaseModel

from homeagent.config.settings import LlmBackendCfg, Secrets, Settings
from homeagent.llm.anthropic import AnthropicBackend
from homeagent.llm.base import Question
from homeagent.llm.laya import LayaBackend
from homeagent.llm.ollama import OllamaBackend
from homeagent.llm.openai_compat import JSON_ONLY, OpenAICompatBackend
from homeagent.llm.router import CloudBackendError, LlmRouter, NoBackendError

BASE_URL = "http://llm.test/v1"
OLLAMA_URL = "http://ollama.test:11434"


class Label(BaseModel):
    label: Literal["SBB", "Kita"]


def _openai_backend(transport: httpx.MockTransport, **cfg: object) -> OpenAICompatBackend:
    backend_cfg = LlmBackendCfg.model_validate(
        {"type": "openai", "url": BASE_URL, "model": "m", "timeout_s": 5, **cfg}
    )
    backend = OpenAICompatBackend("test", backend_cfg, "key-1")
    backend.client = httpx.AsyncClient(
        base_url=BASE_URL, headers=backend.client.headers, transport=transport
    )
    return backend


def _chat_reply(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 3},
        },
    )


async def test_openai_compat_sends_schema_and_parses_reply() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _chat_reply('{"label": "SBB"}')

    backend = _openai_backend(httpx.MockTransport(handle))
    result = await backend.complete(
        "ticket", Label, system="", temperature=0.0, task_class="classify"
    )

    assert result.value == Label(label="SBB")
    assert result.usage.prompt_tokens == 11
    assert result.usage.completion_tokens == 3
    request = seen[0]
    assert request.url == f"{BASE_URL}/chat/completions"
    assert request.headers["Authorization"] == "Bearer key-1"
    body = json.loads(request.content)
    assert body["messages"][0] == {"role": "system", "content": JSON_ONLY}
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["schema"] == Label.model_json_schema()
    assert body["temperature"] == 0.0


async def test_openai_compat_object_mode_puts_schema_in_system_message() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _chat_reply('{"label": "Kita"}')

    backend = _openai_backend(
        httpx.MockTransport(handle), json_mode="object", sends_temperature=False
    )
    result = await backend.complete(
        "invoice", Label, system="Sort mail.", temperature=0.2, task_class="classify"
    )

    assert result.value == Label(label="Kita")
    body = json.loads(seen[0].content)
    assert body["response_format"] == {"type": "json_object"}
    assert json.dumps(Label.model_json_schema()) in body["messages"][0]["content"]
    assert body["messages"][0]["content"].startswith("Sort mail.\n\n")
    assert body["messages"][1] == {"role": "user", "content": "invoice"}
    assert "temperature" not in body


async def test_openai_compat_health_follows_models_endpoint() -> None:
    def ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    def unauthorized(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    healthy = _openai_backend(httpx.MockTransport(ok))
    rejected = _openai_backend(httpx.MockTransport(unauthorized))

    assert await healthy.healthy()
    assert not await rejected.healthy()


async def test_anthropic_backend_parses_structured_output_without_temperature() -> None:
    seen: list[httpx2.Request] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-haiku-5-5",
                "content": [{"type": "text", "text": '{"label": "SBB"}'}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 20, "output_tokens": 4},
            },
        )

    cfg = LlmBackendCfg(type="anthropic", model="claude-haiku-5-5", timeout_s=5, cloud=True)
    backend = AnthropicBackend("anthropic", cfg, "key-2")
    backend.client = AsyncAnthropic(
        api_key="key-2",
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handle)),
    )
    result = await backend.complete(
        "ticket", Label, system="Sort mail.", temperature=0.2, task_class="answer"
    )

    assert result.value == Label(label="SBB")
    assert result.usage.prompt_tokens == 20
    body = json.loads(seen[0].content)
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["system"] == "Sort mail."
    assert "temperature" not in body


def test_secrets_map_each_provider_to_its_key() -> None:
    secrets = Secrets.model_validate({"deepseek_api_key": "d", "alibaba_api_key": "a"})

    assert secrets.llm_key("deepseek") == "d"
    assert secrets.llm_key("alibaba") == "a"
    assert secrets.llm_key("openai") == ""
    assert secrets.llm_key(None) == ""


def test_router_builds_every_configured_backend(settings: Settings) -> None:
    cloud_allowed = settings.llm.model_copy(update={"local_only": False})
    router = LlmRouter(cloud_allowed, Secrets.model_validate({}))

    expected = {name for name, cfg in settings.llm.backends.items() if cfg.type != "laya"}
    assert set(router.backends) == expected
    assert isinstance(router.backends["pi"], OllamaBackend)
    assert isinstance(router.backends["zotac"], OpenAICompatBackend)
    assert isinstance(router.backends["anthropic"], AnthropicBackend)
    cloud = {name for name, backend in router.backends.items() if backend.cloud}
    assert not cloud & {backend.name for backend in router.candidates("classify")}


async def test_ollama_turns_thinking_off_and_sends_the_schema() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "model": "m",
                "message": {"role": "assistant", "content": '{"label": "Kita"}'},
                "done": True,
                "prompt_eval_count": 9,
                "eval_count": 4,
            },
        )

    cfg = LlmBackendCfg.model_validate(
        {"type": "ollama", "url": OLLAMA_URL, "model": "m", "timeout_s": 5}
    )
    backend = OllamaBackend("pi", cfg, "")
    backend.client = AsyncClient(host=OLLAMA_URL, transport=httpx.MockTransport(handle))

    result = await backend.complete(
        "kita", Label, system="Sort mail.", temperature=0.0, task_class="classify"
    )

    assert result.value == Label(label="Kita")
    assert result.usage.prompt_tokens == 9
    body = json.loads(seen[0].content)
    assert body["think"] is False
    assert body["messages"] == [
        {"role": "system", "content": "Sort mail."},
        {"role": "user", "content": "kita"},
    ]
    assert body["format"] == Label.model_json_schema()


LAYA_URL = "http://laya.test:8080"
LAYA_REPLY = {
    "model": "laya-rl-agent",
    "answers": {
        "action": {
            "type": "choice",
            "choice": "none",
            "probabilities": {"none": 0.8418, "pay": 0.1582},
            "confidence": 0.6169,
            "answer_confidence": 0.8418,
            "action": {"act_probability": 1.0},
            "x_jev_confidence": 0.8102,
        }
    },
    "usage": {"input_tokens": 886, "output_tokens": 0, "truncated": False},
    "routing": {"model": "triage", "reason": "explicit model='triage'"},
}


def _laya(transport: httpx.MockTransport) -> LayaBackend:
    cfg = LlmBackendCfg.model_validate(
        {"type": "laya", "url": LAYA_URL, "model": "triage", "timeout_s": 5}
    )
    backend = LayaBackend("laya", cfg)
    backend.client = httpx.AsyncClient(base_url=LAYA_URL, transport=transport)
    return backend


async def test_laya_names_its_model_and_reads_the_top_probability() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=LAYA_REPLY)

    question = Question("action", "choice", "Which action?", {"none": "Nothing.", "pay": "Pay."})
    state = {"from": "A <a@example.ch>", "subject": "Hi", "attachments": "", "body": "Text"}

    decision = await _laya(httpx.MockTransport(handle)).decide(state, [question])

    [request] = seen
    assert request.url.path == "/v1/systemone"
    assert json.loads(request.content) == {
        "model": "triage",
        "state": state,
        "questions": {
            "action": {
                "type": "choice",
                "instructions": "Which action?",
                "criteria": {"none": "Nothing.", "pay": "Pay."},
            }
        },
    }
    [answer] = decision.answers
    assert answer.question_id == "action"
    assert answer.value == "none"
    assert answer.confidence == 0.8418
    assert answer.probabilities == {"none": 0.8418, "pay": 0.1582}
    assert decision.usage.prompt_tokens == 886
    assert decision.model == "triage"
    assert decision.backend == "laya"


async def test_laya_failure_is_no_backend() -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "inference failed"})

    question = Question("action", "choice", "Which action?", {"none": "Nothing."})

    with pytest.raises(NoBackendError):
        await _laya(httpx.MockTransport(broken)).decide({"body": "x"}, [question])


async def test_laya_health_follows_health_endpoint() -> None:
    def ok(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/health"
        return httpx.Response(200, json={"status": "ok", "loaded": ["triage"]})

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    assert await _laya(httpx.MockTransport(ok)).healthy()
    assert not await _laya(httpx.MockTransport(unreachable)).healthy()


async def test_local_only_never_builds_or_pings_a_cloud_backend(settings: Settings) -> None:
    keys = {f"{provider}_api_key": "k" for provider in ("anthropic", "openai", "deepseek")}
    router = LlmRouter(settings.llm, Secrets.model_validate(keys))
    pinged: list[str] = []

    async def healthy(name: str) -> bool:
        pinged.append(name)
        return True

    for name, backend in router.backends.items():
        object.__setattr__(backend, "healthy", partial(healthy, name))
    await router.refresh_health()

    cloud = {name for name, cfg in settings.llm.backends.items() if cfg.cloud}
    assert settings.llm.local_only
    assert {"anthropic", "openai", "deepseek", "moonshot", "alibaba"} <= cloud
    assert not cloud & set(router.backends)
    assert not cloud & set(pinged)
    assert not any(backend.cloud for backend in router.backends.values())
    assert set(pinged) == set(router.backends)


def test_local_only_refuses_a_task_routed_to_the_cloud(settings: Settings) -> None:
    answer = settings.llm.classes["answer"]
    routed = answer.model_copy(update={"backends": [*answer.backends, "anthropic"]})
    classes = {**settings.llm.classes, "answer": routed}
    cfg = settings.llm.model_copy(update={"classes": classes})

    with pytest.raises(CloudBackendError, match="anthropic"):
        LlmRouter(cfg, Secrets.model_validate({"anthropic_api_key": "k"}))
