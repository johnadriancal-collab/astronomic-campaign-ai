import type { MailCampaignStats } from "@/lib/api";

// Campaign detail stats strip (2026-09-17) -- Open rate / Reply rate /
// Unsub rate / Bounce rate, QuickMail-density reference. Reply rate and
// Unsub rate are real, computed backend-side (MailCampaignStatsService).
// Open rate and Bounce rate have NO backing data anywhere for Astronomic
// Mail (confirmed by investigation, not assumed) -- they are hardcoded
// "Not tracked" here, never a fabricated 0%, and never derived from
// `stats` at all (there is no field for either on MailCampaignStats).
export function MailCampaignStatsStrip({ stats }: { stats: MailCampaignStats | null }) {
  return (
    <div className="grid grid-cols-1 divide-y divide-border rounded-lg border border-border text-sm sm:grid-cols-4 sm:divide-y-0 sm:divide-x">
      <StatItem label="Open rate" value="Not tracked" />
      <StatItem label="Reply rate" value={stats ? `${stats.reply_rate_percent}%` : "…"} />
      <StatItem label="Unsub rate" value={stats ? `${stats.unsub_rate_percent}%` : "…"} />
      <StatItem label="Bounce rate" value="Not tracked" />
    </div>
  );
}

function StatItem({ label, value }: { label: string; value: string }) {
  const tracked = value !== "Not tracked";
  return (
    <div className="flex flex-col gap-0.5 px-4 py-2.5">
      <span className="text-xs text-muted-foreground">{label}</span>
      <span className={tracked ? "font-medium tabular-nums" : "text-sm text-muted-foreground/70"}>{value}</span>
    </div>
  );
}
