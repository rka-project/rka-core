# Operator-controlled file access

Status: Unreleased Core hardening. This is a deliberate restriction on older
path-reading behavior, not a new hosted-service or per-user authorization model.
RKA remains local-first and researcher-controlled.

## Two independent authorities

| Operation | Process that reads files | Operator setting |
|---|---|---|
| MCP `register_source(filepath=...)`, workspace tree/scan/bootstrap | Local MCP process | `RKA_HOST_FILE_ROOTS` |
| REST source/artifact `filepath`, workspace scan/ingest, service BibTeX-file import | Core API process | `RKA_SERVER_FILE_ROOTS` |
| Local `rka bootstrap scan` / `rka bootstrap ingest` | CLI (direct local database workflow) | `RKA_SERVER_FILE_ROOTS` |
| Supplied bytes/text, BibTeX upload, existing record retrieval | No caller-nominated filesystem read | No input roots needed |

Both settings default to the JSON array `[]`: path reads are disabled. A request
path, `project_dir`, actor field, relative manifest path or returned server
manifest cannot grant additional permission. Server roots never authorize MCP
host reads, even when both processes happen to run on one machine.

Select narrow, existing input directories that contain only data you intend to
share with this RKA instance. Do not authorize `/`, a drive root, your entire
home directory, credentials folders, or RKA's live database directory. Keep
input roots operator-controlled and preferably read-only to other users.
Anyone permitted to call this instance's file operations can read its authorized
roots; these are instance-wide capabilities, **not project-specific ACLs**.

MCP scans send previews and metadata to the configured Core API. Bootstrap sends
selected content; `dry_run` still scans and sends previews, but does not create
records. Do not point a host with private input roots at someone else's demo.
An owned cloud Space is a separate operator deployment, not automatically a
trusted destination for host data.

## Configuration by operating system

Examples use a synthetic input folder. Substitute only directories explicitly
chosen by the operator. Set each variable on the process that needs it; leave
server path reads disabled for the usual host-MCP-to-Docker workflow.

macOS / Linux (sh, bash, zsh):

```sh
mkdir -p "$HOME/rka-input"
export RKA_HOST_FILE_ROOTS="[\"$HOME/rka-input\"]"
rka mcp
```

For a Dockerless API that also needs direct REST path reads, in its launch shell:

```sh
export RKA_SERVER_FILE_ROOTS="[\"$HOME/rka-input\"]"
rka serve
```

Windows PowerShell (outside OneDrive or other reparse-backed folders):

```powershell
$rkaInput = Join-Path $env:USERPROFILE 'rka-input'
New-Item -ItemType Directory -Force -Path $rkaInput | Out-Null
$env:RKA_HOST_FILE_ROOTS = ConvertTo-Json -InputObject @($rkaInput) -Compress
rka mcp
```

For an API launched separately in PowerShell, set its own environment:

```powershell
$env:RKA_SERVER_FILE_ROOTS = ConvertTo-Json -InputObject @($rkaInput) -Compress
rka serve
```

The value is JSON, not a comma- or path-separator-delimited string. A literal
Windows example is `["C:\\Users\\me\\rka-input"]`. Native absolute local-drive
paths are supported; drive-relative paths, UNC/device paths, alternate data
streams, reserved DOS names, symlinks and junctions are rejected. POSIX rejects
Windows-style paths, backslashes and `..` traversal. All components, including
configured roots, must be real directories, not symlinks or reparse points.
macOS's OS-owned `/tmp`, `/var`, `/etc` aliases are mapped to `/private/...`;
arbitrary symlinks are not resolved to grant access.

For managed MCP launchers, put the JSON string in the launcher's environment
configuration or export it before launching the client. Merely exporting it in
another terminal does not change a running process. Host roots are read only
from the MCP process environment, not a workspace `.env` or API configuration.
Server roots use `RKAConfig` (operator environment/configuration), are validated
at API creation and require restarting that API after a change. Restart the MCP
process/client after changing its launch environment. No agent should modify
these settings without the operator's chosen scope.

For containers, the host-side workflow needs **no new input mount**. If direct
REST reads are specifically required, add a narrow read-only bind mount and
authorize its **container path**, never the host path. Example Compose fragment
for the service that runs the API (merge with its existing configuration):

```yaml
environment:
  RKA_SERVER_FILE_ROOTS: '["/inputs"]'
volumes:
  - /operator/chosen/input-directory:/inputs:ro
```

Do not replace existing data volumes, mount a home directory, or expose the
REST port publicly as part of enabling this feature.

## Read design and limits

The shared policy verifies containment before I/O and opens each path component
without following links. POSIX uses directory-relative descriptors with
`O_NOFOLLOW`. Windows opens non-reparse component handles and keeps them pinned
without write/delete sharing through the read or directory enumeration.
Parsers that require a filename receive a private temporary copy of the bounded
bytes, with the original basename; the copy is removed afterward. Stored source
locators refer to the original authorized path, not the disposable copy.

Implementation references: [Python descriptor-aware filesystem APIs](https://docs.python.org/3/library/os.html#os.scandir)
and [Windows CreateFile sharing and reparse flags](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew).

| Limit | Behavior |
|---|---|
| Per file | 50 MiB default; operator `RKA_REGISTERED_SOURCE_MAX_BYTES` accepts 1 byte–500 MiB in the reading process; a smaller request limit further restricts it |
| Scan/read pass | 200 MiB accepted file bytes, 2,000 regular files, 10,000 examined entries (including ignored entries) |
| Recursion | 32 levels for scans; tree overview accepts depth 0–8 and at most 200 examined entries per directory |
| Time | 30-second cooperative budget checked between operations, not a hard OS/parser timeout |
| Concurrency | Four scans and four file-read/parser snapshots per process; busy work is rejected, not queued without bound |
| Text imports | 2 MiB UTF-8 bytes for workspace content and academic text/BibTeX; BibTeX upload reads at most the limit plus one byte |

The old workspace `max_files=5000` request default is accepted for compatibility
but clamped to the 2,000-file ceiling. Enumeration stops instead of counting the
whole tree. Returned counts may be partial; individual oversized/unreadable
files are skipped with a warning. Host scan sends at most 500 file summaries;
bootstrap processes at most 2,000. No request can raise the operator ceilings.

Source/artifact registration retains full SHA-256. Workspace duplicate/change
detection retains the existing hash recipe (including sampled hashing for files
over 10 MiB), so it is **not proof that every byte of a large file is unchanged**.
Changing that recipe requires its own versioning/migration work.

## Errors, compatibility and verification

REST boundary failures use `{ "error": "...", "detail": "..." }` with 403 for
`file_access_disabled`/`file_access_denied`, 413 for `file_access_limit`, or 503
for `file_access_busy`. Invalid request shapes/field lengths may instead fail
schema validation with 422. A changed file is identified internally as
`file_changed` (409); batch ingestion preserves its existing response contract
and reports it as a failed per-file result, alongside any earlier successes.
MCP host failures are tool errors; later per-file bootstrap failures are included
in its result summary. Preflight rejects malformed/escaping server manifests
before any batch writes. Batch ingestion is not globally atomic: check both
the error count and each result, and rescan changed files before retrying.

To verify a new installation, create one synthetic text file inside the selected
input folder and one outside it. An allowed `workspace_tree`/`workspace_scan`
must work; a source-file read outside it must fail without returning its content.
Leave server roots empty and verify an explicitly supplied byte/text source can
still be registered. Do not use real private documents as test sentinels.

An upgrade requires no data/schema migration and does not grant any new roots
automatically. Existing records remain readable; later operations that reopen
an original artifact path now need explicit permission. A scan manifest is not
a durable permission grant and should be regenerated after content changes.

## Remaining boundaries

This is not an OS sandbox, a multi-user ACL system, a secret scanner, or a
guarantee against an attacker who controls the operator's input directories,
hard links, mounts or process environment. Byte limits bound accepted raw input,
not decompressed PDF/DOCX memory, parser CPU, all HTTP request-body buffering or
blocked filesystem calls. Use trusted local input directories; untrusted
document parsing needs stronger process isolation/resource limits in a later
work package. Managed artifact storage, Knowledge Pack recovery and remote
connector authorization retain separate boundaries and validation gates.

Cross-platform tests live in `tests/test_infra/test_file_access.py`,
`tests/test_api/test_file_access_boundary.py` and
`tests/test_cli/test_file_access_boundary.py`. CI runs them on Linux, macOS and
Windows with Python 3.11/3.13. A local macOS pass does not replace native Windows
and Linux CI results or the release/old-database upgrade gates.
