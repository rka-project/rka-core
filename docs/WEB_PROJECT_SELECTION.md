# Explicit project selection in the dashboard

The dashboard does not choose a project automatically. A first visit must
explicitly select an existing project or create one before project records are
loaded or written. Selecting Default Project is allowed; selecting it is a
user action, not a fallback.

Only unscoped discovery/project-list/project-create requests are needed on the
selection screen. Scoped API-client calls without a selection fail before
fetching. The server's existing explicit-scope validation remains unchanged.

The browser stores a versioned confirmation in `rka.projectSelection.v1`.
Legacy `rka.activeProjectId` values alone are not trusted, because older
clients wrote `proj_default` without a user action. After this upgrade, existing
browser profiles therefore confirm once. Existing project data is untouched.

On reload, a confirmed choice is checked against the project list before
scoped pages mount. A missing project returns to the selector; it does not
silently select the first remaining project. Deleting the current project
likewise returns to explicit selection. Browser storage failure permits a
current-tab choice but requires confirmation again after reload.

The HTTP scope is revoked before a switch and published before pages/query
keys for the next project mount. Project IDs are retained in query keys.
No historical migration (including 004) is modified, and existing
`proj_default` records are neither deleted nor moved.

Verification: `npm test` runs client scope/error and stored-choice unit tests.
Browser checks use only a disposable context and synthetic intercepted API
responses; they must not connect to a real RKA backend.
