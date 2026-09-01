"use client";

import { useState } from "react";
import { Send } from "lucide-react";
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
import { ApiError, startGmailSendUpgrade, type Mailbox } from "@/lib/api";
import { mailboxDisplayName } from "@/lib/mailboxes";

// Astronomic Mail Gmail-send upgrade (see app/api/mailboxes.py's
// start_gmail_send_upgrade()). Same full-top-level-navigation pattern as
// ConnectEmailModal -- ask the backend for an authorize URL, then send
// the browser there directly. The one thing this modal adds beyond that
// pattern: an explicit warning that the Google account authorized here
// MUST be the SAME one already connected to this mailbox -- a different
// account intentionally fails the backend's account-match check (see
// MailboxService.begin_gmail_send_upgrade()'s own docstring) and changes
// nothing, but a user who doesn't expect that failure would otherwise
// have no idea why it happened.
export function EnableGmailSendingModal({
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
      const { authorize_url } = await startGmailSendUpgrade(mailbox.mailbox_id);
      window.location.href = authorize_url;
    } catch (err) {
      setError(
        err instanceof ApiError
          ? `Couldn't start the Gmail sending upgrade (${err.status}): ${err.message}`
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
            <Send className="h-4 w-4" />
            Enable Gmail sending
          </DialogTitle>
          <DialogDescription>
            {mailbox && `Grants Astronomic Mail permission to send campaign email as ${mailboxDisplayName(mailbox)}. `}
            You&apos;ll be sent to Google to approve the additional permission.
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
          <Send className="h-4 w-4" />
          {starting ? "Redirecting to Google..." : "Continue to Google"}
        </Button>

        <DialogFooter>
          <DialogClose render={<Button type="button" variant="outline">Cancel</Button>} />
        </DialogFooter>
      </DialogPopup>
    </Dialog>
  );
}
