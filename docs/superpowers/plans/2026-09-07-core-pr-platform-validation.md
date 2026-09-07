# PR #159 native-platform gate correction

The PI authorized PR creation and merge on 2026-09-07. The isolated branch was
pushed at `f3679b1`; GitHub main remained `f8db01b`. No production deployment or
canonical/peer worktree edits were authorized or performed.

## First remote run and precise causes

[Run 34142298537](https://github.com/rka-project/rka-core/actions/runs/34142298537)
passed the Web build, Docker image smoke, and both Linux wheel matrix jobs.
Merge was held for these concrete native-platform failures:

- Both macOS Python jobs passed base wheel/file/import checks, then lacked
  `sqlite3.Connection.load_extension`. Installing sqlite-vec could not supply
  an interpreter build feature. The new CI vector gate had assumed that feature.
- Both Windows jobs' BOM file fixture used text-mode writes, producing CRLF while
  asserting LF. The product correctly retained the supplied raw entry text;
  this was a platform-dependent test expectation, not source corruption.

The correction changes only CI, tests and documentation. Keep setup-python's
base-wheel/file/import coverage on macOS, then use a separate uv-managed Python
of the same matrix minor version for vector tests. A real in-memory sqlite-vec
load preflight must pass; no skipped vector gate or extension mock is added.
The vector environment explicitly excludes FastEmbed and bibtexparser. The
uv version is pinned; its interpreter/cache/venv are runner-temporary.

The BOM regression now supplies explicit bytes and checks both LF and CRLF,
through text and file paths and both parser variants. The raw-source equality
assertion remains exact. Installation guidance describes the native macOS
interpreter limitation without changing the recommended Docker deployment.

## Local pre-merge evidence

- Before the correction: 176 passed, 1 native-Windows-only skipped in the
  security/ownership/transaction/API/CLI selection, 34.46 seconds.
- The model-free admission/backend selection also passed in the isolated base
  environment: 112 passed, 3.22 seconds.
- Corrected BOM/parser cases: 37 passed (both parser variants, LF/CRLF, file/text),
  4.15 seconds. Ruff and `git diff --check` passed; parsed workflow retains four
  jobs and all six native matrix entries. Real sqlite-vec preflight passed in
  the isolated base environment, reporting v0.1.6.
- Remote rerun results must be checked before merge. The earlier full Core result remains 3536 passed at
  `f3679b1`; it is not evidence that the corrected remote head has passed.

The source merge is separate from release publication and deployment. E2 and
the remaining audit/upgrade gates remain open; this does not close #158.
