"use client";

import { useState } from "react";
import { RefreshCw } from "lucide-react";
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
import { ApiError, startGmailReconnect, type Mailbox } from "@/lib/api";
import { mailboxDisplayName } from "@/lib/mailboxes";

// Proactive Gmail OAuth expiration warnings (2026-09-17). Same
// full-top-level-navigation pattern as EnableGmailSendingModal, but for a
// ROUTINE renewal, not a capability upgrade -- see
// MailboxService.begin_gmail_reconnect()'s own docstring. Renews EXACTLY
// this mailbox's current scopes; never grants anything new. Works on a
// mailbox that is still fully CONNECTED -- that's the whole point of
// reconnecting BEFORE the Testing-mode 7-day window actually expires.
export function ReconnectMailboxModal({
  mailbox,
  onOpenChange,
}: {
  mailbox: Mailbox | null;
  onOpenChange: (open: boolean) => void;
}) {
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleContinue() {
    if (!mailbox) return;
    setStarting(true);
    setError(null);
    try {
      const { authorize_url } = await startGmailReconnect(mailbox.mailbox_id);
      window.location.href = authorize_url;
    } catch (err) {
      setError(
        err instanceof ApiError
          ? `Couldn't start reconnecting this inbox (${err.status}): ${err.message}`
          : "Couldn't reach the backend."
      );
      setStarting(false);
    }
  }

  return (
    <Dialog open={mailbox !== null} onOpenChange={onOpenChange}>
      <DialogPopup className="max-w-sm">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <RefreshCw className="h-4 w-4" />
            Reconnect inbox
          </DialogTitle>
          <DialogDescription>
            {mailbox &&
              `Renews ${mailboxDisplayName(mailbox)}'s Google authorization with its current permissions -- nothing new is added. `}
            You&apos;ll be sent to Google to confirm.
          </DialogDescription>
        </DialogHeader>

        <Alert>
          <AlertDescription>
            {mailbox && `When Google asks you to sign in, use the same Google account as ${mailbox.email}. `}
            Signing in with a different account will fail and won&apos;t change this inbox.
          </AlertDescription>
        </Alert>

        {error && (
          <Alert variant="destructive">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}

        <Button type="button" className="w-full gap-1.5" onClick={handleContinue} disabled={starting}>
          <RefreshCw className="h-4 w-4" />
          {starting ? "Redirecting to Google..." : "Continue to Google"}
        </Button>

        <DialogFooter>
          <DialogClose render={<Button type="button" variant="outline">Cancel</Button>} />
        </DialogFooter>
      </DialogPopup>
    </Dialog>
  );
}
