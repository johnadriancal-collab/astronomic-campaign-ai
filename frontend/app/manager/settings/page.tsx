import Link from "next/link";
import { Mail, Settings } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { buttonVariants } from "@/components/ui/button";
import { cn } from "@/lib/utils";

// Sending inboxes moved to their own canonical home (Campaign Manager ->
// Emails, see app/manager/emails/page.tsx) -- Settings no longer owns any
// mailbox UI of its own, so there is only ever one place to manage inboxes.
// This page is a plain "not built yet" shell (matching ManagerPlaceholder's
// pattern) for whatever workspace-level preferences land here later, plus a
// pointer to where mailbox management actually lives now.
export default function SettingsPage() {
  return (
    <div className="mx-auto max-w-2xl px-6 py-20 text-center">
      <div className="mx-auto mb-5 flex h-12 w-12 items-center justify-center rounded-2xl bg-secondary/60 text-muted-foreground">
        <Settings className="h-5 w-5" />
      </div>
      <div className="mb-3 flex items-center justify-center gap-2">
        <h1 className="font-serif text-xl font-medium tracking-tight">Settings</h1>
        <Badge variant="outline" className="rounded-full font-normal text-muted-foreground">
          Coming soon
        </Badge>
      </div>
      <p className="text-sm text-muted-foreground">
        Workspace-level preferences will be configured here. Sending inboxes are managed under Emails.
      </p>
      <Link href="/manager/emails" className={cn(buttonVariants({ variant: "outline", size: "sm" }), "mt-4 gap-1.5")}>
        <Mail className="h-4 w-4" />
        Go to Emails
      </Link>
    </div>
  );
}
