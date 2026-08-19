"use client";

import { useState } from "react";
import { Mail } from "lucide-react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogClose,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogPopup,
  DialogTitle,
} from "@/components/ui/dialog";
import { ApiError, startGoogleMailboxConnect } from "@/lib/api";

// Astronomic Mail Phase 2 (Google Workspace Mailbox Connection). Clicking
// "Connect Google Workspace" asks the backend for a Google authorize URL
// (see /mailboxes/google/start) and then does a full top-level browser
// navigation to it -- this is NOT a fetch Google redirects back to; Google
// lands the browser on the backend's own /mailboxes/google/callback
// directly, which then 302s back to /manager/emails?connected=1 (or
// ?error=...), handled by that page on mount. Nothing here stores a
// credential, calls Google directly, or marks anything connected itself.
export function ConnectEmailModal({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const [connecting, setConnecting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleConnectGoogle() {
    setConnecting(true);
    setError(null);
    try {
      const { authorize_url } = await startGoogleMailboxConnect();
      window.location.href = authorize_url;
    } catch (err) {
      setError(
        err instanceof ApiError
          ? `Couldn't start Google connection (${err.status}): ${err.message}`
          : "Couldn't reach the backend."
      );
      setConnecting(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogPopup className="max-w-sm">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Mail className="h-4 w-4" />
            Connect Email
          </DialogTitle>
          <DialogDescription>
            Connect a Google Workspace (Gmail) inbox to Astronomic Mail. You&apos;ll sign in with Google and approve
            the requested permissions.
          </DialogDescription>
        </DialogHeader>

        {error && (
          <Alert variant="destructive">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}

        <Button type="button" className="w-full gap-1.5" onClick={handleConnectGoogle} disabled={connecting}>
          <Mail className="h-4 w-4" />
          {connecting ? "Redirecting to Google..." : "Connect Google Workspace"}
        </Button>

        <DialogFooter>
          <DialogClose render={<Button type="button" variant="outline">Cancel</Button>} />
        </DialogFooter>
      </DialogPopup>
    </Dialog>
  );
}
