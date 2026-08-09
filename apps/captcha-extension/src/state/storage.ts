import {
  normalizeWebSocketUrl,
  readStorage,
  type ExtensionStorageArea,
  type StorageRecord,
} from "@sub2gen/extension-core"

import { inferWorkerMode, type WorkerMode } from "./worker-mode"

export const DEFAULT_WORKER_PAGE_URL = "https://labs.google/fx/api/auth/providers"
export const WORKER_RECAPTCHA_SETTLE_DEFAULT_MS = 3000
export const WORKER_RECAPTCHA_SETTLE_MAX_MS = 120000

export const DEFAULT_SETTINGS = {
  serverUrl: "ws://localhost:8000/captcha_ws",
  connectionMode: "endUser" as WorkerMode,
  apiKey: "",
  captchaWorkerAuthKey: "",
  refreshTokenId: "",
  clientLabel: "",
  accountAutoImportEnabled: false,
  accountAutoImportIntervalMinutes: 30,
  accountRefreshIntervalMinutes: 120,
}

export const DEFAULT_WORKER_SETTINGS = {
  workerPageUrl: DEFAULT_WORKER_PAGE_URL,
  usePersistentWorkerTab: true,
  autoRecycleWorkerTabOnCaptchaFailure: true,
  workerRecaptchaSettleMs: WORKER_RECAPTCHA_SETTLE_DEFAULT_MS,
}

export const STORAGE_DEFAULTS: StorageRecord = {
  ...DEFAULT_SETTINGS,
  ...DEFAULT_WORKER_SETTINGS,
}

export interface CaptchaExtensionSettings {
  serverUrl: string
  connectionMode: WorkerMode
  apiKey: string
  captchaWorkerAuthKey: string
  refreshTokenId: string
  clientLabel: string
  workerPageUrl: string
  usePersistentWorkerTab: boolean
  autoRecycleWorkerTabOnCaptchaFailure: boolean
  workerRecaptchaSettleMs: number
  accountAutoImportEnabled: boolean
  accountAutoImportIntervalMinutes: number
  accountRefreshIntervalMinutes: number
}

export function clampAccountInterval(raw: unknown, fallback: number): number {
  const value = Number.parseInt(String(raw ?? ""), 10)
  if (!Number.isFinite(value)) return fallback
  return Math.max(5, Math.min(1440, value))
}

export function clampWorkerRecaptchaSettleMs(raw: unknown): number {
  const value = Number(raw)
  if (!Number.isFinite(value)) return WORKER_RECAPTCHA_SETTLE_DEFAULT_MS
  return Math.max(0, Math.min(WORKER_RECAPTCHA_SETTLE_MAX_MS, Math.floor(value)))
}

export function normalizeWorkerPageUrl(raw: unknown): string {
  const value = String(raw ?? "").trim()
  if (!value) return DEFAULT_WORKER_PAGE_URL
  try {
    const url = new URL(value)
    return url.protocol === "https:" || url.protocol === "http:"
      ? url.toString()
      : DEFAULT_WORKER_PAGE_URL
  } catch {
    return DEFAULT_WORKER_PAGE_URL
  }
}

export function normalizeSettings(values: StorageRecord): CaptchaExtensionSettings {
  return {
    serverUrl: normalizeWebSocketUrl(values.serverUrl ?? DEFAULT_SETTINGS.serverUrl),
    connectionMode: inferWorkerMode(values),
    apiKey: String(values.apiKey ?? "").trim(),
    captchaWorkerAuthKey: String(values.captchaWorkerAuthKey ?? "").trim(),
    refreshTokenId: String(values.refreshTokenId ?? "").trim(),
    clientLabel: String(values.clientLabel ?? "").trim(),
    workerPageUrl: normalizeWorkerPageUrl(values.workerPageUrl),
    usePersistentWorkerTab: values.usePersistentWorkerTab === true,
    autoRecycleWorkerTabOnCaptchaFailure: values.autoRecycleWorkerTabOnCaptchaFailure !== false,
    workerRecaptchaSettleMs: clampWorkerRecaptchaSettleMs(values.workerRecaptchaSettleMs),
    accountAutoImportEnabled: values.accountAutoImportEnabled === true,
    accountAutoImportIntervalMinutes: clampAccountInterval(
      values.accountAutoImportIntervalMinutes,
      DEFAULT_SETTINGS.accountAutoImportIntervalMinutes,
    ),
    accountRefreshIntervalMinutes: clampAccountInterval(
      values.accountRefreshIntervalMinutes,
      DEFAULT_SETTINGS.accountRefreshIntervalMinutes,
    ),
  }
}

export async function loadSettings(area: ExtensionStorageArea): Promise<CaptchaExtensionSettings> {
  return normalizeSettings(await readStorage(area, STORAGE_DEFAULTS))
}
