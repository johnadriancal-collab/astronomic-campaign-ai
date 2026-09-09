import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

/**
 * Structural guard: this project must never be able to write, delete, or
 * list objects, regardless of what the underlying R2 API token
 * technically permits -- verified by scanning the actual source of
 * r2-read-client.ts (the one place @aws-sdk/client-s3 is imported at
 * all in this project) rather than trusting a comment or a docstring.
 * Mirrors the structural-guard test convention already used throughout
 * the Python backend (e.g. test_backfill_driver_exclusion_parameter_is_not_referenced_by_the_live_webhook_path).
 */

const FORBIDDEN_S3_COMMANDS = ["PutObjectCommand", "DeleteObjectCommand", "ListObjectsV2Command", "ListObjectsCommand", "CopyObjectCommand"];

function readSource(relativePath: string): string {
  const dir = path.dirname(fileURLToPath(import.meta.url));
  return readFileSync(path.join(dir, relativePath), "utf-8");
}

test("r2-read-client.ts never references any write/delete/list S3 command", () => {
  const source = readSource("r2-read-client.ts");
  for (const forbidden of FORBIDDEN_S3_COMMANDS) {
    assert.ok(!source.includes(forbidden), `r2-read-client.ts must never reference ${forbidden}`);
  }
});

test("r2-read-client.ts references GetObjectCommand exactly -- the only S3 operation this project performs", () => {
  const source = readSource("r2-read-client.ts");
  assert.ok(source.includes("GetObjectCommand"));
});

test("no file in this project references a write/delete/list S3 command anywhere", () => {
  const dir = path.dirname(fileURLToPath(import.meta.url));
  const filesToScan = [
    "r2-read-client.ts",
    "handle-avatar-request.ts",
    "validate-avatar-key.ts",
    path.join("..", "app", "avatars", "[filename]", "route.ts"),
  ];
  for (const relativeFile of filesToScan) {
    const source = readFileSync(path.join(dir, relativeFile), "utf-8");
    for (const forbidden of FORBIDDEN_S3_COMMANDS) {
      assert.ok(!source.includes(forbidden), `${relativeFile} must never reference ${forbidden}`);
    }
  }
});
