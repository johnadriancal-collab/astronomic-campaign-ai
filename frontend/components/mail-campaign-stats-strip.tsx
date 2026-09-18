import type { MailCampaignStats } from "@/lib/api";
import { openRateDisplay } from "@/lib/mail";

// Campaign detail stats strip (2026-09-17, real Open rate 2026-09-18) --
// Open rate / Reply rate / Unsub rate / Bounce rate, QuickMail-density
// reference. Reply rate and Unsub rate are real, computed backend-side
// (MailCampaignStatsService) -- so is Open rate now, opt-in per campaign
// (see lib/mail.ts's openRateDisplay(), the SAME helper the Campaigns
// list uses, so the two can never show conflicting copy). Bounce rate
// has NO backing data anywhere for Astronomic Mail (confirmed by
// investigation, not assumed) -- it is hardcoded "Not tracked" here,
// never a fabricated 0%.
export function MailCampaignStatsStrip({ stats }: { stats: MailCampaignStats | null }) {
  const openRate = stats ? openRateDisplay(stats) : null;
  return (
    <div className="grid grid-cols-1 divide-y divide-border rounded-lg border border-border text-sm sm:grid-cols-4 sm:divide-y-0 sm:divide-x">
      <StatItem label="Open rate" value={openRate ? openRate.text : "…"} tooltip={openRate?.tooltip} />
      <StatItem label="Reply rate" value={stats ? `${stats.reply_rate_percent}%` : "…"} />
      <StatItem label="Unsub rate" value={stats ? `${stats.unsub_rate_percent}%` : "…"} />
      <StatItem label="Bounce rate" value="Not tracked" />
    </div>
  );
}

function StatItem({ label, value, tooltip }: { label: string; value: string; tooltip?: string }) {
  const tracked = value !== "Not tracked" && value !== "—";
  return (
    <div className="flex items-center justify-center gap-1.5 whitespace-nowrap px-4 py-2" title={tooltip}>
      <span className="text-xs text-muted-foreground">{label}:</span>
      <span className={tracked ? "font-medium tabular-nums" : "text-sm text-muted-foreground/70"}>{value}</span>
    </div>
  );
}
