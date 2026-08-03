import { FlaskConical, RefreshCw } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import type { EmailMessageSource } from "@/lib/api";

/**
 * Every engagement number/record in Campaign Manager must say where it
 * came from -- this is the one place that mapping lives. "Live Apollo"
 * (an unsynced, real-time call) doesn't exist anywhere in this app today
 * -- every Apollo-derived value is a manual, explicit sync, never a
 * live/real-time read -- so only these two ever actually render:
 *   - "Synced Apollo": a real Apollo response, persisted by a manual sync,
 *     always paired with a last-synced timestamp elsewhere in the UI.
 *   - "Test Fixture": fabricated locally, zero Apollo calls involved --
 *     must never be visually confusable with real data.
 */
export function DataSourceBadge({ source }: { source: EmailMessageSource | "synced_apollo" }) {
  if (source === "test_fixture") {
    return (
      <Badge variant="outline" className="gap-1 rounded-full border-amber-500/40 font-normal text-amber-700 dark:text-amber-400">
        <FlaskConical className="h-3 w-3" />
        Test Fixture
      </Badge>
    );
  }
  return (
    <Badge variant="outline" className="gap-1 rounded-full border-emerald-500/40 font-normal text-emerald-700 dark:text-emerald-400">
      <RefreshCw className="h-3 w-3" />
      Synced Apollo
    </Badge>
  );
}
