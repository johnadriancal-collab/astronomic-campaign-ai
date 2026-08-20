"use client";

import { Suspense, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { Loader2, Lock } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ApiError, login } from "@/lib/api";
import { sanitizeNextPath } from "@/lib/auth";

// Astronomic Hub internal login -- a single shared email+password account
// (see app/services/auth_service.py's module docstring for why there's no
// signup/roles here). Preserves the visitor's intended destination via
// `?next=`, sanitized against open-redirect (see lib/auth.ts).
export default function LoginPage() {
  return (
    <Suspense fallback={null}>
      <LoginPageContent />
    </Suspense>
  );
}

function LoginPageContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const next = sanitizeNextPath(searchParams.get("next"));

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!email.trim() || !password) return;
    setLoading(true);
    setError(null);
    try {
      await login(email.trim(), password);
      // A full navigation (not client-side routing) so every server
      // component / middleware check re-evaluates against the
      // now-present session cookie rather than any stale client state.
      window.location.href = next;
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.status === 401
            ? "Incorrect email or password."
            : `Couldn't sign in (${err.status}): ${err.message}`
          : "Couldn't reach the backend."
      );
      setLoading(false);
    }
  }

  return (
    <div className="hero-glow flex min-h-[calc(100vh-3.5rem)] flex-col items-center justify-center px-6 py-16">
      <div className="w-full max-w-sm">
        <div className="mb-8 flex flex-col items-center text-center animate-in fade-in slide-in-from-bottom-2 duration-700">
          <span className="mb-4 inline-flex items-center gap-1.5 rounded-full border border-border/60 bg-secondary/60 px-3 py-1 text-xs text-muted-foreground">
            <Lock className="h-3 w-3 text-primary" />
            ASTRONOMIC HUB
          </span>
          <h1 className="text-balance font-serif text-3xl font-medium tracking-tight sm:text-4xl">Sign in</h1>
          <p className="mt-3 max-w-xs text-balance text-sm text-muted-foreground">
            Internal access only. Enter your Hub credentials to continue.
          </p>
        </div>

        <form
          onSubmit={handleSubmit}
          className="animate-in fade-in slide-in-from-bottom-4 duration-700 [animation-delay:100ms] rounded-lg border border-border bg-card p-5 shadow-sm"
        >
          <div className="space-y-3">
            <div className="space-y-1">
              <label htmlFor="email" className="text-xs font-medium text-muted-foreground">
                Email
              </label>
              <Input
                id="email"
                type="email"
                autoComplete="username"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                disabled={loading}
                required
              />
            </div>
            <div className="space-y-1">
              <label htmlFor="password" className="text-xs font-medium text-muted-foreground">
                Password
              </label>
              <Input
                id="password"
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                disabled={loading}
                required
              />
            </div>
          </div>

          {error && (
            <Alert variant="destructive" className="mt-4">
              <AlertTitle>Couldn&apos;t sign in</AlertTitle>
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}

          <Button type="submit" className="mt-4 w-full gap-1.5" disabled={loading || !email.trim() || !password}>
            {loading ? (
              <>
                <Loader2 className="h-4 w-4 animate-spin" />
                Signing in...
              </>
            ) : (
              "Sign in"
            )}
          </Button>
        </form>
      </div>
    </div>
  );
}
