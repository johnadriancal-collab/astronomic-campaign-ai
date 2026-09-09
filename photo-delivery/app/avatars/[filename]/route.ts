/**
 * GET /avatars/:filename -- the only route this project serves. No POST,
 * PUT, DELETE, or listing route exists anywhere in this project (upload
 * and delete remain exclusively Railway's job via Stage 1's
 * ProfilePhotoService -- see that module's own docstring on centralized
 * overwrite precedence, which this project has no part in).
 *
 * Thin wrapper only -- all real logic lives in handleAvatarRequest() /
 * R2ReadClient, which are unit-tested directly without needing this
 * Next.js layer or real credentials.
 */

import { handleAvatarRequest } from "../../../lib/handle-avatar-request.ts";
import { R2ReadClient } from "../../../lib/r2-read-client.ts";

// One client instance per server process -- R2ReadClient itself defers
// real network/credential use until the first actual getObject() call
// (see its own docstring), so constructing this at module load time does
// not require R2 to be configured yet.
const client = new R2ReadClient();

export async function GET(_request: Request, context: { params: Promise<{ filename: string }> }): Promise<Response> {
  const { filename } = await context.params;
  return handleAvatarRequest(filename, client);
}
