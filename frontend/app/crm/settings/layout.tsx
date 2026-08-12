"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { cn } from "@/lib/utils";

const TABS = [
  { href: "/crm/settings/activity", label: "Activity Log" },
  { href: "/crm/settings/email-intake", label: "Email Intake" },
];

// Deliberately just a tab strip, not a real dashboard -- the CRM sidebar's
// Settings gear keeps opening /crm/settings/activity directly (unchanged),
// and this layout only adds a way to move between the two real Settings
// sections once you're already inside one of them. No /crm/settings index
// route exists; visiting /crm/settings itself would 404, same as before.
export default function CrmSettingsLayout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();

  return (
    <div>
      <div className="border-b border-border px-6 pt-4">
        <nav className="mx-auto flex max-w-3xl gap-4">
          {TABS.map((tab) => {
            const active = pathname === tab.href || pathname?.startsWith(`${tab.href}/`);
            return (
              <Link
                key={tab.href}
                href={tab.href}
                className={cn(
                  "border-b-2 px-1 pb-3 text-sm transition-colors",
                  active
                    ? "border-primary font-medium text-foreground"
                    : "border-transparent text-muted-foreground hover:text-foreground"
                )}
              >
                {tab.label}
              </Link>
            );
          })}
        </nav>
      </div>
      {children}
    </div>
  );
}
