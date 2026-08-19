"use client";

import { useState } from "react";
import { AlertTriangle } from "lucide-react";
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
import { ApiError, disconnectMailbox, type Mailbox } from "@/lib/api";

// Requires explicit confirmation before calling POST /mailboxes/{id}/disconnect
// -- that call best-effort revokes the stored Google credential and deletes
// it outright, then marks the mailbox disconnected (never deleted, for
// audit history). There is no active send to interrupt in this phase.
export function DisconnectMailboxModal({
  mailbox,
  onOpenChange,
  onDisconnected,
}: {
  mailbox: Mailbox | null;
  onOpenChange: (open: boolean) => void;
  onDisconnected: (mailbox: Mailbox) => void;
}) {
  const [disconnecting, setDisconnecting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleConfirm() {
    if (!mailbox) return;
    setDisconnecting(true);
    setError(null);
    try {
      const updated = await disconnectMailbox(mailbox.mailbox_id);
      onDisconnected(updated);
    } catch (err) {
      setError(
        err instanceof ApiError
          ? `Couldn't disconnect (${err.status}): ${err.message}`
          : "Couldn't reach the backend."
      );
    } finally {
      setDisconnecting(false);
    }
  }

  return (
    <Dialog open={mailbox !== null} onOpenChange={onOpenChange}>
      <DialogPopup className="max-w-sm">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <AlertTriangle className="h-4 w-4" />
            Disconnect this inbox?
          </DialogTitle>
          <DialogDescription>
            {mailbox && `${mailbox.email} `}will no longer be usable by Astronomic Mail campaigns. Its stored
            Google authorization is revoked and removed.
          </DialogDescription>
        </DialogHeader>

        {error && (
          <Alert variant="destructive">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}

        <DialogFooter>
          <DialogClose render={<Button type="button" variant="outline">Cancel</Button>} />
          <Button type="button" variant="destructive" onClick={handleConfirm} disabled={disconnecting}>
            {disconnecting ? "Disconnecting..." : "Disconnect"}
          </Button>
        </DialogFooter>
      </DialogPopup>
    </Dialog>
  );
}
