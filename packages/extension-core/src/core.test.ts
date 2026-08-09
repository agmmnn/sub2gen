import { describe, expect, it, vi } from "vitest"

import {
  normalizeWebSocketUrl,
  readStorage,
  requestJson,
  webSocketUrlToHttpBase,
  writeStorage,
  type ExtensionStorageArea,
} from "./index"

describe("extension storage primitives", () => {
  it("adapts callback storage without depending on Chrome globals", async () => {
    const values: Record<string, unknown> = { mode: "endUser" }
    const area: ExtensionStorageArea = {
      get: (_defaults, callback) => callback(values),
      set: (next, callback) => {
        Object.assign(values, next)
        callback?.()
      },
      remove: () => undefined,
    }

    await writeStorage(area, { enabled: true })
    await expect(readStorage(area, { mode: "", enabled: false })).resolves.toEqual({
      mode: "endUser",
      enabled: true,
    })
  })
})

describe("extension transport primitives", () => {
  it("keeps local sockets insecure and upgrades public sockets", () => {
    expect(normalizeWebSocketUrl("ws://localhost:8000/worker_ws")).toBe(
      "ws://localhost:8000/worker_ws",
    )
    expect(normalizeWebSocketUrl("ws://api.example.test/worker_ws")).toBe(
      "wss://api.example.test/worker_ws",
    )
    expect(webSocketUrlToHttpBase("wss://api.example.test/root/worker_ws")).toBe(
      "https://api.example.test/root",
    )
  })

  it("parses JSON responses without throwing on empty bodies", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(null, { status: 204 }))
    await expect(requestJson(fetcher, "https://api.example.test/ping")).resolves.toEqual({
      ok: true,
      status: 204,
      data: null,
    })
  })
})
