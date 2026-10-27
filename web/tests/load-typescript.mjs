import { readFileSync } from "node:fs"
import ts from "typescript"

export async function loadTypescript(path) {
  const source = readFileSync(path, "utf8")
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  })
  return import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`)
}
