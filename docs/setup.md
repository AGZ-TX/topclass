# Setup

Topclass runs on your device alongside an AI host with file tools. Python 3.11+, Node.js 20+ with npm, and Windows, macOS, or Linux are required. WSL is also supported. Browser verification may need a desktop session.

On macOS/Linux run `./scripts/topclass setup --host codex`. On Windows PowerShell run `.\scripts\topclass.cmd setup --host codex`. Use `--host claude` for Claude. Windows commands throughout this guide use `.\scripts\topclass.cmd` in place of `./scripts/topclass`. Setup creates `.venv`, installs Python indexing dependencies, prepares the bundled ISBN finder using its lockfile, installs Chromium, and installs host skills. It accepts your Google key through a hidden prompt. It does not search books or call Google. Reload your host’s skills afterward.

Use `--skip-google` to choose courses without embeddings. Run `./scripts/topclass google` when ready. A key may also come from `GEMINI_API_KEY` or a private plain-text `--key-file`; never pass the key value as a command argument. Noninteractive setup without a key reports the remaining Google setup step.

Add a backup without replacing your primary key with `./scripts/topclass google --fallback`. It accepts a hidden prompt, `GEMINI_API_KEY`, or `--key-file /private/path/backup`. Quota failures try the configured backup; keys from the same Google project share quota. Google enforces quota. Topclass imposes no daily request, token, rate or spending caps. Configured keys stay in owner-only private storage. Key setup changes credentials only; it does not start or resume any agent’s indexing jobs. Resume a chosen agent explicitly with `./scripts/topclass work --agent AGENT_ID`.

An existing search checkout is optional: `./scripts/topclass setup --search-repo /absolute/path/to/search`. The public checkout includes the necessary licensed source. `--skip-browser` leaves Chromium installation pending. If Chromium reports missing system libraries, follow Playwright’s OS-specific instructions; setup does not invoke sudo or silently change system packages.

Setup can be repeated. Matching skills are reused; different existing skills are not overwritten. `--destination /absolute/path/to/project` installs skills elsewhere. Moving the checkout requires reinstalling skills because they contain the executable location, not credentials.

## Updating an existing installation

Ask your agent to update the checkout with `git pull --ff-only`, then refresh Topclass's installed skills. Run setup for the same host and destination project used originally. For Codex those skills are in `PROJECT/.agents/skills/`; for Claude they are in `PROJECT/.claude/skills/`. They are project-local, so open that project in the host and reload its skills. Codex exposes `$hire` or its skills menu; Claude exposes `/hire`.

If setup reports that a destination skill already exists, compare the installed `hire`, `recall` and `add` folders with the Topclass copies first. Preserve any user customizations or unrelated skills. Move only these three Topclass skill folders to a backup outside the project, then repeat setup. Do not delete the entire skills directory. For an unmodified Codex installation whose destination is the checkout, on macOS/Linux:

```sh
git pull --ff-only
topclass_skill_backup=$(mktemp -d "$HOME/topclass-skills-backup.XXXXXX")
for topclass_skill in hire recall add; do
  if [ -d ".agents/skills/$topclass_skill" ]; then
    mv ".agents/skills/$topclass_skill" "$topclass_skill_backup/"
  fi
done
./scripts/topclass setup --host codex
```


On native Windows PowerShell, the equivalent backup-and-reinstall procedure is:

```powershell
git pull --ff-only
$topclassSkillBackup = Join-Path $env:USERPROFILE ("topclass-skills-backup-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $topclassSkillBackup | Out-Null
foreach ($topclassSkill in @("hire", "recall", "add")) {
  $topclassSkillPath = Join-Path ".agents\skills" $topclassSkill
  if (Test-Path $topclassSkillPath) {
    Move-Item -LiteralPath $topclassSkillPath -Destination $topclassSkillBackup
  }
}
.\scripts\topclass.cmd setup --host codex
```

For Claude, use `.claude/skills` and `--host claude`. If the skills were installed in another project, move them from that project's skill directory and pass the same `--destination /absolute/path/to/project` to setup. Keep the backup until installation succeeds. If setup fails, restore the backed-up folders before retrying. Existing Google configuration is reused when no replacement key is supplied. Private agents, books and memory are not removed by this procedure. Older generated HTML retains its previous template; have the agent rebuild it from its validated planner data with the new template and run `check --agent ID`.

## Platform requirements

Use the native terminal on Windows, macOS or Linux. Windows uses `.venv\Scripts\python.exe`, the `topclass.cmd` launcher and native file locks; macOS/Linux use `.venv/bin/python` and Unix locks. WSL is an optional Linux environment, not a Windows requirement. Paths with spaces work, but quote absolute paths in commands. Browser dependencies differ by operating system; installing Chromium does not establish that its system libraries are available. Use `--skip-browser` only when browser installation is deliberately deferred.

The clean installation check records passing native Windows, macOS and Linux setup for both hosts, browser startup and repeat installation. The manual installation workflow runs actual Windows, macOS and Linux runners with Python 3.11 and Node 20. See the report for the tested revision and outcomes. Live host-session skill discovery remains a separate check.

New Linux and WSL workspaces use `$XDG_DATA_HOME/topclass/agents`, defaulting to `~/.local/share/topclass/agents`. macOS uses `~/Library/Application Support/Topclass/agents`. Native Windows uses `%LOCALAPPDATA%\Topclass\agents`, normally under the signed-in user’s profile. `TOPCLASS_HOME` or the global `--home PATH` flag selects private storage. Existing `derived_private/agents` installations are discovered without moving books, keys, or graph paths. Custom workspace locations inside the checkout are rejected unless they use the ignored legacy directories. Keep external locations private.

The flow is `/hire` → answer the purpose question → review HTML → confirm and return choices. The host invokes `select`; Topclass automatically finds and queues those books. Google setup enables indexing. `/add` is only for extra material. `/recall` resumes pending work and searches completed passages. A stopped computer cannot continue processing.

Some sources require local browser verification. The host can run `npm --prefix scripts/finder run browser-setup`, then retry `./scripts/topclass books --agent ID`. Source restrictions and availability may still leave gaps. Choices and prior results remain saved. Hash/format checks alone do not prove a file is the requested textbook.
