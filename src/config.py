from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bearer_token: str = "change-me"
    anthropic_api_key: str = ""
    database_url: str = "postgresql://mem0:mem0@localhost:5432/mem0"
    mem0_user_id: str = "default"
    mem0_agent_id: str = "default"   # fallback when no X-Agent-ID header is sent
    mem0_infer: bool = True           # enable LLM extraction and deduplication
    app_port: int = 8080
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:7b"
    ollama_embed_model: str = "nomic-embed-text"
    ollama_timeout: float = 300.0  # seconds per Ollama request; mem0's default is unbounded
    redis_url: str = ""  # optional: durable add_memory queue (otherwise in-process)
    write_queue_max: int = 1000  # in-process queue bound; add_memory rejects when full
    metrics_cache_seconds: float = 60.0  # /metrics + /api/stats full-scan cache

    # Session working memory (checkpoints) + hook endpoints
    checkpoint_max_chars: int = 6000     # reject larger checkpoints so the model condenses
    auto_notes_max_chars: int = 3000     # pre-compaction auto-capture is truncated to this
    briefing_max_chars: int = 9000       # cap for /api/hooks/briefing output (~2.2k tokens)
    recall_min_score: float = 0.6        # cosine similarity (mem0 pgvector: 1 - distance)
    recall_limit: int = 3                # memories injected per prompt by /api/hooks/recall
    recall_min_prompt_chars: int = 12    # skip recall for "yes", "1", "go on" ...
    hook_hit_max_chars: int = 400        # per-memory text cap in hook output (recall + briefing)
    hook_search_timeout: float = 8.0     # seconds; hooks must never stall the harness
    get_all_default_limit: int = 50      # MCP get_all_memories default page size

    # add_memory extraction. mem0's built-in prompt looks for personal facts
    # about "the user"; technical notes ("Backups run at 3:00") came back with
    # no facts and were dropped silently. These instructions are appended to
    # mem0's extraction prompt; set MEM0_CUSTOM_INSTRUCTIONS="" to disable.
    mem0_custom_instructions: str = (
        "This memory store serves AI coding agents working on a homelab and its software projects. "
        "Every input is a note an agent was explicitly asked to keep, so it always contains something memorable. "
        "Extract every concrete fact: infrastructure, hosts, clusters, services, configuration, versions, "
        "schedules and times, decisions with their reasons, failed approaches (dead ends), incidents and fixes, "
        "conventions and preferences. Keep names, paths, numbers and versions exactly as written. "
        "Write each memory as a self-contained statement about the subject itself "
        "(e.g. 'Backups run daily at 03:00'), not as 'User has ...' unless it is about the user personally. "
        "Never return an empty list: if nothing else fits, return the note itself, condensed, as one memory."
    )
    # If extraction still yields nothing, store the note as written (infer=False)
    # instead of dropping it. Duplicates are merged by the nightly dedup job.
    add_raw_fallback: bool = True


settings = Settings()
