import Link from "next/link";
import { ChartColumn, Inbox, Mail, Megaphone, Settings, Users } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

const SECTIONS = [
  {
    href: "/manager/campaigns",
    icon: Megaphone,
    title: "Campaigns",
    description: "Every campaign built via Campaign Builder, with status and progress at a glance.",
  },
  {
    href: "/manager/sequences",
    icon: Mail,
    title: "Sequences / Emails",
    description: "The email sequences deployed to Apollo for each campaign.",
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
    description: "Connected mailboxes, sending limits, and workspace preferences.",
  },
];

export default function ManagerOverviewPage() {
  return (
    <div className="mx-auto max-w-5xl px-6 py-10">
      <div className="mb-8 animate-in fade-in slide-in-from-bottom-2 duration-500">
        <h1 className="text-2xl font-semibold tracking-tight sm:text-3xl">Campaign Manager</h1>
        <p className="mt-2 max-w-2xl text-sm text-muted-foreground">
          Campaigns built in Campaign Builder are managed here after launch — leads, sequences,
          replies, and performance in one place. Each section below is being built out next.
        </p>
      </div>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {SECTIONS.map((section) => (
          <Link key={section.href} href={section.href} className="block">
            <Card className="h-full transition-colors hover:bg-secondary/40">
              <CardHeader>
                <div className="mb-2 flex items-center justify-between">
                  <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-secondary/60 text-muted-foreground">
                    <section.icon className="h-4 w-4" />
                  </div>
                  <Badge variant="outline" className="rounded-full font-normal text-muted-foreground">
                    Coming soon
                  </Badge>
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
