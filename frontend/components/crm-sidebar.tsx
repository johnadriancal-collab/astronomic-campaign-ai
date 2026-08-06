"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Contact, LayoutDashboard, ListPlus, SlidersHorizontal, Sparkles, Users } from "lucide-react";
import { cn } from "@/lib/utils";

const SECTIONS = [
  { href: "/crm", label: "Contacts", icon: Users, exact: true },
  { href: "/crm/import", label: "Import CSV", icon: ListPlus },
  { href: "/crm/fields", label: "Custom Fields", icon: SlidersHorizontal },
];

function isActive(pathname: string, href: string, exact?: boolean) {
  return exact ? pathname === href : pathname === href || pathname.startsWith(`${href}/`);
}

export function CrmSidebar() {
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
        <Link
          href="/manager"
          className="flex items-center gap-2 rounded-lg px-3 py-2 text-sm text-muted-foreground transition-colors hover:bg-sidebar-accent hover:text-sidebar-accent-foreground"
        >
          <LayoutDashboard className="h-4 w-4" />
          Campaign Manager
        </Link>
        <div className="flex items-center gap-2 rounded-lg bg-sidebar-accent px-3 py-2 text-sm font-medium text-sidebar-accent-foreground">
          <Contact className="h-4 w-4" />
          CRM
        </div>
      </div>

      <div className="px-3">
        <div className="h-px bg-sidebar-border" />
      </div>

      <nav className="flex flex-1 flex-col gap-0.5 px-3 py-3">
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
