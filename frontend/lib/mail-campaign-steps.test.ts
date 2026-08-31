import assert from "node:assert/strict";
import { test } from "node:test";
import { buildStepTimeline, stepBodyPreview } from "./mail-campaign-steps.ts";

function makeStep(overrides: Partial<{ step_id: string; step_number: number; subject: string; body: string; delay_days: number }>) {
  return {
    step_id: overrides.step_id ?? "s1",
    mail_campaign_id: "c1",
    step_number: overrides.step_number ?? 1,
    subject: overrides.subject ?? "Subject",
    body: overrides.body ?? "Body",
    delay_days: overrides.delay_days ?? 0,
    reply_in_thread: true,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  };
}

test("buildStepTimeline is empty for an empty sequence", () => {
  assert.deepEqual(buildStepTimeline([]), []);
});

test("a single step produces one email node, no wait node", () => {
  const s1 = makeStep({ step_id: "s1", step_number: 1 });
  const nodes = buildStepTimeline([s1]);
  assert.equal(nodes.length, 1);
  assert.equal(nodes[0].kind, "email");
  assert.equal(nodes[0].step.step_id, "s1");
});

test("Step 1 never has a Wait node before it", () => {
  const s1 = makeStep({ step_id: "s1", step_number: 1 });
  const s2 = makeStep({ step_id: "s2", step_number: 2, delay_days: 2 });
  const nodes = buildStepTimeline([s1, s2]);
  assert.equal(nodes[0].kind, "email");
  assert.equal(nodes[0].step.step_id, "s1");
});

test("three steps produce email/wait/email/wait/email in order, each wait tied to the FOLLOWING email", () => {
  const s1 = makeStep({ step_id: "s1", step_number: 1, delay_days: 0 });
  const s2 = makeStep({ step_id: "s2", step_number: 2, delay_days: 2 });
  const s3 = makeStep({ step_id: "s3", step_number: 3, delay_days: 5 });
  const nodes = buildStepTimeline([s1, s2, s3]);

  assert.equal(nodes.length, 5);
  assert.deepEqual(
    nodes.map((n) => n.kind),
    ["email", "wait", "email", "wait", "email"]
  );
  // Each wait node's `step` is the email that owns that delay_days -- the
  // one immediately AFTER it in the timeline, not the one before.
  assert.equal(nodes[1].step.step_id, "s2");
  assert.equal(nodes[1].step.delay_days, 2);
  assert.equal(nodes[3].step.step_id, "s3");
  assert.equal(nodes[3].step.delay_days, 5);
});

test("output length is always 2n-1 for n>=1 steps", () => {
  for (let n = 1; n <= 5; n++) {
    const steps = Array.from({ length: n }, (_, i) => makeStep({ step_id: `s${i + 1}`, step_number: i + 1 }));
    assert.equal(buildStepTimeline(steps).length, 2 * n - 1);
  }
});

test("stepBodyPreview collapses whitespace/newlines and truncates long bodies", () => {
  assert.equal(stepBodyPreview("Hi {{first_name}},\n\nHope you're well."), "Hi {{first_name}}, Hope you're well.");
  const long = "a".repeat(100);
  const preview = stepBodyPreview(long, 60);
  assert.equal(preview.length, 61); // 60 chars + the ellipsis character
  assert.ok(preview.endsWith("…"));
});

test("stepBodyPreview returns short bodies unchanged (no ellipsis)", () => {
  assert.equal(stepBodyPreview("Short body."), "Short body.");
});
