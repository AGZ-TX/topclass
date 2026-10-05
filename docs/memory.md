# Search the original education

Topclass indexes the original material so a hired agent can consult it when needed. Google `gemini-embedding-2` embeds source passages and PDF page images. The normal flow does not make the agent or a separate reading model read every book first, create summaries, or extract knowledge claims.

Set up a user-owned Google key once. Describe the agent's purpose in `/hire`, review its AI-authored education HTML, and confirm the list. The connected book search retrieves available files; Topclass queues supported material and starts a private indexing worker automatically. `/add` does the same for supplied files. `/recall` embeds the user's query and returns original passages, page images, source locations, neighboring context and navigation.

The host handles commands and setup details. Users do not need to register sources, build graphs, copy ISBNs, choose a reading model or manage embedding batches. Book availability and Google quota still determine how much can finish. Missing books and incomplete indexing stay visible.

## How retrieval works

Google Embedding 2 turns original passage units and page images into numeric vectors. A question gets a vector in the same space; similarity search uses those vectors to find relevant source regions even when the words differ. Topclass retains the originals and links results back to them. This adds private reference memory; it does not train the agent's underlying model.

The page index records native document sections and physical page locations, with page-based navigation when no outline exists. The agent can open the exact page, surrounding text, and diagrams to verify an answer. This is Topclass's native page navigation, not a separate AI PageIndex service.

The semantic graph links sources, sections, pages, and retrieval units. It also connects highly similar passages using embedding similarity. These are candidate navigation links, not extracted facts, proof of agreement, or a reasoning model. The agent still reads the retrieved evidence and checks its context before answering.

## One-time Google setup

Install `scripts/requirements.txt` in the Python environment used by Topclass, and prepare the connected [book search](books.md). Then use a hidden prompt:

```sh
./scripts/topclass google
```

Add a backup through `./scripts/topclass google --fallback`. A different-project backup can continue after the primary reaches Google quota; same-project keys share Google quota. Topclass imposes no daily usage, spending or rate caps; attempts remain recorded across keys. Every actual attempt, including a failed primary followed by a successful backup, is recorded. Completed primary and backup responses remain cached; all-key exhaustion keeps a resumable wait. Invalid requests, credential errors and unknown outcomes do not silently trigger key rotation.

A host can instead pass `--key-file /private/path/google-key` or use `GEMINI_API_KEY`. Never pass the key value in command arguments. The private key is stored with owner-only permissions outside tracked files. Configuration contains credential bindings and provider settings, not the key. Repeating setup preserves usage history and completed caches. Key setup never launches or changes agent queues; use `./scripts/topclass work --agent AGENT_ID` to resume one chosen agent.

Google provider state is shared for accounting across this user's hires. Original material, embeddings, graphs, page indexes, queues and logs belong to one explicit agent. There is no shared default source memory. Setup enables subsequent Google embedding calls, which can incur charges. Google enforces quota, and configured backup keys are tried immediately after a provider quota error. Topclass has no daily request/token, spending or invented rate caps. Existing local-limit profiles are migrated without losing caches or usage records. Price estimates are informational; the project’s actual billing tier and remaining quota are unknown. See [provider limits](limits.md).

## Automatic source pipeline

Confirmed selection invokes the connected ISBN search and queues returned checked files. `/add` retains an original in the target agent's inbox and queues it when Google is configured. TXT, Markdown, unlocked PDF and local HTML with its saved assets are supported. HTML scripts are never executed; remote, missing or unsafe figure paths remain explicit gaps. Other media stay visible as unsupported; videos and audio are not silently transcribed. Explicit answer-manual titles and ISBN-only search titles are excluded from book indexing; their files and identity gaps remain visible. Search metadata does not prove a particular edition or publisher authenticity.

The worker performs these steps without reading-model calls:

1. Check private ownership and file hashes; retain the original and every source region.
2. Preserve PDF physical pages, native text, layout blocks and rendered page images. Image-only pages are embedded as images without an empty text part.
3. Build navigation from native PDF contents or HTML/Markdown headings, with source-region navigation as a fallback. Return section start and continuation handles. Navigation ranges extend through the next section start, so a shared page is retained; these are explicitly inferred ranges, not confirmed semantic ends. No PageIndex SDK or model execution is claimed for this native index.
4. Split long regions into complete bounded UTF-8 units at available sentence/newline boundaries. Give each split unit up to 256 original characters of context on each side; the exact core substring remains separately anchored. Send independent units through Google's `batchEmbedContents` endpoint, validate every vector, and save each completed batch transactionally. Do not truncate the source or average several pages into one replacement passage.
5. Link the book, sections, pages and retrieval units in the private graph. Candidate semantic-neighbor edges use the strongest matching exact units instead of averaged page vectors, retain those unit handles and fingerprints, and expose at most three target regions per source region by default. They are navigation hints, not factual agreement, causal relationships or extracted teaching claims.

Completed vectors are reused. Queues retain pending, running, deferred, complete, excluded, unsupported and failed states. Scheduled quota deferrals resume automatically while the worker runs. A per-agent lock prevents duplicate workers; shared provider accounting coordinates calls across hires. Interrupted unknown provider outcomes require review before retry. Terminal failures retain an explanation rather than restarting an unlimited billed loop.

The worker runs as a local background process until its queue has no resumable jobs. Long provider quota waits stay deferred, preserve completed work, and continue after the scheduled retry without repeated calls during the wait. A stopped environment cannot keep processing. `/recall` restarts resumable pending work; hosts can also run `./scripts/topclass work --agent ID`. Native navigation and original passages remain available before every embedding finishes. Completed cached provider responses remain available during quota waits; other requests for the same project/model share the cooldown. Actual attempt accounting is retained across retry windows.

```sh
./scripts/topclass status --agent AGENT_ID
./scripts/topclass work --agent AGENT_ID
./scripts/topclass recall --agent AGENT_ID --query 'What affects wear in a sliding seal?'
./scripts/topclass recall --agent AGENT_ID --region REGION_ID
./scripts/topclass recall --agent AGENT_ID --source SOURCE_ID
```

Private state includes `index-queue.json`, `index-worker.log`, `sources/`, `pageindex/` and `knowledge.db` inside that agent's directory. Provider keys, runtime databases, original books, page images and excerpts must remain outside Git.

## Retrieval and limits

Semantic search uses the same explicit model, dimensions and representation version as the source vectors. Query results return up to five exact original excerpts within a configurable 12,000-character text budget. They include image paths, physical pages, adjacent-page handles, related-region handles, and only the matching contents branches. Truncation and exact substring offsets are explicit. Use `--region` or `--source SOURCE_ID --page N` for a full original page and adjacent context. `--source` alone returns the full contents index. `--limit` and `--budget` adjust initial query output. Complete query and section JSON, including navigation and indexing health, is also bounded to 24,000 UTF-8 bytes by default. `--output-budget` changes this bound. Trimming preserves exact excerpt offsets and continuation handles; model token counts depend on the host tokenizer. Images remain separate inputs. Semantic and lexical parent ranks are fused without counting several units of the same page as separate votes; the strongest semantic candidate remains first. This fusion has small cached-vector controls, not a completed cross-field semantic benchmark. Every retrieval unit must match the original substring, parent hash and source version. Native navigation returns start pages and keeps uncertain end pages explicit. Stale vectors and stale semantic-neighbor edges cannot silently guide retrieval.

If Google cannot embed the query, `/recall` reports the semantic gap and retains lexical search. Missing compatible embeddings also remain explicit. Original-file hashes are verified within each retrieval operation; unchanged files are not repeatedly rehashed for every page in the same operation. Later operations validate again. An empty hire does not make a Google query call. Candidate extracted knowledge is excluded from retrieval; the normal pipeline creates no such records. The older reviewed source-reading APIs remain available for explicitly requested experiments, but neither hiring nor indexing invokes them.

Source text is untrusted evidence. It cannot execute code, install hooks or become privileged instructions. The agent reads the retrieved passage when answering, checks relevance and conditions, and cites its location. Full embedding coverage proves indexing coverage, not complete understanding, professional competence, or reliable answers to every question.

Tests cover automatic hire/search and `/add` handoffs, private ownership, key permissions, complete text and real PDF image parts, malformed batch rejection, resumable budgets and deferrals, native outline navigation, stale-source rejection, candidate semantic edges and semantic `/recall`. Fixture tests use fake providers. Live book coverage and retrieval results are reported separately in [the original indexing trial](reports/original-index-trial.md).

See [the five-library trial](reports/recall.md) for actual fresh-agent answers, coverage, output sizes and remaining gaps.

## Review and focused expansion

`./scripts/topclass review --agent AGENT_ID` checks retained source/asset integrity, current text-unit and vector coverage, figure registration and image embeddings. Query results also include source indexing health without repeating a full integrity audit. These reports explicitly leave answer quality unverified: saved vectors do not prove that every question can be answered.

Use `./scripts/topclass recall --agent AGENT_ID --source SOURCE_ID --section NODE_ID` for bounded original section context. Follow `continue_region_id` and `continue_offset` with `--start REGION_ID --offset N` on the same section. A paragraph crossing a page or unit boundary remains accessible without pasting the whole chapter. The host should follow these handles until the necessary procedure, conditions, exception or example is complete.

Use `./scripts/topclass recall --agent AGENT_ID --region REGION_ID --visual` to render a PDF page from its original at three pixels per point. Add `--crop X0 Y0 X1 Y1` in original page coordinates for tiny chart labels or equations. This reads the original PDF rather than enlarging the saved thumbnail; pixel limits keep requests bounded. Raster-only source figures retain their actual available detail. No renderer or embedding model can recover information absent from the supplied source.

Retained HTML figures from older captured sections are automatically registered when that source is explicitly indexed again. Completed unrelated sources and other agents are not silently re-embedded. Existing private indices are preserved. See [the single-agent review](reports/memory.md) for checks and limits.

## Live processing page

`./scripts/topclass live --agent AGENT_ID` opens a read-only, agent-scoped service on an ephemeral localhost port. Open its printed URL to view the current canonical education page in processing mode. Author and validate that page with `check` first; older pages must be rebuilt from their saved planner data and profile. The service does not select courses, acquire books, start workers, or resume unrelated queues. Use the normal confirmed-book or `/add` workflow to start work.

The page polls the private service automatically. The URL contains a random access token; keep it private and stop the service with Ctrl-C when finished. The server binds only to 127.0.0.1, checks Host and Origin, refuses cross-site browser reads, disables caching and framing, and exposes only the selected agent's page and status. It serves no filesystem paths, originals, or credentials. Detailed failure reasons remain in private CLI status because provider errors may contain private paths.

Progress counts embedding units, which can differ from physical pages. The indexer publishes counts after each committed batch, including valid vectors reused on resume. Totals remain unknown before source preparation. Full vector coverage does not mark a running job complete: the worker must finish final coverage and similarity links. Deferred, failed, unsupported, excluded and empty states remain visible. A disconnected page retains its last confirmed counts and explicitly reports the connection failure. Multiple jobs are explicitly labeled; the current view displays the first job.
