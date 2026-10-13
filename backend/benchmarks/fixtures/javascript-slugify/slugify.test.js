import assert from "node:assert/strict";
import test from "node:test";
import { slugify } from "./slugify.js";

test("lowercases, trims, and hyphenates whitespace", () => {
  assert.equal(slugify("  Hello, World!  "), "hello-world");
});

test("collapses punctuation and repeated separators", () => {
  assert.equal(slugify("AI & ML---Tools"), "ai-ml-tools");
});

test("preserves digits and collapses whitespace runs", () => {
  assert.equal(slugify("  Code   Conductor 42 "), "code-conductor-42");
});

test("returns an empty slug for punctuation-only input", () => {
  assert.equal(slugify("!!!"), "");
});
