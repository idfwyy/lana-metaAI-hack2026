// Runs web/tests/*.test.ts in Node: esbuild (already here through Vite) bundles each one, then
// it runs as a plain module. No test framework: a test file throws on the first failure.

import { build } from "esbuild";
import { mkdtempSync, readdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";

const root = resolve(import.meta.dirname, "..");
const tests = readdirSync(join(root, "tests")).filter((f) => f.endsWith(".test.ts"));
const out = mkdtempSync(join(tmpdir(), "web-tests-"));
let failed = 0;

try {
  for (const file of tests) {
    const outfile = join(out, file.replace(/\.ts$/, ".mjs"));
    await build({
      entryPoints: [join(root, "tests", file)],
      outfile,
      bundle: true,
      platform: "node",
      format: "esm",
      logLevel: "error",
    });
    try {
      await import(pathToFileURL(outfile).href);
    } catch (err) {
      failed++;
      console.error(`FAIL ${file}\n`, err);
    }
  }
} finally {
  rmSync(out, { recursive: true, force: true });
}
process.exit(failed ? 1 : 0);
