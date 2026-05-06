# content/hub-docs/

Source files for the Hub Reference Library — the curated set of PDF
field manuals served by the captive portal at Seattle Emergency Hubs.

The build tool (`scripts/build_hub_docs.py`) consumes everything in
this directory and produces a single zip distributable to nodes. See
`docs/hub-reference-library.md` for the full design.

## What's in here

- `manifest.toml` — the list of documents to ship, plus the banner
  text that appears in the UI. Checked into git. **This is the file
  you edit.**
- `*.pdf` — the actual PDF files referenced by the manifest.
  **Gitignored.** You save these manually from Drive into this
  directory; they never get committed.

## Add or update a document

1. **Save the PDF** from Drive (or wherever) into this directory.
   Use a slugified filename — lowercase, hyphens, no spaces or
   punctuation:

   - `pet-first-aid-cpr.pdf` ✓
   - `Pet First Aid & CPR.pdf` ✗

2. **Edit `manifest.toml`** to add (or update) the entry. The shape
   is one `[[doc]]` block per document:

   ```toml
   [[doc]]
   category = "First Aid & Medical"
   title    = "Pet First Aid & CPR"
   file     = "pet-first-aid-cpr.pdf"
   lang     = "en"
   ```

   Documents with the same `category` value are grouped under that
   heading in the UI. Categories appear in the order they first show
   up in the file. Documents within a category appear in the order
   their `[[doc]]` blocks appear. Moving a doc is moving its block.

   For language variants of the same document, write the language
   into the title to disambiguate (the system displays titles
   verbatim and adds a small badge):

   ```toml
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
   ```

3. **Validate** from the project root before building:

   ```
   uv run python scripts/build_hub_docs.py \
       --source content/hub-docs/ \
       --validate
   ```

   This checks that the TOML parses, that every required field is
   present, that every referenced PDF exists, and that every PDF is
   actually a PDF. No zip is produced. Errors include enough context
   to find the problem.

4. **Build** when validation passes:

   ```
   uv run python scripts/build_hub_docs.py \
       --source content/hub-docs/ \
       --out out/
   ```

   Output is `out/hub-docs-<release_id>.zip`. The `<release_id>` is a
   UTC timestamp (`YYYYMMDDTHHMMSSZ`); same-day rebuilds produce
   distinct zips by design.

## Edit the banner copy

The banner attribution and intro text shown on the Reference page
come from the top of `manifest.toml`:

```toml
title = "Hub Reference Library"
source_label = "Seattle Emergency Hubs"
note = "Mirrored from printed Hub handouts. Tap any document to read or download as PDF for offline use."
```

`source_label` and `note` are required.

`title` is **optional**: the library's display name in the captive
portal (the tile label and the page heading). Must be a non-empty
string after trimming. If absent, the SPA falls back to a humanized
form of the URL slug — for `hub-docs` that's "Hub Docs", which is
usually less polished than what you'd write yourself.

## Bump the "last reviewed" date

The UI shows when each document was last reviewed. That date is
derived from the source PDF's mtime. To mark a document as freshly
reviewed without changing its contents:

```
touch content/hub-docs/<filename>.pdf
```

Then rebuild.

## Remove a document

Delete its `[[doc]]` block from `manifest.toml` and (optionally)
delete the PDF from this directory. The next build won't include it.

## What happens after the build

The zip in `out/` is what gets shipped to nodes. The operator who
deploys runs:

```
scp out/hub-docs-<release_id>.zip user@some-node:/tmp/
ssh user@some-node \
    "sudo -u civicmesh civicmesh install-hub-docs /tmp/hub-docs-<release_id>.zip"
```

Each new install replaces the previous library atomically; the web
server doesn't need to restart.

## Don't commit PDFs

The repo's `.gitignore` ignores `content/hub-docs/*.pdf`. If you find
yourself fighting git to commit a PDF, stop and ask. PDFs ship via
the build → install pipeline, not via git.
