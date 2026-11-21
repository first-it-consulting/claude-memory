from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    bearer_token: str = ""
    anthropic_api_key: str = ""
    database_url: str = "postgresql://mem0:mem0@localhost:5432/mem0"
    mem0_user_id: str = "default"
    mem0_agent_id: str = "default"  # fallback when no X-Agent-ID header is sent
    mem0_infer: bool = True  # enable LLM extraction and deduplication
    app_port: int = 8080
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:7b"
    ollama_embed_model: str = "nomic-embed-text"
    # Provider switch per role (bean homelab-saec): "ollama" (default, the
    # OLLAMA_* settings above) or "openai" = any OpenAI-compatible endpoint
    # (LiteLLM, vLLM, TEI, Ollama's own /v1, ...). *_API_KEY_FILE wins over
    # *_API_KEY (secret mounted as a file); keys are never logged.
    embed_provider: str = "ollama"
    embed_base_url: str = ""  # openai provider, e.g. https://litellm.../v1
    embed_model: str = ""  # required when embed_provider=openai
    embed_api_key: str = ""
    embed_api_key_file: str = ""
    llm_provider: str = "ollama"
    llm_base_url: str = ""
    llm_model: str = ""  # required when llm_provider=openai
    llm_api_key: str = ""
    llm_api_key_file: str = ""
    ollama_timeout: float = (
        300.0  # seconds per Ollama request; mem0's default is unbounded
    )
    redis_url: str = ""  # optional: durable add_memory queue (otherwise in-process)
    write_queue_max: int = 1000  # in-process queue bound; add_memory rejects when full
    metrics_cache_seconds: float = 60.0  # /metrics + /api/stats full-scan cache

    # Session working memory (checkpoints) + hook endpoints
    checkpoint_max_chars: int = 6000  # reject larger checkpoints so the model condenses
    auto_notes_max_chars: int = 3000  # pre-compaction auto-capture is truncated to this
    briefing_max_chars: int = 9000  # cap for /api/hooks/briefing output (~2.2k tokens)
    # 0 = off (classic full briefing). >0: SessionStart briefing becomes a
    # compact index (checkpoint + one short line per memory, id included)
    # capped at this MANY ESTIMATED TOKENS (chars/4 heuristic).
    briefing_token_budget: int = 0
    recall_min_score: float = 0.6  # cosine similarity (mem0 pgvector: 1 - distance)
    promoted_recall_penalty: float = (
        0.05  # auto-promoted checkpoint lines rank below deliberate memories
    )
    recall_limit: int = 3  # memories injected per prompt by /api/hooks/recall
    recall_min_prompt_chars: int = 12  # skip recall for "yes", "1", "go on" ...
    hook_hit_max_chars: int = (
        400  # per-memory text cap in hook output (recall + briefing)
    )
    hook_search_timeout: float = 8.0  # seconds; hooks must never stall the harness
    get_all_default_limit: int = 50  # MCP get_all_memories default page size

    # Used only when add_memory is called with infer=True (the MCP default is
    # infer=False: notes are stored as written). Tested 2026-11-15 with
    # llama3.2: extraction dropped notes, turned "outdated 7.7 -> 10.7" into
    # "User upgraded to 10.7", and re-extracted earlier notes from mem0's
    # "Last k Messages" context. mem0's built-in prompt looks for personal facts
    # about "the user"; technical notes ("Backups run at 3:00") came back with
    # no facts and were dropped silently. These instructions are appended to
    # mem0's extraction prompt; set MEM0_CUSTOM_INSTRUCTIONS="" to disable.
    mem0_custom_instructions: str = (
        "This memory store serves AI coding agents working on a homelab and its software projects. "
        "Every input is a note an agent was explicitly asked to keep, so it always contains something memorable. "
        "Extract every concrete fact: infrastructure, hosts, clusters, services, configuration, versions, "
        "schedules and times, decisions with their reasons, failed approaches (dead ends), incidents and fixes, "
        "conventions and preferences. Keep names, paths, numbers and versions exactly as written. "
        "Extract ONLY from the New Messages; earlier messages are context, never extract them again. "
        "Write each memory as a self-contained statement about the subject itself, "
        "not as 'User has ...' unless it is about the user personally. "
        "Never return an empty list: if nothing else fits, return the new note itself, condensed, as one memory."
    )
    # If extraction still yields nothing, store the note as written (infer=False)
    # instead of dropping it. Duplicates are merged by the nightly dedup job.
    add_raw_fallback: bool = True

    # --- Multi-tenancy -----------------------------------------------------
    # A tenant == a workspace == the mem0 user_id namespace. Every user has one
    # tenant (users.id); the nightly job, hooks and the API always operate
    # inside the caller's tenant. See auth.py / users.py.
    shared_tenant_id: str = "global"  # pseudo-tenant: memories visible to everyone
    admin_token: str = ""  # bootstrap admin API token (role admin, tenant "default")

    # Keycloak OIDC — browser (UI) login. Enabled when oidc_issuer is set.
    # The UI sends a Keycloak access token as Authorization header; the server
    # verifies it (JWKS, iss, aud, exp) and maps it to a users row by `sub`.
    oidc_issuer: str = ""
    oidc_client_id: str = "claude-memory-ui"
    oidc_audience: str = ""  # empty -> oidc_client_id
    oidc_admin_roles: str = "claude-memory-admin"  # comma-separated realm/client roles
    oidc_admin_usernames: str = ""  # comma-separated preferred_username fallback
    oidc_auto_create: bool = True  # first OIDC login creates a role=user account


settings = Settings()
