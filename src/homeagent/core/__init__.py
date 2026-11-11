FILES: dict[str, str] = {
    "registry.py": "named registries + register decorators for every ABC",
    "events.py": "Event model, the one shape every source emits",
    "rules.py": "Rule models, TOML rule store, RuleEngine",
    "syntax.py": "token parser for titles (!, ?, #tag, ~size)",
    "mode.py": "work/private/off from hours, overrides, manual",
    "scheduler.py": "thin APScheduler wrapper",
    "ingest.py": "Ingestor: poll, normalise, hash-dedupe, quarantine, commit, then cursor",
    "google_guard.py": "Google allow-list: reads, label add, creates; no delete/trash/edit",
    "dispatch.py": "Dispatcher: rule_runs for every rule, claim idempotency_key, then run actions",
    "triage.py": "mail triage: LLM proposes needs, one approval per need, decisions stored",
}
