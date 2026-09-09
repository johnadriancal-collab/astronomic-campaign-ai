/**
 * Cloudflare R2 read client for this project -- fetching an object's bytes
 * is the ONLY operation performed here. This file is deliberately the
 * single place `@aws-sdk/client-s3` is ever imported in this project; a
 * structural test (r2-read-client.structural.test.ts) scans this file's
 * own source and fails if any S3 write, delete, or listing operation ever
 * appears here -- this is what makes it verifiable, not just asserted,
 * that this project can never write, delete, or list, regardless of what
 * the underlying R2 API token technically permits.
 *
 * Credentials come ONLY from environment variables (R2_BUCKET_NAME,
 * R2_ENDPOINT_URL, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY) -- this
 * project's OWN, separate, read-only R2 token, never Railway's
 * write/delete token (see the approved architecture: two distinct R2 API
 * tokens, one per project, one per required permission level).
 *
 * Constructed lazily (first real call, not module load or class
 * construction) so this module can be imported -- and this whole
 * project can build/typecheck -- even before real R2 credentials exist,
 * matching Stage 1's own R2ObjectStorageClient precedent
 * (app/storage/r2_object_storage_client.py) of never touching the
 * network or requiring configuration merely to be imported.
 */

import { GetObjectCommand, S3Client } from "@aws-sdk/client-s3";

export interface ReadOnlyObjectStorageClient {
  /** Returns the object's bytes, or null if it doesn't exist (NoSuchKey).
   * Any other failure (auth, network, misconfiguration) throws.
   * Explicitly `Uint8Array<ArrayBuffer>` (not the wider, default
   * `Uint8Array<ArrayBufferLike>`) -- the narrower form is what
   * `Response`/`Blob` (BodyInit/BlobPart) actually accept under current
   * TypeScript DOM lib types, and every real byte array here is always
   * backed by a plain ArrayBuffer, never a SharedArrayBuffer. */
  getObject(key: string): Promise<Uint8Array<ArrayBuffer> | null>;
}

export class R2NotConfiguredError extends Error {}

export class R2ReadClient implements ReadOnlyObjectStorageClient {
  private client: S3Client | null = null;

  constructor(
    private readonly bucketName = process.env.R2_BUCKET_NAME,
    private readonly endpointUrl = process.env.R2_ENDPOINT_URL,
    private readonly accessKeyId = process.env.R2_ACCESS_KEY_ID,
    private readonly secretAccessKey = process.env.R2_SECRET_ACCESS_KEY
  ) {}

  private getClient(): S3Client {
    const missing = [
      ["R2_BUCKET_NAME", this.bucketName],
      ["R2_ENDPOINT_URL", this.endpointUrl],
      ["R2_ACCESS_KEY_ID", this.accessKeyId],
      ["R2_SECRET_ACCESS_KEY", this.secretAccessKey],
    ]
      .filter(([, value]) => !value)
      .map(([name]) => name);
    if (missing.length > 0) {
      throw new R2NotConfiguredError(
        `Cloudflare R2 is not configured -- missing: ${missing.join(", ")}. Expected until the production-infrastructure stage provisions real credentials for this project; no credential values are included in this error.`
      );
    }
    if (!this.client) {
      this.client = new S3Client({
        region: "auto",
        endpoint: this.endpointUrl,
        credentials: { accessKeyId: this.accessKeyId as string, secretAccessKey: this.secretAccessKey as string },
      });
    }
    return this.client;
  }

  async getObject(key: string): Promise<Uint8Array<ArrayBuffer> | null> {
    const client = this.getClient();
    try {
      const result = await client.send(new GetObjectCommand({ Bucket: this.bucketName, Key: key }));
      const body = result.Body;
      if (!body) return null;
      const bytes = await (body as { transformToByteArray: () => Promise<Uint8Array> }).transformToByteArray();
      // Copy into a fresh, definitely-plain-ArrayBuffer-backed Uint8Array --
      // the AWS SDK's own return type is the wider Uint8Array<ArrayBufferLike>,
      // which BodyInit/BlobPart don't structurally accept (see this
      // interface's own docstring).
      return new Uint8Array(bytes);
    } catch (error) {
      const name = (error as { name?: string })?.name;
      if (name === "NoSuchKey" || name === "NotFound") return null;
      throw error;
    }
  }
}
