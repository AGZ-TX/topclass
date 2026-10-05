# Books after confirmation

The AI chooses courses and authors the education HTML. When the user confirms those choices and returns the choices file, `./topclass select` validates the saved snapshot, creates the book list, and automatically passes supported ISBNs to the included finder. Returned checked files automatically enter private indexing; `/add` is only for extra material.

That repo’s `search.py` finds records, retrieves supported files, checks catalog MD5, supported format and SHA-256 stability before releasing files. Results use `file_verified` with explicit `checks` and `checked_at` fields; antivirus scanning is not part of this pipeline. Topclass reads its report, checks requested ISBN membership, validates that returned files are inside this selection’s private checked directory, and rechecks MD5, SHA-256 and byte counts. The search handoff itself does not derive knowledge; the following source-index worker opens supported originals for embeddings and page navigation. A checked file is not proof of publisher authenticity, a particular course-assigned edition, or learned knowledge.

## Connect once

Run `./topclass setup` to prepare the bundled finder with Node.js 20+, Python 3.11+, its local MCP server, and Chromium. No private repository access is needed. A desktop session may be needed for verification. See [setup](setup.md). Do not substitute the upstream npm package.

```sh
./topclass setup --host codex
```

An existing prepared AGZ-TX/search checkout is an optional override:

```sh
./topclass finder --repo /absolute/path/to/search
```

The connection is private workspace configuration; it contains only the executable checkout path, not credentials or learned material. `TOPCLASS_SEARCH_REPO` or a prepared sibling `search` checkout also work. Explicit `--search-repo` on `select` or `books` takes precedence, then the environment, saved configuration and sibling fallback. Preparing a connection does not search or download anything.

The host handles command details. Users review courses and confirm them; they do not need to copy ISBNs or manage batches. The portable HTML exports the confirmation file; it does not execute local Python from the browser. The host agent receives that file and runs `select`, then authors the updated HTML with the book results.

## Confirm, search and retry

```sh
./topclass select --agent AGENT_ID /path/to/confirmed-choices.json
./topclass books --agent AGENT_ID
```

Search is allowed only after validated selection. Hire, planning, browsing and HTML validation never start acquisition. Removed and unselected courses are absent from the ISBN request. Queries are normalized to ISBN-13 and deduplicated without combining course/book assignments. More than 100 ISBNs are split into batches of at most 100, using search’s default record limit. Original ISBNs, citations and course assignment evidence are unchanged.

Saved supported reference-edition finding aids may also be searched, with their source and edition uncertainty recorded separately. Title-only and unconfirmed work-only findings are excluded. Books without a reliable ISBN remain in the list with course, library or author-site links. A book without an ISBN is not a failed education choice.

All requests, raw reports, logs, run summaries and checked files remain under:

```text
AGENTS_HOME/AGENT_ID/selections/SELECTION_ID/
  book-search-request.json
  book-search.json
  book-search/RUN_ID/
    summary.json
    isbns-BATCH.json
    report-BATCH.json
    search-BATCH.log
    files-BATCH/checked/SEARCH_RUN_ID/
```

Each retry creates new report/file paths and retains previous outcomes. `book-search.json` describes the latest attempt. Only one book search can run for a given agent at once; other agent workspaces remain separate. Search’s browser session is per OS user and should not be opened concurrently elsewhere.

`complete` means at least one checked local file for every requested ISBN and no selected books left without a supported ISBN. `partial`, `setup-required`, `busy`, `failed`, `interrupted` and per-ISBN statuses stay explicit. Source access errors do not mean a book does not exist. A missing checkout or missing runtime prerequisites never undo confirmed choices or create learned knowledge. Empty/no-ISBN selections do not start the external process.

Topclass invokes search unattended. If a source needs human verification or sign-in, the report retains `verification_required`; complete browser setup from the local desktop as described in search’s README, then retry `books`. Do not bypass verification. Private logs explain local setup failures.

Returned checked files automatically enter this hire's original-source indexing queue. With the user's Google key configured once, Topclass starts the resumable embedding worker and builds private page navigation and semantic similarity links. No reading model, summaries or extracted knowledge claims are involved. Without the key, confirmed choices and downloaded files stay saved with setup required. No material is copied to another hire. See [original indexing](memory.md).

## Validation limits

Integration tests use a controlled external CLI that writes synthetic fixture documents and reports. They exercise confirmed selection through subprocess arguments, batch inputs, private result import and failure handling, including changed bytes and cross-agent paths. These fixtures are not actual textbook retrieval or live source access. Live source availability and manual browser verification depend on the user’s prepared search environment.
