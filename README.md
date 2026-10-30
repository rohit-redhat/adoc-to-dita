# AsciiDoc → DITA

A local tool for three jobs:

1. Paste or upload AsciiDoc and copy/download validated DITA XML.
2. Compare two Git branches, tags, or commits and download the affected topics, before/after XML, and readable diffs.
3. Paste a public GitHub pull request or GitLab merge request URL and download DITA XML for every changed or dependency-affected topic.

See [TESTING.md](TESTING.md) for automated coverage and pinned live results against `openshift/openshift-docs`.

Supports **concept**, **task (procedure)**, and **reference** topics. No Vale, AI service, AEM connection, account, or database is required. The browser interface runs on your computer; GitHub or GitLab access is only used when you choose a remote repository.

## Start

Prerequisites: Git, Python 3.10+, Ruby 3.2+, and Bundler. macOS and Linux are supported; use WSL on Windows.

```sh
git clone https://github.com/gtrivedi88/adoc-to-dita.git
cd adoc-dita
./scripts/setup.sh
./adoc-dita serve
```

Open **http://127.0.0.1:8765**. Setup downloads pinned dependencies. Subsequent standalone conversions and comparisons of locally available Git objects work offline.

### Hosted deployment

The same conversion and comparison features run in a container or OpenShift deployment. Set `ADOC_DITA_BIND_HOST=0.0.0.0` and list the public route host in `ADOC_DITA_ALLOWED_HOSTS`. A hosted browser cannot read paths on a visitor's computer, so users upload local source and attributes files through the form. Network-bound API requests reject server filesystem paths. Release comparison and pull request conversion accept public GitHub and GitLab URLs and need outbound DNS and HTTPS access to those hosts.

Comparison jobs are held in process memory. Run one replica unless job state is moved to shared storage; restarting the pod ends active jobs. The application has no built-in login, so put authentication and access controls in front of it when the route is not intended to be public.

### Convert content

Paste the complete module, or upload an `.adoc` file. Select a type, or use **Detect from content**:

| AsciiDoc metadata | Output |
| --- | --- |
| `:_mod-docs-content-type: CONCEPT` | `<concept>` with `<conbody>` |
| `:_mod-docs-content-type: PROCEDURE` | `<task>` with `<taskbody>` |
| `:_mod-docs-content-type: REFERENCE` | `<reference>` with `<refbody>` |

If metadata is absent, filenames starting with `con-`, `proc-`, and `ref-` identify the type. Otherwise, select the type explicitly. The tool does not infer a document's meaning from its prose. `[role="_abstract"]` marks the short description. Procedures use `.Prerequisites`, `.Procedure`, and `.Verification` blocks.

The `examples/` directory includes representative modules, the supplied reference XML, and the RBAC concept and LDAP task source modules. `reference.adoc` demonstrates the supplied XML vocabulary; it is a shorter illustrative source, not a reconstruction of the entire supplied file.

The browser conversion form requires only the AsciiDoc content. An attributes file is optional: choose the `.adoc` file directly or enter its full local path. It loads shared definitions directly; it never activates a project or guide selector. If a supplied path belongs to a local Git clone and the pasted topic has one unambiguous source match, the source path is used quietly as the base for local snippets, includes, and topic links. The pasted and uploaded content remains in memory and the clone is never modified.

Many modular documentation sources use an ID such as `[id="configuration_{context}"]`, where an assembly normally supplies `context`. Standalone conversion deterministically emits the base ID `configuration`. The same rule applies to structural xref targets. Set `context=admin-guide` under **Attribute overrides** when the desired output is `configuration_admin-guide`. Uses of `{context}` in prose are still reported as missing; content attributes are never guessed.

This workflow is generic. It contains no hard-coded RHDH contexts, guide names, products, or directory layout. The `:_mod-docs-content-type:` convention is supported for modular documentation, and explicit topic-type selection supports repositories that use other conventions.

**Attribute overrides** accepts `name=value`, one per line, and takes precedence over file definitions. Use these to choose a build variant or provide values normally supplied by an external build configuration:

```text
product=Red Hat Developer Hub
product-short=Developer Hub
context=standalone
```

Without an attributes file, pasted and uploaded files are self-contained. An attributes file outside a Git clone is read directly, with includes confined to its folder. Missing documentation attributes in prose are listed once in the UI; placeholders inside inline code, code blocks, paths, commands, and configuration examples are preserved literally and are not reported as missing attributes. Unsupported content produces diagnostics instead of silently disappearing. Existing explicit IDs are preserved. Files without an ID receive a stable ID derived from their logical source path.

The CLI retains an advanced `--repository --guide` mode for builds that explicitly need inherited guide attributes and a publication-wide link registry. It is separate from the browser’s paste workflow. The converter does not run repository scripts or infer externally supplied build flags.

### Compare releases

Enter a public GitHub or GitLab repository URL or a local Git repository path, then select two branch names, tags, or commit IDs. **Load releases** populates suggestions. GitLab nested groups are supported (`https://gitlab.com/group/sub/project`). Default GitLab hosts are `gitlab.com` and `gitlab.cee.redhat.com`; add more with `ADOC_DITA_GITLAB_HOSTS=host1,host2`. Local repositories may use `origin/release-1.9` when that branch has not been checked out locally. Local repositories are not fetched automatically; run `git fetch` yourself when needed.

Topic paths are optional. Leave the field blank to inspect all `.adoc` topics in both snapshots, map direct Git changes and dependency effects, and include only topics whose source or generated XML changed. To narrow the run, patterns use Python `fnmatch`: `*` also matches subdirectories, and the field accepts several comma-separated patterns. The scope is individual typed modules, not DITA map or book generation.

As with pasted standalone topics, a missing structural `_{context}` or `-{context}` suffix is removed from topic IDs and xref targets during comparison. Define `context=value` only when the release output must retain a specific guide context. Uses of `{context}` in prose remain unresolved and are reported rather than guessed.

The comparison is **baseline snapshot → target snapshot**, not a pull-request merge-base comparison. The selected refs are resolved to commit IDs and recorded in the report. Your checkout and branches are never switched or modified.

Every selected topic is converted at both commits. This catches changes caused by included snippets, code files, conditions, and shared attributes even when a topic's own source is unchanged. Topics with identical source and XML are omitted. A changed source file with unchanged XML (for example a comment edit) remains in the report.

Set **Guide entry file** to resolve attributes and topic links independently from the guide in **each snapshot**. Only matching topics included by that guide in either snapshot are compared. A topic with multiple active inclusions must be converted individually with an inclusion selection; a missing inclusion is reported as unavailable rather than silently choosing a context. Without a guide, `artifacts/attributes.adoc` is automatically loaded when present. You can enter one attributes file path relative to the repository so that each release uses its own version, or upload one `.adoc` attributes file to apply the same definitions to both releases. Uploaded comparison attributes cannot contain includes because they have no repository-relative directory; use the repository path option when an attributes file has an include chain. Explicit overrides take precedence over every attributes file.

Download the ZIP and open `report.html`, or inspect `report.json`:

```text
report.html                 Readable comparison
report.json                 Commit IDs, settings, diagnostics, line ranges
before/modules/example.xml  Valid baseline XML, when available
after/modules/example.xml   Valid target XML, when available
diffs/...source.diff        AsciiDoc unified diff
diffs/...xml.diff           XML unified diff
diffs/...source.no-change.txt  Topic included without a content-line change
diffs/...xml.no-change.txt     Conversion succeeded; generated XML is identical
diffs/...xml.unavailable.txt   Conversion failed; diagnostics explain why
diffs/README.txt               Meaning of every diff artifact
```

Additions, deletions, and Git-detected renames are reported. A successfully converted addition has a complete XML unified diff from `/dev/null` to the new `.xml` file. Deleted topics only have baseline XML. A conversion failure is explicitly marked as unavailable; it is never represented as an XML deletion. The bundle does not write empty diff artifacts: a `.no-change.txt` or `.unavailable.txt` file explains why no patch exists. Changed snippets and files without a title are listed as dependencies/non-topics rather than emitted as invalid standalone documents.

**Source and XML hunks are separate diffs, not a one-to-one source map.** Their ranges refer to the exact files in this run. Inserts and deletions have a zero-length side with an `after_line` insertion point. AEM editor line numbers and synchronization are outside Phase 1.

### Convert a pull request

Open **03 Convert pull request** and paste a public GitHub URL such as `https://github.com/owner/repository/pull/123`, or a GitLab merge request URL such as `https://gitlab.com/group/project/-/merge_requests/123`. The tool resolves the change request through the host API, pins the exact base and head commits, discovers directly changed and dependency-affected `.adoc` topics, and runs the same conversion and validation pipeline used by the other workflows. It does not execute code from the repository.

Leave **Topic paths** blank to process the complete PR/MR. The attributes controls behave like release comparison: use a repository-relative path to read the file independently at the base and PR commits, upload one `.adoc` file to apply it to both, or rely on automatic `artifacts/attributes.adoc` detection. Each successful topic exposes its complete target XML in the page. The ZIP contains target XML under `after/`, base XML when available, source and XML diffs, `report.html`, and `report.json`.

The report records the PR/MR number, title, URL, exact merge-base/head commit IDs, the current base tip, and conversion settings. Using the merge base makes the result match the changes introduced by the request even when its base branch advances. If the head changes while a job is starting, the run stops instead of mixing snapshots. A hosted deployment requires outbound HTTPS access to GitHub (`api.github.com`, `github.com`) and/or your GitLab host. Set optional `GITHUB_TOKEN` or `GITLAB_TOKEN` environment variables for private repositories and higher API rate limits; tokens are never included in reports.

Self-hosted GitLab (for example `gitlab.cee.redhat.com`) often uses a private CA. The tool relaxes TLS verification for those hosts unless you set `ADOC_DITA_CA_BUNDLE` / `SSL_CERT_FILE` to your corporate CA bundle, or force verification with `ADOC_DITA_SSL_VERIFY=1`. Public `github.com` and `gitlab.com` keep certificate verification enabled.

## CLI

```sh
# Convert a complete module; write XML to stdout by default.
./adoc-dita convert examples/reference.adoc -o results/reference.xml

# Paste through stdin and explicitly select a topic type.
printf '= Introduction\n\nHello *world*.\n' | ./adoc-dita convert --type concept

# Paste through stdin and load the same attributes file used by the browser.
cat modules/con-application-configuration-file.adoc | ./adoc-dita convert - \
  --filename con-application-configuration-file.adoc \
  --attribute-file /path/to/docs/artifacts/attributes.adoc \
  -o results/application-configuration-file.xml

# Resolve local includes and attribute files inside a chosen root.
./adoc-dita convert examples/proc-share-a-secret-with-ldap.adoc \
  --root examples --attribute-file attributes.adoc -o results/ldap.xml

# Compare a local documentation repository.
./adoc-dita compare /path/to/rhdh \
  --base origin/release-1.9 --target origin/release-1.10 \
  --include 'modules/*.adoc' \
  --attribute-file /path/to/attributes.adoc \
  -o results/release-1.9-to-1.10

# Use any repository's guide entry document for a single topic or a release diff.
./adoc-dita convert /path/to/docs/topics/configuration.adoc \
  --repository /path/to/docs --guide guides/admin.adoc --type concept
./adoc-dita compare /path/to/docs --base v1 --target v2 \
  --guide guides/admin.adoc --include 'topics/*.adoc' --type concept \
  -o results/admin-v1-to-v2

# Or compare public GitHub branches directly.
./adoc-dita compare \
  https://github.com/redhat-developer/red-hat-developers-documentation-rhdh \
  --base release-1.9 --target release-1.10 \
  --include 'modules/*.adoc' \
  -o results/github-comparison

# Convert all changed and impacted topics in a public GitHub pull request.
./adoc-dita pull-request \
  https://github.com/redhat-developer/red-hat-developers-documentation-rhdh/pull/123 \
  --attribute-file artifacts/attributes.adoc \
  -o results/pr-123
```

Use a new/empty comparison output directory to avoid mixing runs. `convert --json` returns XML and diagnostics together. Exit status: `0` successful (possibly with review warnings), `1` invocation/Git/setup error, `2` one or more conversion failures. Comparisons still save successful XML and diagnostics when some topics fail.

For CLI comparisons, a repository-relative `--attribute-file` path loads that file independently from each release. One absolute `--attribute-file` path reads a local file once and applies those definitions to both releases, matching the browser upload behavior.

## Supported content and limits

See [SUPPORTED.md](SUPPORTED.md) for the audited element matrix, including the supplied plugin topic and map files.

- Paragraphs, emphasis, inline code, external links, lists and definition lists, tables, images, code blocks, notes/warnings, short descriptions, and procedure sections use the upstream conversion rules.
- Native Asciidoctor handles relative includes, include tags/line ranges, attributes, and conditional content. Include paths resolve relative to the current file first, then relative to the selected input root (for modular paths such as `artifacts/snippet.adoc`). Includes stay inside the selected input root. Remote includes and symlinks outside the root are rejected. Git snapshots preserve internal include aliases and omit symlinks that leave the snapshot.
- Nested section structures and other constructs that cannot be converted safely return an error. There is no claim that arbitrary AsciiDoc can always become valid specialized DITA.
- Converter warnings that indicate possible content loss block output. Cross-reference warnings and standalone `+` paragraphs remain available for review. The application never removes a literal `+` automatically.
- All successful XML is validated against vendored **DITA 1.3 plus Errata 02** XSDs, with additional ID checks. Validation is structural; it does not verify technical correctness, external URLs, or whether referenced image assets exist.
- Local topic references are changed to `.xml`. Cross-file references with fragments may need review because a complete publication-wide ID/key registry is not part of this phase.
- Images are referenced, not uploaded or copied. DITA map/bookmap generation, AEM metadata and identity, publishing, and content synchronization are deferred.
- Browser input is limited to 2 MB. Remote access supports HTTPS GitHub repositories; for private repositories, use an authenticated local clone. GitHub snapshots are cached in `.cache/repos/` and can be removed when the application is stopped.

## Determinism

With the same source snapshots, attributes, conversion settings, and runtime/toolchain, XML and report ZIPs are reproducible. Dependencies are pinned; paths and attributes are ordered; generated IDs are stable; ZIP entry timestamps are fixed. No LLM, random topic IDs, or build timestamps are used. Asciidoctor's implicit date/time attributes are fixed to the Unix epoch; supply explicit release dates as your own source attributes.

Whitespace in code blocks and mixed XML content is preserved. Formatting may differ from hand-authored XML. The goal is equivalent valid content, not byte-for-byte reproduction of previously migrated XML.

## Development and validation

```sh
PYTHONPATH=. .venv/bin/python -m unittest discover -s tests -v
```

Tests exercise the CLI as subprocesses for file, stdin, and branch-comparison workflows, plus all three topic types, the supplied plugin-topic vocabulary, standalone context IDs, automatic local snippet resolution, the LDAP task and RBAC concept, escaping, code whitespace, missing attributes, include confinement, tagged/conditional includes, type selection, additions/deletions/renames, dependency changes, invalid output handling, and repeatable XML/ZIP results. An inactive GitHub Actions template is provided at `ci/github-actions.yml`. To enable automatic tests on pushes and pull requests, use a GitHub login with workflow permission and copy it to `.github/workflows/test.yml`.

Implementation: a Python CLI/local HTTP interface, a JSON bridge to the pinned `asciidoctor-dita-topic` Ruby converter, `dita-convert` XSLT specialization, and lxml schema validation. See [THIRD_PARTY.md](THIRD_PARTY.md) for upstream tools, schemas, and source attribution.
