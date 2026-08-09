import { afterEach, describe, expect, it, vi } from "vitest";
import { ensureOriginPermission, isPrivateDevelopmentHost, normalizeBaseUrl, permissionOrigin } from "./url-policy";

afterEach(() => vi.unstubAllGlobals());

describe("sub2gen Base URL policy", () => {
  it("normalizes HTTPS servers", () => {
    expect(normalizeBaseUrl(" https://flow-api.example.com/ ")).toBe("https://flow-api.example.com");
    expect(permissionOrigin("https://flow-api.example.com/base")).toBe("https://flow-api.example.com/*");
  });

  it("permits local and RFC1918 HTTP development servers", () => {
    expect(normalizeBaseUrl("http://localhost:8000/")).toBe("http://localhost:8000");
    expect(normalizeBaseUrl("http://192.168.1.5:8000")).toBe("http://192.168.1.5:8000");
    expect(isPrivateDevelopmentHost("172.20.0.4")).toBe(true);
  });

  it("rejects insecure public servers and embedded credentials", () => {
    expect(() => normalizeBaseUrl("http://api.example.com")).toThrow(/HTTPS/);
    expect(() => normalizeBaseUrl("https://user:pass@api.example.com")).toThrow(/credentials/);
  });

  it("does not connect when runtime host permission is denied", async () => {
    const request = vi.fn().mockResolvedValue(false);
    vi.stubGlobal("chrome", { permissions: { contains: vi.fn().mockResolvedValue(false), request } });
    await expect(ensureOriginPermission("https://api.example.com")).resolves.toBe(false);
    expect(request).toHaveBeenCalledWith({ origins: ["https://api.example.com/*"] });
  });
});
