# Google quota and retries

Topclass’s Google workflow has no daily request, token, spending or invented rate caps. Google enforces the project’s quota. The runtime records attempts and available usage, retains completed responses, and follows provider quota errors and retry timing.

## Backup keys

Add a private backup with `./scripts/topclass google --fallback`, using a hidden prompt or private `--key-file`. A provider HTTP 429 immediately tries the next configured key rather than waiting on the first key. Each key has its own credential-bound request cache and provider cooldown. Keys from different Google projects can have separate quota; keys in the same project share quota. Topclass cannot infer a trustworthy project identity from the key string.

Completed caches across all keys are checked before a live call, including after the primary recovers. If every key is limited, work stays deferred until the earliest provider retry is due. Google’s Retry-After and RetryInfo delays are respected. When no delay is supplied, transient backoff prevents a rapid retry loop; Topclass does not invent a daily allowance or daily blocking window. Google quota retries do not expire merely because a local request counter or retry window was reached.

Invalid credentials, permission failures and malformed requests remain explicit; they are not assumed to be quota exhaustion. Unknown request outcomes require review before retry, preventing accidental duplicate billed work. Finite handling of transport/server failures is separate from usage allowance. Batches, text units and image size bounds preserve valid requests and manageable memory; they do not limit daily consumption.

## Existing installations

The Google profile is migrated on normal use. Old daily request/token, spending and local rate fields are removed. Stored waits caused by those local limits are released, without discarding completed caches, source vectors, actual attempt records or genuine Google cooldowns. The explicitly selected agent’s worker can resume its old local-limit wait. Migration and key setup never launch unrelated agents.

The normal worker has no per-run embedding-call allowance. Older low-level interfaces retain explicit caller-selected batch controls for compatibility; they are not default usage caps. The legacy Google CLI daily-cap option cannot restore an invented Google limit.

## Accounting and privacy

Provider accounting is coordinated in the private runtime database, while each hire’s sources, graph, page index and queues stay separate. Usage and price estimates are informational, not limits or invoices. Remaining Google quota and the billing total are unknown. The Pacific day boundary is an accounting grouping, not permission to block requests until midnight. API keys are bound to HTTP transports and kept in owner-only private files, outside tracked configuration and reports.

Source material and course plans cannot select privileged credentials or execute provider calls by themselves. The user configures Google once and authorizes confirmed-book indexing or extra-material ingestion. No separate reading model runs in the normal indexing pipeline.

## Development adapters

Optional source-reading and Jev/TypeSafe experiments remain separate from normal `/hire`, `/recall` and `/add`. They require their explicitly configured credentials and model-call authorization. Maintainer development state and user source-processing state must remain separate. Legacy explicit experimental profiles do not define the normal Google workflow’s usage policy.

## Validation

Tests cover provider-managed profiles, migration from local-limit deferrals, preserved cache and attempt history, immediate short-delay quota failover, all-key exhaustion, credential binding, concurrent request ownership, malformed outcomes and resumable source batches. Live source coverage and retrieval checks are recorded in the memory review.

Provider reference: [Google rate limits](https://ai.google.dev/gemini-api/docs/rate-limits).
