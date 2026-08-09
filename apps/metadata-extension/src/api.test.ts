import { afterEach, describe, expect, it, vi } from "vitest";
import { generateMetadata, validateSession } from "./api";
import { DEFAULT_PREFERENCES } from "./storage";

afterEach(() => vi.unstubAllGlobals());

describe("sub2gen client", () => {
  it("validates an extension session", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      active: true, service: "sub2gen-metadata", keyLabel: "team", capabilities: ["adobe:metadata"],
    }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    await expect(validateSession("https://api.example.test", "secret")).resolves.toMatchObject({ keyLabel: "team" });
    expect(fetchMock.mock.calls[0][1].headers.Authorization).toBe("Bearer secret");
  });

  it("sends server-managed metadata settings and no provider/model", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      optionA: { title: "Ocean sunset", keywords: "ocean, sunset", categoryId: 11 },
    }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    const result = await generateMetadata(
      { baseUrl: "https://api.example.test", apiKey: "secret", keyLabel: "team", validatedAt: 1 },
      "data:image/png;base64,AAAA",
      "illustration",
      {
        ...DEFAULT_PREFERENCES,
        language: "es",
        includeCategory: true,
        titleMin: 90,
        titleMax: 155,
        keywordMin: 31,
        keywordMax: 48,
        descriptionMin: 40,
        descriptionMax: 120,
        platforms: ["adobe-stock", "shutterstock"],
        customPlatforms: "custom-market, Shutterstock",
        includeReleases: true,
        titleStyle: "commercial",
        keywordStyle: "double-word",
        transparentBackground: true,
      },
    );
    const body = JSON.parse(fetchMock.mock.calls[0][1].body);
    expect(body).not.toHaveProperty("model");
    expect(body).not.toHaveProperty("backend");
    expect(body.metadataSettings).toEqual(expect.objectContaining({
      language: "es",
      assetType: "illustration",
      titleMin: 90,
      titleMax: 155,
      keywordMin: 31,
      keywordMax: 48,
      descriptionMin: 40,
      descriptionMax: 120,
      platforms: ["adobe-stock", "shutterstock", "custom-market"],
      includeCategory: true,
      includeReleases: true,
      titleStyle: "commercial",
      keywordTypes: { singleWord: false, doubleWord: true, mixed: false },
      transparentBackground: true,
    }));
    expect(result.category).toBe("10584");
  });

  it("surfaces authentication failures without retrying", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: "Invalid API key" }), {
      status: 401,
      headers: { "Content-Type": "application/json" },
    }));
    vi.stubGlobal("fetch", fetchMock);
    await expect(validateSession("https://api.example.test", "bad"))
      .rejects.toMatchObject({ status: 401, message: "Invalid API key" });
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it("downloads public Adobe CDN images without credentials", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(new Uint8Array([255, 216, 255]), {
        status: 200,
        headers: { "Content-Type": "image/jpeg" },
      }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        optionA: { title: "Public asset", keywords: "asset, stock" },
      }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);

    await generateMetadata(
      { baseUrl: "https://api.example.test", apiKey: "secret", keyLabel: "team", validatedAt: 1 },
      "https://as1.ftcdn.net/example.jpg",
      "photo",
      DEFAULT_PREFERENCES,
    );

    expect(fetchMock.mock.calls[0]).toEqual([
      "https://as1.ftcdn.net/example.jpg",
      { credentials: "omit", mode: "cors" },
    ]);
  });
});
