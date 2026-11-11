FILES: dict[str, str] = {
    "anthropic.py": "Anthropic backend with Pydantic structured output",
    "base.py": "LlmBackend and DecisionBackend ABCs",
    "ollama.py": "Ollama backend with schema-constrained output",
    "openai_compat.py": "OpenAI-style chat backend: llama.cpp, OpenAI, DeepSeek, Moonshot, Alibaba",
    "router.py": "task class -> ordered backends from llm.toml",
}
