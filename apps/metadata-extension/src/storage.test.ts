import { afterEach, describe, expect, it, vi } from "vitest";
import { clearConnection, DEFAULT_BASE_URL, getConnection, saveConnection } from "./storage";

afterEach(() => vi.unstubAllGlobals());

describe("connection storage", () => {
  it("defaults to the local sub2gen API", () => {
    expect(DEFAULT_BASE_URL).toBe("http://localhost:8000");
  });

  it("persists and disconnects without exposing provider credentials", async () => {
    const values: Record<string, unknown> = {};
    vi.stubGlobal("chrome", {
      storage: {
        local: {
          get: vi.fn(async (key: string) => ({ [key]: values[key] })),
          set: vi.fn(async (next: Record<string, unknown>) => Object.assign(values, next)),
          remove: vi.fn(async (key: string) => { delete values[key]; }),
        },
      },
    });
    const connection = { baseUrl: "https://api.example.test", apiKey: "flow-key", keyLabel: "team", validatedAt: 1 };
    await saveConnection(connection);
    await expect(getConnection()).resolves.toEqual(connection);
    expect(JSON.stringify(values)).not.toContain("gemini");
    expect(JSON.stringify(values)).not.toContain("openai");
    expect(JSON.stringify(values)).not.toContain("groq");
    await clearConnection();
    await expect(getConnection()).resolves.toBeNull();
  });
});
