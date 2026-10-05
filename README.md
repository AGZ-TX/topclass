# Topclass

**AI agents:** Read [the agent instructions](docs/AGENTS.md) before setup or changes.

Give your AI agent useful books for the work you need.

Tell the agent its purpose. It suggests university courses and books. You choose the courses on a web page. Topclass then finds available books and makes them searchable for that agent.

This is a fun, experimental project that helps agents "remember what they learned in school".

## Start with your agent

Download or clone this repository. Open the folder in Codex or Claude. Ask your agent:

> Install Topclass for this project. Then help me choose courses for an agent that develops my brand and guides its design.

Replace the example with your tasks and field of work. You can also use `$hire` in Codex or `/hire` in Claude.

1. Answer: “What is your agent's purpose?”
2. Review the course page. Your agent suggests 10 useful courses, or explains why it found fewer.
3. Add or remove courses. All suggestions start selected; there are no course tiers.
4. Press **Start**. Give the saved choices to your agent.
5. Ask your agent to confirm the choices. Topclass finds and indexes available books for that agent.

The course page uses the same layout for everyone. Your agent changes the course data, role title and role description.

| Command | Use it to |
| --- | --- |
| hire | Choose courses and books for a purpose. |
| recall | Search that agent's original text and page images. |
| add | Add extra books, papers or notes that you supply. |

Some books will be missing. Some editions will be uncertain. Your agent must report these gaps.

## Manual installation

Use **Python 3.11+** and **Node.js 20+**. Windows, macOS and Linux are supported. WSL is optional.

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

For Claude, change `codex` to `claude`. Open this project in your AI app and reload its skills.

Setup installs Python packages, the book finder, Chromium and three skills. It does not search for books or call a model.

Setup asks for a Google API key through a hidden prompt. Use `--skip-google` to choose courses before you set up a key. Indexing requires the key. Read the [setup guide](docs/setup.md) for updates and browser help.

## Your data

Each agent has separate books and search memory. Keys, books, browser data and personal plans stay outside the public repository.

Google receives text and page images to create embeddings. Embeddings help find related passages. They do not prove that the agent understands a book.

Your Google account controls quota and charges. Topclass adds no daily request, token, rate or spending caps. Read the [privacy guide](docs/privacy.md).

## The catalog

The public catalog includes course names, book details, dates and source links. It does not include saved university descriptions, source excerpts or textbooks.

Your agent can check the linked sources when needed and permitted. It must not invent course content from a title. The catalog is incomplete. Older records do not prove that a university still uses a book.

Clean installation checks passed for Codex and Claude on all three operating systems. Those checks do not prove that every book is available or that every AI app loads skills in the same way.

## License

Topclass code uses the [MIT license](docs/LICENSE). The included book finder keeps its [license](scripts/finder/LICENSE) and [source record](scripts/finder/origin.json).

Source links credit the universities and book providers. No university endorses Topclass. Read the [source notice](data/NOTICE.md).

## Files

`data/` holds the catalog. `docs/` holds guides, agent instructions, and the license. `scripts/` holds the code, launchers and dependency list. `.gitignore` keeps private files and generated files out of normal commits.

After updating an older checkout, use `./scripts/topclass` (Windows: `.\scripts\topclass.cmd`) and refresh previously installed skills using [the setup guide](docs/setup.md).
