# Hire an agent and choose its education

Say `/hire` in a Topclass chat. Your agent asks:

> What is your agent's purpose?

For example, you could reply: “I want an agent to develop my brand and guide its design.”

Reply in your own words. There is no menu of job titles or predefined agent types. A role, goal, project or examples of work can all express the purpose. The AI identifies the foundations, supporting subjects and specializations needed for that purpose, reads the course JSONL library, chooses courses and documented books, and **fills the approved education HTML**. Open it to add or remove courses, and see the resulting books and missing coverage.

Save your choices and return the file to the chat. The AI gives you the selected book list and edits the HTML for any further changes. After confirmation, Topclass searches for the books and, with your Google key configured once, automatically indexes available original material for this agent. A degree plan, full prerequisite chain and learning schedule are not required. Authors, editions and ISBNs are optional extras, not selection requirements. Obvious devices such as iClicker remotes are excluded from learning-book views while the original records remain available as evidence. Books and provider keys are not needed to choose the education. A hire creates a private education workspace, not an autonomous running bot or learned knowledge.

## Who does what

| AI host agent | Code tools | User |
|---|---|---|
| Interprets the purpose, explores foundations and specializations, reads course evidence, chooses courses, explains gaps, creates and edits HTML. | Expose the complete saved course catalog, paginate or apply explicit literal filters, validate IDs and quotes, preserve private memory, check saved choices and update book data. | Describes the purpose in their own words, adds or removes courses, confirms the choices, and reviews checked books and remaining links. |

**The hire path has no automatic candidate search, relevance ranking, top-N shortlist, priority assignment, or HTML generator.** A course is included only when the AI explicitly chooses its ID. Even if several courses address the same skill, code preserves every choice and its order. Quotes are mechanically checked, but relevance remains the AI's judgment. The older automatic matching CLI remains for compatibility; `/hire` does not use it.

## Three commands

| Command | Current behavior |
|---|---|
| `/hire` | One short question, AI-led catalog exploration and selection, approved editable HTML with AI-selected content, automatic ISBN search for confirmed books, and private original-source indexing with Google configured. |
| `/recall` | Embeds the query with Google Embedding 2 and searches this agent's original passages, page images, navigation and similarity links. |
| `/add` | Adds extra user-supplied knowledge to this agent. Confirmed education books are already indexed automatically; `/add` is not required for them. |

Set up the Google key once using `./scripts/topclass google` with its hidden prompt or a private plain-text `--key-file`. The host handles installation and connected-search setup. Topclass indexes original evidence without a reading model or generated summaries. Videos and audio remain unsupported for indexing. Queues preserve quota deferrals and failures; full coverage and retrieval quality must be reported from actual results. See [source memory](memory.md).

## A readable catalog for AI

The full saved usable course library is exported once per snapshot. Every hire can read the same public metadata; private knowledge is stored separately. A manifest provides subject and university directories with counts. There is one course per line in `courses.jsonl`, ordered by title and ID, with:

- Stable course ID, title, code and university.
- All supported subject fields, rather than a single required primary field.
- Full saved course descriptions, without clipped previews.
- Book identities, stated reading roles, edition uncertainty and source URLs.
- A file reference and hash for complete original course/material evidence.

Rows and source passages are data, not instructions. The manifest records scope, snapshot identity, hashes and record count. Bibliographic candidates do not prove current adoption or unread book teachings. The full usable library is still incomplete relative to universities' complete offerings; courses without documented books are not selectable education.

`catalog --agent ID` gives the directory. `browse --agent ID` returns pages with totals and `next_offset`; any course remains accessible, and the first page is not a list of best matches. The AI may request an exact subject or university filter, or a literal `--contains TEXT` filter across readable record text. Filters are explicit AI tool arguments; code never creates them from the user's purpose. The AI can also read/grep JSONL directly. `inspect --agent ID COURSE_ID ...` returns complete original evidence and valid quote field names.

## Using skills

docs/AGENTS.md routes `/hire`, `/recall` and `/add` to `docs/skills/`. Hosts reading these repository instructions can follow the commands directly. For native skills, install into the project:

```sh
./scripts/topclass skills --host codex
./scripts/topclass skills --host claude
```

Reload host skills afterward. Claude supports `/hire`; Codex supports `$hire` or its skills menu. Slash-menu availability is host-dependent. The installer refuses to overwrite existing skills and embeds the absolute checkout path. Reinstall explicitly if the checkout moves. `--destination /path/to/project` installs into another project. No background hook, service, key or paid provider is configured.

The [hire skill](skills/hire/SKILL.md) defines AI ownership of both matching and HTML editing. From the user's purpose, it creates a proposed education brief and explores the catalog itself. The AI considers foundations and specialist depth, preserving existing knowledge and exclusions. Inferred needs stay visible assumptions. A scope question is appropriate only when different plausible interpretations need materially different education. Examples illustrate free-form answers; they never define a supported-profession list or fixed curriculum. See [purpose examples](examples/hire-purposes.md).

## Local tool interface

Run from the checkout on Linux or macOS with Python 3.11+:

```sh
./scripts/topclass hire
./scripts/topclass hire --brief PRIVATE_BRIEF.json --name 'Game developer'
./scripts/topclass catalog --agent AGENT_ID
./scripts/topclass browse --agent AGENT_ID --contains 'database' --offset 0 --limit 25
./scripts/topclass inspect --agent AGENT_ID COURSE_ID
./scripts/topclass plan --agent AGENT_ID --proposal PRIVATE_PROPOSAL.json
./scripts/topclass check --agent AGENT_ID
./scripts/topclass finder --repo /absolute/path/to/search
./scripts/topclass select --agent AGENT_ID /path/to/saved-choices.json
./scripts/topclass books --agent AGENT_ID
./scripts/topclass add --agent AGENT_ID /path/to/paper.pdf --title 'Paper title'
./scripts/topclass recall --agent AGENT_ID --query 'the current task'
```

A bare interactive `hire` asks one question. In a noninteractive host tool it prints the question and exits without creating a hire. The host asks in chat and supplies the answer, preferably in its interpreted private brief. The description-only CLI can preserve literal capabilities, but it does not choose any courses. `--description-file` avoids shell interpolation of user text. `agents` lists names and IDs, and `status --agent ID` returns current paths. IDs are tool details; users receive helpful names and links.

The AI proposal has this structure (IDs and quotes below are illustrative):

```json
{
  "courses": [{
    "course_id": "EXACT_CATALOG_ID",
    "reviews": [{
      "course_id": "EXACT_CATALOG_ID",
      "requirement": "Exact capability description from the brief",
      "decision": "essential",
      "field": "description_variants",
      "quote": "Exact original source quotation",
      "rationale": "The AI explains why this teaching helps with the requested work."
    }]
  }]
}
```

Choose 10 relevant book-backed courses for an already capable LLM, or explain a genuine catalog shortfall. Each suggestion should add useful depth; avoid redundant basics and irrelevant filler. All suggestions start selected. The user adds or removes courses; there are no course priority tiers.

New proposal rows contain `course_id` and `reviews`. Evidence review decisions still distinguish direct `essential` support from `supplemental` foundations or partial support; these assess evidence, not selectable course tiers. The validator checks identity, quotes, requirements and named-book eligibility, not the truth of the AI's relevance reasoning. Legacy proposals and choices with priority fields remain readable for compatibility. New browser exports contain only course IDs.

`plan` writes the validated plan, candidate evidence and `planner-data.json`; it does not write HTML. The AI copies the returned `education_template` (`docs/templates/asterium.html`) into the private HTML destination and replaces only the validated DATA and role PROFILE JSON placeholders with script-safe escapes. `check` verifies data equality, the role profile, the initial 10-course recommendation (or explained shortfall), and every noneditable byte of the approved template. See [the presentation contract](presentation.md). Add/remove, book dialogs and snapshot-bound saved-choice import/export remain available. After `select`, update the exact DATA payload and preserve the valid profile in a fresh copy of the approved template. The tool distinguishes pending selection from pending HTML preparation.

## Private memory and durable choices

Each hire receives a random ID even when names or descriptions match:

```text
AGENTS_HOME/
  .catalogs/SNAPSHOT/catalog.json
  .catalogs/SNAPSHOT/courses.jsonl
  .catalogs/SNAPSHOT/evidence/COURSE_HASH.json
  AGENT_ID/
    agent.json
    state.json
    knowledge.db
    sources/
    pageindex/
    inbox/
    plans/PLAN_ID/
    selections/SELECTION_ID/
```

The graph starts empty with an ownership marker. Its schema supports source regions, knowledge records, semantic relationships, vectors and imported PageIndex trees; PageIndex working files have a separate private directory. Public course data never becomes learned memory by being selected. The large SQL course graph is not copied per hire.

Explicit agent IDs are required for reads and writes; there is no global active-agent fallback. Traversal, workspace symlinks, swapped ownership markers and source paths outside the agent's retained sources are rejected. This prevents accidental mixing through these tools; it is not a sandbox against manual filesystem changes or lower-level tool misuse.

Saved choices bind the agent, original AI plan and complete alternative pool. Another agent's choices, altered evidence and choices from a changed plan fail validation. Selection uses the saved snapshot even after the public catalog changes. Prior versions remain retained. Removing courses removes their book assignments. Empty choices explicitly remain incomplete. Books retain required/optional roles, unresolved editions, ISBNs and source links.

`/add` copies and hashes local files, preserves title/edition/origin, and deduplicates repeated bytes with matching metadata within one agent. It never executes material, retrieves remote URLs or changes instructions. The indexing worker retains the original in this agent's source store before it becomes a recall result. Graph, sources, page-index files and queue remain private per hire; Google accounting is coordinated across hires. Default private workspaces are outside the checkout; the previous in-repository location remains Git-ignored. Keep custom homes private.

The learning-book projection excludes explicit device/equipment records and exact iClicker remote product titles, even if a source marked them as books. It retains books about devices and textbooks with digital/access packaging. Source snapshots and their original classification/counts remain unchanged. New JSONL exports use a versioned learning-book policy so an older cached projection is not reused.

Confirmed selections automatically invoke the connected [book search](books.md). Its reports, logs and checked files stay under that agent’s selection directory. Search never runs during hire, plan, HTML checks or ordinary page editing. A missing local search setup does not undo confirmed choices. Retrieved files are not learned knowledge and are not placed into another agent’s inbox.
