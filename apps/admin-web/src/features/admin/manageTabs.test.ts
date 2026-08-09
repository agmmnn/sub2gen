import { describe, expect, it } from "vitest"

import { parseManageTab } from "./manageTabs"

describe("manage tab query parsing", () => {
  it("accepts known tabs and rejects stale query values", () => {
    expect(parseManageTab("apikeys")).toBe("apikeys")
    expect(parseManageTab("platform")).toBe("platform")
    expect(parseManageTab("removed-tab")).toBe("platform")
    expect(parseManageTab(null)).toBe("platform")
  })
})
