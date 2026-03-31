# MUSE — Comprehensive Test Plan

## Overview

This document defines the full testing strategy for MUSE, covering unit tests, integration tests, API tests, skill tests, frontend tests, and end-to-end scenarios. Tests are organized into six phases, each building on the previous.

**Conventions:**
- Test framework: `pytest` + `pytest-asyncio` (backend), `vitest` + `@testing-library/react` (frontend)
- Mocking: `unittest.mock` / `pytest-mock` (backend), `vi.mock` (frontend)
- Fixtures: shared in `tests/conftest.py`
- Naming: `test_<module>_<behavior>` for files, `test_<method>_<scenario>` for functions

---

## Shared Fixtures (`tests/conftest.py`)

| Fixture | Scope | Description |
|---------|-------|-------------|
| `tmp_data_dir` | function | Temporary directory as `Config.data_dir` |
| `config` | function | `Config` with `tmp_data_dir`, debug=False |
| `agent_db` | function | In-memory aiosqlite connection, schema initialized |
| `wal_db` | function | In-memory aiosqlite WAL connection |
| `memory_repo` | function | `MemoryRepository` backed by `agent_db` |
| `memory_cache` | function | `MemoryCache` with 1 MB budget |
| `embedding_service` | function | `EmbeddingService` (real model or stub) |
| `permission_manager` | function | `PermissionManager` with `agent_db` |
| `trust_budget` | function | `TrustBudgetManager` with `agent_db` |
| `skill_loader` | function | `SkillLoader` with `tmp_data_dir` |
| `mock_provider` | function | Fake `ProviderService` returning canned completions |
| `orchestrator` | function | Fully wired `Orchestrator` with all mocked deps |
| `app_client` | function | `httpx.AsyncClient` wrapping the FastAPI test app |
| `ws_client` | function | WebSocket test client with auth token |

---

## Phase 1: Unit Tests

### 1.1 Config (`tests/unit/test_config.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_default_data_dir_platform` | `data_dir` resolves correctly per platform (LOCALAPPDATA on Windows, ~/Library on macOS, XDG on Linux) |
| 2 | `test_ensure_dirs_creates_subdirs` | `ensure_dirs()` creates skills/, logs/, ipc/ under data_dir |
| 3 | `test_config_frozen` | Assigning to a frozen field raises `FrozenInstanceError` |
| 4 | `test_memory_config_defaults` | Default values: cache_budget_mb=50, embedding_model="all-MiniLM-L6-v2", dimensions=384 |
| 5 | `test_builtin_providers_complete` | `BUILTIN_PROVIDERS` contains all 8 entries (OpenAI, Anthropic, Gemini, etc.) |
| 6 | `test_provider_def_api_style` | Each `ProviderDef` has api_style in {"openai", "anthropic"} |
| 7 | `test_db_path_under_data_dir` | `config.db_path` is `data_dir / agent.db` |
| 8 | `test_execution_config_limits` | `max_concurrent_tasks` and `subtask_depth_limit` are positive integers |

### 1.2 Intent Classifier (`tests/unit/test_intent_classifier.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_greeting_fast_path` | "hello", "hi", "hey" match `INLINE_RE` and return INLINE mode without LLM call |
| 2 | `test_meta_question_fast_path` | "what can you do?", "help" match fast-path |
| 3 | `test_single_skill_delegation` | LLM returns DELEGATED mode with correct skill_id for "search for cats" |
| 4 | `test_multi_task_decomposition` | "search X and save to notes" produces MULTI_DELEGATED with 2 sub-tasks and dependency DAG |
| 5 | `test_sub_task_dependency_order` | Sub-task depending on another has correct `depends_on` index |
| 6 | `test_unknown_intent_falls_inline` | Ambiguous input returns INLINE with low confidence |
| 7 | `test_confidence_threshold` | Confidence below `HIGH_CONFIDENCE` (0.55) triggers inline fallback |
| 8 | `test_model_override_in_intent` | Intent can specify `model_override` for specialized tasks |
| 9 | `test_register_skill_updates_catalog` | `register_skill()` adds skill to classifier's known set |
| 10 | `test_classify_with_no_skills` | Classifier with empty skill catalog returns INLINE |

### 1.3 Task Manager (`tests/unit/test_task_manager.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_spawn_creates_task` | `spawn()` returns `TaskInfo` with status "pending" and unique ID |
| 2 | `test_spawn_respects_concurrency_limit` | Spawning beyond `max_concurrent_tasks` raises or queues |
| 3 | `test_update_status_persists` | `update_status("running")` reflected in DB and `get_task()` |
| 4 | `test_completion_callback_fires` | Registered callback invoked when task status → "completed" |
| 5 | `test_token_accumulation` | `accumulate_tokens(100, 50)` updates in-memory counters |
| 6 | `test_task_has_parent_reference` | Subtask's `parent_task_id` points to parent |
| 7 | `test_isolation_tier_assignment` | Task inherits isolation tier from skill manifest |
| 8 | `test_get_task_not_found` | `get_task("nonexistent")` returns None |

### 1.4 Context Assembly (`tests/unit/test_context_assembly.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_load_identity_from_file` | `load_identity()` reads identity.md when present |
| 2 | `test_load_identity_fallback` | Returns default system instructions when identity.md missing |
| 3 | `test_to_messages_format` | `AssembledContext.to_messages()` returns OpenAI-compatible list[dict] |
| 4 | `test_system_instructions_first` | System message is first in output |
| 5 | `test_conversation_turns_order` | Turns appear in chronological order |
| 6 | `test_token_count_tracking` | Token counts reflect assembled content lengths |

### 1.5 Memory Repository (`tests/unit/test_memory_repository.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_write_and_read` | Write entry, read it back — value matches |
| 2 | `test_write_upsert` | Writing same (namespace, key) updates value |
| 3 | `test_list_keys` | Lists all keys in namespace; prefix filter works |
| 4 | `test_delete_marks_deleted` | Delete sets deleted flag, `get()` returns None |
| 5 | `test_supersede` | Superseded entry has `superseded_by` set |
| 6 | `test_get_by_relevance` | Returns entries sorted by relevance_score DESC |
| 7 | `test_search_similar_vector` | Vector similarity search returns nearest neighbors |
| 8 | `test_search_similar_empty_db` | Returns empty list on empty table |
| 9 | `test_namespace_isolation` | Entries in namespace A invisible when querying namespace B |
| 10 | `test_access_count_incremented` | Each `get()` call increments access_count |

### 1.6 Memory Cache (`tests/unit/test_memory_cache.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_put_and_get` | Basic cache write/read cycle |
| 2 | `test_eviction_under_budget` | Inserting entries beyond budget evicts least-recently-used |
| 3 | `test_eviction_prefers_low_relevance` | Among same-age entries, lower relevance evicted first |
| 4 | `test_namespace_separation` | Same key in different namespaces stored independently |
| 5 | `test_dirty_flag_on_put` | New puts are marked dirty for flush |
| 6 | `test_size_tracking` | `cache.size` reflects actual stored bytes |
| 7 | `test_get_updates_access_time` | `get()` updates entry's last-access timestamp |

### 1.7 Embedding Service (`tests/unit/test_embeddings.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_embed_returns_vector` | `embed("hello")` returns list[float] of correct dimension (384) |
| 2 | `test_embed_deterministic` | Same text produces same vector |
| 3 | `test_embed_batch` | Batch embedding returns correct number of vectors |
| 4 | `test_similar_texts_close` | Cosine similarity of "cat" and "kitten" > 0.7 |
| 5 | `test_dissimilar_texts_far` | Cosine similarity of "cat" and "database" < 0.3 |

### 1.8 Permission Manager (`tests/unit/test_permission_manager.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_check_no_grant_requires_approval` | Ungrated permission returns `requires_user_approval=True` |
| 2 | `test_grant_always_persists` | `grant(approval_mode="always")` persists across sessions |
| 3 | `test_grant_session_scoped` | Session-scoped grant expires after session change |
| 4 | `test_grant_once_consumed` | Once-grant consumed after single use |
| 5 | `test_revoke_permission` | Revoked grant no longer allows access |
| 6 | `test_risk_tier_critical` | `*:delete`, `skill:install` classified as "critical" |
| 7 | `test_risk_tier_high` | `*:send`, `*:execute` classified as "high" |
| 8 | `test_risk_tier_medium` | `*:write` classified as "medium" |
| 9 | `test_risk_tier_low` | `*:read` and unknown patterns classified as "low" |
| 10 | `test_first_party_skill_auto_grant` | First-party skills with low-risk permissions auto-approved |

### 1.9 Trust Budget (`tests/unit/test_trust_budget.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_check_budget_within_limits` | Returns allowed when under limits |
| 2 | `test_check_budget_exceeded_actions` | Returns denied when action count exceeded |
| 3 | `test_check_budget_exceeded_tokens` | Returns denied when token count exceeded |
| 4 | `test_consume_budget_decrements` | `consume_budget()` decrements remaining allowance |
| 5 | `test_budget_period_reset` | Budget resets after period elapses |

### 1.10 Skill Manifest (`tests/unit/test_skill_manifest.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_from_json_valid` | Parse valid manifest.json into `SkillManifest` |
| 2 | `test_from_json_missing_required` | Missing `name` or `version` raises validation error |
| 3 | `test_to_dict_round_trip` | `from_json(to_json(manifest))` equals original |
| 4 | `test_actions_parsed` | Actions list contains correct `ActionSpec` entries |
| 5 | `test_credentials_parsed` | Credential specs include type, label, required flag |
| 6 | `test_allowed_domains_list` | `allowed_domains` correctly parsed as list[str] |
| 7 | `test_isolation_tier_values` | Only "lightweight", "standard", "hardened" accepted |
| 8 | `test_default_timeout` | Default `timeout_seconds` is reasonable (e.g., 30) |

### 1.11 Skill Sandbox (`tests/unit/test_skill_sandbox.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_skill_id_validation` | Invalid characters in skill_id rejected |
| 2 | `test_lightweight_runs_in_process` | Lightweight-tier skill invokes `LocalBridge` |
| 3 | `test_standard_spawns_subprocess` | Standard-tier skill launches subprocess |
| 4 | `test_kill_terminates_process` | `kill(task_id)` terminates running subprocess |
| 5 | `test_execute_returns_result` | Successful execution returns `SkillResult` dict |
| 6 | `test_execute_timeout` | Skill exceeding timeout is killed and error reported |

### 1.12 Skill Loader (`tests/unit/test_skill_loader.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_install_skill` | `install()` copies files and records in DB |
| 2 | `test_uninstall_skill` | `uninstall()` removes files and DB entry |
| 3 | `test_get_installed_lists_all` | Returns all installed skills with manifests |
| 4 | `test_load_first_party_skills` | Loads all skills from source `/skills` directory |
| 5 | `test_stale_skill_cleanup` | Skill whose source dir was removed gets uninstalled |
| 6 | `test_update_skill_replaces` | `update_skill()` updates files and manifest in DB |

### 1.13 Gateway Proxy (`tests/unit/test_gateway_proxy.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_allowed_domain_passes` | Request to whitelisted domain succeeds |
| 2 | `test_blocked_domain_rejected` | Request to non-whitelisted domain returns 403 |
| 3 | `test_private_ip_blocked` | Requests to 127.0.0.1, 10.x.x.x, 192.168.x.x blocked |
| 4 | `test_credential_injection` | Registered domain gets auth header injected |
| 5 | `test_rate_limit_enforcement` | Exceeding RPM returns 429 |
| 6 | `test_audit_log_recorded` | Each proxied request creates audit entry |
| 7 | `test_register_skill_domains` | `register_skill_domains()` adds domains to allowlist |

### 1.14 Rate Limiter (`tests/unit/test_rate_limiter.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_under_limit_allowed` | Requests below limit pass |
| 2 | `test_over_limit_rejected` | Requests above limit rejected |
| 3 | `test_tokens_replenish` | After waiting, tokens refill (token bucket) |
| 4 | `test_per_domain_limits` | Domain-specific limits enforced independently |

### 1.15 Write-Ahead Log (`tests/unit/test_wal.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_write_appends_entry` | `write()` creates uncommitted entry |
| 2 | `test_commit_marks_committed` | `commit()` marks entry as committed |
| 3 | `test_get_uncommitted` | Returns only uncommitted entries |
| 4 | `test_operation_types` | All operation types (task_spawn, memory_write, etc.) accepted |
| 5 | `test_recovery_replays_uncommitted` | Uncommitted entries available after DB reopen |

### 1.16 Audit Repository (`tests/unit/test_audit_repository.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_log_creates_entry` | `log()` inserts audit record |
| 2 | `test_query_by_skill` | Filter by skill_id returns matching entries |
| 3 | `test_query_by_time_range` | Date range filter works |
| 4 | `test_audit_is_append_only` | No update/delete methods exist |

### 1.17 Debug Tracer (`tests/unit/test_debug_tracer.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_enabled_writes_jsonl` | With debug=True, `event()` writes to log file |
| 2 | `test_disabled_is_noop` | With debug=False, no file I/O occurs |
| 3 | `test_event_structure` | Written JSON has timestamp, category, event, data |
| 4 | `test_ws_events` | `ws_connect()`, `ws_send()`, etc. produce correct category |
| 5 | `test_global_tracer_singleton` | `get_tracer()` returns same instance after `set_tracer()` |

### 1.18 Orchestrator `sanitize_response` (`tests/unit/test_sanitize.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_strips_tool_blocks` | Hallucinated `<tool_call>` XML removed |
| 2 | `test_preserves_normal_text` | Regular text unchanged |
| 3 | `test_preserves_code_fences` | Markdown code blocks preserved |

### 1.19 LLM Provider Registry (`tests/unit/test_provider_registry.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_register_and_route` | Registered prefix routes to correct provider |
| 2 | `test_fallback_provider` | Unknown prefix routes to fallback (OpenRouter) |
| 3 | `test_complete_dispatches` | `complete("openai/gpt-4o", ...)` calls OpenAI provider |
| 4 | `test_list_models_aggregates` | `list_models()` returns models from all providers |

### 1.20 Session Repository (`tests/unit/test_session_repository.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_create_session` | Creates session record with ID and timestamp |
| 2 | `test_get_session` | Retrieves session by ID |
| 3 | `test_list_sessions` | Returns all sessions ordered by recency |
| 4 | `test_update_session_title` | Title update persisted |

---

## Phase 2: Integration Tests

### 2.1 Orchestrator Lifecycle (`tests/integration/test_orchestrator_lifecycle.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_start_initializes_subsystems` | `start()` starts scheduler, gateway, loads skills |
| 2 | `test_stop_graceful_shutdown` | `stop()` drains tasks, closes DB connections |
| 3 | `test_create_session_returns_id` | `create_session()` returns dict with session_id |
| 4 | `test_set_session_loads_history` | `set_session()` loads conversation history |
| 5 | `test_subscribe_receives_events` | `subscribe()` queue receives orchestrator events |

### 2.2 Message Flow (`tests/integration/test_message_flow.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_greeting_returns_inline` | "hello" → INLINE → streamed response (no skill spawn) |
| 2 | `test_skill_dispatch_single` | "search for cats" → DELEGATED → search skill spawned |
| 3 | `test_multi_task_waves` | "search X then save" → 2 tasks in correct wave order |
| 4 | `test_pipeline_context_passed` | Task 2 receives Task 1's result via pipeline_context |
| 5 | `test_conversation_context_included` | Follow-up message includes prior conversation turns |
| 6 | `test_compressed_context_long_conv` | After 8000+ chars, context is LLM-compressed |

### 2.3 Permission Flow (`tests/integration/test_permission_flow.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_skill_requests_permission` | Skill needing unapproved permission triggers user prompt |
| 2 | `test_approve_grants_access` | User approval allows skill to proceed |
| 3 | `test_deny_blocks_skill` | User denial prevents skill execution |
| 4 | `test_always_grant_persists` | "always" grant doesn't re-prompt next time |
| 5 | `test_once_grant_expires` | "once" grant re-prompts on next invocation |
| 6 | `test_trust_budget_blocks_excess` | Exceeding trust budget blocks even with grant |
| 7 | `test_resume_after_permission` | Skill resumes correctly after async permission approval |

### 2.4 Memory Tier Transitions (`tests/integration/test_memory_tiers.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_write_to_db_then_cache` | Memory write stores in DB and promotes to cache |
| 2 | `test_hot_entry_promoted` | Frequently accessed entry promoted to cache |
| 3 | `test_cold_entry_demoted` | Stale cache entry demoted after inactivity |
| 4 | `test_search_spans_tiers` | Similarity search checks both cache and DB |
| 5 | `test_eviction_under_pressure` | Cache at budget limit evicts lowest-value entries |

### 2.5 Skill Execution (`tests/integration/test_skill_execution.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_lightweight_skill_runs` | In-process skill (e.g., notes) executes and returns result |
| 2 | `test_standard_skill_subprocess` | Standard-tier skill runs in subprocess and returns |
| 3 | `test_skill_timeout_killed` | Skill exceeding timeout is terminated with error |
| 4 | `test_skill_crash_contained` | Subprocess crash doesn't crash orchestrator |
| 5 | `test_warm_pool_reuse` | Second invocation reuses warm pool process |
| 6 | `test_cross_skill_invoke` | Skill A calls Skill B via `ctx.skill.invoke()` |

### 2.6 Dreaming (`tests/integration/test_dreaming.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_dreaming_triggers_after_idle` | Memory consolidation fires after 2 min idle |
| 2 | `test_facts_extracted_to_memory` | Extracted facts written to persistent memory |
| 3 | `test_conversation_archived` | Conversation summary stored in conversation_archive |

### 2.7 Scheduler (`tests/integration/test_scheduler.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_schedule_task` | Task scheduled for future execution |
| 2 | `test_scheduled_task_fires` | Scheduled task runs at specified time |
| 3 | `test_scheduled_results_stored` | Results stored in `_scheduled` namespace |
| 4 | `test_cancel_scheduled_task` | Cancelled task doesn't execute |

### 2.8 Database Schema (`tests/integration/test_db_schema.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_init_agent_db_creates_tables` | All expected tables exist after init |
| 2 | `test_init_wal_db_creates_tables` | WAL tables exist after init |
| 3 | `test_indexes_created` | All declared indexes present |
| 4 | `test_idempotent_init` | Running init twice doesn't error or duplicate |

### 2.9 Credential Vault (`tests/integration/test_credential_vault.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_store_and_retrieve` | Store credential, retrieve returns same value |
| 2 | `test_domain_registration` | `register_domain()` maps domain to credential |
| 3 | `test_get_for_domain` | Returns auth header for registered domain |
| 4 | `test_missing_credential` | Retrieve unknown credential returns None |

*Note: Use `keyring.backends.null.Keyring` or mock keyring for test isolation.*

### 2.10 OAuth Flow (`tests/integration/test_oauth.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_initiate_returns_auth_url` | `initiate_oauth()` returns valid authorization URL |
| 2 | `test_exchange_code_stores_token` | `exchange_code()` stores access/refresh tokens |
| 3 | `test_refresh_token_updates` | `refresh_token()` replaces expired access token |
| 4 | `test_unknown_provider_errors` | Unknown provider name returns error |

*Note: Mock external OAuth endpoints.*

---

## Phase 3: API Tests

### 3.1 Authentication (`tests/api/test_auth.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_bootstrap_token_localhost` | `GET /api/auth/token` from localhost returns bearer token |
| 2 | `test_bootstrap_token_nonlocal_rejected` | Non-localhost request to /api/auth/token returns 403 |
| 3 | `test_rest_without_token_401` | Any REST endpoint without Authorization header returns 401 |
| 4 | `test_rest_with_valid_token` | Valid bearer token passes auth |
| 5 | `test_ws_without_token_rejected` | WebSocket connection without token query param rejected |
| 6 | `test_ws_with_valid_token` | WebSocket connection with token succeeds |
| 7 | `test_invalid_token_401` | Wrong token returns 401 |

### 3.2 Health Check (`tests/api/test_health.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_health_returns_200` | `GET /api/health` returns 200 |
| 2 | `test_health_no_auth_required` | Health endpoint accessible without token |

### 3.3 Chat Endpoints (`tests/api/test_chat.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_ws_connect_starts_session` | WS connection receives `session_started` event |
| 2 | `test_ws_send_message` | Sending `{"type":"message","content":"hi"}` returns response events |
| 3 | `test_ws_resume_session` | Connecting with existing session_id loads history |
| 4 | `test_ws_kill_task` | `{"type":"kill_task","task_id":"..."}` terminates running task |
| 5 | `test_ws_user_response` | `{"type":"user_response"}` delivered to waiting skill |
| 6 | `test_ws_steer_message` | `{"type":"steer"}` redirects in-progress task |
| 7 | `test_ws_permission_approve` | Permission approval through WebSocket |
| 8 | `test_ws_disconnect_cleanup` | Disconnecting cleans up subscriptions |
| 9 | `test_ws_timezone_header` | `tz` query param sets session timezone |
| 10 | `test_rest_chat_deprecated` | `POST /api/chat` still functional but WS preferred |

### 3.4 Settings Endpoints (`tests/api/test_settings.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_get_setting` | `GET /api/settings/{key}` returns stored value |
| 2 | `test_put_setting` | `PUT /api/settings/{key}` stores value |
| 3 | `test_get_missing_setting` | Missing key returns 404 or null |
| 4 | `test_list_models` | `GET /api/settings/models` returns model list |
| 5 | `test_model_override_per_skill` | `PUT /api/settings/models/overrides/{skill_id}` stores override |
| 6 | `test_get_model_override` | `GET /api/settings/models/overrides/{skill_id}` returns override |
| 7 | `test_provider_setup` | `PUT /api/settings/providers` configures LLM provider |
| 8 | `test_get_providers` | `GET /api/settings/providers` returns configured providers |
| 9 | `test_credential_status` | `GET /api/settings/credentials` returns credential summary |

### 3.5 Skills Endpoints (`tests/api/test_skills.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_list_skills` | `GET /api/skills` returns all installed skills |
| 2 | `test_get_skill_detail` | `GET /api/skills/{skill_id}` returns manifest and permissions |
| 3 | `test_get_skill_not_found` | Unknown skill_id returns 404 |
| 4 | `test_skill_settings` | `GET /api/skills/{skill_id}/settings` returns credential status |
| 5 | `test_update_skill_config` | `POST /api/skills/{skill_id}/config` updates config |

### 3.6 Permissions Endpoints (`tests/api/test_permissions.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_list_permissions` | `GET /api/permissions` returns active grants |
| 2 | `test_approve_permission` | `PUT /api/permissions/{skill_id}/{permission}` grants |
| 3 | `test_revoke_permission` | `DELETE /api/permissions/{skill_id}/{permission}` revokes |
| 4 | `test_audit_log_query` | `GET /api/permissions/audit` returns audit entries |
| 5 | `test_audit_log_filter` | Audit query with skill_id filter works |

### 3.7 Tasks Endpoints (`tests/api/test_tasks.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_list_tasks` | `GET /api/tasks` returns task list |
| 2 | `test_get_task_detail` | `GET /api/tasks/{task_id}` returns task info |
| 3 | `test_kill_task` | `DELETE /api/tasks/{task_id}` terminates task |
| 4 | `test_task_not_found` | Unknown task_id returns 404 |

### 3.8 Sessions Endpoints (`tests/api/test_sessions.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_create_session` | `POST /api/sessions` returns new session_id |
| 2 | `test_list_sessions` | `GET /api/sessions` returns session list |
| 3 | `test_get_session` | `GET /api/sessions/{id}` returns session details |
| 4 | `test_update_session` | `PUT /api/sessions/{id}` updates title |

### 3.9 Files Endpoints (`tests/api/test_files.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_list_files` | `GET /api/files` returns file listing |
| 2 | `test_read_file` | `GET /api/files/{path}` returns file content |
| 3 | `test_write_file` | `POST /api/files` creates/overwrites file |
| 4 | `test_path_traversal_blocked` | `../` in path rejected |

### 3.10 OAuth Endpoints (`tests/api/test_oauth.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_authorize_redirect` | `GET /api/oauth/{provider}/authorize` returns redirect URL |
| 2 | `test_callback_exchanges_code` | `GET /api/oauth/{provider}/callback?code=...` stores tokens |
| 3 | `test_unknown_provider` | Unknown provider returns 400 |

### 3.11 CORS (`tests/api/test_cors.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_cors_allowed_origin` | localhost:3000 origin allowed |
| 2 | `test_cors_blocked_origin` | Random origin blocked |
| 3 | `test_cors_preflight` | OPTIONS request returns correct headers |

---

## Phase 4: Skill Tests

Each first-party skill tested against its manifest contract using mock SDK context.

### 4.1 Search Skill (`tests/skills/test_search_skill.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_search_returns_results` | `search` action returns results list |
| 2 | `test_search_provider_fallback` | Falls back to next provider on failure |
| 3 | `test_search_no_results` | Empty results handled gracefully |
| 4 | `test_configure_sets_provider` | `configure` action updates preferred provider |
| 5 | `test_requires_web_fetch` | Skill checks for `web:fetch` permission |

### 4.2 Notes Skill (`tests/skills/test_notes_skill.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_create_note` | Creates note in memory namespace |
| 2 | `test_list_notes` | Lists all notes |
| 3 | `test_read_note` | Reads specific note by key |
| 4 | `test_update_note` | Updates existing note content |
| 5 | `test_delete_note` | Removes note |
| 6 | `test_search_notes` | Semantic search across notes |

### 4.3 Files Skill (`tests/skills/test_files_skill.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_read_file` | Reads file content |
| 2 | `test_write_file` | Writes file to disk |
| 3 | `test_mkdir` | Creates directory |
| 4 | `test_delete_file` | Deletes file |
| 5 | `test_list_directory` | Lists directory contents |
| 6 | `test_approve_directory` | Approval flow for new directories |
| 7 | `test_max_file_size_enforced` | Files > 2MB rejected |
| 8 | `test_path_validation` | Invalid/traversal paths rejected |
| 9 | `test_tree_output` | `tree` action returns directory structure |
| 10 | `test_diff_action` | `diff` action shows file differences |

### 4.4 Shell Skill (`tests/skills/test_shell_skill.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_run_command` | Executes command and returns output |
| 2 | `test_run_timeout` | Long-running command times out |
| 3 | `test_get_pwd` | Returns current working directory |
| 4 | `test_list_env` | Returns environment variables |
| 5 | `test_dangerous_command_blocked` | Potentially destructive commands flagged |

### 4.5 Code Runner Skill (`tests/skills/test_code_runner_skill.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_run_python` | Python code executes and returns output |
| 2 | `test_run_javascript` | JavaScript code executes |
| 3 | `test_run_bash` | Bash script executes |
| 4 | `test_syntax_error_reported` | Syntax errors returned in result |
| 5 | `test_timeout_enforced` | Infinite loop killed after timeout |

### 4.6 Webpage Reader Skill (`tests/skills/test_webpage_reader_skill.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_read_webpage` | Fetches and parses URL content |
| 2 | `test_summarize_webpage` | Returns summarized content |
| 3 | `test_invalid_url` | Invalid URL returns error |
| 4 | `test_requires_web_fetch` | Checks for `web:fetch` permission |

### 4.7 Calendar Skill (`tests/skills/test_calendar_skill.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_list_events` | Lists calendar events |
| 2 | `test_create_event` | Creates new event |
| 3 | `test_find_availability` | Returns free/busy slots |
| 4 | `test_requires_oauth` | Prompts for OAuth if no token |

### 4.8 Email Skill (`tests/skills/test_email_skill.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_list_emails` | Lists inbox messages |
| 2 | `test_read_email` | Reads specific email |
| 3 | `test_draft_email` | Creates draft |
| 4 | `test_send_email` | Sends email (mocked) |
| 5 | `test_search_emails` | Search by query |

### 4.9 Notify Skill (`tests/skills/test_notify_skill.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_send_notification` | Sends notification event to user |

### 4.10 Reminders Skill (`tests/skills/test_reminders_skill.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_create_reminder` | Creates scheduled reminder |
| 2 | `test_list_reminders` | Lists active reminders |
| 3 | `test_delete_reminder` | Removes reminder |

### 4.11 Skill Author Skill (`tests/skills/test_skill_author_skill.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_scaffold_manifest` | Generates valid manifest.json |
| 2 | `test_scaffold_skill_py` | Generates skill.py with correct entry point |
| 3 | `test_validate_manifest` | Validates manifest structure |

---

## Phase 5: Frontend Tests

### 5.1 Hook: useApiToken (`tests/frontend/hooks/useApiToken.test.ts`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_fetches_token_on_mount` | Calls `/api/auth/token` on mount |
| 2 | `test_sets_authorization_header` | `apiFetch()` includes bearer token |
| 3 | `test_handles_fetch_failure` | Graceful error state on token fetch failure |

### 5.2 Hook: useWebSocket (`tests/frontend/hooks/useWebSocket.test.ts`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_connects_with_token` | WebSocket URL includes token query param |
| 2 | `test_reconnects_on_close` | Reconnection attempt after unexpected close |
| 3 | `test_sends_message` | `sendMessage()` sends JSON to server |
| 4 | `test_receives_events` | Incoming messages added to events array |
| 5 | `test_auto_approve_permission` | Auto-approve setting sends approval automatically |
| 6 | `test_session_id_propagated` | Session ID passed in WebSocket URL |

### 5.3 Component: ChatStream (`tests/frontend/components/ChatStream.test.tsx`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_renders_messages` | Messages displayed in order |
| 2 | `test_input_sends_message` | Typing + submit triggers send |
| 3 | `test_streaming_response` | Partial responses render incrementally |
| 4 | `test_permission_prompt_displayed` | Permission request shows approve/deny UI |
| 5 | `test_skill_confirm_displayed` | Skill confirmation shows action description |
| 6 | `test_task_started_indicator` | Task started event shows skill badge |
| 7 | `test_error_message_styled` | Error messages visually distinct |

### 5.4 Component: Settings (`tests/frontend/components/Settings.test.tsx`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_renders_provider_list` | Shows configured providers |
| 2 | `test_add_provider_api_key` | API key input saves to backend |
| 3 | `test_model_selection` | Model dropdown shows available models |
| 4 | `test_credential_status` | Shows connected/disconnected status per credential |

### 5.5 Component: TaskTray (`tests/frontend/components/TaskTray.test.tsx`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_shows_running_tasks` | Active tasks displayed with skill name |
| 2 | `test_kill_button` | Kill button sends kill_task message |
| 3 | `test_completed_task_removed` | Finished tasks removed from tray |

### 5.6 Component: SessionSidebar (`tests/frontend/components/SessionSidebar.test.tsx`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_lists_sessions` | Sessions fetched and displayed |
| 2 | `test_click_loads_session` | Clicking session triggers resume |
| 3 | `test_new_session_button` | New session button creates fresh session |

### 5.7 Component: SetupCard (`tests/frontend/components/SetupCard.test.tsx`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_shows_when_no_provider` | Card displayed when no LLM provider configured |
| 2 | `test_provider_setup_flow` | Entering API key dismisses card |

### 5.8 Component: ErrorBoundary (`tests/frontend/components/ErrorBoundary.test.tsx`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_catches_child_errors` | Error in child component shows fallback UI |
| 2 | `test_normal_render` | No error renders children normally |

---

## Phase 6: End-to-End Tests

### 6.1 Core Workflows (`tests/e2e/test_workflows.py`)

All E2E tests start a full server instance and connect via WebSocket.

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_search_and_save_to_notes` | "Search for X" → "Save that to notes" — result persisted in memory |
| 2 | `test_multi_task_with_dependency` | "Search for X and write a summary file" — search completes, file written with search results |
| 3 | `test_cross_skill_pipeline` | "Read webpage Y and save notes" — webpage reader → notes, pipeline_context flows |
| 4 | `test_permission_approval_flow` | First file write prompts permission → approve → file created |
| 5 | `test_permission_denial_flow` | Deny file write → skill reports error, no file created |
| 6 | `test_session_persistence` | Send message, disconnect, reconnect with session_id — history preserved |
| 7 | `test_concurrent_tasks` | "Search A, search B, search C" — 3 tasks run in parallel |
| 8 | `test_kill_running_task` | Start long task → kill → task stopped, no zombie processes |
| 9 | `test_follow_up_context` | "Search for cats" → "Tell me more about the first result" — context maintained |
| 10 | `test_greeting_and_setup` | First connection → greeting message received |

### 6.2 Error Recovery (`tests/e2e/test_error_recovery.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_skill_crash_recovery` | Skill subprocess crash → error reported, orchestrator healthy |
| 2 | `test_llm_provider_timeout` | LLM timeout → user notified, no hang |
| 3 | `test_external_api_failure` | Search API returns 500 → error surfaced to user |
| 4 | `test_db_write_failure` | Simulated DB error → WAL entries preserved for recovery |
| 5 | `test_websocket_reconnect` | Drop WS connection → client reconnects → session intact |

### 6.3 Security (`tests/e2e/test_security.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_gateway_blocks_private_ips` | Skill HTTP to 127.0.0.1 blocked |
| 2 | `test_gateway_blocks_unlisted_domain` | Skill HTTP to unlisted domain blocked |
| 3 | `test_path_traversal_file_skill` | `../../etc/passwd` rejected by file skill |
| 4 | `test_subprocess_isolation` | Standard-tier skill can't access orchestrator internals |
| 5 | `test_auth_required_all_endpoints` | Every REST endpoint (except health, token) requires auth |
| 6 | `test_trust_budget_enforcement` | Rapid-fire skill invocations hit budget limit |

### 6.4 Performance Baselines (`tests/e2e/test_performance.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_classification_latency` | Intent classification < 2s (with LLM, < 50ms for fast-path) |
| 2 | `test_task_spawn_latency` | Task spawn < 200ms |
| 3 | `test_memory_cache_lookup` | Cache read < 5ms |
| 4 | `test_warm_pool_reuse_faster` | Warm pool launch < cold start |
| 5 | `test_concurrent_task_throughput` | 10 concurrent tasks complete without timeout |

---

## SDK Tests (`tests/sdk/`)

### SDK Context & Clients (`tests/sdk/test_sdk_context.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_has_permission` | `ctx.has_permission("file:read")` returns correct bool |
| 2 | `test_require_permission_raises` | Missing permission raises `PermissionDenied` |
| 3 | `test_brief_accessible` | `ctx.brief` returns task brief dict |
| 4 | `test_sub_clients_initialized` | `ctx.memory`, `ctx.http`, `ctx.user`, etc. are non-None |

### SDK Memory Client (`tests/sdk/test_sdk_memory.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_read_write_cycle` | `ctx.memory.write()` then `read()` returns value |
| 2 | `test_search` | `ctx.memory.search(query)` returns relevant entries |
| 3 | `test_list_keys` | Lists keys with prefix |
| 4 | `test_delete` | Deletes entry |
| 5 | `test_read_profile` | Reads from `_profile` namespace |
| 6 | `test_cross_namespace_read` | `read_namespace()` accesses other skill's data |

### SDK HTTP Client (`tests/sdk/test_sdk_http.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_get_request` | `ctx.http.get(url)` returns response dict |
| 2 | `test_post_request` | `ctx.http.post(url, json=...)` sends POST |
| 3 | `test_routes_through_gateway` | Requests go through gateway proxy, not direct |

### SDK User Client (`tests/sdk/test_sdk_user.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_ask_question` | `ctx.user.ask("pick one", options=["a","b"])` returns choice |
| 2 | `test_notify` | `ctx.user.notify("done")` sends notification |
| 3 | `test_confirm_approved` | `ctx.user.confirm("delete?")` returns True on approval |
| 4 | `test_confirm_denied` | Denial raises `UserCancelled` |

### SDK LLM Client (`tests/sdk/test_sdk_llm.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_complete` | `ctx.llm.complete(prompt)` returns text |
| 2 | `test_complete_json` | `ctx.llm.complete_json(prompt, schema)` returns valid dict |
| 3 | `test_complete_json_invalid_retries` | Invalid JSON retried or error raised |

---

## Test Infrastructure

### CI Configuration

```yaml
# Suggested GitHub Actions workflow
stages:
  - unit        # Phase 1 — fast, no external deps
  - sdk         # SDK tests — mock IPC
  - integration # Phase 2 — in-memory DB, mock LLM
  - api         # Phase 3 — FastAPI test client
  - skills      # Phase 4 — mock SDK context
  - e2e         # Phase 6 — full server, real WebSocket
  - frontend    # Phase 5 — vitest, jsdom
```

### Environment Variables for Testing

| Variable | Purpose |
|----------|---------|
| `MUSE_TEST=1` | Enable test mode (disable real LLM calls) |
| `MUSE_TEST_DATA_DIR` | Override data_dir to temp directory |
| `MUSE_MOCK_LLM=1` | Use mock LLM provider |
| `MUSE_MOCK_KEYRING=1` | Use null keyring backend |

### Coverage Targets

| Module | Target |
|--------|--------|
| `kernel/` | 90% |
| `memory/` | 90% |
| `permissions/` | 95% |
| `skills/` | 85% |
| `api/` | 85% |
| `gateway/` | 90% |
| `sdk/` | 90% |
| `frontend/` | 75% |
| **Overall** | **85%** |

---

## Test Count Summary

| Phase | Category | Tests |
|-------|----------|-------|
| 1 | Unit Tests | 120 |
| 2 | Integration Tests | 38 |
| 3 | API Tests | 45 |
| 4 | Skill Tests | 40 |
| 5 | Frontend Tests | 27 |
| 6 | End-to-End Tests | 21 |
| — | SDK Tests | 19 |
| **Total** | | **310** |
