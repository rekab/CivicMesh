# Hub Reference Library — Design

**Status:** Draft. This document is the source of truth for the CIV-90
umbrella issue and is intended to be handed to Claude Code as input for
the three implementation sub-issues (build, install, UI).

**Scope.** Captive portal feature that serves a curated set of PDF field
manuals (water disinfection, emergency toilet directions, utility
shutoff, etc.) to walk-up users at a Hub. The PDFs are content owned by
external sources (Seattle Emergency Hubs, primarily); CivicMesh mirrors
them locally so they are reachable without internet. Companion to
`docs/captive-portal-precedent.md` (the surrounding portal model) and
`docs/civicmesh-tool.md` (the install/rollback CLI surface).

**Out of scope for this round.** Multi-format conversion pipeline
(`.docx` / `.xlsx` / `.pptx` → PDF via libreoffice). Google Drive API
auth, automated fetching, change detection. Multilingual UX as a
first-class feature — handled at build time via a `lang` field per row,
not at runtime.

**Vocabulary.** Throughout, the noun is **"hub-docs"** — the source
directory, the zip filename prefix, the directory inside the zip, the
installed directory, the URL path, and the config field all use this
exact spelling. Mixing in "library" anywhere is a bug in the doc;
please flag.

---

## 1. WHY

The captive portal already gives walk-up users read/write access to
mesh channels. That works for *current* information ("the gas main on
4th smells funny") but not for *reference* information ("how do I shut
off my gas?"). Reference content is stable, printed-handout-shaped,
and exists today as a Google Drive of PDFs maintained by Seattle
Emergency Hubs. Mirroring a curated subset onto the node turns the
captive portal from a messaging surface into a messaging surface
*plus* a takeaway field manual a neighbor can save to their phone
before they walk away from the AP.

Three properties matter:

1. **Read-only and curated.** Walk-up users cannot upload PDFs. The
   list is editorial. A captain edits a TOML manifest; a build
   runs; a zip ships.
2. **Optional.** A node with no hub-docs installed shows no hub-docs
   UI and serves no hub-docs files. The captive portal works exactly
   as it does today.
3. **Single-file distribution.** The hub-docs are delivered as one zip
   file. One install command swaps it into place atomically.

---

## 2. ARCHITECTURE

```
content/hub-docs/manifest.toml         (a) checked in, human-edited
content/hub-docs/*.pdf               (b) gitignored, captain's local saves
        │
        │ (c) scripts/build_hub_docs.py
        ▼
out/hub-docs-<release_id>.zip          (d) build artifact, gitignored
        │
        │ scp + civicmesh install-hub-docs
        ▼
<var>/hub-docs.releases/<release_id>/  (e) extracted, validated
        │
        │ atomic symlink swap (rename(2))
        ▼
<var>/hub-docs  →  hub-docs.releases/<release_id>/   (f) current release
        │
        │ static HTTP, no server-side rendering
        ▼
Browser
  GET /hub-docs/index.json             (g) JSON describes contents
  GET /hub-docs/<filename>.pdf         (h) Content-Disposition: attachment
```

The seam between every stage is the **zip artifact** plus the
**`index.json` schema** inside it. Both are specified below. Anything
the build tool can put in a valid zip, the install tool can install,
and anything the install tool installs, the UI can render.

The web server has no hub-docs-specific endpoints. It serves
`<var>/hub-docs/` as static files. The UI's Reference page is a
client-side render of `index.json`.

### Release ID

`<release_id>` is the `built_at` timestamp formatted as
`YYYYMMDDTHHMMSSZ` (always UTC, second-level granularity, no
punctuation). Example: a build at `2026-04-01T14:32:00Z` has
`<release_id>` of `20260401T143200Z`.

This format is used identically in:
- the zip filename: `out/hub-docs-20260401T143200Z.zip`
- the release directory: `<var>/hub-docs.releases/20260401T143200Z/`
- rollback target arguments: `civicmesh rollback-hub-docs --to 20260401T143200Z`

Second-level granularity exists so that multiple builds in a single
day (the realistic demo-prep workflow) do not collide on filename or
release directory.

---

## 3. THE CONTRACT: `index.json` SCHEMA

Every release zip contains exactly one `index.json` at
`hub-docs/index.json`. It is the contract between the build tool, the
install tool, and the UI. All three must agree on this shape.
`schema_version` exists so future installers can reject unknown
shapes.

```json
{
  "schema_version": 1,
  "built_at": "2026-04-01T14:32:00Z",
  "source_label": "Seattle Emergency Hubs",
  "note": "Mirrored from printed Hub handouts. Tap any document to read or download as PDF for offline use.",
  "categories": [
    {
      "name": "First Aid & Medical",
      "docs": [
        {
          "filename": "pet-first-aid-cpr.pdf",
          "title": "Pet First Aid & CPR",
          "lang": "en",
          "last_reviewed": "2025-08-12",
          "size_bytes": 421888
        }
      ]
    },
    {
      "name": "Sanitation",
      "docs": [
        {
          "filename": "emergency-toilet-instructions-es.pdf",
          "title": "Emergency Toilet — Instructions (ES)",
          "lang": "es",
          "published": "2024-09",
          "last_reviewed": "2024-10-02",
          "size_bytes": 317440
        }
      ]
    }
  ]
}
```

### Field semantics

| Field | Source | Notes |
|---|---|---|
| `schema_version` | constant in build tool | Increment only on breaking changes. v1 covers this doc. |
| `built_at` | build tool, UTC ISO-8601 | Surfaced in UI as "last sync …". Drives `<release_id>`. |
| `source_label` | manifest top-level key | Banner attribution. Pass-through from manifest, never derived. |
| `note` | manifest top-level key | Banner copy under the title. Pass-through from manifest, never derived. |
| `categories[].name` | manifest `[[doc]].category` | Categories grouped by build tool; order = first-appearance order in manifest. |
| `categories[].docs[].filename` | manifest `[[doc]].file` | Must end in `.pdf`, must be unique across manifest. Same name in zip and once installed. |
| `categories[].docs[].title` | manifest `[[doc]].title` | Display title. UTF-8, punctuation OK. Displayed verbatim by the UI. |
| `categories[].docs[].lang` | manifest `[[doc]].lang` | ISO 639-1. `en` renders no badge; other values render a 2-letter uppercase badge (`ES`, `ZH`, `VI`). |
| `categories[].docs[].published` | manifest `[[doc]].published`, optional | Date stamped on or implied by the source document, in `YYYY`, `YYYY-MM`, or `YYYY-MM-DD` form. Use for documents with dated content (frequency lists, dated maps); omit for timeless content. Distinct from `last_reviewed` — `published` is when the document was authored, `last_reviewed` is when the captain last touched it. Pass-through from manifest, never derived. |
| `categories[].docs[].last_reviewed` | derived: `os.path.getmtime` | Source PDF's mtime, formatted YYYY-MM-DD in UTC. Indicates when the captain last handled the file. To bump the date, `touch` the file. |
| `categories[].docs[].size_bytes` | derived: `os.path.getsize` | Verified by install tool against on-disk size. |

No `pages` field. The page count is not worth a `pypdf` dependency for
the marginal value of showing "2 pages" in the metadata sheet.
`size_bytes` is enough information for the user to gauge whether to
download.

### Validation rules (install tool enforces)

1. `schema_version` is recognized by the installed `civicmesh` version.
2. Every `filename` listed in `index.json` exists in `hub-docs/`
   inside the zip.
3. Every file's actual `size_bytes` matches the value in `index.json`.
4. Every PDF starts with `%PDF-` magic bytes.
5. No file in `hub-docs/` is *not* listed in `index.json` (no
   orphans).

Failure of any rule aborts the install before the symlink swap. The
running hub-docs are unaffected.

---

## 4. MANIFEST FORMAT

The manifest is a TOML file. TOML rather than markdown or CSV because
parsing collapses to one stdlib call (`tomllib.load`), error messages
with line numbers come for free, comments are supported natively, and
the schema is enforced by TOML's grammar rather than hand-rolled
parser rules. The manifest captures all editorial decisions —
including banner copy and source attribution — in one place.

`content/hub-docs/manifest.toml`:

```toml
# Top-level banner metadata, surfaced in the UI's Reference banner.
source_label = "Seattle Emergency Hubs"
note = "Mirrored from printed Hub handouts. Tap any document to read or download as PDF for offline use."

[[doc]]
category = "First Aid & Medical"
title    = "Pet First Aid & CPR"
file     = "pet-first-aid-cpr.pdf"
lang     = "en"

[[doc]]
category = "First Aid & Medical"
title    = "Psychological First Aid (Hub Volunteers)"
file     = "psychological-first-aid.pdf"
lang     = "en"

[[doc]]
category = "Water"
title    = "Disinfection of Drinking Water (EPA)"
file     = "disinfection-of-drinking-water.pdf"
lang     = "en"

[[doc]]
category = "Water"
title    = "Drinking Water from Your Water Heater"
file     = "drinking-water-from-your-water-heater.pdf"
lang     = "en"

[[doc]]
category = "Sanitation"
title    = "Emergency Toilet — Directions"
file     = "emergency-toilet-directions.pdf"
lang     = "en"

[[doc]]
category = "Sanitation"
title    = "Emergency Toilet — Instructions"
file     = "emergency-toilet-instructions.pdf"
lang     = "en"

[[doc]]
category = "Sanitation"
title    = "Emergency Toilet — Instructions (ES)"
file     = "emergency-toilet-instructions-es.pdf"
lang     = "es"
published = "2024-09"

[[doc]]
category = "Sanitation"
title    = "Emergency Toilet — Instructions (ZH)"
file     = "emergency-toilet-instructions-zh.pdf"
lang     = "zh"
```

The captain disambiguates language variants by writing them into the
title (`Instructions (ES)`). The system does no clever rendering; the
title is displayed exactly as written. The language badge in the UI
is additional, not a replacement.

### Schema

Top-level keys (all required):

| Key | Type | Notes |
|---|---|---|
| `source_label` | string | Banner attribution. Surfaced in `index.json`. |
| `note` | string | Banner copy under the title. Surfaced in `index.json`. |
| `doc` | array of tables | One entry per document to ship. |

Per-`[[doc]]` keys, required:

| Key | Type | Notes |
|---|---|---|
| `category` | string | Free text. Documents with the same `category` are grouped under that heading in the UI. |
| `title` | string | Display title. UTF-8, punctuation OK. Displayed verbatim by the UI. |
| `file` | string | Bare filename (no path components), must end in `.pdf`, must be unique across the entire manifest, must exist in the source directory. |
| `lang` | string | ISO 639-1 code (lowercase, two letters). `en` renders no badge in the UI; other values render a 2-letter uppercase badge. |

Per-`[[doc]]` keys, optional:

| Key | Type | Notes |
|---|---|---|
| `published` | string | Date stamped on or implied by the source document, in `YYYY`, `YYYY-MM`, or `YYYY-MM-DD` form. Granularity is the captain's choice — match what the source document actually states ("2025" for a year-stamped map, "2023-02" for "Feb 2023"). Mixed granularities across the manifest are allowed. Use for documents with dated content (frequency lists, dated maps); omit for timeless content. Distinct from `last_reviewed`: `published` is when the document was authored, `last_reviewed` is when the captain last touched it. |

### Build-tool validation (in addition to TOML grammar)

The build tool MUST enforce, post-parse:

1. All three required top-level keys are present and have the correct
   type.
2. Every `[[doc]]` has all four required keys with the correct type,
   and any present optional keys with the correct type.
3. No `[[doc]]` has unknown keys — anything outside the required and
   optional sets fails the build (defends against typos; no silent
   ignoring).
4. `file` values are unique across the manifest.
5. Every `file` exists at `<source_dir>/<file>` and starts with
   `%PDF-` magic bytes.
6. `lang` values match `^[a-z]{2}$`.
7. `published` values, when present, match one of `^\d{4}$`,
   `^\d{4}-\d{2}$`, or `^\d{4}-\d{2}-\d{2}$`. Format only; no
   real-world plausibility check.

Any failure aborts the build with a clear message naming the failing
`[[doc]]` (by index and title) and the violated rule.

### Order

Order in the manifest is order in the rendered UI:

- Categories appear in the order they first appear in any `[[doc]]`'s
  `category` field.
- Documents within a category appear in the order their `[[doc]]`
  blocks appear in the file.

There is no explicit `order` field. The file is the order. Moving a
document is a matter of moving its `[[doc]]` block.

### Source files

The PDFs themselves live alongside the manifest under
`content/hub-docs/`. The `content/` directory at the project root is
the convention for editorial material served by the captive portal —
each feature gets its own subdirectory (this one, plus the source
code browser tracked under CIV-89, plus any future additions). The
build tools, zip artifacts, and runtime paths remain feature-specific;
`content/` is purely an organizational convention for source files
checked into the repo, kept so a future non-Hub deployment of
CivicMesh that wants to host different materials has a clean place to
add them.

```
content/hub-docs/
├── manifest.toml                       (checked in)
├── pet-first-aid-cpr.pdf              (gitignored)
├── psychological-first-aid.pdf        (gitignored)
├── disinfection-of-drinking-water.pdf (gitignored)
└── ...
```

`.gitignore` for this feature:

```
content/hub-docs/*.pdf
out/
var/
```

Captains save the files manually from Drive (or wherever) into
`content/hub-docs/`. The build tool reads them by the `file` value in
the manifest.

`last_reviewed` for each document is derived from the source PDF's
mtime. To mark a document as freshly reviewed, `touch
content/hub-docs/<filename>.pdf`.

---

## 5. BUILD TOOL

The build tool is **standalone** — a script under `scripts/`, not a
subcommand of the `civicmesh` CLI. It runs only on a developer's
machine, never on a node, and has no overlap with node operational
concerns.

### Invocations

Build a zip:

```
uv run python scripts/build_hub_docs.py \
    --source content/hub-docs/ \
    --out out/
```

Validate without producing a zip (fast feedback during curation):

```
uv run python scripts/build_hub_docs.py \
    --source content/hub-docs/ \
    --validate
```

### Build output

`out/hub-docs-<release_id>.zip`, where `<release_id>` is
`YYYYMMDDTHHMMSSZ` from §2. If a zip with that exact name already
exists, fail (refuse to silently overwrite a release artifact —
should be very rare given second-level granularity, but a hard rule
nonetheless).

### Inputs

- `content/hub-docs/manifest.toml`
- `content/hub-docs/*.pdf` (one per `file` entry in manifest)

### Pipeline (build mode)

1. Parse `manifest.toml` with `tomllib.load`. Apply post-parse
   validation per §4 (required keys, unique filenames, lang format,
   etc.). Any failure aborts with a clear message.
2. For each `[[doc]]` entry:
   a. Open `content/hub-docs/<file>` for reading.
   b. Validate magic bytes start with `%PDF-`. If not, fail.
   c. Read `size_bytes = os.path.getsize(...)`.
   d. Read `last_reviewed = strftime("%Y-%m-%d",
      gmtime(os.path.getmtime(...)))`.
   e. Stage the file in a build temp directory at
      `hub-docs/<file>`.
3. Construct `index.json` from the manifest + derived facts. The
   `published` field, when present in the manifest, is passed through
   verbatim. Compute `built_at = utcnow()` and derive `<release_id>`
   from it. Group docs by category (in first-appearance order);
   preserve doc order within each category.
4. Write the temp directory to `out/hub-docs-<release_id>.zip.tmp`.
5. Rename `out/hub-docs-<release_id>.zip.tmp` to
   `out/hub-docs-<release_id>.zip`. **This rename is the atomic
   commit point** — an interrupted build leaves a `.tmp` file (which
   the operator can delete) and never a corrupt `.zip`.

Failure handling: any parse error, validation error, or missing file
aborts the build with a nonzero exit and a clear message. Partial
zips are never written.

### Validate mode (`--validate`)

Runs steps 1 and 2 above (manifest parse + post-parse validation,
per-file existence and magic-byte check) and exits. No staging
directory, no zip output, no `built_at` computation. Exit code 0
means the manifest and source directory are in a state where a real
build would succeed; nonzero prints the same errors a build would.

This is the fast inner loop while editing the manifest: edit, run
`--validate`, fix, repeat. Real builds are reserved for when
something is actually being shipped.

### Dependencies

- Python stdlib (`tomllib`, `zipfile`, `json`, `os`, `pathlib`,
  `argparse`, `time`)
- Nothing else. No `pypdf`, no `requests`, no Drive client.
- Requires Python 3.11+ for `tomllib` (the project's existing
  baseline; if this changes, swap for the `tomli` backport).

This is intentional. The build tool's risk surface should be
arbitrarily close to zero so that it stays out of the way and never
becomes the reason a release didn't ship.

---

## 6. ZIP DISTRIBUTION

The zip is the unit of distribution and the unit of release. It can be
built once on a dev machine and copied to any number of nodes.

```
hub-docs-20260401T143200Z.zip
└── hub-docs/
    ├── index.json
    ├── pet-first-aid-cpr.pdf
    ├── psychological-first-aid.pdf
    └── ...
```

The zip is opaque to the install tool until it has been extracted and
the `index.json` validated. Nothing in the zip is trusted by filename
alone; everything goes through the validation rules in §3.

Future build tools (libreoffice conversion, Drive API auth) produce a
zip with the same shape. The shape is the contract.

---

## 7. INSTALL PROCESS

### Invocation

Added to the `civicmesh` CLI per `docs/civicmesh-tool.md` conventions.

```
civicmesh install-hub-docs /path/to/hub-docs-<release_id>.zip
civicmesh rollback-hub-docs                # roll back to previous
civicmesh rollback-hub-docs --to <release_id>
```

In PROD, the typical operator flow is:

```
scp hub-docs-20260401T143200Z.zip user@some-node:/tmp/
ssh user@some-node \
    'sudo -u civicmesh civicmesh install-hub-docs /tmp/hub-docs-20260401T143200Z.zip'
```

### Filesystem layout

```
<var>/
├── hub-docs                          → hub-docs.releases/20260401T143200Z/
└── hub-docs.releases/
    ├── 20260315T093015Z/             (previous release, kept for rollback)
    └── 20260401T143200Z/             (current release)
        ├── index.json
        └── *.pdf
```

DEV `<var>` is `<project_root>/var/`. PROD `<var>` is
`/usr/local/civicmesh/var/`. The web server's `web.hub_docs_path`
config field points at `<var>/hub-docs` (the symlink, not a release
directory directly).

### Procedure

1. **Ensure `<var>/hub-docs.releases/` exists.** `mkdir -p` it.
   First install on a fresh node has nothing there; this step makes
   the install self-bootstrapping.
2. **Validate zip is a zip.** Refuse anything else.
3. **Peek at `index.json`** without committing to extraction. Parse
   `built_at` and derive `<release_id>` per §2.
4. **Extract** to
   `<var>/hub-docs.releases/<release_id>/.incoming/`. If
   `<var>/hub-docs.releases/<release_id>/` already exists outside
   `.incoming/` (i.e., a prior install used this exact release_id),
   fail.
5. **Apply all validation rules in §3** to the extracted contents. On
   any failure, `rm -rf` the incoming directory and abort.
6. **Promote incoming to release**: rename
   `<release_id>/.incoming/` to `<release_id>/`.
7. **Atomic symlink swap**:

   ```
   ln -sfn hub-docs.releases/<release_id> <var>/hub-docs.new
   mv -T <var>/hub-docs.new <var>/hub-docs
   ```

   `rename(2)` on a symlink is atomic. Open file handles in the web
   server (mid-download) survive because the kernel keeps the inode
   alive until the handle closes.

8. **Prune** old releases beyond the configured retention count
   (default keep last 3).

### Rollback

Same symlink-swap mechanism, target an older release directory under
`hub-docs.releases/`. No re-extract. Operator can `ls
<var>/hub-docs.releases/` to see what's available.

### Why symlink swap, not directory rename

A two-step rename of the directory itself
(`hub-docs/` → `hub-docs.previous/`, then
`hub-docs.incoming/` → `hub-docs/`) has a window between the two
operations where `hub-docs/` does not exist. A request that arrives in
that window 404s. The symlink-rename is genuinely atomic: either the
old target or the new target is current at every instant, never
neither.

---

## 8. UI BEHAVIOR

### Optional rendering

The Reference section is **invisible until populated.** On portal page
load, the SPA does:

```
fetch('/hub-docs/index.json')
  .then(r => r.ok ? r.json() : null)
  .then(idx => {
    if (idx) renderReferenceSection(idx);
    // else: render nothing. No empty state, no error.
  })
```

A node with no hub-docs installed returns 404 (no `index.json` to
serve), the SPA omits the section, and the captive portal is identical
to today's. This is the only place the UI cares about the hub-docs'
existence.

### Surface

Per the mockup attached to CIV-90:

- **Left nav** has a new `REFERENCE` group below `MESHCORE CHANNELS`,
  containing one tile: `Hub Reference Library`. Tile shows document
  count and the words "read & download".
- **Right pane**, on tile tap, shows:
  - Banner: title "Hub Reference Library", subtitle "Offline emergency
    documents", source attribution (`source_label`), the `note` text,
    document count, "last sync" date (from `built_at`).
  - Sectioned flat list, one section per category. Each row shows a
    PDF icon, title, a small language badge for non-`en` docs, and a
    metadata strip (last reviewed date, size).
- **Tap a row** opens a metadata bottom sheet with title, category,
  language, published date (when present), last reviewed, size,
  source, and a single `Download PDF` button. `published` is rendered
  above `last_reviewed` and only appears if the field is present in
  `index.json`. The body of the document is **never rendered inline**
  — only downloaded.

### Forced download

The web server adds `Content-Disposition: attachment;
filename="<filename>"` to responses for paths matching
`/hub-docs/*.pdf`. This is the only hub-docs-specific code path on the
server side. Everything else is `SimpleHTTPRequestHandler` doing what
it already does (mtime caching via `Last-Modified` /
`If-Modified-Since`).

### Why download-only

A neighbor walks up to the Hub, taps a couple of field manuals into
their phone's Files / Downloads app, walks away. The PDFs work an hour
later at home, when the AP is no longer in range. Inline view via the
browser's PDF viewer is transient — leaving the page or the network
loses the document.

---

## 9. DEV vs PROD

| | DEV | PROD |
|---|---|---|
| Manifest | `<project_root>/content/hub-docs/manifest.toml` | (not present — built artifact only) |
| Source PDFs | `<project_root>/content/hub-docs/*.pdf` | (not present) |
| Build script | `<project_root>/scripts/build_hub_docs.py` | (not present) |
| Build output | `<project_root>/out/hub-docs-*.zip` | (not present) |
| Hub-docs path (`web.hub_docs_path`) | `<project_root>/var/hub-docs` | `/usr/local/civicmesh/var/hub-docs` |
| Releases parent | `<project_root>/var/hub-docs.releases/` | `/usr/local/civicmesh/var/hub-docs.releases/` |

The build tool runs only in DEV. The install and rollback commands run
in either mode.

`var/` does not currently exist in DEV. The install command creates
it on first run via `mkdir -p`; it should be added to `.gitignore`
alongside `out/` and `content/hub-docs/*.pdf`.

---

## 10. RELEASE PROCEDURE (operator flow)

For Toorcamp and beyond, until/unless this is automated:

```
# 1. Save the curated PDFs into content/hub-docs/ from Drive,
#    matching the filenames listed in manifest.toml.

# 2. Edit the manifest if anything changed
vi content/hub-docs/manifest.toml

# 3. Quick-check the manifest is parseable and all files exist
uv run python scripts/build_hub_docs.py \
    --source content/hub-docs/ \
    --validate

# 4. Build the zip
uv run python scripts/build_hub_docs.py \
    --source content/hub-docs/ \
    --out out/

# 5. Inspect (recommended for first few releases)
unzip -l out/hub-docs-20260401T143200Z.zip
unzip -p out/hub-docs-20260401T143200Z.zip hub-docs/index.json | jq

# 6. Ship to each node
ZIP=out/hub-docs-20260401T143200Z.zip
for node in toorcamp-01 toorcamp-02 toorcamp-03; do
  scp "$ZIP" user@$node:/tmp/
  ssh user@$node \
      "sudo -u civicmesh civicmesh install-hub-docs /tmp/$(basename $ZIP)"
done
```

For a single node, the bash loop collapses to two lines.

---

## 11. SUB-ISSUES UNDER CIV-90

The umbrella CIV-90 issue tracks "demo-ready hub-docs at Toorcamp."
Three sub-issues hang off it. The schema in §3 is the contract; each
sub-issue can be developed against fixtures without the others
existing.

### CIV-91 — Build tool

Implements §5. Standalone script. Produces a valid zip per §6 from
the curated `manifest.toml` and PDFs in `content/hub-docs/`. Includes
both build and `--validate` modes.

Acceptance: given the 12 demo PDFs from `civ-90-mvp-demo-pdfs.md`
saved into `content/hub-docs/` and a written-out `manifest.toml`,
produces `out/hub-docs-<release_id>.zip`. A human can `unzip -p ...
hub-docs/index.json | jq` and see the expected structure. Every PDF
in the zip starts with `%PDF-`. `--validate` flags malformed
manifests with line numbers.

**Done first.** The contract (`index.json` shape, manifest grammar)
gets pinned by writing this. The other two sub-issues use real zips
as fixtures rather than hand-crafted JSON.

### CIV-92 — UI display

Implements §8. Adds the Reference section to the captive portal,
including the metadata bottom sheet and the forced-download endpoint
on the web server.

Acceptance: given a hand-extracted zip's contents in
`<project_root>/var/hub-docs/`, renders correctly per the mockup. With
no hub-docs installed, the section is invisible and the rest of the
portal is unchanged.

**Done second.** Once the build tool exists, this can use real zips
extracted by hand as fixtures, and this is what gates "demo-able" for
Toorcamp.

### CIV-93 — Install tool

Implements §7. Adds `civicmesh install-hub-docs` and
`civicmesh rollback-hub-docs` to the CLI per `docs/civicmesh-tool.md`
conventions.

Acceptance: given a zip from CIV-91, installs it onto a running node
without restarting `civicmesh-web`, with atomic visibility (no 404 mid
swap). Rollback to the previous release works without re-extract.
Validation failures abort cleanly without modifying the running
hub-docs. First install on a fresh node creates `<var>/` and
`<var>/hub-docs.releases/` itself.

**Done last.** Until it exists, "install" is `scp + ssh + unzip + ln
-sfn` by hand. Annoying but workable. If schedule pressure hits, this
is the slip-tolerant one.

---

## 12. EXPLICITLY DEFERRED

These items have come up in conversation and are deliberately not part
of CIV-90. Noted here so they don't get reintroduced as "obvious
additions" during implementation:

- **LibreOffice conversion of `.docx` / `.xlsx` / `.pptx` sources.**
  Future ticket. Same zip output shape.
- **Drive API auth, automated fetching, change detection.** Future
  ticket. Same zip output shape. For now, a captain manually saves
  files into `content/hub-docs/`.
- **Multilingual UX features beyond a per-row badge.** Language
  pickers, `Accept-Language` autodetection, per-topic language
  pages — none needed if the UI is a flat categorized list with
  badges.
- **Source-of-truth conflict reconciliation.** The new zip is truth.
  Replace wholesale; do not merge with the previous release's
  contents.
- **Inline PDF viewer.** Out of scope. Download only.
- **Search.** Out of scope. The list is short enough to scan.
- **Per-Hub manifest variation.** All Hubs install the same zip in
  v1. If a Hub needs a different list, that's a new ticket.
- **Page count in `index.json`.** Dropped. Not worth a `pypdf`
  dependency. `size_bytes` is sufficient.
- **Install concurrency / lockfile.** Single-operator system; two
  simultaneous installs on the same node are not a realistic failure
  mode.
- **System-aware title disambiguation.** Captains write the
  disambiguating text into the title themselves (e.g.,
  `(ES)`). The system displays titles verbatim and does no clever
  rendering.

---

## 13. KNOWN UNKNOWNS

- **Pi Zero 2W disk pressure with N retained releases.** 12 PDFs at
  ~10 MB total × 3 retained = 30 MB. Negligible. Worth re-checking
  when a future conversion-pipeline ticket lands and total payload
  grows.
- **Concurrent download under sequential `http.server`.** A handful
  of walk-ups simultaneously downloading PDFs will block the channel
  list briefly. Acceptable for v1; if real-world load demands it,
  swapping `SimpleHTTPRequestHandler` for `ThreadingHTTPServer` is a
  mechanical change.
- **mtime preservation through Drive download.** Captains saving PDFs
  from Drive will get an mtime that reflects the local download time,
  not the document's authored date. For v1 this is fine — "last
  reviewed" reasonably means "last time a captain looked at this and
  decided to ship it." If captains want the date to reflect Drive's
  modified-time, they can `touch -d` after saving. A future
  Drive-API ticket makes this automatic.
