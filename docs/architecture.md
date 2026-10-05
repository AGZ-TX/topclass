# Architecture

`topclass` is the public executable. It locates its checkout, uses the local Python environment, and routes setup, auditing, and agent commands. Installed skills invoke that absolute executable; the host does not need to guess a working directory.

| Module in `scripts/core/` | Responsibility |
|---|---|
| `app.py` | Agent identity, commands, saved choices, private ownership |
| `hire.py`, `catalog.py`, `brief.py` | AI-readable evidence and validation of AI-authored education |
| `planner.py`, `reports.py` | Planner data and book reports; AI authors HTML |
| `search.py` | Confirmed ISBN requests, finder connection, receipt validation |
| `pipeline.py` | Automatic queue, worker, resumable jobs, provider coordination |
| `sources.py`, `index.py`, `pages.py` | Originals, embeddings, native navigation, similarity links |
| `recall.py`, `output.py` | Original evidence, context, detailed visuals, bounded complete retrieval output |
| `markup.py`, `health.py` | Retained HTML assets and equations, explicit source/visual coverage review |
| `google.py`, `provider.py`, `keys.py` | Google adapter, usage, budgets, quotas, private cache |
| `paths.py` | User storage and existing-workspace compatibility |

`scripts/setup.py` and `privacy.py` handle installation and explicit auditing. `scripts/finder/` contains the licensed search source with separate locked Node dependencies and tests. Catalog collectors remain outside the agent runtime.

Older flat entry points import the same core module objects, preserving commands, imports, and tests. Historical data and report names remain stable. Core modules and new public guides use one-word names.

AI owns course choice, priorities, and HTML. The page exports choices; the host runs `select`. That command searches ISBNs and automatically queues returned checked files. There is no required `/add` step between confirmation and indexing.

Public metadata can be reused. Graphs, sources, page indexes, selections, and queues cannot be silently shared across hires. Provider usage coordinates across hires using the user’s key. Source memory stays separate; completed embedding units are retained for resume.

The finder snapshot is pinned in `scripts/finder/origin.json`, including copied-file hashes. Keep its MIT attribution and original third-party filenames/comments. Review updates, run its tests, regenerate the manifest, and exclude user state, browser profiles, and generated dependencies. No automatic updater or GitHub checks are installed.
