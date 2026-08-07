/**
 * Mirrors the canonical option lists in app/models/crm.py exactly (same
 * question, same order, same wording) -- Astronomic's real Investor
 * Thesis Google Form. Kept as a static frontend constant (not fetched
 * from an endpoint) since these never change per-request; if the backend
 * list ever changes, update both files together.
 */

export const ASSET_TYPE_OPTIONS = [
  "Carbon credits / ESG investments",
  "Collectibles (e.g., art, wine, watches)",
  "Commodities (e.g., gold, oil, agriculture)",
  "Cryptocurrency / digital assets",
  "Fund-of-funds",
  "Hedge funds (multi-asset strategies)",
  "Infrastructure (e.g., toll roads, utilities, airports)",
  "Private credit (e.g., private loans, direct lending)",
  "Private equity",
  "Public equities (stocks, ETFs)",
  "Real estate (direct ownership, syndications, REITs)",
  "Royalty financing",
  "Search funds / ETA (entrepreneurship through acquisition)",
  "Secondaries (e.g., buying/selling LP interests or founder equity)",
  "Structured products (e.g., notes, derivatives)",
  "Venture capital (e.g., angel checks, early-stage startups, high-growth tech)",
];

export const BUSINESS_MODEL_OPTIONS = [
  "Agencies / Services (e.g., marketing, development firms)",
  "Auction or Bidding Platforms",
  "Brokerages",
  "Community-based / Network-led growth",
  "Creator-led / Influencer brands",
  "Direct-to-Consumer (DTC) eCommerce",
  "Franchises",
  "Hardware / Physical products",
  "Licensing / IP-based",
  "Manufacturing",
  "Marketplaces (e.g., Airbnb, Uber-style platforms)",
  "Software as a Service (SaaS)",
  "Subscription / Membership businesses",
  "Web3 / Tokenized business models",
];

export const INDUSTRY_OPTIONS = [
  "Aerospace & Defense",
  "AgTech & Food Production",
  "Artificial Intelligence / Machine Learning",
  "Automotive & Mobility",
  "Biotech & Life Sciences",
  "Climate Tech & Sustainability",
  "Construction & Built Environment",
  "Consumer Goods & Retail",
  "Creative Industries (Media, Music, Photo, etc.)",
  "Cybersecurity",
  "EdTech (Education Technology)",
  "Entertainment & Gaming",
  "Fashion & Apparel",
  "Fintech (Finance & Insurance)",
  "Food & Beverage",
  "GovTech / Civic Tech",
  "Healthcare & HealthTech",
  "HR Tech & Future of Work",
  "Industrial / Manufacturing / Robotics",
  "LegalTech",
  "Marketing & AdTech",
  "Mental Health & Wellness",
  "Real Estate & PropTech",
  "SaaS / Software Infrastructure",
  "Social Media & Creator Economy",
  "Telecom & Connectivity",
  "Travel, Tourism & Hospitality",
  "Veterinary / Animal Health",
];

export const CHECK_SIZE_OPTIONS = [
  "$1k - $10k",
  "$10k - $25k",
  "$25k - $50k",
  "$50k - $100k",
  "$100k - $250k",
  "$250k - $500k",
  "$500k - $1M",
  "$1M - $2M",
  "$2M - $5M",
  "$5M - $10M",
  "$10M+",
];

export const DEAL_STAGE_OPTIONS = [
  "Friends & Family (idea or concept stage, often pre-incorporation)",
  "Pre-Seed (early development, pre-revenue or minimal traction)",
  "Seed (product in market, early customers or pilots)",
  "Series A (scaling phase, revenue traction, team expansion)",
  "Series B or later (growth or expansion stage, institutional rounds)",
  "Fund LP (investor in venture/private equity funds)",
  "Secondary (buying equity from early investors or founders)",
  "Growth Equity (post-Series B+, but still private)",
  "Pre-IPO / Late-Stage Private (companies nearing exit or IPO)",
];

export const MEETING_PREFERENCE_OPTIONS = [
  "In an email intro",
  "I'd do a Zoom call",
  "I'd meet them at a restaurant or coffee shop",
  "I'd host a dinner at my house",
];

export const DEMOGRAPHIC_PREFERENCE_OPTIONS = [
  "I'm open to investing in anyone",
  "I prefer female fundraisers",
  "I prefer male fundraisers",
  "I prefer black fundraisers",
  "I prefer Latino fundraisers",
  "I prefer Asian fundraisers",
  "I prefer Indian fundraisers",
  "I prefer LGBTQ+ fundraisers",
  "I prefer military fundraisers",
];

export const INVESTOR_MODE_OPTIONS = ["Privately", "Institutionally", "Both"];

// Mirrors DIETARY_PREFERENCE_OPTIONS in app/models/crm.py exactly (29 dietary
// options + None + Other = 31 total).
export const DIETARY_PREFERENCE_OPTIONS = [
  "Vegetarian", "Vegan", "Pescatarian", "Pollotarian", "Paleo", "Keto", "Low Carb",
  "Halal", "Kosher", "Gluten-Free", "Dairy-Free", "Lactose-Free", "Nut-Free",
  "Soy-Free", "Egg-Free", "Pork-Free", "Beef-Free", "Shellfish-Free", "Fish-Free",
  "Seafood-Free", "Alcohol-Free", "Sugar-Free", "No Spicy Food", "Wheat-Free",
  "Mollusks-Free", "Grain-Free", "Corn-Free", "MSG-Free", "Seed Oil-Free",
  "None", "Other",
];

export interface ThesisSectionField {
  key: string; // suffix, e.g. "asset_types" -> thesis_private_asset_types / thesis_institutional_asset_types
  label: string;
  options: string[];
}

/** The seven questions Section 2 (private) and Section 3 (institutional) both ask, in form order. */
export const THESIS_SECTION_FIELDS: ThesisSectionField[] = [
  { key: "asset_types", label: "Which types of assets do you invest in or are you interested in?", options: ASSET_TYPE_OPTIONS },
  { key: "business_models", label: "Which business models do you invest in or are you interested in?", options: BUSINESS_MODEL_OPTIONS },
  { key: "industries", label: "Which industries do you invest in or are you interested in?", options: INDUSTRY_OPTIONS },
  { key: "check_sizes", label: "Which size investments are you open to making?", options: CHECK_SIZE_OPTIONS },
  { key: "deal_stages", label: "During which deal stages are you open to investing?", options: DEAL_STAGE_OPTIONS },
  { key: "meeting_preferences", label: "How would you like to meet fundraisers?", options: MEETING_PREFERENCE_OPTIONS },
  { key: "demographic_preferences", label: "Do you have demographic preferences for the people whose companies you invest in?", options: DEMOGRAPHIC_PREFERENCE_OPTIONS },
];
