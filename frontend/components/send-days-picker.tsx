"use client";

import { isAllSendingDaysSelected, toggleAllSendingDays, toggleSendingDay, WEEKDAY_LABELS } from "@/lib/mail";
import { cn } from "@/lib/utils";

// Shared by the Create Campaign modal and the Mail campaign detail page's
// Schedule card -- the exact same day-pill visual pattern the detail page
// already established, now with an "All days" convenience pill in front.
export function SendDaysPicker({
  days,
  onChange,
  disabled = false,
}: {
  days: number[];
  onChange: (days: number[]) => void;
  disabled?: boolean;
}) {
  const allSelected = isAllSendingDaysSelected(days);

  return (
    <div className="flex flex-wrap gap-1.5">
      <button
        type="button"
        disabled={disabled}
        onClick={() => onChange(toggleAllSendingDays(days))}
        className={cn(
          "h-8 rounded-md border px-2.5 text-xs transition-colors disabled:cursor-not-allowed disabled:opacity-60",
          allSelected ? "border-primary bg-primary/10 font-medium text-primary" : "border-input text-muted-foreground"
        )}
      >
        All days
      </button>
      {WEEKDAY_LABELS.map((label, day) => (
        <button
          key={day}
          type="button"
          disabled={disabled}
          onClick={() => onChange(toggleSendingDay(days, day))}
          className={cn(
            "h-8 w-12 rounded-md border text-xs transition-colors disabled:cursor-not-allowed disabled:opacity-60",
            days.includes(day) ? "border-primary bg-primary/10 font-medium text-primary" : "border-input text-muted-foreground"
          )}
        >
          {label}
        </button>
      ))}
    </div>
  );
}
