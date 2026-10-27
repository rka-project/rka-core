import assert from "node:assert/strict"
import test from "node:test"
import { loadTypescript } from "./load-typescript.mjs"

const { api, setApiProjectId } = await loadTypescript(new URL("../src/api/client.ts", import.meta.url))
setApiProjectId("prj_synthetic")

test("validation errors retain their location and message", async (t) => {
  t.mock.method(globalThis, "fetch", async () => new Response(JSON.stringify({
    detail: [{ loc: ["body", "content"], msg: "Field required" }],
  }), { status: 422 }))
  await assert.rejects(api.createNote({}), (error) =>
    error.status === 422 && error.detail === "body.content: Field required")
})

test("JSON and non-JSON backend errors stay errors", async (t) => {
  t.mock.method(globalThis, "fetch", async () => new Response(JSON.stringify({
    detail: { error: "scope_missing" },
  }), { status: 400 }))
  await assert.rejects(api.listNotes(), (error) => error.status === 400 && /scope_missing/.test(error.detail))
  t.mock.method(globalThis, "fetch", async () => new Response("unavailable", { status: 503, statusText: "Unavailable" }))
  await assert.rejects(api.listNotes(), (error) => error.status === 503 && error.detail === "Unavailable")
})
