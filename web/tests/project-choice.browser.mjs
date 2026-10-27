// Local browser acceptance. Requires Playwright supplied by the test environment.
// Uses a static preview with NO API proxy and synthetic responses only.
import assert from "node:assert/strict"
import { preview } from "vite"
import { writeFile, mkdir } from "node:fs/promises"

const { chromium } = await import(process.env.RKA_PLAYWRIGHT_MODULE ?? "playwright")
const evidence = process.env.RKA_UI_EVIDENCE_DIR
const server = await preview({
  configFile: false, root: process.cwd(),
  preview: { host: "127.0.0.1", port: 0, strictPort: true, proxy: {} },
})
const address = server.httpServer.address()
assert.ok(address && typeof address !== "string" && address.port > 0)
const url = `http://127.0.0.1:${address.port}`
let browser
const passed = []
const project = (id, name) => ({ id, name, description: null, created_by: "system", created_at: null, updated_at: null })

async function fixture(stored = {}, failList = false) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } })
  await context.addInitScript((data) => {
    for (const [key, value] of Object.entries(data)) localStorage.setItem(key, value)
  }, stored)
  const page = await context.newPage()
  const calls = []
  const pageErrors = []
  page.on("pageerror", (error) => pageErrors.push(error.message))
  const projects = [project("proj_default", "Default Project"), project("prj_alpha", "Alpha research")]
  const state = { failList }
  await page.route("**/api/**", async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    const method = request.method()
    const scope = request.headers()["x-rka-project"] ?? null
    calls.push({ path, method, scope })
    let body = []
    let status = 200
    if (path === "/api/projects" && method === "GET") {
      if (state.failList) { status = 503; body = { detail: "Synthetic list failure" } }
      else body = projects
    } else if (path === "/api/projects" && method === "POST") {
      body = project("prj_created", request.postDataJSON().name)
      projects.push(body)
    } else if (path === "/api/health") {
      body = { status: "ok", version: "synthetic" }
    } else if (path.endsWith("/entity-counts") && method === "GET") {
      body = { project_id: scope, total_rows: 0, entity_counts: {} }
    } else if (path === "/api/projects/prj_alpha" && method === "DELETE") {
      assert.equal(scope, "prj_alpha")
      assert.equal(new URL(request.url()).searchParams.get("confirm"), "true")
      projects.splice(projects.findIndex((item) => item.id === "prj_alpha"), 1)
      body = { project_id: "prj_alpha", confirmed: true, total_rows: 0 }
    } else if (path === "/api/capabilities") {
      body = { schema_version: "rka.core-capabilities/v1", embedding: {
        available: false, search_mode: "lexical", reason_unavailable: "synthetic fixture",
      } }
    } else if (path === "/api/status") {
      assert.ok(scope, "scoped status must identify the project")
      body = { project_id: scope, project_name: projects.find((item) => item.id === scope)?.name,
        current_phase: "testing", phases_config: [], summary: "Synthetic browser fixture" }
    } else if (!["/api/notes", "/api/missions", "/api/checkpoints", "/api/events"].includes(path) || method !== "GET") {
      status = 500
      body = { detail: `Unexpected synthetic request: ${method} ${path}` }
      pageErrors.push(body.detail)
    }
    await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) })
  })
  return { context, page, calls, pageErrors, state }
}

const scoped = (calls) => calls.filter((call) => !["/api/projects", "/api/health", "/api/capabilities"].includes(call.path))
const openGate = async (page) => {
  await page.goto(url + "/journal")
  await page.getByLabel("Existing project").waitFor()
}

try {
  browser = await chromium.launch({ headless: true, executablePath: process.env.RKA_BROWSER_EXECUTABLE })
  const fresh = await fixture()
  await openGate(fresh.page)
  assert.equal(scoped(fresh.calls).length, 0)
  assert.ok(fresh.calls.every((call) => call.method === "GET" && call.scope === null))
  assert.equal(await fresh.page.evaluate(() => localStorage.getItem("rka.activeProjectId")), null)
  if (evidence) {
    await mkdir(evidence, { recursive: true })
    await fresh.page.screenshot({ path: evidence + "/first-run.png", fullPage: true })
  }
  passed.push("fresh deep-link mounts only the selector, with no default header or scoped calls")
  await fresh.page.getByLabel("Existing project").selectOption("prj_alpha")
  assert.equal(scoped(fresh.calls).length, 0)
  await fresh.page.getByRole("button", { name: "Use selected project" }).click()
  await fresh.page.getByRole("heading", { name: "Research Journal", exact: true }).waitFor()
  assert.ok(scoped(fresh.calls).length > 0)
  assert.ok(scoped(fresh.calls).every((call) => call.scope === "prj_alpha"))
  passed.push("explicit project choice publishes the correct header before page queries")

  fresh.calls.length = 0
  await fresh.page.reload()
  await fresh.page.getByRole("heading", { name: "Research Journal", exact: true }).waitFor()
  assert.ok(scoped(fresh.calls).every((call) => call.scope === "prj_alpha"))
  passed.push("reload validates and restores the confirmed choice")

  await fresh.page.getByRole("combobox").first().click()
  await fresh.page.getByRole("option", { name: "Alpha research", exact: true }).click()
  await fresh.page.getByRole("heading", { name: "Research Journal", exact: true }).waitFor()
  assert.equal(await fresh.page.evaluate(() => JSON.parse(localStorage.getItem("rka.projectSelection.v1")).projectId), "prj_alpha")
  assert.ok(scoped(fresh.calls).every((call) => call.scope === "prj_alpha"))
  passed.push("reselecting the current project keeps its page and request scope available")

  fresh.calls.length = 0
  await fresh.page.getByRole("combobox").first().click()
  await fresh.page.getByRole("option", { name: "Default Project", exact: true }).click()
  await fresh.page.waitForFunction(() => JSON.parse(localStorage.getItem("rka.projectSelection.v1")).projectId === "proj_default")
  await fresh.page.getByRole("heading", { name: "Research Journal", exact: true }).waitFor()
  assert.ok(scoped(fresh.calls).length > 0)
  assert.ok(scoped(fresh.calls).every((call) => call.scope === "proj_default"))
  assert.deepEqual(fresh.pageErrors, [])
  passed.push("switching to Default Project is explicit and never uses the prior header")
  await fresh.context.close()

  const legacy = await fixture({ "rka.activeProjectId": "proj_default" })
  await openGate(legacy.page)
  assert.equal(scoped(legacy.calls).length, 0)
  assert.equal(await legacy.page.evaluate(() => localStorage.getItem("rka.projectSelection.v1")), null)
  assert.deepEqual(legacy.pageErrors, [])
  passed.push("legacy automatic default storage requires one explicit confirmation")
  await legacy.context.close()

  const missing = await fixture({ "rka.projectSelection.v1": JSON.stringify({ version: 1, projectId: "prj_missing" }) })
  await openGate(missing.page)
  assert.equal(scoped(missing.calls).length, 0)
  await missing.page.waitForFunction(() => localStorage.getItem("rka.projectSelection.v1") === null)
  assert.deepEqual(missing.pageErrors, [])
  passed.push("a missing saved project returns to selection without choosing another project")
  await missing.context.close()

  const created = await fixture()
  await openGate(created.page)
  await created.page.getByLabel("New project name").fill("Created research")
  await created.page.getByRole("button", { name: "Create and open project" }).click()
  await created.page.getByRole("heading", { name: "Research Journal", exact: true }).waitFor()
  assert.equal(created.calls.filter((call) => call.method === "POST" && call.path === "/api/projects" && call.scope === null).length, 1)
  assert.ok(scoped(created.calls).every((call) => call.scope === "prj_created"))
  assert.deepEqual(created.pageErrors, [])
  passed.push("explicit creation is unscoped and opens only the returned project")
  await created.context.close()

  const deleted = await fixture({ "rka.projectSelection.v1": JSON.stringify({ version: 1, projectId: "prj_alpha" }) })
  await deleted.page.goto(url + "/journal")
  await deleted.page.getByRole("heading", { name: "Research Journal", exact: true }).waitFor()
  await deleted.page.getByRole("button", { name: "Delete project", exact: true }).click()
  await deleted.page.getByPlaceholder("Project name", { exact: true }).fill("Alpha research")
  await deleted.page.getByRole("button", { name: "Delete Forever", exact: true }).click()
  await deleted.page.getByLabel("Existing project").waitFor()
  assert.ok(deleted.calls.some((call) => call.method === "DELETE"))
  assert.ok(deleted.calls.every((call) => call.scope !== "proj_default"))
  assert.equal(await deleted.page.evaluate(() => localStorage.getItem("rka.projectSelection.v1")), null)
  assert.deepEqual(deleted.pageErrors, [])
  passed.push("deleting the selected synthetic project revokes scope and never selects another project")
  await deleted.context.close()

  const unavailable = await fixture({}, true)
  await unavailable.page.goto(url)
  await unavailable.page.getByRole("button", { name: "Retry loading projects" }).waitFor({ timeout: 10000 })
  assert.equal(scoped(unavailable.calls).length, 0)
  unavailable.state.failList = false
  await unavailable.page.getByRole("button", { name: "Retry loading projects" }).click()
  await unavailable.page.getByLabel("Existing project").waitFor()
  assert.equal(scoped(unavailable.calls).length, 0)
  assert.deepEqual(unavailable.pageErrors, [])
  passed.push("project-list failure is recoverable and never triggers an automatic fallback")
  await unavailable.context.close()

  console.log(JSON.stringify({ passed: passed.length, checks: passed, backend: "synthetic-only", previewPort: address.port }, null, 2))
  if (evidence) await writeFile(evidence + "/browser-results.json", JSON.stringify({ passed, backend: "synthetic-only" }, null, 2))
} finally {
  await browser?.close()
  await new Promise((resolve) => server.httpServer.close(resolve))
}
