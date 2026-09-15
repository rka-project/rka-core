# Core 3.0.1 — truthful dashboard search status

This patch replaces the hardcoded semantic-search banner and misleading Vector
badge with runtime capabilities from `/api/capabilities`. Available hybrid
retrieval, keyword-only degradation and unconfirmed/error status are distinct.
Configuration/backfill transitions refresh the display. Dismissal preferences
remain intact. There are no database migrations, provider-default changes,
retrieval changes or dependency upgrades.

Core remains local-first. This release does not enable remote MCP, change access
policy, make the Core image package public or deploy a shared service. A separate
RKA App image may consume the released Core digest for user-owned trials.

## Upgrade and rollback

Follow the [Core 3.0 upgrade runbook](RELEASE_3_0.md), substituting **3.0.1** for
the target version. Record the previous image/source digest and preserve your
data/configuration before a coordinated API/worker upgrade. A service restart
alone does not replace the built dashboard. Check health/CLI version 3.0.1 and
compare the displayed search status with `/api/capabilities`, not just health's
`vec_available`. There is no additional migration for this patch.

Use only a digest reported by the successful container publication workflow.
Release/tag existence does not prove an image is ready or anonymously pullable.
Existing installations are not automatically upgraded by this release. Core's
base Python wheel does not include the dashboard; the full container does.

## Validation scope

The release gate includes the new frontend status regression tests, production
frontend build, Core profile, wheel/platform tests, historical upgrade acceptance
and container smoke. The API contract itself is unchanged. Existing dependency
advisories and broader frontend/performance work are separate follow-ups; this
targeted patch is not a dependency security clearance.
