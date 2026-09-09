import type { NextConfig } from "next";

// Deliberately empty -- no rewrites needed. The external URL shape Stage 1
// requires (https://photos.astronomicconnect.com/avatars/<uuid>.jpg) is
// already the real route path here (app/avatars/[filename]/route.ts),
// since this is a dedicated project with no other routes to work around.
const nextConfig: NextConfig = {};

export default nextConfig;
