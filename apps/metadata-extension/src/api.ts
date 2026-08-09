import { normalizeMetadataResponse } from "./adapter";
import { keywordTypesFor, normalizePlatforms } from "./preferences";
import { expandCustomPrompt } from "./title";
import type { Connection, Sub2GenMetadataResponse, GeneratedMetadata, Preferences, SessionResponse } from "./types";

export class Sub2GenApiError extends Error {
  constructor(message: string, public readonly status: number, public readonly retryAfter = 0) {
    super(message);
  }
}

async function errorMessage(response: Response): Promise<string> {
  try {
    const body = await response.json() as { detail?: unknown; error?: unknown; message?: unknown };
    const value = body.detail ?? body.error ?? body.message;
    return typeof value === "string" ? value : JSON.stringify(value ?? `HTTP ${response.status}`);
  } catch {
    return `HTTP ${response.status}`;
  }
}

async function requestJson<T>(url: string, init: RequestInit, attempts: number, timeoutMs = 150_000): Promise<T> {
  let lastError: unknown;
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(url, { ...init, signal: controller.signal });
      if (response.ok) return await response.json() as T;
      const retryAfter = Math.min(Number(response.headers.get("Retry-After") || 0), 60);
      const message = await errorMessage(response);
      const error = new Sub2GenApiError(message, response.status, retryAfter);
      if (response.status !== 429 && response.status < 500) throw error;
      lastError = error;
      if (attempt + 1 < attempts) await delay((retryAfter || 2 ** attempt) * 1000);
    } catch (error) {
      if (error instanceof Sub2GenApiError && error.status < 500 && error.status !== 429) throw error;
      lastError = error;
      if (attempt + 1 < attempts) await delay(2 ** attempt * 1000);
    } finally {
      clearTimeout(timeout);
    }
  }
  if (lastError instanceof Sub2GenApiError) throw lastError;
  throw new Sub2GenApiError(lastError instanceof Error ? lastError.message : "sub2gen request failed.", 0);
}

const delay = (milliseconds: number) => new Promise((resolve) => setTimeout(resolve, milliseconds));

function headers(apiKey: string): HeadersInit {
  return { Accept: "application/json", Authorization: `Bearer ${apiKey}`, "Content-Type": "application/json" };
}

export async function validateSession(baseUrl: string, apiKey: string): Promise<SessionResponse> {
  const session = await requestJson<SessionResponse>(
    `${baseUrl}/api/extension/metadata-session`,
    { method: "GET", headers: headers(apiKey) },
    1,
    15_000,
  );
  if (!session.active || session.service !== "sub2gen-metadata" || !session.capabilities?.includes("adobe:metadata")) {
    throw new Sub2GenApiError("This key cannot activate sub2gen Metadata.", 403);
  }
  return session;
}

export async function imageUrlToBase64(imageUrl: string): Promise<{ base64: string; mimeType: string }> {
  if (imageUrl.startsWith("data:")) {
    const match = /^data:([^;,]+);base64,(.+)$/s.exec(imageUrl);
    if (!match) throw new Error("Unsupported image data URL.");
    return { mimeType: match[1], base64: match[2] };
  }
  // Adobe's public ftcdn.net thumbnails allow cross-origin reads with `*`.
  // Sending cookies makes that response invalid under CORS, and these public
  // asset URLs do not need contributor-session credentials.
  const response = await fetch(imageUrl, { credentials: "omit", mode: "cors" });
  if (!response.ok) throw new Error(`Unable to download Adobe image (HTTP ${response.status}).`);
  const bytes = new Uint8Array(await response.arrayBuffer());
  if (!bytes.length) throw new Error("Adobe image is empty.");
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
  }
  const mimeType = response.headers.get("Content-Type")?.split(";", 1)[0]?.trim() || "image/jpeg";
  return { base64: btoa(binary), mimeType };
}

export async function generateMetadata(
  connection: Connection,
  imageUrl: string,
  assetType: string,
  preferences: Preferences,
): Promise<GeneratedMetadata> {
  const image = await imageUrlToBase64(imageUrl);
  const { titleMin, titleMax, keywordMin, keywordMax, descriptionMin, descriptionMax } = preferences;
  const customText = expandCustomPrompt(preferences.customPrompt, preferences, assetType);
  const transparentBackground = preferences.transparentBackground
    || preferences.titleSuffix === "transparent"
    || preferences.titleSuffix === "png_transparent";
  const body = {
    image_base64: image.base64,
    mimeType: image.mimeType,
    metadataSettings: {
      titleMin, titleMax, keywordMin, keywordMax,
      descriptionMin, descriptionMax,
      platforms: normalizePlatforms(preferences.platforms, preferences.customPlatforms),
      includeCategory: preferences.includeCategory,
      includeReleases: preferences.includeReleases,
      titleStyle: preferences.titleStyle,
      keywordTypes: keywordTypesFor(preferences.keywordStyle),
      transparentBackground,
      language: preferences.language,
      assetType: assetType || "photo",
      customPrompt: { enabled: preferences.customPromptEnabled && Boolean(customText.trim()), text: customText },
    },
    dnaNoBgWorkflowActive: transparentBackground,
  };
  const response = await requestJson<Sub2GenMetadataResponse>(
    `${connection.baseUrl}/api/generate-metadata`,
    { method: "POST", headers: headers(connection.apiKey), body: JSON.stringify(body) },
    3,
  );
  return normalizeMetadataResponse(response);
}
