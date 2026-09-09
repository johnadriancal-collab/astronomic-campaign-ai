import { test } from "node:test";
import assert from "node:assert/strict";
import { CACHE_HEADERS, handleAvatarRequest } from "./handle-avatar-request.ts";
import type { ReadOnlyObjectStorageClient } from "./r2-read-client.ts";

const VALID_FILENAME = "3fa85f64-5717-4562-b3fc-2c963f66afa6.jpg";
const FAKE_JPEG_BYTES = new Uint8Array([0xff, 0xd8, 0xff, 0xdb, 1, 2, 3]); // a JPEG magic-number-shaped stand-in, real bytes don't matter for these tests

/** In-memory fake -- mirrors the Python MemoryObjectStorageClient pattern
 * used for Stage 1's own tests. Also tracks every key ever requested, so
 * a test can assert R2 was (or, critically, was NOT) ever called. */
class FakeReadOnlyClient implements ReadOnlyObjectStorageClient {
  objects = new Map<string, Uint8Array<ArrayBuffer>>();
  requestedKeys: string[] = [];

  async getObject(key: string): Promise<Uint8Array<ArrayBuffer> | null> {
    this.requestedKeys.push(key);
    return this.objects.get(key) ?? null;
  }
}

test("a valid uuid4 filename for an existing object returns 200 with the image bytes", async () => {
  const client = new FakeReadOnlyClient();
  client.objects.set(`avatars/${VALID_FILENAME}`, FAKE_JPEG_BYTES);

  const response = await handleAvatarRequest(VALID_FILENAME, client);
  const body = new Uint8Array(await response.arrayBuffer());

  assert.equal(response.status, 200);
  assert.deepEqual(body, FAKE_JPEG_BYTES);
});

test("the object key requested is exactly avatars/<filename>, nothing else", async () => {
  const client = new FakeReadOnlyClient();
  client.objects.set(`avatars/${VALID_FILENAME}`, FAKE_JPEG_BYTES);

  await handleAvatarRequest(VALID_FILENAME, client);

  assert.deepEqual(client.requestedKeys, [`avatars/${VALID_FILENAME}`]);
});

test("Content-Type is always image/jpeg on a successful response", async () => {
  const client = new FakeReadOnlyClient();
  client.objects.set(`avatars/${VALID_FILENAME}`, FAKE_JPEG_BYTES);

  const response = await handleAvatarRequest(VALID_FILENAME, client);

  assert.equal(response.headers.get("Content-Type"), "image/jpeg");
});

test("a successful response carries 1-year immutable cache headers for both browser and Vercel CDN", async () => {
  const client = new FakeReadOnlyClient();
  client.objects.set(`avatars/${VALID_FILENAME}`, FAKE_JPEG_BYTES);

  const response = await handleAvatarRequest(VALID_FILENAME, client);
  const oneYear = 60 * 60 * 24 * 365;

  for (const headerName of ["Cache-Control", "CDN-Cache-Control", "Vercel-CDN-Cache-Control"]) {
    const value = response.headers.get(headerName);
    assert.ok(value, `expected ${headerName} to be set`);
    assert.match(value as string, /public/);
    assert.match(value as string, new RegExp(`max-age=${oneYear}\\b`));
    assert.match(value as string, /immutable/);
  }
  // CACHE_HEADERS is exported specifically so this exact contract is
  // pinned and reused consistently -- not re-derived ad hoc per test.
  assert.deepEqual(Object.keys(CACHE_HEADERS).sort(), ["CDN-Cache-Control", "Cache-Control", "Vercel-CDN-Cache-Control"]);
});

test("a missing object returns a plain 404 with no body", async () => {
  const client = new FakeReadOnlyClient();
  // deliberately never populated

  const response = await handleAvatarRequest(VALID_FILENAME, client);
  const body = await response.text();

  assert.equal(response.status, 404);
  assert.equal(body, "");
});

test("a malformed filename returns 404 WITHOUT ever calling R2", async () => {
  const client = new FakeReadOnlyClient();

  const response = await handleAvatarRequest("../../etc/passwd", client);

  assert.equal(response.status, 404);
  assert.deepEqual(client.requestedKeys, []); // never even attempted -- validation happens first
});

test("path traversal attempts all resolve to plain 404, identical to a missing object", async () => {
  const client = new FakeReadOnlyClient();
  const attempts = [
    "../../etc/passwd",
    "avatars/../../secret.jpg",
    "..%2f..%2fetc%2fpasswd",
    `${VALID_FILENAME}/../../secret`,
    "3fa85f64-5717-4562-b3fc-2c963f66afa6.jpg;rm -rf",
  ];

  for (const attempt of attempts) {
    const response = await handleAvatarRequest(attempt, client);
    assert.equal(response.status, 404, `expected 404 for ${JSON.stringify(attempt)}`);
  }
  assert.deepEqual(client.requestedKeys, []); // none of these ever reached R2
});

test("a malformed filename and a well-formed-but-missing filename are indistinguishable to the caller", async () => {
  const client = new FakeReadOnlyClient();

  const malformed = await handleAvatarRequest("not-a-uuid.jpg", client);
  const missing = await handleAvatarRequest(VALID_FILENAME, client);

  assert.equal(malformed.status, missing.status);
  assert.equal(await malformed.text(), await missing.text());
});
