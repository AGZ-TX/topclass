# Privacy

The repository contains code, course/book metadata, source evidence, generic templates, and validation reports. It does not distribute API keys, personal provider profiles, cookies, downloaded textbooks, personal education plans, graph databases, vectors, inbox files, or search receipts.

New workspaces are outside the checkout. Existing `derived_private/agents` workspaces remain supported and ignored by Git. `TOPCLASS_HOME` and `--home` choose private storage; non-ignored locations inside the checkout are rejected. Keep external locations private. Key files use mode 600 and private agent directories use mode 700.

Google receives text and page images submitted for embeddings and queries submitted for semantic retrieval. Local storage is not published to GitHub. Topclass imposes no daily request, token, rate or spending caps on this workflow. Google enforces provider quotas and charges; Topclass follows provider retry timing and tries configured backup keys on quota failures. Keys from the same Google project share quota.

The finder connects to external source sites and uses a dedicated Chromium profile, normally at `~/.local/share/search/browser`. Requested ISBNs reach those sources. Cookies are not exported into reports. Private reports can contain titles, URLs, and local paths; do not share them automatically.

Every hire has separate memory. `/recall` requires the intended agent ID. `/add` adds extra knowledge only to that hire. Confirmed books are indexed automatically in the same owned workspace. Selected public metadata is not learned source knowledge.

Run `./scripts/topclass audit --history` explicitly before sharing. It scans eligible current files and locally reachable Git blobs for known credential patterns and private filenames, printing only paths and finding types. Fetch relevant refs before relying on historical coverage. Review releases, artifacts, other branches, exports, and unrecognized content separately. Git ignores cannot remove committed history. Rotate any credential that was committed and remove its historical exposure before publication.

Backup credentials are stored in the owner-only private `.google-keys` file; `.google.json` stores only bindings and non-secret provider settings. Both filenames are excluded from tracked public configuration, and the manual audit flags a `.google-keys` file if it enters eligible files.
