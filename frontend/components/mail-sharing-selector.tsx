"use client";

import type { MailCampaignSharing } from "@/lib/api";
import { cn } from "@/lib/utils";

// Shared by the Create Campaign modal and the Mail campaign detail page's
// Campaign Settings card. A stored preference only -- see
// MailCampaignSharing's docstring in app/models/mail.py for why nothing
// actually enforces it yet.
export function SharingSelector({
  value,
  onChange,
  disabled = false,
}: {
  value: MailCampaignSharing;
  onChange: (value: MailCampaignSharing) => void;
  disabled?: boolean;
}) {
  return (
    <div className="grid grid-cols-2 gap-2">
      <button
        type="button"
        disabled={disabled}
        onClick={() => onChange("everyone")}
        className={cn(
          "rounded-md border px-3 py-2 text-left text-sm transition-colors disabled:cursor-not-allowed disabled:opacity-60",
          value === "everyone" ? "border-primary bg-primary/10" : "border-input hover:bg-secondary/40"
        )}
      >
        <p className="font-medium">Everyone</p>
        <p className="text-xs text-muted-foreground">Visible to workspace members</p>
      </button>
      <button
        type="button"
        disabled={disabled}
        onClick={() => onChange("only_me")}
        className={cn(
          "rounded-md border px-3 py-2 text-left text-sm transition-colors disabled:cursor-not-allowed disabled:opacity-60",
          value === "only_me" ? "border-primary bg-primary/10" : "border-input hover:bg-secondary/40"
        )}
      >
        <p className="font-medium">Only me</p>
        <p className="text-xs text-muted-foreground">Private to the campaign owner</p>
      </button>
    </div>
  );
}
