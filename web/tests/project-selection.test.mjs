import assert from "node:assert/strict"
import test from "node:test"
import { loadTypescript } from "./load-typescript.mjs"

const { readConfirmedProject, saveProjectChoice, CONFIRMED_PROJECT_SELECTION_KEY, PROJECT_SELECTION_STORAGE_KEY } =
  await loadTypescript(new URL("../src/lib/projectSelection.ts", import.meta.url))

function storage(seed = {}) {
  const data = new Map(Object.entries(seed))
  return { data, getItem: (key) => data.get(key) ?? null,
    setItem: (key, value) => data.set(key, value), removeItem: (key) => data.delete(key) }
}

test("a fresh browser has no project and reading does not persist a fallback", () => {
  const saved = storage()
  assert.equal(readConfirmedProject(saved), null)
  assert.equal(saved.data.size, 0)
})

test("legacy automatic and non-default selections both require one confirmation", () => {
  for (const id of ["proj_default", "prj_old"]) {
    const saved = storage({ [PROJECT_SELECTION_STORAGE_KEY]: id })
    assert.equal(readConfirmedProject(saved), null)
    assert.equal(saved.getItem(PROJECT_SELECTION_STORAGE_KEY), id)
  }
})

test("an explicitly selected Default Project remains valid on reload", () => {
  const saved = storage()
  saveProjectChoice("proj_default", saved)
  assert.equal(readConfirmedProject(saved), "proj_default")
})

test("selection trims ids; clearing never falls back to default", () => {
  const saved = storage()
  saveProjectChoice(" prj_selected ", saved)
  assert.equal(readConfirmedProject(saved), "prj_selected")
  saveProjectChoice(null, saved)
  assert.equal(readConfirmedProject(saved), null)
  assert.equal(saved.getItem(PROJECT_SELECTION_STORAGE_KEY), null)
})

test("malformed or unsupported stored choices fail closed", () => {
  for (const raw of ["invalid", "null", "[]", '{"version":2,"projectId":"prj_x"}',
    '{"version":1,"projectId":4}', '{"version":1,"projectId":"  "}']) {
    assert.equal(readConfirmedProject(storage({ [CONFIRMED_PROJECT_SELECTION_KEY]: raw })), null)
  }
})

test("unavailable browser storage does not break current-tab interaction", () => {
  const blocked = { getItem() { throw Error("blocked") }, setItem() { throw Error("blocked") },
    removeItem() { throw Error("blocked") } }
  assert.equal(readConfirmedProject(blocked), null)
  assert.doesNotThrow(() => saveProjectChoice("prj_x", blocked))
  assert.doesNotThrow(() => saveProjectChoice(null, blocked))
})
