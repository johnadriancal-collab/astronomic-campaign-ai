import { Badge } from "@/components/ui/badge";
import type { CampaignFilters } from "@/lib/api";

const GROUPS: { key: keyof CampaignFilters; label: string }[] = [
  { key: "titles", label: "Titles" },
  { key: "locations", label: "Locations" },
  { key: "industries", label: "Industries" },
  { key: "company_size", label: "Company size" },
  { key: "funding_stage", label: "Funding stage" },
];

export function FilterBadges({ filters }: { filters: CampaignFilters }) {
  const populated = GROUPS.filter((g) => filters[g.key]?.length);

  if (populated.length === 0) {
    return <p className="text-sm text-muted-foreground">No targeting filters generated.</p>;
  }

  return (
    <div className="space-y-3">
      {populated.map((group) => (
        <div key={group.key} className="flex flex-wrap items-baseline gap-2">
          <span className="w-28 shrink-0 text-xs font-medium text-muted-foreground">
            {group.label}
          </span>
          <div className="flex flex-wrap gap-1.5">
            {filters[group.key].map((value) => (
              <Badge key={value} variant="secondary" className="rounded-full font-normal">
                {value}
              </Badge>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}
