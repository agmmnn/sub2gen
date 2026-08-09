import { describe, expect, it } from "vitest"

import { connectionPresentation, formatRelativeTime, successRate } from "./ui-model"

describe("extension UI presentation", () => {
  it("turns transport details into a plain-language status", () => {
    expect(connectionPresentation({ wsStatus: "open", lastRegisterStatus: "ok" })).toEqual({
      tone: "positive",
      label: "Connected",
      detail: "Ready for sub2gen jobs",
    })
    expect(
      connectionPresentation({
        wsStatus: "open_register_error",
        lastRegisterStatus: "error",
        lastRegisterError: "invalid key",
      }),
    ).toMatchObject({ tone: "negative", label: "Authentication failed", detail: "invalid key" })
  })

  it("formats compact activity summaries", () => {
    const now = 1_000_000
    expect(formatRelativeTime(now - 20_000, now)).toBe("Just now")
    expect(formatRelativeTime(now - 120_000, now)).toBe("2m ago")
    expect(successRate(34, 0)).toBe("100% success · 34 jobs")
    expect(successRate(0, 0)).toBe("No jobs yet")
  })
})
