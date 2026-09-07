// Client CRM sidebar navigation config -- same "testable as plain data"
// split as lib/manager-nav.ts. Stage 1C deliberately lists ONLY "Clients"
// -- no Pipeline/Follow-ups/Tasks/Analytics entries, disabled or
// otherwise: those don't exist yet, and a disabled/fake nav item would
// misrepresent what this area actually does today. Add a new entry here
// only once its own page genuinely exists.

export interface ClientCrmNavSection {
  href: string;
  label: string;
  exact?: boolean;
}

export const CLIENT_CRM_NAV_SECTIONS: ClientCrmNavSection[] = [{ href: "/clients", label: "Clients", exact: true }];
