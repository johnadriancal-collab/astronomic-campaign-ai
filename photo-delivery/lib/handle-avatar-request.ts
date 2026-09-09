/**
 * The actual request-handling logic for GET /avatars/:filename -- decoupled
 * from Next.js's route-handler plumbing (app/avatars/[filename]/route.ts
 * is a thin wrapper around this) so it's directly testable with a fake
 * ReadOnlyObjectStorageClient, no real R2 credentials or network call
 * needed, mirroring the split already used in the Stage 1 Python backend
 * (ProfilePhotoService vs. the thin FastAPI route in app/api/crm.py).
 *
 * Every response uses one shared cache-header set: 1 year, immutable, at
 * both the browser AND Vercel's own CDN -- safe because every replacement
 * gets a brand-new random UUID object key (see Stage 1's
 * ProfilePhotoService), so a given URL's bytes can never change once
 * published; there is no "stale key" problem to guard against. Per
 * Vercel's own current documentation (vercel.com/docs/caching/cdn-cache,
 * confirmed 2026-09-09): `Cache-Control` alone with `max-age` governs the
 * browser; `Vercel-CDN-Cache-Control` is the header Vercel treats as
 * authoritative for ITS OWN edge cache specifically (never forwarded to
 * the browser or any other CDN); `CDN-Cache-Control` additionally covers
 * any other/downstream CDN. All three are set here, identically, so
 * nothing is left ambiguous.
 */

import { avatarObjectKey, isValidAvatarFilename } from "./validate-avatar-key.ts";
import type { ReadOnlyObjectStorageClient } from "./r2-read-client.ts";

const ONE_YEAR_SECONDS = 60 * 60 * 24 * 365;
const IMMUTABLE_CACHE_CONTROL_VALUE = `public, max-age=${ONE_YEAR_SECONDS}, immutable`;

export const CACHE_HEADERS: Record<string, string> = {
  "Cache-Control": IMMUTABLE_CACHE_CONTROL_VALUE,
  "CDN-Cache-Control": IMMUTABLE_CACHE_CONTROL_VALUE,
  "Vercel-CDN-Cache-Control": IMMUTABLE_CACHE_CONTROL_VALUE,
};

const NOT_FOUND_RESPONSE = () => new Response(null, { status: 404 });

export async function handleAvatarRequest(filename: string, client: ReadOnlyObjectStorageClient): Promise<Response> {
  // Malformed/path-traversal filenames are rejected before any R2 call --
  // and resolve to the exact same plain 404 a genuinely missing object
  // would, so nothing about a request ever reveals whether a key was
  // well-formed-but-absent vs. rejected outright.
  if (!isValidAvatarFilename(filename)) {
    return NOT_FOUND_RESPONSE();
  }

  const bytes = await client.getObject(avatarObjectKey(filename));
  if (bytes === null) {
    return NOT_FOUND_RESPONSE();
  }

  return new Response(bytes, {
    status: 200,
    headers: {
      "Content-Type": "image/jpeg", // hardcoded, not trusted from R2's stored metadata -- every
      // object under avatars/ is guaranteed JPEG by Stage 1's own upload contract (see
      // app/services/profile_photo_service.py's OUTPUT_CONTENT_TYPE), so this is a stronger
      // guarantee than trusting whatever Content-Type happens to be stored.
      ...CACHE_HEADERS,
    },
  });
}
