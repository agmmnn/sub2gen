import { describe, expect, it, vi } from "vitest"

import { importGoogleAccount } from "./api"
import { createAccountSyncState, reduceAccountSync } from "./account-sync"
import { DEFAULT_SETTINGS, normalizeSettings } from "./storage"
import { reduceWebSocketPhase } from "./websocket"
import { buildWorkerSocketUrl, inferWorkerMode } from "./worker-mode"

describe("worker mode state", () => {
  it("keeps legacy worker settings compatible and isolates credentials", () => {
    expect(inferWorkerMode({ connectionMode: "worker" })).toBe("refreshWorker")
    expect(inferWorkerMode({ captchaWorkerAuthKey: "captcha", apiKey: "" })).toBe("captchaWorker")
    const url = buildWorkerSocketUrl(
      "ws://localhost:8000/captcha_ws",
      "refreshWorker",
      { apiKey: "must-not-leak", refreshTokenId: "42" },
      "instance-1",
    )
    expect(url.searchParams.get("refresh_token_id")).toBe("42")
    expect(url.searchParams.has("key")).toBe(false)
  })
})

describe("connection and account state machines", () => {
  it("models WebSocket registration failure separately from transport failure", () => {
    expect(reduceWebSocketPhase("connecting", { type: "open" })).toBe("open")
    expect(reduceWebSocketPhase("open", { type: "register", ok: false })).toBe(
      "open_register_error",
    )
    expect(reduceWebSocketPhase("open", { type: "error" })).toBe("error")
  })

  it("prevents overlapping account imports and records completion", () => {
    const running = reduceAccountSync(createAccountSyncState(), { type: "begin" })
    expect(() => reduceAccountSync(running, { type: "begin" })).toThrow("account_import_busy")
    expect(
      reduceAccountSync(running, { type: "success", at: 10, message: "ok" }),
    ).toEqual({ inFlight: false, lastAt: 10, lastStatus: "success", lastMessage: "ok" })
  })
})

describe("settings and API boundaries", () => {
  it("defaults to the local sub2gen WebSocket", () => {
    expect(DEFAULT_SETTINGS.serverUrl).toBe("ws://localhost:8000/captcha_ws")
  })

  it("normalizes public sockets and clamps persisted intervals", () => {
    const settings = normalizeSettings({
      serverUrl: "ws://api.example.test/captcha_ws",
      accountAutoImportIntervalMinutes: 1,
      accountRefreshIntervalMinutes: 5000,
    })
    expect(settings.serverUrl).toBe("wss://api.example.test/captcha_ws")
    expect(settings.accountAutoImportIntervalMinutes).toBe(5)
    expect(settings.accountRefreshIntervalMinutes).toBe(1440)
  })

  it("imports an account through the typed REST boundary", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ success: true, email: "user@example.test", token_id: 7 }), {
        status: 200,
      }),
    )
    await expect(
      importGoogleAccount(fetcher, {
        serverUrl: "ws://localhost:8000/captcha_ws",
        apiKey: "key",
        sessionToken: "session",
        googleCookies: [{ name: "SID", value: "secret" }],
        refreshIntervalMinutes: 120,
      }),
    ).resolves.toMatchObject({ success: true, token_id: 7 })
    expect(fetcher).toHaveBeenCalledWith(
      "http://localhost:8000/api/extension/import-current-account",
      expect.objectContaining({ method: "POST" }),
    )
  })
})
