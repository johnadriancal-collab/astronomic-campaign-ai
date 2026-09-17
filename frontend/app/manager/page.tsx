import Link from "next/link";
import { ChartColumn, Inbox, Mail, Megaphone, Settings, Users } from "lucide-react";
import { Card, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { MailboxHealthWarning } from "@/components/mailbox-health-warning";

// Overview-card presentation only (2026-09-17): none of these six cards
// carries a not-yet-built badge -- that's a deliberate call by the
// product owner, independent of whether a given section's own page
// (Analytics, Settings) is still a ManagerPlaceholder. Do not
// reintroduce one here without an explicit request; if a section's own
// page is still unbuilt, that page says so itself.
const SECTIONS = [
  {
    href: "/manager/campaigns",
    icon: Megaphone,
    title: "Campaigns",
    description: "Every campaign built via Campaign Builder, with status and progress at a glance.",
  },
  {
    href: "/manager/emails",
    icon: Mail,
    title: "Emails",
    description: "Sending inboxes connected to Astronomic Mail campaigns.",
  },
  {
    href: "/manager/leads",
    icon: Users,
    title: "Leads",
    description: "Prospects across every campaign, with status and history.",
  },
  {
    href: "/manager/inbox",
    icon: Inbox,
    title: "Inbox",
    description: "Replies from leads, unified across all campaigns.",
  },
  {
    href: "/manager/analytics",
    icon: ChartColumn,
    title: "Analytics",
    description: "Send, open, click, and reply performance across campaigns.",
  },
  {
    href: "/manager/settings",
    icon: Settings,
    title: "Settings",
    description: "Workspace-level preferences.",
  },
];

export default function ManagerOverviewPage() {
  return (
    <div className="mx-auto max-w-5xl px-6 py-10">
      <div className="mb-8 animate-in fade-in slide-in-from-bottom-2 duration-500">
        <h1 className="font-serif text-2xl font-medium tracking-tight sm:text-3xl">Campaign Manager</h1>
        <p className="mt-2 max-w-2xl text-sm text-muted-foreground">
          Campaigns built in Campaign Builder are managed here after launch — leads, sequences,
          replies, and performance in one place.
        </p>
      </div>

      <MailboxHealthWarning />

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {SECTIONS.map((section) => (
          <Link key={section.href} href={section.href} className="block">
            <Card className="h-full transition-colors hover:bg-secondary/40">
              <CardHeader>
                <div className="mb-2 flex items-center justify-between">
                  <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-secondary/60 text-muted-foreground">
                    <section.icon className="h-4 w-4" />
                  </div>
                </div>
                <CardTitle>{section.title}</CardTitle>
                <CardDescription>{section.description}</CardDescription>
              </CardHeader>
            </Card>
          </Link>
        ))}
      </div>
    </div>
  );
}
