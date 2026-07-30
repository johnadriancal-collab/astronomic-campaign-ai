/**
 * Client for the existing FastAPI backend. Requests go through the
 * `/backend/*` rewrite in next.config.ts, which proxies server-side to
 * BACKEND_ORIGIN -- this keeps the browser same-origin (no CORS) without
 * requiring any change to the FastAPI app itself.
 */

export interface CampaignFilters {
  locations: string[];
  industries: string[];
  titles: string[];
  company_size: string[];
  funding_stage: string[];
}

export interface SequenceStep {
  day: number;
  subject: string;
  body: string;
}

export interface CampaignPlan {
  campaign_name: string;
  filters: CampaignFilters;
  sequence: SequenceStep[];
  launch: boolean;
}

export interface ApolloPerson {
  id: string;
  first_name?: string;
  last_name_obfuscated?: string;
  title?: string;
  has_email?: boolean;
  organization?: {
    name?: string;
  };
}

export interface CampaignSearchResult {
  total_entries?: number;
  people: ApolloPerson[];
}

export interface CampaignExecutionReport {
  campaign_name: string;
  apollo_list_id: string | null;
  apollo_sequence_id: string | null;
  prospects_found: number;
  prospects_enrolled: number;
  activated: boolean;
  errors: string[];
}

export interface CampaignBuildResult {
  plan: CampaignPlan;
  report: CampaignExecutionReport;
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
    this.name = "ApiError";
  }
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`/backend${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  if (!res.ok) {
    const detail = await res.text().catch(() => "");
    throw new ApiError(res.status, detail || `Request to ${path} failed with ${res.status}`);
  }

  return res.json() as Promise<T>;
}

export function previewCampaign(prompt: string): Promise<CampaignPlan> {
  return post<CampaignPlan>("/campaign/preview", { prompt });
}

export function searchProspects(prompt: string): Promise<CampaignSearchResult> {
  return post<CampaignSearchResult>("/campaign/search", { prompt });
}

export function buildCampaign(
  prompt: string,
  autoLaunch = false
): Promise<CampaignBuildResult> {
  const query = autoLaunch ? "?auto_launch=true" : "";
  return post<CampaignBuildResult>(`/campaign${query}`, { prompt });
}
