import { requestJson, webSocketUrlToHttpBase } from "@sub2gen/extension-core"

export interface AccountImportRequest {
  serverUrl: string
  apiKey: string
  sessionToken: string
  googleCookies: unknown[]
  refreshIntervalMinutes: number
  workerId?: string
}

export interface AccountImportResponse {
  success?: boolean
  email?: string
  token_id?: number | string
  added?: number
  updated?: number
  detail?: string
  message?: string
}

export async function importGoogleAccount(
  fetcher: typeof fetch,
  request: AccountImportRequest,
): Promise<AccountImportResponse> {
  const baseUrl = webSocketUrlToHttpBase(request.serverUrl, "/worker_ws")
  if (!baseUrl) throw new Error("Invalid WebSocket URL")
  const response = await requestJson<AccountImportResponse>(
    fetcher,
    `${baseUrl}/api/extension/import-current-account`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${request.apiKey}`,
      },
      body: JSON.stringify({
        session_token: request.sessionToken,
        google_cookies: JSON.stringify(request.googleCookies),
        refresh_interval_minutes: request.refreshIntervalMinutes,
        worker_id: request.workerId || null,
      }),
    },
  )
  const payload = response.data ?? {}
  if (!response.ok || payload.success !== true) {
    throw new Error(payload.detail || payload.message || `Import failed (HTTP ${response.status})`)
  }
  return payload
}
