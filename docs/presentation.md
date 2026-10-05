# Approved hire page

Course book lists consolidate checksum-valid ISBN-10 and ISBN-13 equivalents into one reading entry. The fuller author metadata is displayed, while all differing source records, reading roles, source links and finding aids remain available in bibliographic details. Different ISBNs and unidentified citations remain separate. This is presentation grouping, not permission to merge acquired files or claim an assigned edition was verified. New catalog snapshots use the ISBN-grouping evidence policy; raw catalog evidence is unchanged. The public sample applies the same grouping to saved public metadata. The reading popup shares the approved page's lavender text and dark glass surfaces.

`docs/templates/asterium.html` is the approved constellation design, adapted from Site version 42. Each hire receives its path as `education_template`. The older `education.html` remains for legacy report APIs; it is not the `/hire` template.

Fresh agents copy the template into the hire's private HTML destination and replace exactly two placeholders using file tools:

| Placeholder | Allowed content |
| --- | --- |
| `__PLANNER_DATA__` | Exact JSON returned by `plan` or `select`, including its evidence, recommendation, manifest and catalog views. Never hand-edit these claims or fingerprints. |
| `__AGENT_PROFILE__` | JSON with only `title`, `description`, and `shortfall`. Title: 2–3 role words, at most 80 characters. Description: 1–2 short sentences describing tasks, responsibilities and specialties, at most 300 characters. Shortfall: empty for 10 recommendations; otherwise explain fewer relevant choices in at most 300 characters. |

Escape `<`, `>`, `&`, U+2028 and U+2029 as JSON Unicode escapes. Do not change whitespace or text outside the payloads. The template renders content with text nodes and validates HTTP(S) links. Reference ISBNs and finding sources appear as readable reference-edition panels with assigned-edition uncertainty; verified course-code aliases participate in search. Profiles are presentation text; the original user purpose and capability evidence remain in the validated plan.

The AI recommends 10 distinct useful book-backed courses, with source-anchored reviews and no user-facing tiers. Catalog shortages may justify 1–9 recommendations with an explicit shortfall. Never pad with irrelevant courses. AI-reviewed alternatives belong in the validated proposal. Add Knowledge also searches the complete saved book-backed catalog, preserving exact source evidence but assigning no task-fit credit to unreviewed courses. This browsing pool does not change the AI's ten recommendations. The portable DATA contains the readable catalog and validated plan, not unused backend evidence-pool and per-priority book-view copies. Full evidence stays in the private catalog; catalog-only candidates keep its file/hash and their source material records. Code exposes and validates choices; it does not select the ten. User add/remove controls can produce any final count, including zero. Exported choices retain the validated plan and snapshot fingerprints and work with `topclass select`.

The course grid keeps two columns on desktop and tablet, switching to one only at phone widths up to 480px. It reserves a separate gutter with at least 24px between desktop cards and an always-visible track and draggable thumb synchronized to the native scroll region. Keyboard and wheel scrolling still use the focusable grid. On compact screens the grid has a bounded scrolling region while the page can also scroll. The visual cue does not depend on the operating system's overlay-scrollbar preference.

`topclass check --agent ID` compares both embedded data and all noneditable bytes with the repository template, validates the role profile, and checks the initial recommendation count. It rejects changed CSS, scripts, markup, static copy and fingerprints. A page that fails must be repaired from the template; never weaken validation to pass it. This is local validation, not a sandbox preventing a host with filesystem access from editing files. Open needs are recomputed from validated reviewed-support links whenever courses change, including partial support, empty imports, and reopened selections. Unreviewed catalog additions cannot close those needs. Browser checks remain necessary for visual and interaction behavior.

Maintainers may update the template or contract when the user explicitly requests a design or behavior change. Existing private pages may need rebuilding from the current template before the next check. Do not rewrite other hires or publish private education. Report book-finding results in the private book report and chat rather than adding arbitrary page sections.

Installed skills reference this checkout. New skill installations receive the updated instructions; existing installed copies do not refresh automatically. Reinstall an existing skill deliberately using the documented setup process before relying on these limits in another host.

## Browser regression checks

The canonical-template browser test uses an optional privately installed Playwright package and Chromium. It checks the initial grid/scrollbar cue, dragging, catalog alternatives, add/remove round trips, book dialogs, stale-selection rejection, card bounds and row overlap at narrow widths, and empty-selection export, then validates exported choices with Python. No books or provider calls are needed:

```sh
TOPCLASS_PLAYWRIGHT_MODULE=/absolute/private/node_modules/playwright \
  PYTHONPATH=scripts:scripts/tests python -m unittest test_presentation -q
```

`TOPCLASS_CHROMIUM` can select a different installed Chromium binary. Legacy jsdom tests remain separate.

## One public preview implementation

The hosted sample is filled from this same canonical template. `scripts/preview.py` is a maintenance helper for public sample metadata only; it never authors a private hire. It reads the current public `courses.jsonl` export and sibling `catalog.json`, uses the ten saved demo choices in `docs/templates/asterium-demo.json`, and checks exact template parity before writing the sample HTML. It does not copy an agent's brief, plans, private materials or memory. The sample identifies itself and exports the same choice schema, bound to its own public sample snapshot. A sample export is not a personal hire's confirmation.

```sh
python scripts/preview.py --catalog /absolute/public-catalog/courses.jsonl --out /absolute/site/dist/index.html
```

Changing layout or behavior means changing the canonical template and rebuilding the sample. Do not maintain a second preview script with different search, priorities, bibliography or confirmation behavior. `test_preview` enforces the approved-template and sample-data contract; the canonical browser tests compare visible coverage against Python selection results.

The saved-choices file button is removed at the user’s request. The selection import API remains available for host workflows. Course rows use intrinsic maximum content sizing so headings, priorities and controls stay within their card and successive rows do not overlap.

Cards share three subgrid tracks per row: heading, description and controls. Shared tracks adapt to the taller content in either column, align descriptions and controls, and give both cards equal outer height without fixed clipping dimensions.

New proposal rows omit course priority; new browser choice rows contain only `course_id`. Every default suggestion begins selected. Add and remove change only membership. Legacy priority-bearing imports are validated, then normalized to membership-only exports. Source reading roles and evidence support strength are preserved; they do not create course tiers.
