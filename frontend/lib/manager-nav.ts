// Campaign Manager sidebar navigation config -- extracted from
// manager-sidebar.tsx so it's testable as plain data (matching this
// codebase's convention of testing pure lib modules, not React components).
//
// `icon` is deliberately omitted here -- it's a React component, not test-
// relevant data, and importing lucide-react into a `node --test` run would
// be pointless. manager-sidebar.tsx maps `href`/`label`/`exact` from this
// array and attaches the icon itself via a small local lookup.

export interface ManagerNavSection {
  href: string;
  label: string;
  exact?: boolean;
}

export const MANAGER_NAV_SECTIONS: ManagerNavSection[] = [
  { href: "/manager", label: "Overview", exact: true },
  { href: "/manager/campaigns", label: "Campaigns" },
  { href: "/manager/emails", label: "Emails" },
  { href: "/manager/leads", label: "Leads" },
  { href: "/manager/inbox", label: "Inbox" },
  { href: "/manager/analytics", label: "Analytics" },
  { href: "/manager/settings", label: "Settings" },
];
