import assert from "node:assert/strict";
import { test } from "node:test";
import { appendUserMessage, canSubmit, shouldSubmitOnKeyDown } from "./astro-ai-chat.ts";

// --- canSubmit -- the single duplicate-submission gate ----------------------

test("canSubmit is true for non-empty input while not loading", () => {
  assert.equal(canSubmit("What is a family office?", false), true);
});

test("canSubmit is false while a request is already loading", () => {
  assert.equal(canSubmit("another question", true), false);
});

test("canSubmit is false for empty input", () => {
  assert.equal(canSubmit("", false), false);
});

test("canSubmit is false for whitespace-only input", () => {
  assert.equal(canSubmit("   \n  ", false), false);
});

test("canSubmit is false when both blank input AND loading", () => {
  assert.equal(canSubmit("", true), false);
});

// --- appendUserMessage -------------------------------------------------------

test("appendUserMessage adds a trimmed user message to an empty conversation", () => {
  const result = appendUserMessage([], "  Hello Astro  ");

  assert.deepEqual(result, [{ role: "user", content: "Hello Astro" }]);
});

test("appendUserMessage preserves existing history and appends after it", () => {
  const history = [
    { role: "user" as const, content: "What is a family office?" },
    { role: "assistant" as const, content: "A private wealth management firm." },
  ];

  const result = appendUserMessage(history, "Give an example");

  assert.deepEqual(result, [
    ...history,
    { role: "user", content: "Give an example" },
  ]);
});

test("appendUserMessage does not mutate the original array", () => {
  const history = [{ role: "user" as const, content: "hi" }];

  appendUserMessage(history, "another message");

  assert.equal(history.length, 1);
});

// --- shouldSubmitOnKeyDown ---------------------------------------------------

test("Enter without Shift submits", () => {
  assert.equal(shouldSubmitOnKeyDown("Enter", false), true);
});

test("Shift+Enter does not submit (inserts a newline instead)", () => {
  assert.equal(shouldSubmitOnKeyDown("Enter", true), false);
});

test("any other key never submits", () => {
  assert.equal(shouldSubmitOnKeyDown("a", false), false);
  assert.equal(shouldSubmitOnKeyDown("Tab", false), false);
  assert.equal(shouldSubmitOnKeyDown("Escape", true), false);
});
