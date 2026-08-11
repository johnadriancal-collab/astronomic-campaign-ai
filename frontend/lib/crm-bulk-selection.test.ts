import { test } from "node:test";
import assert from "node:assert/strict";
import { fetchAllMatchingContacts, resolveContactsForExport } from "./crm-bulk-selection.ts";
import type { CrmContact, CrmContactPage, FilterQuery } from "./api.ts";

function contact(id: string): CrmContact {
  return { crm_contact_id: id } as CrmContact;
}

const BASE_QUERY: FilterQuery = { filters: [], logic: "AND" };

// --- fetchAllMatchingContacts ---

test("fetchAllMatchingContacts probes then re-fetches everything when the result spans multiple pages", async () => {
  const ids = Array.from({ length: 5 }, (_, i) => `c${i}`);
  let calls = 0;
  const queryContacts = async (query: FilterQuery): Promise<CrmContactPage> => {
    calls++;
    const pageSize = query.page_size ?? 50;
    return { items: ids.slice(0, pageSize).map(contact), total: ids.length, page: 1, page_size: pageSize };
  };
  const result = await fetchAllMatchingContacts(BASE_QUERY, queryContacts);
  assert.deepEqual(result.map((c) => c.crm_contact_id), ids);
  assert.equal(calls, 2); // probe (page_size: 1) + full re-fetch
});

test("fetchAllMatchingContacts skips the second fetch when the probe already returned everything", async () => {
  let calls = 0;
  const queryContacts = async (): Promise<CrmContactPage> => {
    calls++;
    return { items: [contact("only")], total: 1, page: 1, page_size: 1 };
  };
  const result = await fetchAllMatchingContacts(BASE_QUERY, queryContacts);
  assert.deepEqual(result.map((c) => c.crm_contact_id), ["only"]);
  assert.equal(calls, 1);
});

test("fetchAllMatchingContacts requests page_size 1 to probe, then the exact total on the second call", async () => {
  const requestedPageSizes: (number | undefined)[] = [];
  const queryContacts = async (query: FilterQuery): Promise<CrmContactPage> => {
    requestedPageSizes.push(query.page_size);
    if (query.page_size === 1) return { items: [contact("a")], total: 653, page: 1, page_size: 1 };
    return { items: Array.from({ length: 653 }, (_, i) => contact(`c${i}`)), total: 653, page: 1, page_size: query.page_size ?? 653 };
  };
  await fetchAllMatchingContacts(BASE_QUERY, queryContacts);
  assert.deepEqual(requestedPageSizes, [1, 653]);
});

test("fetchAllMatchingContacts ignores whatever page/page_size the caller's query already had", async () => {
  const seenPages: number[] = [];
  const queryContacts = async (query: FilterQuery): Promise<CrmContactPage> => {
    seenPages.push(query.page ?? -1);
    return { items: [contact("a")], total: 1, page: 1, page_size: query.page_size ?? 1 };
  };
  await fetchAllMatchingContacts({ ...BASE_QUERY, page: 7, page_size: 25 }, queryContacts);
  assert.deepEqual(seenPages, [1]); // always probes page 1, regardless of the caller's own page state
});

// --- resolveContactsForExport ---

test("resolveContactsForExport resolves from knownContacts without fetching when every selected id is already known", async () => {
  const known = [contact("a"), contact("b"), contact("c")];
  let fetchCalls = 0;
  const queryContacts = async (): Promise<CrmContactPage> => {
    fetchCalls++;
    return { items: [], total: 0, page: 1, page_size: 1 };
  };
  const result = await resolveContactsForExport(new Set(["a", "c"]), known, BASE_QUERY, queryContacts);
  assert.deepEqual(result.map((c) => c.crm_contact_id), ["a", "c"]);
  assert.equal(fetchCalls, 0);
});

test("resolveContactsForExport fetches the full matching set when selection includes ids outside the current page (export across pages)", async () => {
  const currentPage = [contact("p1-a"), contact("p1-b")]; // only page 1 is "known" -- simulates More Filters/Astro holding just one page
  const fullMatchingSet = [contact("p1-a"), contact("p1-b"), contact("p2-a"), contact("p2-b")];
  const queryContacts = async (query: FilterQuery): Promise<CrmContactPage> => {
    if (query.page_size === 1) return { items: [fullMatchingSet[0]], total: fullMatchingSet.length, page: 1, page_size: 1 };
    return { items: fullMatchingSet, total: fullMatchingSet.length, page: 1, page_size: query.page_size ?? fullMatchingSet.length };
  };
  const selected = new Set(["p1-a", "p2-a"]); // p2-a is NOT in currentPage
  const result = await resolveContactsForExport(selected, currentPage, BASE_QUERY, queryContacts);
  assert.deepEqual(result.map((c) => c.crm_contact_id).sort(), ["p1-a", "p2-a"]);
});

test("resolveContactsForExport preserves the pool's own order, not Set insertion order", async () => {
  const known = [contact("z"), contact("a"), contact("m")];
  const selected = new Set(["a", "z"]); // insertion order a, z -- pool order is z, a, m
  const queryContacts = async (): Promise<CrmContactPage> => ({ items: [], total: 0, page: 1, page_size: 1 });
  const result = await resolveContactsForExport(selected, known, BASE_QUERY, queryContacts);
  assert.deepEqual(result.map((c) => c.crm_contact_id), ["z", "a"]);
});

test("resolveContactsForExport with an empty selection resolves to nothing, without fetching", async () => {
  let fetchCalls = 0;
  const queryContacts = async (): Promise<CrmContactPage> => {
    fetchCalls++;
    return { items: [], total: 0, page: 1, page_size: 1 };
  };
  const result = await resolveContactsForExport(new Set(), [contact("a")], BASE_QUERY, queryContacts);
  assert.deepEqual(result, []);
  assert.equal(fetchCalls, 0);
});
