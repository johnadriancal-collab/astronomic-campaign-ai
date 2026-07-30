import { create } from "zustand";
import type { CampaignExecutionReport, CampaignPlan, CampaignSearchResult } from "@/lib/api";

interface CampaignState {
  prompt: string;
  plan: CampaignPlan | null;
  search: CampaignSearchResult | null;
  report: CampaignExecutionReport | null;
  setPrompt: (prompt: string) => void;
  setPlan: (plan: CampaignPlan) => void;
  setSearch: (search: CampaignSearchResult) => void;
  setReport: (report: CampaignExecutionReport) => void;
  reset: () => void;
}

export const useCampaignStore = create<CampaignState>((set) => ({
  prompt: "",
  plan: null,
  search: null,
  report: null,
  setPrompt: (prompt) => set({ prompt }),
  setPlan: (plan) => set({ plan, search: null, report: null }),
  setSearch: (search) => set({ search }),
  setReport: (report) => set({ report }),
  reset: () => set({ prompt: "", plan: null, search: null, report: null }),
}));
