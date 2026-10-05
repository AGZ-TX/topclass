# Topclass

Give your AI agent an education.

Tell Codex or Claude what you need an agent to do. It suggests ten university courses and their books. Add or remove courses, then confirm your choices. Topclass finds available books and builds a private library that your agent can search.

This is an experimental project. Some books will be missing, and course records may list older editions.

## Get started

Download or clone this repository and open it in Codex or Claude. Ask your agent:

> Read docs/AGENTS.md and install Topclass. Help me choose courses for an agent that develops my brand and guides its design.

Replace the example with your own task. Once setup is complete, use `$hire` in Codex or `/hire` in Claude.

Your agent asks for its purpose and prepares a course page with ten suggestions. If the catalog has fewer useful courses, it explains the gap. Add or remove courses, press **Start**, and return the saved choices to your agent. Your agent confirms the list, then Topclass searches for the books and indexes the files it can obtain.

| Command | What it does |
| --- | --- |
| `hire` | Suggests courses and books for your agent's purpose. |
| `recall` | Searches that agent's original text and page images. |
| `add` | Adds extra books, papers, or notes that you supply. |

AI agents working in this repository must read [docs/AGENTS.md](docs/AGENTS.md).

## Manual setup

You need Python 3.11+ and Node.js 20+. Topclass supports Windows, macOS, and Linux.

```sh
git clone https://github.com/AGZ-TX/topclass.git
cd topclass
```

On macOS or Linux:

```sh
./scripts/topclass setup --host codex
```

On Windows PowerShell:

```powershell
.\scripts\topclass.cmd setup --host codex
```

For Claude, replace `codex` with `claude`. Open the project in your AI app and reload its skills.

Setup installs the Python packages, book finder, Chromium, and host skills. It asks for your Google API key through a hidden prompt. Setup makes no book searches or model calls.

Use `--skip-google` if you want to choose courses before adding a key. You need the key to index books. The [setup guide](docs/setup.md) covers updates, skill refreshes, and browser issues.

## How the library works

Each agent has its own books and search memory. Google Embedding 2 processes original text and page images so the agent can find passages by meaning. A page index points back to the source pages. A similarity graph connects related passages for further reading. Those links do not establish facts or train the agent's model.

Your files stay in local private storage, outside the public repository. Text and page images go to Google for embeddings. Your Google account controls quota and charges. Topclass adds no usage or spending caps. See [source memory](docs/memory.md) and [privacy](docs/privacy.md).

## Catalog and license

The catalog contains course names, book details, dates, and source links. It includes no textbooks or saved university descriptions. Your agent uses the records to suggest courses and can check linked sources for more detail. A book title alone does not prove what it teaches.

Topclass uses the [MIT license](docs/LICENSE). The book finder retains its [license](scripts/finder/LICENSE) and [source record](scripts/finder/origin.json). The [source notice](data/NOTICE.md) explains the catalog's attribution and limits.

## Repository files

- `data/` contains the course catalog.
- `docs/` contains the guides, agent instructions, and license.
- `scripts/` contains the code, launchers, and dependencies.
- `.gitignore` excludes private files and generated files from normal commits.
