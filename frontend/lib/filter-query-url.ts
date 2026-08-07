// More Filters URL persistence -- plain, debuggable query params (f0_field=state&
// f0_op=eq&f0_value=Texas&f0_value=California) rather than an opaque base64url JSON
// blob, so a filtered URL is readable/editable directly in the address bar. Repeated
// `f{i}_value` entries (URLSearchParams' native multi-value support) carry a
// condition's multiple values -- never comma-joined, since several real CRM option
// strings (e.g. "Collectibles (e.g., art, wine, watches)") contain literal commas.

import type { FilterCondition, FilterQuery } from "./api";

const DEFAULT_PAGE = 1;
const DEFAULT_PAGE_SIZE = 50;

export function encodeFilterQuery(query: FilterQuery): URLSearchParams {
  const params = new URLSearchParams();
  if (query.logic === "OR") params.set("logic", "OR");
  if ((query.page ?? DEFAULT_PAGE) !== DEFAULT_PAGE) params.set("page", String(query.page));
  if ((query.page_size ?? DEFAULT_PAGE_SIZE) !== DEFAULT_PAGE_SIZE) params.set("page_size", String(query.page_size));
  if (query.include_archived) params.set("include_archived", "1");
  if (query.sort) {
    params.set("sort_field", query.sort.field);
    params.set("sort_dir", query.sort.direction);
  }
  query.filters.forEach((condition, i) => {
    params.set(`f${i}_field`, condition.field);
    params.set(`f${i}_op`, condition.operator);
    for (const v of valuesOf(condition.value)) params.append(`f${i}_value`, String(v));
  });
  return params;
}

export function decodeFilterQuery(params: URLSearchParams): FilterQuery {
  const logic = params.get("logic") === "OR" ? "OR" : "AND";
  const page = toPositiveInt(params.get("page"), DEFAULT_PAGE);
  const pageSize = toPositiveInt(params.get("page_size"), DEFAULT_PAGE_SIZE);
  const includeArchived = params.get("include_archived") === "1";
  const sortField = params.get("sort_field");
  const sort = sortField ? { field: sortField, direction: (params.get("sort_dir") === "desc" ? "desc" : "asc") as "asc" | "desc" } : null;

  const filters: FilterCondition[] = [];
  for (let i = 0; params.has(`f${i}_field`); i++) {
    const field = params.get(`f${i}_field`) ?? "";
    const operator = params.get(`f${i}_op`) ?? "";
    const values = params.getAll(`f${i}_value`);
    filters.push({ field, operator, value: values.length > 0 ? values : undefined });
  }

  return { filters, logic, page, page_size: pageSize, include_archived: includeArchived, sort };
}

function valuesOf(value: unknown): unknown[] {
  if (value === undefined || value === null || value === "") return [];
  return Array.isArray(value) ? value : [value];
}

function toPositiveInt(raw: string | null, fallback: number): number {
  const n = Number(raw);
  return raw && Number.isInteger(n) && n > 0 ? n : fallback;
}
