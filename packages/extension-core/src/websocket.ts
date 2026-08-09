const LOCAL_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]"])

export function normalizeWebSocketUrl(raw: unknown): string {
  const value = String(raw ?? "").trim()
  if (!value) return value
  try {
    const url = new URL(value)
    if (url.protocol !== "ws:") return value
    const host = url.hostname.toLowerCase()
    if (LOCAL_HOSTS.has(host) || host.endsWith(".local")) return value
    url.protocol = "wss:"
    return url.toString()
  } catch {
    return value
  }
}

export function webSocketUrlToHttpBase(raw: unknown, socketPath = "/worker_ws"): string {
  const value = String(raw ?? "").trim()
  if (!value) return ""
  try {
    const url = new URL(value)
    if (url.protocol !== "ws:" && url.protocol !== "wss:") return ""
    const protocol = url.protocol === "wss:" ? "https:" : "http:"
    let path = url.pathname || ""
    if (socketPath && path.endsWith(socketPath)) {
      path = path.slice(0, -socketPath.length)
    }
    path = path === "/" ? "" : path.replace(/\/$/, "")
    return `${protocol}//${url.host}${path}`
  } catch {
    return ""
  }
}
