// Pure helpers for the open-ended (no predefined options) multi-select tag
// editor -- kept separate from the component so the add/remove/dedupe rules
// are unit-testable without rendering React.

export function addTagValue(current: string[], raw: string): string[] {
  const value = raw.trim();
  if (!value) return current; // never add an empty value
  if (current.includes(value)) return current; // never add an exact duplicate
  return [...current, value];
}

export function removeTagValue(current: string[], value: string): string[] {
  return current.filter((v) => v !== value);
}
