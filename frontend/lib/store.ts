import { create } from "zustand";
import type { Campaign } from "@/lib/api";

interface CampaignState {
  desiredProspectCount: number;
  campaign: Campaign | null;
  setDesiredProspectCount: (count: number) => void;
  setCampaign: (campaign: Campaign) => void;
  reset: () => void;
}

// Single source of truth on the frontend too: one Campaign object, updated
// (never replaced with a differently-shaped object) at each stage. There's
// no separate "plan" or "search" or "report" field that could show stale
// or inconsistent data -- everything reads from the one stored campaign.
export const useCampaignStore = create<CampaignState>((set) => ({
  desiredProspectCount: 25,
  campaign: null,
  setDesiredProspectCount: (desiredProspectCount) => set({ desiredProspectCount }),
  setCampaign: (campaign) => set({ campaign }),
  reset: () => set({ desiredProspectCount: 25, campaign: null }),
}));
