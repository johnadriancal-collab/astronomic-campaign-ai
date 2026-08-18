import { Inbox } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

// Deliberately static -- no backend model or API call exists for Mailboxes
// yet (Phase 2). No fake "connected" mailboxes are ever shown here, and no
// Google credentials of any kind are involved.
export default function MailMailboxesPage() {
  return (
    <div className="mx-auto max-w-3xl px-6 py-10">
      <div className="mb-6">
        <h1 className="mb-2 font-serif text-2xl font-medium tracking-tight">Mailboxes</h1>
        <p className="text-sm text-muted-foreground">Astronomic Mail -- sending inbox connections.</p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-sm">
            <Inbox className="h-4 w-4" />
            No mailboxes connected
          </CardTitle>
        </CardHeader>
        <CardContent>
          <Alert>
            <AlertTitle>Mailbox connections will be configured in Phase 2.</AlertTitle>
            <AlertDescription>
              This phase (Foundation) has no Google OAuth connection, no Gmail credentials, and no way to actually
              send an email. Campaigns can be drafted, reviewed, and marked ready without a connected mailbox.
            </AlertDescription>
          </Alert>
        </CardContent>
      </Card>
    </div>
  );
}
