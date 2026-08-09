export const WORKER_MODES = ["endUser", "captchaWorker", "refreshWorker"] as const

export type WorkerMode = (typeof WORKER_MODES)[number]

export interface WorkerModeSettings {
  connectionMode?: unknown
  apiKey?: unknown
  captchaWorkerAuthKey?: unknown
  refreshTokenId?: unknown
  clientLabel?: unknown
}

export function normalizeWorkerMode(value: unknown): WorkerMode {
  if (value === "captchaWorker" || value === "refreshWorker") return value
  return "endUser"
}

export function inferWorkerMode(settings: WorkerModeSettings): WorkerMode {
  const explicit = String(settings.connectionMode ?? "").trim()
  if (["endUser", "captchaWorker", "refreshWorker"].includes(explicit)) {
    return normalizeWorkerMode(explicit)
  }
  return "endUser"
}
