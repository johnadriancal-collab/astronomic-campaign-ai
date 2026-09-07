"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Briefcase, Contact, LayoutDashboard, Sparkles, Users } from "lucide-react";
import { CLIENT_CRM_NAV_SECTIONS } from "@/lib/client-crm-nav";
import { cn } from "@/lib/utils";

const SECTION_ICONS: Record<string, typeof Users> = {
  "/clients": Users,
};

function isActive(pathname: string, href: string, exact?: boolean) {
  return exact ? pathname === href : pathname === href || pathname.startsWith(`${href}/`);
}

// Stage 1C -- only "Clients" exists in this area yet. Other sections of
// this product surface are intentionally not listed here, disabled or
// otherwise, until their own stages land (see lib/client-crm-nav.ts).
export function ClientCrmSidebar() {
  const pathname = usePathname();

  return (
    <aside className="sticky top-16 flex h-[calc(100vh-4rem)] w-60 shrink-0 flex-col overflow-y-auto border-r border-sidebar-border bg-sidebar text-sidebar-foreground">
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
        <Link
          href="/crm"
          className="flex items-center gap-2 rounded-lg px-3 py-2 text-sm text-muted-foreground transition-colors hover:bg-sidebar-accent hover:text-sidebar-accent-foreground"
        >
          <Contact className="h-4 w-4" />
          Contacts
        </Link>
        <div className="flex items-center gap-2 rounded-lg bg-sidebar-accent px-3 py-2 text-sm font-medium text-sidebar-accent-foreground">
          <Briefcase className="h-4 w-4" />
          Client CRM
        </div>
      </div>

      <div className="px-3">
        <div className="h-px bg-sidebar-border" />
      </div>

      <nav className="flex flex-1 flex-col gap-0.5 px-3 py-3">
        {CLIENT_CRM_NAV_SECTIONS.map((section) => {
          const active = isActive(pathname, section.href, section.exact);
          const Icon = SECTION_ICONS[section.href] ?? Users;
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
