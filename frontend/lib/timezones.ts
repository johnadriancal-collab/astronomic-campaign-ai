// A curated, human-readable subset of IANA timezones for the sending-window
// timezone picker -- NOT the full ~400-zone IANA database (unwieldy in a
// plain dropdown) and NOT limited to US zones either. The backend accepts
// ANY valid IANA identifier (see validate_mail_timezone in app/models/mail.py)
// -- this list is purely a frontend convenience. Includes zones relevant to
// where this team actually works (Austin, the Philippines, Nigeria) rather
// than defaulting to a US-only list.

export interface TimezoneOption {
  value: string; // canonical IANA identifier -- this is what gets persisted
  label: string; // human-readable display label
}

export const TIMEZONE_OPTIONS: TimezoneOption[] = [
  { value: "America/New_York", label: "Eastern Time (US & Canada)" },
  { value: "America/Chicago", label: "Central Time (US & Canada)" },
  { value: "America/Denver", label: "Mountain Time (US & Canada)" },
  { value: "America/Los_Angeles", label: "Pacific Time (US & Canada)" },
  { value: "America/Anchorage", label: "Alaska" },
  { value: "Pacific/Honolulu", label: "Hawaii" },
  { value: "UTC", label: "UTC" },
  { value: "Europe/London", label: "London" },
  { value: "Europe/Paris", label: "Paris, Berlin, Madrid" },
  { value: "Africa/Lagos", label: "West Africa (Lagos)" },
  { value: "Asia/Manila", label: "Philippines (Manila)" },
  { value: "Asia/Singapore", label: "Singapore" },
  { value: "Asia/Tokyo", label: "Japan" },
  { value: "Australia/Sydney", label: "Sydney" },
];

export const DEFAULT_TIMEZONE = "America/Chicago";

// Falls back to the raw IANA string itself if it isn't in the curated list
// above -- a campaign's stored timezone must always render as *something*
// recognizable, even one this dropdown didn't anticipate (e.g. set via a
// future integration, or a zone outside this shortlist).
export function timezoneLabel(value: string): string {
  return TIMEZONE_OPTIONS.find((tz) => tz.value === value)?.label ?? value;
}

// Ensures a <select> always has an <option> matching the campaign's
// currently-stored value, even if that value isn't in the curated list --
// otherwise the browser would silently fall back to its first option,
// silently corrupting the field the moment the form is saved unchanged.
export function timezoneOptionsIncluding(currentValue: string | null): TimezoneOption[] {
  if (!currentValue || TIMEZONE_OPTIONS.some((tz) => tz.value === currentValue)) {
    return TIMEZONE_OPTIONS;
  }
  return [{ value: currentValue, label: currentValue }, ...TIMEZONE_OPTIONS];
}
