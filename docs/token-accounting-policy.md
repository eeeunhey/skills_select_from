# Token accounting policy (PRD 19, OBS-01..12)

Unit hierarchy: Run → Step → Attempt → LLM call. Every `llm_calls` row links `run_id`, `step_id`, `attempt_id`, `call_id`, `skill_id` (`NO_SKILL` when none), `skill_hash`, `model_id`.

## Claude field mapping (19.3 v1.3)
| Ledger | `claude -p` result message | Messages API |
|---|---|---|
| `input_tokens` | `modelUsage.<m>.inputTokens` (uncached remainder only) | `usage.input_tokens` |
| `cache_write_tokens` | `cacheCreationInputTokens` | `usage.cache_creation_input_tokens` |
| `cached_input_tokens` | `cacheReadInputTokens` | `usage.cache_read_input_tokens` |
| `output_tokens` | `outputTokens` (from the result message, not per-step messages) | `usage.output_tokens` |
| `estimated_cost` | `total_cost_usd` (Claude Code's built-in price table, an estimate) | computed from `price_schedules` |

## Rules
1. **Total input = input + cache write + cache read.** Each is counted once.
2. Measured values come only from the provider (`usage_source=provider`). Missing usage is stored as `NULL` with `usage_source=unavailable`, **never 0**. The zeros a crashed run reports also count as unavailable.
3. `prompt_parts_estimate_json` is our own estimate. It is never shown as a measurement.
4. Costs are named `estimated_cost`. On profile S (subscription) there is no per-call billing, so costs are always estimates. Real billing can only be reconciled on profile K (Usage & Cost API).
5. Failed attempts and retries keep their usage (OBS-06). Retries are counted from `system/api_retry` events.
6. Resumed sessions (`--resume`) report cumulative cost, so only per-call increments are stored. Prefer one new session per step.
7. GitHub API calls go in `collection_runs.github_api_calls` and are never converted to tokens (OBS-12).
8. Secrets and raw prompts are not logged (OBS-09). `provider_usage_json` is redacted.
