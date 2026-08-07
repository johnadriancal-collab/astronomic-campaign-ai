import assert from "node:assert/strict";
import { test } from "node:test";
import { decodeFilterQuery, encodeFilterQuery } from "./filter-query-url.ts";

test("encodes a single simple condition", () => {
  const params = encodeFilterQuery({ filters: [{ field: "state", operator: "eq", value: "Texas" }], logic: "AND" });
  assert.equal(params.get("f0_field"), "state");
  assert.equal(params.get("f0_op"), "eq");
  assert.equal(params.get("f0_value"), "Texas");
  assert.equal(params.has("logic"), false); // AND is the default, omitted for a cleaner URL
});

test("encodes multi-value conditions as repeated params, not comma-joined", () => {
  const params = encodeFilterQuery({
    filters: [{ field: "state", operator: "eq", value: ["Texas", "California"] }],
    logic: "AND",
  });
  assert.deepEqual(params.getAll("f0_value"), ["Texas", "California"]);
});

test("a value containing a literal comma round-trips correctly", () => {
  const value = "Collectibles (e.g., art, wine, watches)";
  const params = encodeFilterQuery({ filters: [{ field: "thesis_private_asset_types", operator: "contains_any", value: [value] }], logic: "AND" });
  const decoded = decodeFilterQuery(params);
  assert.deepEqual(decoded.filters[0].value, [value]);
});

test("no-value operators encode with no f0_value entries", () => {
  const params = encodeFilterQuery({ filters: [{ field: "city", operator: "is_empty" }], logic: "AND" });
  assert.deepEqual(params.getAll("f0_value"), []);
  const decoded = decodeFilterQuery(params);
  assert.equal(decoded.filters[0].value, undefined);
});

test("logic OR is preserved, sort/page/page_size round-trip", () => {
  const query = {
    filters: [{ field: "state", operator: "eq", value: "Texas" }],
    logic: "OR" as const,
    page: 3,
    page_size: 25,
    sort: { field: "last_name", direction: "desc" as const },
  };
  const decoded = decodeFilterQuery(encodeFilterQuery(query));
  assert.equal(decoded.logic, "OR");
  assert.equal(decoded.page, 3);
  assert.equal(decoded.page_size, 25);
  assert.deepEqual(decoded.sort, { field: "last_name", direction: "desc" });
});

test("default page/page_size/logic are omitted from the URL entirely", () => {
  const params = encodeFilterQuery({ filters: [], logic: "AND", page: 1, page_size: 50 });
  assert.equal(params.toString(), "");
});

test("decoding an empty URLSearchParams yields defaults with no filters", () => {
  const decoded = decodeFilterQuery(new URLSearchParams());
  assert.deepEqual(decoded, { filters: [], logic: "AND", page: 1, page_size: 50, include_archived: false, sort: null });
});

test("multiple conditions round-trip independently", () => {
  const query = {
    filters: [
      { field: "state", operator: "eq", value: "Texas" },
      { field: "custom:investor_type", operator: "contains_any", value: ["Family Office", "Venture Capital"] },
    ],
    logic: "AND" as const,
  };
  const decoded = decodeFilterQuery(encodeFilterQuery(query));
  assert.equal(decoded.filters.length, 2);
  assert.equal(decoded.filters[0].field, "state");
  assert.deepEqual(decoded.filters[1].value, ["Family Office", "Venture Capital"]);
});

test("include_archived round-trips", () => {
  const decoded = decodeFilterQuery(encodeFilterQuery({ filters: [], logic: "AND", include_archived: true }));
  assert.equal(decoded.include_archived, true);
});

test("the default sort (last_name/asc) still round-trips explicitly through the URL", () => {
  // The page always keeps a non-null sort in state (defaulting to last_name/asc)
  // and passes it to encodeFilterQuery on every search -- so even the "default"
  // sort must survive a refresh/new-tab exactly, not rely on the page silently
  // re-applying its own default when the URL has no sort params.
  const query = { filters: [], logic: "AND" as const, sort: { field: "last_name", direction: "asc" as const } };
  const params = encodeFilterQuery(query);
  assert.equal(params.get("sort_field"), "last_name");
  assert.equal(params.get("sort_dir"), "asc");
  const decoded = decodeFilterQuery(params);
  assert.deepEqual(decoded.sort, { field: "last_name", direction: "asc" });
});
