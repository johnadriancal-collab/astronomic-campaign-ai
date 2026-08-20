// The Hub's single, canonical list of top-level product areas, shared by
// the site header and read by both app sidebars. Astro AI is the Hub's
// only AI entry point and lives at "/" using the original Astro hero
// design (see app/page.tsx) -- /astro-ai still works as a redirect to "/"
// for old links, but there is no separate Astro AI page. Campaign Builder
// (the old Apollo prompt-based plan generator, relocated to
// /campaign-builder) is intentionally NOT listed here. It's reached only
// through Campaign Manager's explicit "Create Campaign -> Apollo" choice,
// not as its own top-level product, so it can never again be confused for
// Astro AI by sharing a nav slot.
export interface TopLevelNavArea {
  href: string;
  label: string;
}

export const TOP_LEVEL_NAV_AREAS: TopLevelNavArea[] = [
  { href: "/", label: "Astro AI" },
  { href: "/manager", label: "Campaign Manager" },
  { href: "/crm", label: "CRM" },
];
