import type { Sub2GenMetadataResponse, GeneratedMetadata } from "./types";

export const FLOW2_TO_ADOBE_CATEGORY: Readonly<Record<number, string>> = {
  1: "10001", 2: "10092", 3: "10162", 4: "10209", 5: "10235", 6: "10255", 7: "10283",
  8: "10432", 9: "10486", 10: "10556", 11: "10584", 12: "10631", 13: "10683", 14: "10733",
  15: "10778", 16: "10797", 17: "10834", 18: "10868", 19: "10927", 20: "10958", 21: "10988",
};

function normalizeKeywords(value: unknown): string {
  if (Array.isArray(value)) return value.map(String).map((item) => item.trim()).filter(Boolean).join(", ");
  return typeof value === "string"
    ? value.split(",").map((item) => item.trim()).filter(Boolean).join(", ")
    : "";
}

export function normalizeMetadataResponse(response: Sub2GenMetadataResponse): GeneratedMetadata {
  const option = response?.optionA;
  const title = typeof option?.title === "string" ? option.title.trim() : "";
  const keywords = normalizeKeywords(option?.keywords);
  if (!title || !keywords) throw new Error("sub2gen returned empty or invalid metadata.");

  const categoryId = Number(option?.categoryId);
  return {
    title,
    keywords,
    category: Number.isInteger(categoryId) ? FLOW2_TO_ADOBE_CATEGORY[categoryId] ?? "" : "",
  };
}
