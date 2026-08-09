import { describe, expect, it } from "vitest";
import { FLOW2_TO_ADOBE_CATEGORY, normalizeMetadataResponse } from "./adapter";

describe("sub2gen metadata response adapter", () => {
  it("contains every Adobe category mapping", () => {
    expect(Object.keys(FLOW2_TO_ADOBE_CATEGORY)).toHaveLength(21);
    expect(FLOW2_TO_ADOBE_CATEGORY[1]).toBe("10001");
    expect(FLOW2_TO_ADOBE_CATEGORY[21]).toBe("10988");
  });

  it("selects optionA and normalizes keywords and category", () => {
    expect(normalizeMetadataResponse({
      optionA: { title: " Red bird ", keywords: ["bird", " wildlife "], categoryId: 1 },
      optionB: { title: "Ignored", keywords: "ignored" },
    })).toEqual({ title: "Red bird", keywords: "bird, wildlife", category: "10001" });
  });

  it("rejects partial metadata", () => {
    expect(() => normalizeMetadataResponse({ optionA: { title: "Only title" } })).toThrow(/empty or invalid/);
  });
});
