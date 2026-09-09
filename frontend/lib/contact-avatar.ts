/**
 * Pure helpers for the Contact avatar fallback state (Profile Photos Stage 1)
 * -- no photo stored yet, or a photo URL that fails to load. Kept separate
 * from the presentational component so the initials logic is independently
 * testable without rendering anything.
 */

export function getContactInitials(firstName: string | null, lastName: string | null, email: string | null): string {
  const first = (firstName ?? "").trim();
  const last = (lastName ?? "").trim();
  if (first && last) return (first[0] + last[0]).toUpperCase();
  if (first) return first.slice(0, 2).toUpperCase();
  if (last) return last.slice(0, 2).toUpperCase();
  const trimmedEmail = (email ?? "").trim();
  if (trimmedEmail) return trimmedEmail[0].toUpperCase();
  return "?";
}
