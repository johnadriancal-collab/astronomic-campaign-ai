"use client";

import { Mail } from "lucide-react";
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

// Purely informational -- there is no Gmail OAuth, no Google credentials,
// and no way to actually connect a mailbox in this phase (see
// app/models/mail.py's MailboxConfig/Mailbox docstring). This modal never
// starts an OAuth redirect, never calls a backend endpoint, and never marks
// anything as connected -- it only explains what's coming next.
export function ConnectEmailModal({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogPopup className="max-w-sm">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Mail className="h-4 w-4" />
            Connect Email
          </DialogTitle>
          <DialogDescription>
            Google Workspace connection will be available in the next phase. There is no Gmail OAuth, no stored
            credentials, and no sending capability yet -- this is a preview of where inbox connection will live.
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <DialogClose render={<Button type="button">Got it</Button>} />
        </DialogFooter>
      </DialogPopup>
    </Dialog>
  );
}
