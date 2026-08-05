// Pure comparator for alphabetizing the CRM contact list -- kept separate
// from the page component so the ordering rules are unit-testable without
// rendering React. Contacts with no name at all sort to the END of the
// list (grouped together, tie-broken by email) rather than scattering
// unpredictably or crashing on missing data.

interface NameableContact {
  first_name?: string | null;
  last_name?: string | null;
  email?: string | null;
}

function fullName(contact: NameableContact): string {
  return `${contact.first_name ?? ""} ${contact.last_name ?? ""}`.trim();
}

export function compareContactsByName(a: NameableContact, b: NameableContact): number {
  const nameA = fullName(a);
  const nameB = fullName(b);

  if (nameA && !nameB) return -1; // named contacts always come before nameless ones
  if (!nameA && nameB) return 1;
  if (nameA && nameB) return nameA.localeCompare(nameB, undefined, { sensitivity: "base" });

  // Both nameless -- still deterministic, tie-broken by email.
  const emailA = a.email ?? "";
  const emailB = b.email ?? "";
  return emailA.localeCompare(emailB, undefined, { sensitivity: "base" });
}
