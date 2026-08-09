import type { WorkerMode } from "./state/worker-mode"

export interface StatusPresentation {
  tone: "positive" | "warning" | "negative" | "neutral"
  label: string
  detail: string
}

export const MODE_PRESENTATION: Record<
  WorkerMode,
  { label: string; shortLabel: string; description: string }
> = {
  endUser: {
    label: "My Google account",
    shortLabel: "Account",
    description: "Recommended. Syncs this Chrome profile and handles its CAPTCHA requests.",
  },
  captchaWorker: {
    label: "Shared CAPTCHA worker",
    shortLabel: "CAPTCHA",
    description: "Solves server-wide CAPTCHA jobs. It does not synchronize an account.",
  },
  refreshWorker: {
    label: "Token refresh worker",
    shortLabel: "Refresh",
    description: "Refreshes one existing token. It does not solve CAPTCHA jobs.",
  },
}

export function connectionPresentation(state: Record<string, unknown>): StatusPresentation {
  const socket = String(state.wsStatus ?? "idle")
  const register = String(state.lastRegisterStatus ?? "never")
  const error = String(state.lastRegisterError || state.lastError || "").trim()

  if (socket === "open" && register === "ok") {
    return { tone: "positive", label: "Connected", detail: "Ready for sub2gen jobs" }
  }
  if (socket === "connecting" || register === "pending") {
    return { tone: "warning", label: "Connecting", detail: "This usually takes a few seconds" }
  }
  if (socket === "open_register_error") {
    return { tone: "negative", label: "Authentication failed", detail: error || "Check your key" }
  }
  if (socket === "error" || socket === "closed") {
    return { tone: "negative", label: "Disconnected", detail: error || "Reconnect to sub2gen" }
  }
  return { tone: "neutral", label: "Not connected", detail: "Complete connection settings" }
}

export function formatRelativeTime(timestamp: unknown, now = Date.now()): string {
  const value = Number(timestamp)
  if (!value) return "Never"
  const seconds = Math.max(0, Math.round((now - value) / 1000))
  if (seconds < 45) return "Just now"
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.round(hours / 24)
  return `${days}d ago`
}

export function successRate(succeeded: unknown, failed: unknown): string {
  const ok = Math.max(0, Number(succeeded) || 0)
  const bad = Math.max(0, Number(failed) || 0)
  const total = ok + bad
  if (!total) return "No jobs yet"
  return `${Math.round((ok / total) * 100)}% success · ${total} job${total === 1 ? "" : "s"}`
}
