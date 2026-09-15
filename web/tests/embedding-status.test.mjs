import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import test from "node:test"
import ts from "typescript"

// Compile the same pure TS resolver the UI uses, with the existing toolchain.
const source = readFileSync(new URL("../src/lib/embeddingStatus.ts", import.meta.url), "utf8")
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
})
const { embeddingStatus } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`)
const capabilities = (available, search_mode) => ({
  schema_version: "rka.core-capabilities/v1",
  embedding: { available, search_mode, reason_unavailable: null },
})

test("only a confirmed available hybrid runtime advertises semantic search", () => {
  assert.equal(embeddingStatus(capabilities(true, "hybrid"), false).mode, "hybrid")
})
for (const reason of ["disabled", "backend unavailable", "index rebuilding", "sqlite-vec not loaded"]) {
  test(`${reason}: lexical status never advertises enabled semantics`, () => {
    const data = capabilities(false, "lexical")
    data.embedding.reason_unavailable = reason
    assert.equal(embeddingStatus(data, false).label, "Keyword search only")
  })
}
test("loading, unsupported and contradictory responses stay unknown", () => {
  for (const data of [undefined, null, {}, { ...capabilities(true, "hybrid"), schema_version: "v0" },
    capabilities(true, "lexical"), capabilities(false, "hybrid"),
    { schema_version: "rka.core-capabilities/v1" }]) {
    assert.equal(embeddingStatus(data, false).mode, "unknown")
  }
})
test("refetch failure suppresses stale positive status", () => {
  assert.equal(embeddingStatus(capabilities(true, "hybrid"), true).mode, "unknown")
})
