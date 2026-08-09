import { describe, expect, it, vi } from "vitest";
import { ACTIVITY_LIMIT, activityFor, sanitizeActivityMessage, upsertActivity } from "./runtime";
import { DEFAULT_RUNTIME } from "./storage";
import type { RunActivity } from "./types";

function item(index: number, phase: RunActivity["phase"] = "generating"): RunActivity {
  return { id: `1:${index}`, assetNumber: index, page: 1, phase, message: `Asset ${index}`, updatedAt: index };
}

describe("current-run activity", () => {
  it("updates an existing asset without duplicating its timeline node", () => {
    const result = upsertActivity([item(1)], item(1, "success"));
    expect(result).toHaveLength(1);
    expect(result[0].phase).toBe("success");
  });

  it("retains only the latest 50 assets", () => {
    const activities = Array.from({ length: ACTIVITY_LIMIT + 5 }, (_, index) => item(index + 1));
    const result = activities.reduce<RunActivity[]>((current, activity) => upsertActivity(current, activity), []);
    expect(result).toHaveLength(ACTIVITY_LIMIT);
    expect(result[0].assetNumber).toBe(6);
    expect(result.at(-1)?.assetNumber).toBe(55);
  });

  it("does not retain URLs, bearer values, or managed keys in activity copy", () => {
    expect(sanitizeActivityMessage("Failed at https://api.example.test Bearer secret s2g_live_abc123"))
      .toBe("Failed at remote service authentication token API key");
  });

  it("creates a page-scoped asset identity", () => {
    vi.spyOn(Date, "now").mockReturnValue(1234);
    const activity = activityFor({ ...DEFAULT_RUNTIME, currentPage: 3 }, 7, "applying", "Applying metadata");
    expect(activity).toEqual({
      id: "3:7",
      assetNumber: 7,
      page: 3,
      phase: "applying",
      message: "Applying metadata",
      updatedAt: 1234,
    });
  });
});
