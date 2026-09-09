/**
 * Strict validation for the one filename shape this project ever serves --
 * exactly what Stage 1's backend (app/services/profile_photo_service.py)
 * generates: `avatars/${uuid.uuid4()}.jpg`, where uuid.uuid4() always
 * produces a lowercase, hyphenated UUIDv4 string.
 *
 * This is the ONLY gate between an incoming request path and an R2
 * GetObject call. A request whose filename doesn't match this exact shape
 * is rejected before any R2 call is ever made -- this is what makes path
 * traversal ("../../etc/passwd", "..%2f..", an absolute path, anything
 * containing a "/" at all) structurally impossible to reach R2: the regex
 * requires the ENTIRE string to be nothing but the UUID + ".jpg", so any
 * extra character (including any slash) fails the match.
 */

const AVATAR_FILENAME_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.jpg$/;

export function isValidAvatarFilename(filename: string): boolean {
  return AVATAR_FILENAME_PATTERN.test(filename);
}

/** The full R2 object key for a filename already confirmed valid by
 * isValidAvatarFilename() -- callers must validate first; this function
 * does not re-validate, matching the single-responsibility split used
 * throughout this codebase (validation is the caller's job, exactly once). */
export function avatarObjectKey(filename: string): string {
  return `avatars/${filename}`;
}
