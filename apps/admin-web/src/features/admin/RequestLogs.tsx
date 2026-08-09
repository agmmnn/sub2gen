import { useState, useEffect, useCallback, useRef } from "react"
import { useAuth } from "../../contexts/AuthContext"
import { adminFetch, adminJson } from "../../lib/adminApi"
import type { LogDetail, LogListItem, LogsListResponse } from "../../types/admin"
import { formatLogOutcomeRowClass, formatLogProgressField, logStatusPillClass, statusCodePillClass } from "./requestLogDetail"
import { formatLogStatus, formatOutcome, getOperationKind, operationLabel } from "./requestLogUi"
import { LogDetailStatic } from "./LogDetailStatic"
import { Card, CardContent, CardHeader, CardTitle } from "../../components/ui/card"
import { Switch } from "../../components/ui/switch"
import { Label } from "../../components/ui/label"
import { Button } from "../../components/ui/button"
import { Input } from "../../components/ui/input"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../../components/ui/table"
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "../../components/ui/dialog"
import { toast } from "sonner"
import { ChevronLeft, ChevronRight, RefreshCw, Trash2, Loader2, XCircle, Search } from "lucide-react"
import { cn } from "@/lib/utils"

const LOG_PAGE_SIZE = 50

export function RequestLogs() {
  const { token } = useAuth()
  const [logs, setLogs] = useState<LogListItem[]>([])
  const [loading, setLoading] = useState(false)
  const [page, setPage] = useState(0)
  const [total, setTotal] = useState(0)
  const [detailOpen, setDetailOpen] = useState(false)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detail, setDetail] = useState<LogDetail | null>(null)
  const [hideGeneration, setHideGeneration] = useState(false)
  const [search, setSearch] = useState("")
  const fetchInFlight = useRef(false)
  const fetchLogsRef = useRef<(silent?: boolean) => Promise<void>>(async () => undefined)

  const fetchLogs = useCallback(async (silent = false) => {
    if (!token || fetchInFlight.current) return
    fetchInFlight.current = true
    if (!silent) setLoading(true)
    try {
      const offset = page * LOG_PAGE_SIZE
      const exclude = hideGeneration ? "&exclude_operations=generate_image%2Cgenerate_video%2Cgeminigen_image%2Cgeminigen_video" : ""
      const searchParam = search.trim() ? `&search=${encodeURIComponent(search.trim())}` : ""
      const r = await adminFetch(`/api/logs?limit=${LOG_PAGE_SIZE}&offset=${offset}${exclude}${searchParam}`, token)
      if (!r?.ok) throw new Error("fetch failed")
      const data = (await r.json()) as LogsListResponse | LogListItem[]
      if (Array.isArray(data)) {
        setLogs(data)
        setTotal(data.length)
      } else if (data && Array.isArray(data.logs)) {
        setLogs(data.logs)
        setTotal(typeof data.total === "number" ? data.total : data.logs.length)
      } else {
        setLogs([])
        setTotal(0)
      }
    } catch {
      if (!silent) toast.error("Failed to load logs")
    } finally {
      fetchInFlight.current = false
      if (!silent) setLoading(false)
    }
  }, [token, page, hideGeneration, search])

  useEffect(() => {
    fetchLogsRef.current = fetchLogs
  }, [fetchLogs])

  useEffect(() => {
    queueMicrotask(() => {
      void fetchLogs()
    })
  }, [fetchLogs])

  useEffect(() => {
    if (!token) return

    let socket: WebSocket | null = null
    let reconnectTimer: number | null = null
    let refreshTimer: number | null = null
    let stopped = false
    let reconnectDelay = 500
    const cursorKey = "sub2gen.admin.events.cursor"

    const scheduleRefresh = () => {
      if (document.visibilityState !== "visible" || refreshTimer !== null) return
      refreshTimer = window.setTimeout(() => {
        refreshTimer = null
        void fetchLogsRef.current(true)
      }, 150)
    }

    const applyProgress = (data: Record<string, unknown>) => {
      const rawId = data.id ?? (data.task_kind === "request_log" ? data.task_id : null)
      const id = Number(rawId)
      if (!Number.isFinite(id)) return
      setLogs((current) => current.map((row) => {
        if (row.id !== id) return row
        return {
          ...row,
          status_code: typeof data.status_code === "number" ? data.status_code : row.status_code,
          status_text: typeof data.status_text === "string" ? data.status_text : row.status_text,
          progress: typeof data.progress === "number" ? data.progress : row.progress,
          duration: typeof data.duration === "number" ? data.duration : row.duration,
          updated_at: new Date().toISOString(),
        }
      }))
    }

    const connect = () => {
      if (stopped) return
      const cursor = sessionStorage.getItem(cursorKey) || "$"
      const scheme = window.location.protocol === "https:" ? "wss" : "ws"
      socket = new WebSocket(
        `${scheme}://${window.location.host}/api/admin/events/ws?cursor=${encodeURIComponent(cursor)}`
      )
      socket.onopen = () => {
        reconnectDelay = 500
      }
      socket.onmessage = (message) => {
        try {
          const event = JSON.parse(String(message.data || "{}")) as {
            type?: string
            cursor?: string
            data?: Record<string, unknown>
          }
          if (event.cursor && event.cursor !== "$") sessionStorage.setItem(cursorKey, event.cursor)
          const data = event.data || {}
          if (event.type === "task_progress" || event.type === "request_summary_updated") {
            applyProgress(data)
            if (
              Number(data.status_code || 0) >= 200 ||
              ["completed", "failed", "cancelled", "canceled", "error", "timeout"].includes(String(data.status_text || "").toLowerCase())
            ) {
              scheduleRefresh()
            }
          }
          if (event.type === "request_summary_created" || event.type === "resync") {
            scheduleRefresh()
          }
        } catch {
          // Ignore malformed or forward-compatible event frames.
        }
      }
      socket.onclose = () => {
        socket = null
        if (stopped) return
        reconnectTimer = window.setTimeout(connect, reconnectDelay)
        reconnectDelay = Math.min(reconnectDelay * 2, 10_000)
      }
      socket.onerror = () => socket?.close()
    }

    const refreshIfVisible = () => {
      if (document.visibilityState === "visible") {
        void fetchLogsRef.current(true)
        if (!socket && reconnectTimer === null) connect()
      }
    }
    connect()
    document.addEventListener("visibilitychange", refreshIfVisible)
    return () => {
      stopped = true
      socket?.close()
      if (reconnectTimer !== null) window.clearTimeout(reconnectTimer)
      if (refreshTimer !== null) window.clearTimeout(refreshTimer)
      document.removeEventListener("visibilitychange", refreshIfVisible)
    }
  }, [token])

  const clearLogs = async () => {
    if (!token) return
    if (!confirm("Clear all request logs? This cannot be undone.")) return
    try {
      const r = await adminFetch("/api/logs", token, { method: "DELETE" })
      if (!r) return
      const d = await r.json().catch(() => ({}))
      if (d.success) {
        toast.success("Logs cleared")
        setLogs([])
        setTotal(0)
        setPage(0)
        setDetailOpen(false)
        setDetail(null)
      } else toast.error(d.message || "Failed to clear logs")
    } catch {
      toast.error("Network error")
    }
  }

  const openDetail = async (id: number) => {
    if (!token) return
    setDetailOpen(true)
    setDetailLoading(true)
    setDetail(null)
    try {
      const { ok, data } = await adminJson<LogDetail>(`/api/logs/${id}`, token)
      if (!ok || !data) {
        toast.error("Failed to load log details")
        return
      }
      setDetail(data)
    } catch {
      toast.error("Failed to load log details")
    } finally {
      setDetailLoading(false)
    }
  }

  const canCancelGeminiGenLog = (log: LogListItem) => {
    const op = String(log.operation || "")
    const st = String(log.status_text || "")
    return (
      (op === "geminigen_image" || op === "geminigen_video") &&
      Number(log.status_code) === 102 &&
      [
        "geminigen_queued",
        "geminigen_polling",
        "geminigen_submitted",
        "geminigen_account_selected",
        "geminigen_submitting",
      ].includes(st)
    )
  }

  const cancelGeminiGenLog = async (log: LogListItem) => {
    if (!token) return
    if (!confirm("Cancel this GeminiGen job locally and release its sub2gen slot?")) return
    try {
      const r = await adminFetch(`/api/logs/${log.id}/geminigen/cancel`, token, { method: "POST" })
      const d = await r?.json().catch(() => null)
      if (r?.ok && d?.success) {
        toast.success("GeminiGen job cancelled")
        await fetchLogs()
      } else {
        toast.error(d?.detail || "Could not cancel GeminiGen job")
      }
    } catch {
      toast.error("Network error")
    }
  }

  const colCount = 11

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between gap-4 pb-4 border-b">
        <div className="space-y-1">
          <CardTitle className="text-lg font-semibold">Request logs</CardTitle>
          <div className="flex items-center gap-2">
            <Switch
              id="hide-gen-logs"
              checked={hideGeneration}
              onCheckedChange={(v) => {
                setHideGeneration(Boolean(v))
                setPage(0)
              }}
            />
            <Label htmlFor="hide-gen-logs" className="text-xs font-normal text-muted-foreground cursor-pointer">
              Hide image/video generation rows
            </Label>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <div className="relative w-56">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              value={search}
              onChange={(e) => {
                setSearch(e.target.value)
                setPage(0)
              }}
              placeholder="Search job ID"
              className="h-8 pl-8 text-xs"
            />
          </div>
          <Button
            variant="ghost"
            size="sm"
            onClick={clearLogs}
            className="h-8 text-sm text-destructive hover:text-destructive hover:bg-destructive/10"
          >
            <Trash2 className="h-4 w-4 mr-1" />
            Clear
          </Button>
          <Button variant="ghost" size="icon" onClick={() => void fetchLogs()} disabled={loading} className="h-8 w-8" title="Refresh">
            <RefreshCw className={cn("h-4 w-4", loading && "animate-spin")} />
          </Button>
        </div>
      </CardHeader>
      <CardContent className="p-0">
        <div className="w-full overflow-auto max-h-[min(70vh,600px)]">
          <Table>
            <TableHeader className="sticky top-0 z-20 bg-background">
              <TableRow className="hover:bg-transparent border-b">
                <TableHead className="h-10 w-40 max-w-40 px-3 text-left font-medium text-muted-foreground">Job ID</TableHead>
                <TableHead className="h-10 px-3 text-left font-medium text-muted-foreground">Operation</TableHead>
                <TableHead className="h-10 px-3 text-left font-medium text-muted-foreground">API key</TableHead>
                <TableHead className="h-10 px-3 text-left font-medium text-muted-foreground">Token email</TableHead>
                <TableHead className="h-10 px-3 text-left font-medium text-muted-foreground">Status</TableHead>
                <TableHead className="h-10 px-3 text-left font-medium text-muted-foreground">Progress</TableHead>
                <TableHead className="h-10 px-3 text-left font-medium text-muted-foreground">HTTP</TableHead>
                <TableHead className="h-10 w-68 max-w-68 px-3 text-left font-medium text-muted-foreground">Summary</TableHead>
                <TableHead className="h-10 px-3 text-left font-medium text-muted-foreground">Duration (s)</TableHead>
                <TableHead className="h-10 px-3 text-left font-medium text-muted-foreground">Time</TableHead>
                <TableHead className="h-10 px-3 text-left font-medium text-muted-foreground">Details</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {!logs.length ? (
                <TableRow>
                  <TableCell colSpan={colCount} className="py-8 px-3 text-center text-sm text-muted-foreground">
                    {loading ? (
                      <span className="inline-flex items-center gap-2 justify-center">
                        <Loader2 className="h-5 w-5 animate-spin" />
                        Loading…
                      </span>
                    ) : (
                      "No logs yet"
                    )}
                  </TableCell>
                </TableRow>
              ) : (
                logs.map((log) => {
                  const outcome = formatOutcome(log)
                  const outcomePreview = outcome.length > 96 ? `${outcome.slice(0, 93)}…` : outcome
                  const email = log.token_email || "Unknown"
                  const keyLabel = log.api_key_label || log.api_key_prefix || ""
                  const jobId = String(log.job_id || "").trim()
                  return (
                    <TableRow key={log.id} className="border-border/60">
                      <TableCell className="py-2.5 px-3 text-xs align-top">
                        <span
                          className={cn(
                            "block max-w-40 truncate font-mono",
                            jobId ? "text-foreground" : "text-muted-foreground"
                          )}
                          title={jobId || undefined}
                        >
                          {jobId || "â€”"}
                        </span>
                      </TableCell>
                      <TableCell className="py-2.5 px-3 text-sm align-top">
                        {operationLabel(getOperationKind(log.operation), log.operation)}
                      </TableCell>
                      <TableCell className="py-2.5 px-3 text-xs align-top">
                        <span
                          className={cn(keyLabel ? "text-foreground" : "text-muted-foreground")}
                          title={keyLabel || undefined}
                        >
                          {keyLabel || "—"}
                        </span>
                      </TableCell>
                      <TableCell className="py-2.5 px-3 text-xs align-top">
                        <span className={cn(log.token_email ? "text-primary" : "text-muted-foreground")} title={email}>
                          {email}
                        </span>
                      </TableCell>
                      <TableCell className="py-2.5 px-3 align-top">
                        <div className="flex flex-wrap items-center gap-1">
                          <span
                            className={cn("inline-flex items-center rounded px-2 py-0.5 text-xs", logStatusPillClass(log))}
                          >
                            {formatLogStatus(log)}
                          </span>
                          {log.captcha_user_agent_set && log.status_text !== "captcha_user_agent_set" ? (
                            <span
                              className="inline-flex items-center rounded border border-cyan-500/35 bg-cyan-500/15 px-2 py-0.5 text-xs font-medium text-cyan-900 dark:text-cyan-200"
                              title={log.captcha_provider ? `Captcha provider: ${log.captcha_provider}` : "Captcha provider User-Agent applied"}
                            >
                              SET UA
                            </span>
                          ) : null}
                        </div>
                      </TableCell>
                      <TableCell className="py-2.5 px-3 text-xs align-top text-foreground">
                        {formatLogProgressField(log)}
                      </TableCell>
                      <TableCell className="py-2.5 px-3 align-top">
                        <span
                          className={cn(
                            "inline-flex items-center rounded px-2 py-0.5 text-xs",
                            statusCodePillClass(log.status_code)
                          )}
                        >
                          {log.status_code ?? "-"}
                        </span>
                      </TableCell>
                      <TableCell className="py-2.5 px-3 align-top w-68 max-w-68">
                        <div
                          className={cn(
                            "text-xs leading-5 whitespace-pre-wrap wrap-break-word line-clamp-2",
                            formatLogOutcomeRowClass(log)
                          )}
                          title={outcome}
                        >
                          {outcomePreview}
                        </div>
                      </TableCell>
                      <TableCell className="py-2.5 px-3 align-top tabular-nums text-sm">
                        {Number(log.duration || 0).toFixed(2)}
                      </TableCell>
                      <TableCell className="py-2.5 px-3 text-xs text-muted-foreground align-top whitespace-nowrap">
                        {log.created_at ? new Date(log.created_at).toLocaleString("en-US") : "—"}
                      </TableCell>
                      <TableCell className="py-2.5 px-3 align-top">
                        <div className="flex items-center gap-1">
                          {canCancelGeminiGenLog(log) ? (
                            <Button
                              variant="ghost"
                              size="sm"
                              onClick={() => void cancelGeminiGenLog(log)}
                              className="h-7 px-2 text-xs text-destructive hover:bg-destructive/10 hover:text-destructive"
                            >
                              <XCircle className="h-3.5 w-3.5 mr-1" />
                              Cancel
                            </Button>
                          ) : null}
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={() => void openDetail(log.id)}
                            className="h-7 px-2 text-xs hover:bg-accent hover:text-accent-foreground"
                          >
                            View
                          </Button>
                        </div>
                      </TableCell>
                    </TableRow>
                  )
                })
              )}
            </TableBody>
          </Table>
        </div>
        <div className="flex flex-wrap items-center justify-between gap-2 border-t px-3 py-2 text-xs text-muted-foreground">
          <span>
            {total === 0
              ? "No entries"
              : `Showing ${page * LOG_PAGE_SIZE + 1}–${Math.min(page * LOG_PAGE_SIZE + logs.length, total)} of ${total}`}
          </span>
          <div className="flex items-center gap-1">
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="h-8"
              disabled={loading || page <= 0}
              onClick={() => setPage((p) => Math.max(0, p - 1))}
            >
              <ChevronLeft className="h-4 w-4" />
              Previous
            </Button>
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="h-8"
              disabled={loading || (page + 1) * LOG_PAGE_SIZE >= total}
              onClick={() => setPage((p) => p + 1)}
            >
              Next
              <ChevronRight className="h-4 w-4" />
            </Button>
          </div>
        </div>
      </CardContent>

      <Dialog open={detailOpen} onOpenChange={setDetailOpen}>
        <DialogContent
          className={cn(
            "flex max-h-[80vh] w-[calc(100vw-2rem)] max-w-3xl translate-x-[-50%] translate-y-[-50%] flex-col gap-0 overflow-hidden p-0 sm:max-w-3xl sm:rounded-xl",
            "border-border bg-background text-foreground shadow-lg"
          )}
        >
          <DialogHeader className="shrink-0 flex-row items-center justify-between space-y-0 border-b border-border p-5 text-left pr-12">
            <DialogTitle className="text-lg font-semibold leading-none tracking-tight">Log details</DialogTitle>
          </DialogHeader>
          <div className="min-h-0 flex-1 overflow-y-auto bg-background p-5">
            {detailLoading ? (
              <div className="rounded-lg border border-border bg-muted/50 p-4 text-sm text-muted-foreground">Loading log details…</div>
            ) : detail ? (
              <LogDetailStatic log={detail} />
            ) : (
              <p className="py-8 text-center text-sm text-muted-foreground">No data</p>
            )}
          </div>
        </DialogContent>
      </Dialog>
    </Card>
  )
}
