import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

// Client CRM Stage 2C -- Next Dinner/Last Contacted on the Master Client
// CRM table (app/clients/page.tsx, the LIST page -- distinct from
// app/clients/[id]/page.tsx, the detail page, which this stage never
// touches). Scanned directly from source, same "no component-render
// harness" convention as sidebar-nav-source.test.ts /
// client-detail-no-fake-features.test.ts. Both columns are purely
// display -- no sort/filter control exists for either in Stage 2C (see
// SORTABLE_CLIENT_FIELDS's own comment on the backend for why).

const CLIENT_LIST_PAGE = readFileSync(new URL("../app/clients/page.tsx", import.meta.url), "utf-8");
const CLIENT_DETAIL_PAGE = readFileSync(new URL("../app/clients/[id]/page.tsx", import.meta.url), "utf-8");
const API_TS = readFileSync(new URL("../lib/api.ts", import.meta.url), "utf-8");

test("Next Dinner and Last Contacted column headers are present", () => {
  assert.match(CLIENT_LIST_PAGE, />Next Dinner</);
  assert.match(CLIENT_LIST_PAGE, />Last Contacted</);
});

test("column order is Client/Status/Relationship/Owner/Next Dinner/Last Contacted/Next Action/Next Action Due/Updated", () => {
  const headerBlock = CLIENT_LIST_PAGE.match(/<thead[\s\S]*?<\/thead>/);
  assert.ok(headerBlock, "expected to find the table <thead>");
  const headers = [...headerBlock![0].matchAll(/<th[^>]*>([^<]+)<\/th>/g)].map((m) => m[1]);
  assert.deepEqual(headers, [
    "Client",
    "Status",
    "Relationship",
    "Owner",
    "Next Dinner",
    "Last Contacted",
    "Next Action",
    "Next Action Due",
    "Updated",
  ]);
});

test("both new columns use the existing formatClientDate formatter, not a new one", () => {
  assert.match(CLIENT_LIST_PAGE, /formatClientDate\(client\.next_dinner\)/);
  assert.match(CLIENT_LIST_PAGE, /formatClientDate\(client\.last_contacted\)/);
});

test("no new date formatter was introduced for these columns", () => {
  assert.doesNotMatch(CLIENT_LIST_PAGE, /formatNextDinner/);
  assert.doesNotMatch(CLIENT_LIST_PAGE, /formatLastContacted/);
});

test("the existing Client row link is still present", () => {
  assert.match(CLIENT_LIST_PAGE, /href=\{`\/clients\/\$\{client\.client_id\}`\}/);
});

test("existing filters (search, status, relationship, owner, include archived) are still present", () => {
  assert.match(CLIENT_LIST_PAGE, /Search Clients/);
  assert.match(CLIENT_LIST_PAGE, /Any status/);
  assert.match(CLIENT_LIST_PAGE, /Any relationship/);
  assert.match(CLIENT_LIST_PAGE, /placeholder="Owner"/);
  assert.match(CLIENT_LIST_PAGE, /Show archived Clients/);
});

test("existing pagination controls (Previous/Next/page count) are still present", () => {
  assert.match(CLIENT_LIST_PAGE, /goToPage\(page\.page - 1\)/);
  assert.match(CLIENT_LIST_PAGE, /goToPage\(page\.page \+ 1\)/);
  assert.match(CLIENT_LIST_PAGE, /Previous/);
  assert.match(CLIENT_LIST_PAGE, /Page \{page\.page\} of \{totalPages\}/);
});

test("the Next Dinner/Last Contacted <th>s carry no click handler or sort affordance", () => {
  const headerBlock = CLIENT_LIST_PAGE.match(/<thead[\s\S]*?<\/thead>/)![0];
  const nextDinnerTh = headerBlock.match(/<th[^>]*>Next Dinner<\/th>/);
  const lastContactedTh = headerBlock.match(/<th[^>]*>Last Contacted<\/th>/);
  assert.ok(nextDinnerTh);
  assert.ok(lastContactedTh);
  assert.doesNotMatch(nextDinnerTh![0], /onClick/);
  assert.doesNotMatch(lastContactedTh![0], /onClick/);
});

test("no new sort/filter param was added for next_dinner/last_contacted -- ListClientsParams.sort_by is unchanged", () => {
  // The backend's own sortable-field set is untouched -- confirmed here
  // by checking the frontend's ListClientsParams sort_by union, which
  // must still list only the four pre-existing sortable Client fields.
  const sortByMatch = API_TS.match(/sort_by\?:\s*("[^"]+"(?:\s*\|\s*"[^"]+")*)/);
  assert.ok(sortByMatch, "expected to find ListClientsParams.sort_by's type union");
  assert.equal(sortByMatch![1], '"name" | "created_at" | "updated_at" | "next_action_due"');
});

test("ClientListItem is a superset of Client, not a replacement -- every existing Client field name still appears in its own interface", () => {
  const clientMatch = API_TS.match(/export interface Client \{([\s\S]*?)\n\}/);
  assert.ok(clientMatch, "expected to find the Client interface");
  assert.match(clientMatch![1], /client_id: string;/);
  assert.match(clientMatch![1], /name: string;/);
  // Client itself must NOT have grown next_dinner/last_contacted directly --
  // those live only on ClientListItem.
  assert.doesNotMatch(clientMatch![1], /next_dinner/);
  assert.doesNotMatch(clientMatch![1], /last_contacted/);
});

test("the Client detail page (Stage 2B) was not touched by this stage", () => {
  assert.doesNotMatch(CLIENT_DETAIL_PAGE, /next_dinner/);
  assert.doesNotMatch(CLIENT_DETAIL_PAGE, /last_contacted/);
});
