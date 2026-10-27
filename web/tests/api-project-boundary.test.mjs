import assert from "node:assert/strict"
import test from "node:test"
import { loadTypescript } from "./load-typescript.mjs"

const { api, setApiProjectId, getApiProjectId, subscribeApiProjectId } = await loadTypescript(new URL("../src/api/client.ts", import.meta.url))
function mockFetch(t) {
  const calls = []
  t.mock.method(globalThis, "fetch", async (url, options) => {
    calls.push({ url, options })
    return new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } })
  })
  return calls
}

test("scope subscribers see the published value and unsubscribe cleanly", () => {
  setApiProjectId(null)
  const seen = []
  const unsubscribe = subscribeApiProjectId(() => seen.push(getApiProjectId()))
  setApiProjectId("prj_one")
  setApiProjectId(" prj_one ")
  setApiProjectId(null)
  unsubscribe()
  setApiProjectId("prj_two")
  assert.deepEqual(seen, ["prj_one", null])
  assert.equal(getApiProjectId(), "prj_two")
  setApiProjectId(null)
})

test("project listing, creation and discovery work without a scoped header", async (t) => {
  setApiProjectId(null)
  const calls = mockFetch(t)
  await api.listProjects()
  await api.createProject({ name: "Synthetic" })
  await api.health()
  await api.capabilities()
  assert.equal(calls.length, 4)
  for (const call of calls) assert.equal(call.options.headers.has("X-RKA-Project"), false)
})

test("unselected reads and writes fail before contacting the backend", async (t) => {
  setApiProjectId(null)
  const calls = mockFetch(t)
  await assert.rejects(api.listNotes(), (error) => error.status === 422 && /Choose a project/.test(error.detail))
  await assert.rejects(api.createNote({ content: "Must not be sent" }), /Choose a project/)
  await assert.rejects(api.exportKnowledgePack(), /Choose a project/)
  assert.equal(calls.length, 0)
})

test("explicit selection pins the project on GET and POST", async (t) => {
  setApiProjectId("proj_default")
  const calls = mockFetch(t)
  await api.listNotes()
  await api.createNote({ content: "Synthetic" })
  assert.equal(calls.length, 2)
  for (const call of calls) assert.equal(call.options.headers.get("X-RKA-Project"), "proj_default")
  assert.equal(calls[1].options.headers.get("Content-Type"), "application/json")
})

test("switching captures each request's own project; clearing revokes writes", async (t) => {
  const calls = mockFetch(t)
  setApiProjectId("prj_one")
  const first = api.listNotes()
  setApiProjectId("prj_two")
  const second = api.createNote({ content: "Synthetic" })
  await Promise.all([first, second])
  assert.deepEqual(calls.map((call) => call.options.headers.get("X-RKA-Project")), ["prj_one", "prj_two"])
  setApiProjectId("")
  await assert.rejects(api.createNote({ content: "Must not be sent" }), /Choose a project/)
  assert.equal(calls.length, 2)
})
