"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  ChartColumn,
  Contact,
  Inbox,
  LayoutDashboard,
  Mail,
  Megaphone,
  Plus,
  Settings,
  Sparkles,
  Users,
} from "lucide-react";
import { buttonVariants } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { cn } from "@/lib/utils";

const SECTIONS = [
  { href: "/manager", label: "Overview", icon: LayoutDashboard, exact: true },
  { href: "/manager/campaigns", label: "Campaigns", icon: Megaphone },
  { href: "/manager/sequences", label: "Sequences / Emails", icon: Mail },
  { href: "/manager/leads", label: "Leads", icon: Users },
  { href: "/manager/inbox", label: "Inbox", icon: Inbox },
  { href: "/manager/analytics", label: "Analytics", icon: ChartColumn },
  { href: "/manager/settings", label: "Settings", icon: Settings },
];

function isActive(pathname: string, href: string, exact?: boolean) {
  return exact ? pathname === href : pathname === href || pathname.startsWith(`${href}/`);
}

export function ManagerSidebar() {
  const pathname = usePathname();

  return (
    <aside className="flex w-60 shrink-0 flex-col border-r border-sidebar-border bg-sidebar text-sidebar-foreground">
      <div className="flex flex-col gap-1 p-3">
        <Link
          href="/"
          className="flex items-center gap-2 rounded-lg px-3 py-2 text-sm text-muted-foreground transition-colors hover:bg-sidebar-accent hover:text-sidebar-accent-foreground"
        >
          <Sparkles className="h-4 w-4" />
          Astro AI
        </Link>
        <div className="flex items-center gap-2 rounded-lg bg-sidebar-accent px-3 py-2 text-sm font-medium text-sidebar-accent-foreground">
          <LayoutDashboard className="h-4 w-4" />
          Campaign Manager
        </div>
        <Link
          href="/crm"
          className="flex items-center gap-2 rounded-lg px-3 py-2 text-sm text-muted-foreground transition-colors hover:bg-sidebar-accent hover:text-sidebar-accent-foreground"
        >
          <Contact className="h-4 w-4" />
          CRM
        </Link>
      </div>

      <div className="px-3">
        <Separator className="bg-sidebar-border" />
      </div>

      <div className="p-3">
        <Link href="/manager/campaigns/new" className={cn(buttonVariants({ size: "sm" }), "w-full gap-1.5")}>
          <Plus className="h-4 w-4" />
          Create Campaign
        </Link>
      </div>

      <nav className="flex flex-1 flex-col gap-0.5 px-3 pb-4">
        {SECTIONS.map((section) => {
          const active = isActive(pathname, section.href, section.exact);
          const Icon = section.icon;
          return (
            <Link
              key={section.href}
              href={section.href}
              className={cn(
                "flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm transition-colors",
                active
                  ? "bg-sidebar-accent font-medium text-sidebar-accent-foreground"
                  : "text-sidebar-foreground/70 hover:bg-sidebar-accent/60 hover:text-sidebar-accent-foreground"
              )}
            >
              <Icon className="h-4 w-4" />
              {section.label}
            </Link>
          );
        })}
      </nav>
    </aside>
  );
}
