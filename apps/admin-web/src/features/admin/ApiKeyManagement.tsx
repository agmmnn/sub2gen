import { useCallback, useEffect, useState } from "react"
import { useAuth } from "../../contexts/AuthContext"
import { adminFetch, adminJson } from "../../lib/adminApi"
import { Card, CardContent, CardHeader, CardTitle } from "../../components/ui/card"
import { Button } from "../../components/ui/button"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../../components/ui/table"
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "../../components/ui/dialog"
import { Input } from "../../components/ui/input"
import { Label } from "../../components/ui/label"
import { Switch } from "../../components/ui/switch"
import { Badge } from "../../components/ui/badge"
import { toast } from "sonner"
import { ChevronLeft, ChevronRight, Copy, Eye, FolderKanban, KeyRound, Loader2, Pencil, Plus, RefreshCw, Trash2, Unplug } from "lucide-react"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../../components/ui/select"
import type {
  CaptchaWorkerKeyRow,
  CreateCaptchaWorkerKeyResponse,
  DeleteCaptchaWorkerKeyResponse,
  KillCaptchaWorkerSessionsResponse,
  ListCaptchaWorkerKeysResponse,
  ManagedApiKeyProjectsResponse,
} from "../../types/admin"

type TokenRow = { id: number; email?: string; is_active?: boolean }
type ManagedApiKey = {
  id: number
  client_name: string
  label: string
  key_prefix: string
  scopes: string
  is_active: boolean
  expires_at?: string | null
  last_used_at?: string | null
  last_presence_at?: string | null
  is_online: boolean
  created_at?: string | null
  account_ids: number[]
  requires_rotation?: boolean
  can_reveal_plaintext?: number
  adobe_cloning_enabled?: number | boolean
  adobe_metadata_enabled?: number | boolean
  adobe_tracker_enabled?: number | boolean
}
type EndpointLimit = { endpoint: string; rpm: number; rph: number; burst: number }
type ManagedApiKeyDetail = ManagedApiKey & {
  key_plaintext?: string | null
  endpoint_limits?: EndpointLimit[]
}
type AuditLog = {
  id: number
  api_key_id?: number | null
  key_prefix?: string | null
  label?: string | null
  endpoint: string
  account_id?: number | null
  status_code: number
  detail?: string
  ip?: string
  created_at?: string
}

type LimitRow = { endpoint: string; rpm: string; rph: string; burst: string }
type ScopeOption = { id: string; label: string; description: string }

type AdobeUsageMonthRow = { year_month: string; success_count: number }
type AdobeUsageOpRow = { year_month: string; operation: string; success_count: number }
type GenerationStats = {
  total_images: number
  total_videos: number
  total_errors: number
  today_images: number
  today_videos: number
  today_errors: number
}

const AUDIT_PAGE_SIZE = 25
const KEY_PROJECT_PAGE_SIZE = 10
const SHOW_VIDEOFX_PROJECTS = false

const AVAILABLE_SCOPES: ScopeOption[] = [
  { id: "models:read", label: "Read models", description: "Allows `/v1/models`, `/v1/models/aliases`, and Gemini model listing endpoints." },
  { id: "tokens:import", label: "Import browser account", description: "Allows the Chrome extension to add or refresh the Google account signed in to its browser profile." },
  { id: "projects:read", label: "Read projects", description: "Allows listing projects assigned to this API key for optional project pinning." },
  { id: "generate:chat", label: "Generate chat", description: "Allows `/v1/chat/completions` (stream and non-stream)." },
  { id: "generate:gemini", label: "Generate gemini", description: "Allows Gemini `generateContent` and `streamGenerateContent` endpoints." },
  {
    id: "adobe:cloning",
    label: "Adobe cloning",
    description: "Allows `POST /api/generate-cloning-prompts` and `POST /api/generate-cloning-video-prompt`.",
  },
  {
    id: "adobe:metadata",
    label: "Adobe metadata",
    description: "Allows `POST /api/generate-metadata` (stock metadata from an image).",
  },
  {
    id: "adobe:tracker",
    label: "Adobe task tracker",
    description: "Allows `POST /api/tracker/contributor` and `POST /api/tracker/keyword`.",
  },
  {
    id: "runway:generate",
    label: "Runway generation",
    description: "Allows Runway models through `/v1/chat/completions` and `/v1/runway/*` task routes.",
  },
  {
    id: "geminigen:generate",
    label: "GeminiGen generation",
    description: "Allows GeminiGen models through OpenAI/Gemini-compatible generation routes.",
  },
]

/** All selectable scope ids (no wildcard); used for Select all and legacy `*` expansion. */
const ALL_MANAGED_SCOPE_IDS: string[] = AVAILABLE_SCOPES.map((s) => s.id)

function asEnabled(v: unknown, defaultValue = true): boolean {
  if (v === null || v === undefined) return defaultValue
  return v === true || v === 1 || v === "1"
}

function parseScopesFromKey(scopesRaw?: string | null): string[] {
  const parsed = String(scopesRaw || "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean)
  if (!parsed.length) return [...ALL_MANAGED_SCOPE_IDS]
  const noStar = parsed.filter((s) => s !== "*")
  if (parsed.includes("*") && noStar.length === 0) {
    return [...ALL_MANAGED_SCOPE_IDS]
  }
  if (parsed.includes("*")) {
    return noStar.length ? noStar : [...ALL_MANAGED_SCOPE_IDS]
  }
  return noStar
}

export function ApiKeyManagement() {
  const { token } = useAuth()
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [keys, setKeys] = useState<ManagedApiKey[]>([])
  const [tokens, setTokens] = useState<TokenRow[]>([])
  const [auditLogs, setAuditLogs] = useState<AuditLog[]>([])
  const [auditPage, setAuditPage] = useState(0)
  const [auditTotal, setAuditTotal] = useState(0)
  const [auditClearing, setAuditClearing] = useState(false)

  const [createOpen, setCreateOpen] = useState(false)
  const [editOpen, setEditOpen] = useState(false)
  const [plainKeyOpen, setPlainKeyOpen] = useState(false)
  const [editingKeyId, setEditingKeyId] = useState<number | null>(null)
  const [plainKeyValue, setPlainKeyValue] = useState("")
  const [clientName, setClientName] = useState("")
  const [label, setLabel] = useState("default")
  const [selectedScopes, setSelectedScopes] = useState<string[]>(() => [...ALL_MANAGED_SCOPE_IDS])
  const [expiresAt, setExpiresAt] = useState("")
  const [selectedAccountIds, setSelectedAccountIds] = useState<number[]>([])
  const [limits, setLimits] = useState<LimitRow[]>([
    { endpoint: "/v1/chat/completions", rpm: "60", rph: "2000", burst: "10" },
  ])
  const [adobeUsageRows, setAdobeUsageRows] = useState<AdobeUsageMonthRow[]>([])
  const [adobeUsageOpRows, setAdobeUsageOpRows] = useState<AdobeUsageOpRow[]>([])
  const [adobeUsageLoading, setAdobeUsageLoading] = useState(false)
  const [generationStats, setGenerationStats] = useState<GenerationStats | null>(null)
  const [generationStatsLoading, setGenerationStatsLoading] = useState(false)
  const [createdKey, setCreatedKey] = useState("")
  const [captchaWorkerKeys, setCaptchaWorkerKeys] = useState<CaptchaWorkerKeyRow[]>([])
  const [captchaWorkerSessions, setCaptchaWorkerSessions] = useState<NonNullable<ListCaptchaWorkerKeysResponse["sessions"]>>([])
  const [captchaWorkerLabel, setCaptchaWorkerLabel] = useState("")
  const [createdCaptchaWorkerKey, setCreatedCaptchaWorkerKey] = useState("")
  const [captchaWorkerSaving, setCaptchaWorkerSaving] = useState(false)
  const [captchaWorkerBusyId, setCaptchaWorkerBusyId] = useState<number | null>(null)
  const [captchaWorkerDraftLabels, setCaptchaWorkerDraftLabels] = useState<Record<number, string>>({})

  const [keyProjectsPage, setKeyProjectsPage] = useState(0)
  const [keyProjectsLoading, setKeyProjectsLoading] = useState(false)
  const [keyProjectsTotal, setKeyProjectsTotal] = useState(0)
  const [keyProjectRows, setKeyProjectRows] = useState<NonNullable<ManagedApiKeyProjectsResponse["projects"]>>([])
  const [keyProjectAccounts, setKeyProjectAccounts] = useState<NonNullable<ManagedApiKeyProjectsResponse["accounts"]>>([])
  const [newProjTokenId, setNewProjTokenId] = useState("")
  const [newProjTitle, setNewProjTitle] = useState("")
  const [newProjSetCurrent, setNewProjSetCurrent] = useState(true)
  const [creatingProj] = useState(false)
  const [legacyScopesNotice, setLegacyScopesNotice] = useState(false)

  const loadManagedKeys = useCallback(async () => {
    if (!token) return
    const response = await adminJson<{ success?: boolean; keys?: ManagedApiKey[] }>(
      "/api/admin/managed-apikeys",
      token
    )
    if (response.ok && response.data?.keys) setKeys(response.data.keys)
  }, [token])

  const loadAll = useCallback(async () => {
    if (!token) return
    setLoading(true)
    try {
      const auditOffset = auditPage * AUDIT_PAGE_SIZE
      const [, t, a, cw] = await Promise.all([
        loadManagedKeys(),
        adminJson<TokenRow[]>("/api/tokens", token),
        adminJson<{ success?: boolean; logs?: AuditLog[]; total?: number }>(
          `/api/admin/managed-apikeys/audit?limit=${AUDIT_PAGE_SIZE}&offset=${auditOffset}`,
          token
        ),
        adminJson<ListCaptchaWorkerKeysResponse>("/api/admin/captcha-worker-keys", token),
      ])
      if (t.ok && Array.isArray(t.data)) setTokens(t.data)
      if (a.ok && a.data?.logs) setAuditLogs(a.data.logs)
      if (a.ok && typeof a.data?.total === "number") setAuditTotal(a.data.total)
      if (cw.ok && cw.data?.success) {
        const rows = Array.isArray(cw.data.keys) ? cw.data.keys : []
        setCaptchaWorkerKeys(rows)
        setCaptchaWorkerSessions(Array.isArray(cw.data.sessions) ? cw.data.sessions : [])
        const drafts: Record<number, string> = {}
        for (const row of rows) drafts[row.id] = String(row.label || "")
        setCaptchaWorkerDraftLabels(drafts)
      }
    } finally {
      setLoading(false)
    }
  }, [token, auditPage, loadManagedKeys])

  useEffect(() => {
    const timeoutId = window.setTimeout(() => {
      void loadAll()
    }, 0)
    return () => window.clearTimeout(timeoutId)
  }, [loadAll])

  useEffect(() => {
    if (!token) return
    const intervalId = window.setInterval(() => {
      void loadManagedKeys()
    }, 15_000)
    return () => window.clearInterval(intervalId)
  }, [token, loadManagedKeys])

  const loadKeyProjects = useCallback(async () => {
    if (!token || editingKeyId == null || !editOpen) return
    setKeyProjectsLoading(true)
    try {
      setKeyProjectRows([])
      setKeyProjectsTotal(0)
      setKeyProjectAccounts([])
    } finally {
      setKeyProjectsLoading(false)
    }
  }, [token, editingKeyId, editOpen])

  useEffect(() => {
    void loadKeyProjects()
  }, [loadKeyProjects])

  const loadAdobeUsage = useCallback(async (keyId: number) => {
    if (!token) return
    setAdobeUsageLoading(true)
    try {
      const r = await adminJson<{
        success?: boolean
        by_month?: AdobeUsageMonthRow[]
        by_month_by_operation?: AdobeUsageOpRow[]
      }>(`/api/admin/managed-apikeys/${keyId}/adobe-usage?months=12`, token)
      if (r.ok) {
        setAdobeUsageRows(Array.isArray(r.data?.by_month) ? (r.data.by_month as AdobeUsageMonthRow[]) : [])
        setAdobeUsageOpRows(
          Array.isArray(r.data?.by_month_by_operation) ? (r.data.by_month_by_operation as AdobeUsageOpRow[]) : []
        )
      } else {
        setAdobeUsageRows([])
        setAdobeUsageOpRows([])
      }
    } catch {
      setAdobeUsageRows([])
      setAdobeUsageOpRows([])
    } finally {
      setAdobeUsageLoading(false)
    }
  }, [token])

  const loadGenerationStats = useCallback(async (keyId: number) => {
    if (!token) return
    setGenerationStatsLoading(true)
    try {
      const r = await adminJson<{ success?: boolean } & GenerationStats>(
        `/api/admin/managed-apikeys/${keyId}/generation-stats`,
        token
      )
      if (r.ok && r.data?.success) {
        setGenerationStats({
          total_images: Number(r.data.total_images || 0),
          total_videos: Number(r.data.total_videos || 0),
          total_errors: Number(r.data.total_errors || 0),
          today_images: Number(r.data.today_images || 0),
          today_videos: Number(r.data.today_videos || 0),
          today_errors: Number(r.data.today_errors || 0),
        })
      } else {
        setGenerationStats(null)
      }
    } catch {
      setGenerationStats(null)
    } finally {
      setGenerationStatsLoading(false)
    }
  }, [token])

  const clearAuditLogs = async () => {
    if (!token || auditTotal === 0) return
    if (!confirm("Clear all managed API key audit logs? This cannot be undone.")) return

    setAuditClearing(true)
    try {
      const r = await adminJson<{ success?: boolean; detail?: string; deleted?: number }>(
        "/api/admin/managed-apikeys/audit",
        token,
        { method: "DELETE" }
      )
      if (r.ok && r.data?.success) {
        setAuditLogs([])
        setAuditTotal(0)
        setAuditPage(0)
        toast.success("Key audit logs cleared")
      } else {
        toast.error(r.data?.detail || "Failed to clear key audit logs")
      }
    } catch {
      toast.error("Failed to clear key audit logs")
    } finally {
      setAuditClearing(false)
    }
  }

  const toggleAccount = (id: number) => {
    setSelectedAccountIds((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]))
  }

  const toggleScope = (scopeId: string) => {
    setSelectedScopes((prev) => (prev.includes(scopeId) ? prev.filter((s) => s !== scopeId) : [...prev, scopeId]))
  }

  const addLimitRow = () => {
    setLimits((prev) => [...prev, { endpoint: "", rpm: "0", rph: "0", burst: "0" }])
  }

  const updateLimitRow = (idx: number, patch: Partial<LimitRow>) => {
    setLimits((prev) => prev.map((row, i) => (i === idx ? { ...row, ...patch } : row)))
  }

  const buildLimitsPayload = () => {
    const out: Record<string, { rpm: number; rph: number; burst: number }> = {}
    for (const row of limits) {
      const endpoint = row.endpoint.trim()
      if (!endpoint) continue
      out[endpoint] = {
        rpm: Math.max(0, parseInt(row.rpm || "0", 10) || 0),
        rph: Math.max(0, parseInt(row.rph || "0", 10) || 0),
        burst: Math.max(0, parseInt(row.burst || "0", 10) || 0),
      }
    }
    return out
  }

  const createKey = async () => {
    if (!token) return
    if (!clientName.trim()) return toast.error("Client name is required")
    if (!selectedScopes.length) return toast.error("Select at least one scope")

    setSaving(true)
    try {
      const res = await adminFetch("/api/admin/managed-apikeys", token, {
        method: "POST",
        body: JSON.stringify({
          client_name: clientName.trim(),
          label: label.trim() || "default",
          scopes: selectedScopes.join(","),
          account_ids: selectedAccountIds,
          endpoint_limits: buildLimitsPayload(),
          expires_at: expiresAt.trim() || null,
        }),
      })
      if (!res) return
      const data = await res.json()
      if (data.success && data.key?.api_key) {
        setCreatedKey(String(data.key.api_key))
        toast.success("Managed API key created")
        await loadAll()
      } else {
        toast.error(data.detail || data.message || "Create failed")
      }
    } finally {
      setSaving(false)
    }
  }

  const createCaptchaWorkerKey = async () => {
    if (!token) return
    setCaptchaWorkerSaving(true)
    setCreatedCaptchaWorkerKey("")
    try {
      const r = await adminJson<CreateCaptchaWorkerKeyResponse>("/api/admin/captcha-worker-keys", token, {
        method: "POST",
        body: JSON.stringify({ label: captchaWorkerLabel.trim() || "Captcha worker" }),
      })
      if (r.ok && r.data?.success && r.data.captcha_worker_key) {
        setCreatedCaptchaWorkerKey(r.data.captcha_worker_key)
        setCaptchaWorkerLabel("")
        toast.success("Captcha worker key created")
        await loadAll()
      } else {
        toast.error(r.data?.detail || "Create captcha worker key failed")
      }
    } finally {
      setCaptchaWorkerSaving(false)
    }
  }

  const saveCaptchaWorkerKey = async (row: CaptchaWorkerKeyRow) => {
    if (!token) return
    setCaptchaWorkerBusyId(row.id)
    try {
      const r = await adminJson<{ success?: boolean; detail?: string }>(`/api/admin/captcha-worker-keys/${row.id}`, token, {
        method: "PATCH",
        body: JSON.stringify({ label: captchaWorkerDraftLabels[row.id] ?? "" }),
      })
      if (r.ok && r.data?.success) {
        toast.success("Captcha worker key updated")
        await loadAll()
      } else {
        toast.error(r.data?.detail || "Update failed")
      }
    } finally {
      setCaptchaWorkerBusyId(null)
    }
  }

  const toggleCaptchaWorkerKey = async (row: CaptchaWorkerKeyRow) => {
    if (!token) return
    setCaptchaWorkerBusyId(row.id)
    try {
      const enabled = asEnabled(row.is_active)
      const r = await adminJson<{ success?: boolean; detail?: string }>(`/api/admin/captcha-worker-keys/${row.id}`, token, {
        method: "PATCH",
        body: JSON.stringify({ is_active: !enabled }),
      })
      if (r.ok && r.data?.success) {
        toast.success(!enabled ? "Captcha worker enabled" : "Captcha worker disabled")
        await loadAll()
      } else {
        toast.error(r.data?.detail || "Update failed")
      }
    } finally {
      setCaptchaWorkerBusyId(null)
    }
  }

  const deleteCaptchaWorkerKey = async (row: CaptchaWorkerKeyRow) => {
    if (!token) return
    if (!confirm("Delete this captcha worker key? Connected workers using it will not be able to reconnect.")) return
    setCaptchaWorkerBusyId(row.id)
    try {
      const r = await adminJson<DeleteCaptchaWorkerKeyResponse>(`/api/admin/captcha-worker-keys/${row.id}`, token, {
        method: "DELETE",
      })
      if (r.ok && r.data?.success) {
        toast.success("Captcha worker key deleted")
        await loadAll()
      } else {
        toast.error(r.data?.detail || "Delete failed")
      }
    } finally {
      setCaptchaWorkerBusyId(null)
    }
  }

  const killCaptchaWorkerSessions = async (row: CaptchaWorkerKeyRow) => {
    if (!token) return
    setCaptchaWorkerBusyId(row.id)
    try {
      const r = await adminJson<KillCaptchaWorkerSessionsResponse>(
        `/api/admin/captcha-worker-keys/${row.id}/kill-sessions`,
        token,
        { method: "POST" }
      )
      if (r.ok && r.data?.success) {
        toast.success(r.data.message || `${r.data.killed_count ?? 0} session(s) closed`)
        await loadAll()
      } else {
        toast.error(r.data?.detail || "Kill sessions failed")
      }
    } finally {
      setCaptchaWorkerBusyId(null)
    }
  }

  const copyCaptchaWorkerKey = async (value: string) => {
    const v = String(value || "").trim()
    if (!v) return toast.error("Full key not available")
    try {
      await navigator.clipboard.writeText(v)
      toast.success("Copied")
    } catch {
      toast.error("Copy failed")
    }
  }

  const openEditKey = async (keyId: number) => {
    if (!token) return
    const res = await adminJson<{ success?: boolean; key?: ManagedApiKeyDetail }>(`/api/admin/managed-apikeys/${keyId}`, token)
    if (!res.ok || !res.data?.key) {
      toast.error("Failed to load key details")
      return
    }
    const key = res.data.key
    setEditingKeyId(key.id)
    setKeyProjectRows([])
    setKeyProjectAccounts([])
    setKeyProjectsTotal(0)
    setClientName(key.client_name || "")
    setLabel(key.label || "default")
    setSelectedScopes(parseScopesFromKey(key.scopes))
    setLegacyScopesNotice(String(key.scopes || "").trim() === "*")
    setExpiresAt(String(key.expires_at || ""))
    setSelectedAccountIds(Array.isArray(key.account_ids) ? key.account_ids : [])
    setLimits(
      (key.endpoint_limits || []).length
        ? (key.endpoint_limits || []).map((r) => ({
            endpoint: r.endpoint,
            rpm: String(r.rpm ?? 0),
            rph: String(r.rph ?? 0),
            burst: String(r.burst ?? 0),
          }))
        : [{ endpoint: "/v1/chat/completions", rpm: "60", rph: "2000", burst: "10" }]
    )
    setKeyProjectsPage(0)
    const accIds = Array.isArray(key.account_ids) ? key.account_ids : []
    setNewProjTokenId(accIds.length ? String(accIds[0]) : "")
    setNewProjTitle("")
    setNewProjSetCurrent(true)
    setEditOpen(true)
    void loadAdobeUsage(key.id)
    void loadGenerationStats(key.id)
  }

  const handleEditOpenChange = (open: boolean) => {
    setEditOpen(open)
    if (!open) {
      setEditingKeyId(null)
      setKeyProjectsPage(0)
      setKeyProjectRows([])
      setKeyProjectAccounts([])
      setKeyProjectsTotal(0)
      setAdobeUsageRows([])
      setAdobeUsageOpRows([])
      setGenerationStats(null)
      setGenerationStatsLoading(false)
      setLegacyScopesNotice(false)
      setNewProjTokenId("")
      setNewProjTitle("")
      setNewProjSetCurrent(true)
    }
  }

  const createKeyProject = async () => {
    toast.error("Project management has been removed")
  }

  const tokenEmail = (tid: number) => tokens.find((x) => x.id === tid)?.email || "—"
  const activeProjectByToken = new Map(
    keyProjectAccounts.map((a) => [
      a.token_id,
      a.active_project_id || a.current_project_id || null,
    ])
  )
  const activeProjectNameByToken = new Map(
    keyProjectAccounts.map((a) => [
      a.token_id,
      a.active_project_name || a.current_project_name || null,
    ])
  )

  const submitEditKey = async () => {
    if (!token || editingKeyId == null) return
    if (!clientName.trim()) return toast.error("Client name is required")
    if (!selectedScopes.length) return toast.error("Select at least one scope")
    setSaving(true)
    try {
      const res = await adminFetch(`/api/admin/managed-apikeys/${editingKeyId}`, token, {
        method: "PUT",
        body: JSON.stringify({
          client_name: clientName.trim(),
          label: label.trim() || "default",
          scopes: selectedScopes.join(","),
          expires_at: expiresAt.trim() || null,
          account_ids: selectedAccountIds,
          endpoint_limits: buildLimitsPayload(),
        }),
      })
      if (!res) return
      const data = await res.json()
      if (data.success) {
        toast.success("Managed API key updated")
        handleEditOpenChange(false)
        await loadAll()
      } else {
        toast.error(data.detail || data.message || "Update failed")
      }
    } finally {
      setSaving(false)
    }
  }

  const deleteKey = async (keyId: number) => {
    if (!token) return
    if (!confirm("Delete this API key? This action cannot be undone.")) return
    const res = await adminFetch(`/api/admin/managed-apikeys/${keyId}`, token, { method: "DELETE" })
    if (!res) return
    const data = await res.json()
    if (data.success) {
      toast.success("Managed API key deleted")
      await loadAll()
    } else {
      toast.error(data.detail || data.message || "Delete failed")
    }
  }

  const revealPlainKey = async (keyId: number) => {
    if (!token) return
    const res = await adminJson<{ success?: boolean; key?: ManagedApiKeyDetail }>(
      `/api/admin/managed-apikeys/${keyId}?reveal_plaintext=true`,
      token
    )
    if (!res.ok || !res.data?.key) {
      toast.error("Failed to reveal key")
      return
    }
    const plain = String(res.data.key.key_plaintext || "")
    if (!plain) {
      toast.error("Plain key not available for this record")
      return
    }
    setPlainKeyValue(plain)
    setPlainKeyOpen(true)
  }

  const toggleKeyEnabled = async (key: ManagedApiKey) => {
    if (!token) return
    const res = await adminFetch(`/api/admin/managed-apikeys/${key.id}`, token, {
      method: "PUT",
      body: JSON.stringify({ is_active: !key.is_active }),
    })
    if (!res) return
    const data = await res.json()
    if (data.success) {
      toast.success(!key.is_active ? "Key enabled" : "Key disabled")
      await loadAll()
    } else {
      toast.error(data.detail || "Update failed")
    }
  }

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <CardTitle>Managed API keys</CardTitle>
          <div className="flex items-center gap-2">
            <Button size="icon" variant="outline" onClick={() => loadAll()} disabled={loading}>
              <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />
            </Button>
            <Button
              onClick={() => {
                setSelectedScopes([...ALL_MANAGED_SCOPE_IDS])
                setCreateOpen(true)
              }}
            >
              <Plus className="h-4 w-4 mr-2" /> New key
            </Button>
          </div>
        </CardHeader>
        <CardContent className="p-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Client</TableHead>
                <TableHead>Label</TableHead>
                <TableHead>Prefix</TableHead>
                <TableHead>Accounts</TableHead>
                <TableHead>Last used</TableHead>
                <TableHead>Status</TableHead>
                <TableHead className="text-right">Action</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {!keys.length ? (
                <TableRow>
                  <TableCell colSpan={7} className="text-center text-muted-foreground py-8">
                    {loading ? "Loading..." : "No managed API keys"}
                  </TableCell>
                </TableRow>
              ) : (
                keys.map((k) => (
                  <TableRow key={k.id}>
                    <TableCell>{k.client_name}</TableCell>
                    <TableCell>
                      <div className="flex flex-wrap items-center gap-2">
                        <span>{k.label}</span>
                        <Badge
                          variant={k.is_online ? "default" : "secondary"}
                          className={k.is_online ? "bg-emerald-600 text-white hover:bg-emerald-600" : undefined}
                        >
                          <span
                            className={`mr-1.5 h-1.5 w-1.5 rounded-full ${k.is_online ? "bg-emerald-100" : "bg-muted-foreground/60"}`}
                            aria-hidden="true"
                          />
                          {k.is_online ? "Online" : "Offline"}
                        </Badge>
                      </div>
                    </TableCell>
                    <TableCell>
                      <div className="flex items-center gap-2">
                        <span className="font-mono text-xs">{k.key_prefix}</span>
                        {k.requires_rotation ? <Badge variant="destructive">Replace key</Badge> : null}
                      </div>
                    </TableCell>
                    <TableCell>{k.account_ids.length}</TableCell>
                    <TableCell className="text-xs">{k.last_used_at || "-"}</TableCell>
                    <TableCell>
                      <Badge variant={k.is_active ? "default" : "secondary"}>{k.is_active ? "Active" : "Disabled"}</Badge>
                    </TableCell>
                    <TableCell className="text-right">
                      <div className="inline-flex items-center gap-2">
                        <Button size="icon" variant="outline" onClick={() => void openEditKey(k.id)} title="Edit key">
                          <Pencil className="h-4 w-4" />
                        </Button>
                        <Button size="icon" variant="outline" onClick={() => void revealPlainKey(k.id)} title="View plain key">
                          <Eye className="h-4 w-4" />
                        </Button>
                        <Button size="icon" variant="destructive" onClick={() => void deleteKey(k.id)} title="Delete key">
                          <Trash2 className="h-4 w-4" />
                        </Button>
                        <Switch checked={k.is_active} onCheckedChange={() => void toggleKeyEnabled(k)} />
                      </div>
                    </TableCell>
                  </TableRow>
                ))
              )}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <CardTitle>Captcha worker keys</CardTitle>
          <Button size="icon" variant="outline" onClick={() => loadAll()} disabled={loading}>
            <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />
          </Button>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-[1fr_auto]">
            <Input
              value={captchaWorkerLabel}
              onChange={(e) => setCaptchaWorkerLabel(e.target.value)}
              placeholder="Worker label"
            />
            <Button onClick={() => void createCaptchaWorkerKey()} disabled={captchaWorkerSaving}>
              {captchaWorkerSaving ? <Loader2 className="h-4 w-4 animate-spin" /> : <KeyRound className="h-4 w-4 mr-2" />}
              New captcha key
            </Button>
          </div>
          {createdCaptchaWorkerKey ? (
            <div className="rounded-md border bg-muted/30 p-3">
              <Label>Captcha worker key</Label>
              <div className="mt-2 flex gap-2">
                <Input readOnly className="font-mono text-xs" value={createdCaptchaWorkerKey} />
                <Button
                  type="button"
                  variant="outline"
                  size="icon"
                  onClick={() => void copyCaptchaWorkerKey(createdCaptchaWorkerKey)}
                  title="Copy key"
                >
                  <Copy className="h-4 w-4" />
                </Button>
              </div>
            </div>
          ) : null}
          <div className="rounded-md border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Label</TableHead>
                  <TableHead>Prefix</TableHead>
                  <TableHead>Sessions</TableHead>
                  <TableHead>Last seen</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead className="text-right">Action</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {!captchaWorkerKeys.length ? (
                  <TableRow>
                    <TableCell colSpan={6} className="text-center text-muted-foreground py-8">
                      No captcha worker keys
                    </TableCell>
                  </TableRow>
                ) : (
                  captchaWorkerKeys.map((row) => {
                    const activeSessions = captchaWorkerSessions.filter((s) => s.captcha_worker_id === row.id).length
                    const enabled = asEnabled(row.is_active)
                    const busy = captchaWorkerBusyId === row.id
                    return (
                      <TableRow key={row.id}>
                        <TableCell className="min-w-[180px]">
                          <Input
                            className="h-8"
                            value={captchaWorkerDraftLabels[row.id] ?? ""}
                            onChange={(e) =>
                              setCaptchaWorkerDraftLabels((prev) => ({ ...prev, [row.id]: e.target.value }))
                            }
                          />
                        </TableCell>
                        <TableCell className="font-mono text-xs">
                          <button
                            type="button"
                            className="max-w-full text-left break-all text-primary hover:underline focus-visible:outline-hidden focus-visible:ring-1 focus-visible:ring-ring"
                            onClick={() => void copyCaptchaWorkerKey(String(row.key_plaintext || ""))}
                            title="Click to copy full key"
                          >
                            {row.key_prefix}
                          </button>
                        </TableCell>
                        <TableCell>{activeSessions}</TableCell>
                        <TableCell className="text-xs">{row.last_seen_at || "-"}</TableCell>
                        <TableCell>
                          <Badge variant={enabled ? "default" : "secondary"}>{enabled ? "Active" : "Disabled"}</Badge>
                        </TableCell>
                        <TableCell className="text-right">
                          <div className="inline-flex flex-wrap justify-end gap-2">
                            <Button size="icon" variant="outline" disabled={busy} onClick={() => void saveCaptchaWorkerKey(row)} title="Save label">
                              {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Pencil className="h-4 w-4" />}
                            </Button>
                            <Button type="button" size="icon" variant="outline" onClick={() => void copyCaptchaWorkerKey(String(row.key_plaintext || ""))} title="Copy full key">
                              <Copy className="h-4 w-4" />
                            </Button>
                            <Button size="icon" variant="outline" disabled={busy} onClick={() => void killCaptchaWorkerSessions(row)} title="Kill sessions">
                              <Unplug className="h-4 w-4" />
                            </Button>
                            <Button size="icon" variant="destructive" disabled={busy} onClick={() => void deleteCaptchaWorkerKey(row)} title="Delete key">
                              <Trash2 className="h-4 w-4" />
                            </Button>
                            <Switch checked={enabled} disabled={busy} onCheckedChange={() => void toggleCaptchaWorkerKey(row)} />
                          </div>
                        </TableCell>
                      </TableRow>
                    )
                  })
                )}
              </TableBody>
            </Table>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="flex flex-row items-center justify-between gap-3">
          <CardTitle>Recent key audit logs</CardTitle>
          <Button
            type="button"
            variant="destructive"
            size="sm"
            disabled={loading || auditClearing || auditTotal === 0}
            onClick={() => void clearAuditLogs()}
          >
            {auditClearing ? <Loader2 className="h-4 w-4 animate-spin" /> : <Trash2 className="h-4 w-4" />}
            Clear all
          </Button>
        </CardHeader>
        <CardContent className="p-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Time</TableHead>
                <TableHead>Key</TableHead>
                <TableHead>Endpoint</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Detail</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {!auditLogs.length ? (
                <TableRow>
                  <TableCell colSpan={5} className="text-center text-muted-foreground py-6">
                    No audit logs yet
                  </TableCell>
                </TableRow>
              ) : (
                auditLogs.map((log) => (
                  <TableRow key={log.id}>
                    <TableCell className="text-xs">{log.created_at || "-"}</TableCell>
                    <TableCell className="font-mono text-xs">{log.key_prefix || "legacy/none"}</TableCell>
                    <TableCell className="text-xs">{log.endpoint}</TableCell>
                    <TableCell>{log.status_code}</TableCell>
                    <TableCell className="text-xs">{log.detail || ""}</TableCell>
                  </TableRow>
                ))
              )}
            </TableBody>
          </Table>
          <div className="flex flex-wrap items-center justify-between gap-2 border-t px-4 py-2 text-xs text-muted-foreground">
            <span>
              {auditTotal === 0
                ? "No entries"
                : `Showing ${auditPage * AUDIT_PAGE_SIZE + 1}–${Math.min(auditPage * AUDIT_PAGE_SIZE + auditLogs.length, auditTotal)} of ${auditTotal}`}
            </span>
            <div className="flex items-center gap-1">
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="h-8"
                disabled={loading || auditPage <= 0}
                onClick={() => setAuditPage((p) => Math.max(0, p - 1))}
              >
                <ChevronLeft className="h-4 w-4" />
                Previous
              </Button>
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="h-8"
                disabled={loading || (auditPage + 1) * AUDIT_PAGE_SIZE >= auditTotal}
                onClick={() => setAuditPage((p) => p + 1)}
              >
                Next
                <ChevronRight className="h-4 w-4" />
              </Button>
            </div>
          </div>
        </CardContent>
      </Card>

      <Dialog open={createOpen} onOpenChange={setCreateOpen}>
        <DialogContent className="max-w-3xl max-h-[90vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>Create managed API key</DialogTitle>
          </DialogHeader>
          <div className="space-y-4">
            <div className="grid grid-cols-2 gap-3">
              <div>
                <Label>Client name</Label>
                <Input className="mt-1" value={clientName} onChange={(e) => setClientName(e.target.value)} />
              </div>
              <div>
                <Label>Label</Label>
                <Input className="mt-1" value={label} onChange={(e) => setLabel(e.target.value)} />
              </div>
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <Label>Scopes</Label>
                  <div className="flex flex-wrap gap-1.5">
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      className="h-8 text-xs"
                      onClick={() => setSelectedScopes([...ALL_MANAGED_SCOPE_IDS])}
                    >
                      Select all
                    </Button>
                    <Button type="button" variant="outline" size="sm" className="h-8 text-xs" onClick={() => setSelectedScopes([])}>
                      Clear all
                    </Button>
                  </div>
                </div>
                <div className="mt-2 space-y-2 border rounded-md p-3 max-h-44 overflow-auto">
                  {AVAILABLE_SCOPES.map((scope) => (
                    <label key={scope.id} className="flex items-start justify-between gap-3 text-sm">
                      <span>
                        <span className="font-medium">{scope.label}</span>
                        <span className="block text-xs text-muted-foreground">{scope.description}</span>
                      </span>
                      <input
                        type="checkbox"
                        checked={selectedScopes.includes(scope.id)}
                        onChange={() => toggleScope(scope.id)}
                      />
                    </label>
                  ))}
                </div>
                <div className="mt-2 flex flex-wrap gap-2">
                  {selectedScopes.length ? (
                    selectedScopes.map((scope) => (
                      <Badge key={scope} variant="secondary" className="font-mono text-xs">
                        {scope}
                      </Badge>
                    ))
                  ) : (
                    <span className="text-xs text-muted-foreground">No scope selected</span>
                  )}
                </div>
              </div>
              <div>
                <Label>Expires at (optional)</Label>
                <Input className="mt-1" value={expiresAt} onChange={(e) => setExpiresAt(e.target.value)} placeholder="YYYY-MM-DD HH:MM:SS" />
              </div>
            </div>

            <div className="space-y-2 border rounded-md p-3">
              <Label>Assign accounts (server-side routing pool)</Label>
              <div className="max-h-52 overflow-auto space-y-2">
                {tokens.map((t) => (
                  <label key={t.id} className="flex items-center justify-between gap-3 text-sm">
                    <span>
                      #{t.id} {t.email || ""}
                    </span>
                    <div className="flex items-center gap-2">
                      <Badge variant={t.is_active ? "default" : "secondary"}>{t.is_active ? "active" : "disabled"}</Badge>
                      <input type="checkbox" checked={selectedAccountIds.includes(t.id)} onChange={() => toggleAccount(t.id)} />
                    </div>
                  </label>
                ))}
              </div>
            </div>

            <div className="space-y-2 border rounded-md p-3">
              <div className="flex items-center justify-between">
                <Label>Per-endpoint limits</Label>
                <Button type="button" variant="outline" size="sm" onClick={addLimitRow}>
                  Add row
                </Button>
              </div>
              {limits.map((row, idx) => (
                <div key={idx} className="grid grid-cols-4 gap-2">
                  <Input placeholder="/v1/chat/completions" value={row.endpoint} onChange={(e) => updateLimitRow(idx, { endpoint: e.target.value })} />
                  <Input placeholder="rpm" value={row.rpm} onChange={(e) => updateLimitRow(idx, { rpm: e.target.value })} />
                  <Input placeholder="rph" value={row.rph} onChange={(e) => updateLimitRow(idx, { rph: e.target.value })} />
                  <Input placeholder="burst" value={row.burst} onChange={(e) => updateLimitRow(idx, { burst: e.target.value })} />
                </div>
              ))}
            </div>

            {createdKey ? (
              <div className="space-y-2 border rounded-md p-3">
                <Label>Generated API key (shown once)</Label>
                <Input readOnly className="font-mono text-xs" value={createdKey} />
              </div>
            ) : null}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setCreateOpen(false)}>
              Close
            </Button>
            <Button onClick={createKey} disabled={saving}>
              {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : "Create key"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={editOpen} onOpenChange={handleEditOpenChange}>
        <DialogContent className="max-w-4xl max-h-[90vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>Edit managed API key</DialogTitle>
          </DialogHeader>
          <div className="space-y-4">
            <div className="grid grid-cols-2 gap-3">
              <div>
                <Label>Client name</Label>
                <Input className="mt-1" value={clientName} onChange={(e) => setClientName(e.target.value)} />
              </div>
              <div>
                <Label>Label</Label>
                <Input className="mt-1" value={label} onChange={(e) => setLabel(e.target.value)} />
              </div>
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <Label>Scopes</Label>
                  <div className="flex flex-wrap gap-1.5">
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      className="h-8 text-xs"
                      onClick={() => setSelectedScopes([...ALL_MANAGED_SCOPE_IDS])}
                    >
                      Select all
                    </Button>
                    <Button type="button" variant="outline" size="sm" className="h-8 text-xs" onClick={() => setSelectedScopes([])}>
                      Clear all
                    </Button>
                  </div>
                </div>
                <div className="mt-2 space-y-2 border rounded-md p-3 max-h-44 overflow-auto">
                  {AVAILABLE_SCOPES.map((scope) => (
                    <label key={`edit-${scope.id}`} className="flex items-start justify-between gap-3 text-sm">
                      <span>
                        <span className="font-medium">{scope.label}</span>
                        <span className="block text-xs text-muted-foreground">{scope.description}</span>
                      </span>
                      <input
                        type="checkbox"
                        checked={selectedScopes.includes(scope.id)}
                        onChange={() => toggleScope(scope.id)}
                      />
                    </label>
                  ))}
                </div>
                {legacyScopesNotice ? (
                  <p className="mt-2 text-xs text-amber-800 dark:text-amber-200">
                    This key used legacy full access (<code className="rounded bg-muted px-1">*</code>). Checkboxes now show explicit
                    permissions—save to store them on the key.
                  </p>
                ) : null}
              </div>
              <div>
                <Label>Expires at (optional)</Label>
                <Input className="mt-1" value={expiresAt} onChange={(e) => setExpiresAt(e.target.value)} placeholder="YYYY-MM-DD HH:MM:SS" />
              </div>
            </div>

            <div className="space-y-3 rounded-md border p-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div>
                  <Label>Generation stats</Label>
                  <p className="mt-0.5 text-xs text-muted-foreground">
                    Durable image/video generation counters for this key.
                  </p>
                </div>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  className="h-8"
                  disabled={generationStatsLoading || editingKeyId == null}
                  onClick={() => editingKeyId != null && void loadGenerationStats(editingKeyId)}
                >
                  <RefreshCw className={`h-3.5 w-3.5 mr-1 ${generationStatsLoading ? "animate-spin" : ""}`} />
                  Refresh
                </Button>
              </div>
              {generationStatsLoading && !generationStats ? (
                <div className="flex justify-center py-6 text-muted-foreground">
                  <Loader2 className="h-6 w-6 animate-spin" />
                </div>
              ) : (
                <div className="grid gap-3 sm:grid-cols-3">
                  <div className="rounded-md border bg-muted/20 px-3 py-2">
                    <div className="text-xs text-muted-foreground">Images</div>
                    <div className="text-lg font-semibold tabular-nums">{generationStats?.total_images ?? 0}</div>
                    <div className="text-[11px] text-muted-foreground">Today {generationStats?.today_images ?? 0}</div>
                  </div>
                  <div className="rounded-md border bg-muted/20 px-3 py-2">
                    <div className="text-xs text-muted-foreground">Videos</div>
                    <div className="text-lg font-semibold tabular-nums">{generationStats?.total_videos ?? 0}</div>
                    <div className="text-[11px] text-muted-foreground">Today {generationStats?.today_videos ?? 0}</div>
                  </div>
                  <div className="rounded-md border bg-muted/20 px-3 py-2">
                    <div className="text-xs text-muted-foreground">Errors</div>
                    <div className="text-lg font-semibold tabular-nums">{generationStats?.total_errors ?? 0}</div>
                    <div className="text-[11px] text-muted-foreground">Today {generationStats?.today_errors ?? 0}</div>
                  </div>
                </div>
              )}
            </div>

            <div className="space-y-3 rounded-md border p-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div>
                  <Label>Adobe success (HTTP 200)</Label>
                  <p className="mt-0.5 text-xs text-muted-foreground">
                    Every logged Adobe endpoint (<code className="rounded bg-muted px-1">adobe:*</code> in request logs) in the last 12 months.
                  </p>
                </div>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  className="h-8"
                  disabled={adobeUsageLoading || editingKeyId == null}
                  onClick={() => editingKeyId != null && void loadAdobeUsage(editingKeyId)}
                >
                  <RefreshCw className={`h-3.5 w-3.5 mr-1 ${adobeUsageLoading ? "animate-spin" : ""}`} />
                  Refresh
                </Button>
              </div>
              {adobeUsageLoading && !adobeUsageRows.length && !adobeUsageOpRows.length ? (
                <div className="flex justify-center py-6 text-muted-foreground">
                  <Loader2 className="h-6 w-6 animate-spin" />
                </div>
              ) : adobeUsageRows.length || adobeUsageOpRows.length ? (
                <div className="space-y-4">
                  {adobeUsageRows.length ? (
                    <div>
                      <p className="mb-1.5 text-xs font-medium text-muted-foreground">Total by month</p>
                      <div className="rounded-md border bg-background">
                        <Table>
                          <TableHeader>
                            <TableRow>
                              <TableHead className="text-xs">Month</TableHead>
                              <TableHead className="text-xs">Successful requests</TableHead>
                            </TableRow>
                          </TableHeader>
                          <TableBody>
                            {adobeUsageRows.map((row) => (
                              <TableRow key={row.year_month}>
                                <TableCell className="font-mono text-xs">{row.year_month}</TableCell>
                                <TableCell className="text-xs tabular-nums">{row.success_count}</TableCell>
                              </TableRow>
                            ))}
                          </TableBody>
                        </Table>
                      </div>
                    </div>
                  ) : null}
                  {adobeUsageOpRows.length ? (
                    <div>
                      <p className="mb-1.5 text-xs font-medium text-muted-foreground">By endpoint</p>
                      <div className="rounded-md border bg-background">
                        <Table>
                          <TableHeader>
                            <TableRow>
                              <TableHead className="text-xs">Month</TableHead>
                              <TableHead className="text-xs">Endpoint</TableHead>
                              <TableHead className="text-xs">Successful requests</TableHead>
                            </TableRow>
                          </TableHeader>
                          <TableBody>
                            {adobeUsageOpRows.map((row) => (
                              <TableRow key={`${row.year_month}-${row.operation}`}>
                                <TableCell className="font-mono text-xs">{row.year_month}</TableCell>
                                <TableCell className="font-mono text-[11px]">{row.operation}</TableCell>
                                <TableCell className="text-xs tabular-nums">{row.success_count}</TableCell>
                              </TableRow>
                            ))}
                          </TableBody>
                        </Table>
                      </div>
                    </div>
                  ) : null}
                </div>
              ) : (
                <p className="text-xs text-muted-foreground py-2">No successful Adobe requests in the selected window yet.</p>
              )}
            </div>

            <div className="space-y-2 border rounded-md p-3">
              <Label>Assign accounts (server-side routing pool)</Label>
              <div className="max-h-52 overflow-auto space-y-2">
                {tokens.map((t) => (
                  <label key={`edit-account-${t.id}`} className="flex items-center justify-between gap-3 text-sm">
                    <span className="min-w-0">
                      <span className="block truncate">
                        #{t.id} {t.email || ""}
                      </span>
                      {activeProjectByToken.get(t.id) ? (
                        <span
                          className="block truncate font-mono text-[10px] text-muted-foreground"
                          title={`${activeProjectByToken.get(t.id) || ""} ${activeProjectNameByToken.get(t.id) || ""}`.trim()}
                        >
                          active project: {activeProjectByToken.get(t.id)}
                          {activeProjectNameByToken.get(t.id) ? ` (${activeProjectNameByToken.get(t.id)})` : ""}
                        </span>
                      ) : null}
                    </span>
                    <div className="flex items-center gap-2">
                      <Badge variant={t.is_active ? "default" : "secondary"}>{t.is_active ? "active" : "disabled"}</Badge>
                      <input type="checkbox" checked={selectedAccountIds.includes(t.id)} onChange={() => toggleAccount(t.id)} />
                    </div>
                  </label>
                ))}
              </div>
            </div>
            <div className="space-y-2 border rounded-md p-3">
              <div className="flex items-center justify-between">
                <Label>Per-endpoint limits</Label>
                <Button type="button" variant="outline" size="sm" onClick={addLimitRow}>
                  Add row
                </Button>
              </div>
              {limits.map((row, idx) => (
                <div key={`edit-limit-${idx}`} className="grid grid-cols-4 gap-2">
                  <Input placeholder="/v1/chat/completions" value={row.endpoint} onChange={(e) => updateLimitRow(idx, { endpoint: e.target.value })} />
                  <Input placeholder="rpm" value={row.rpm} onChange={(e) => updateLimitRow(idx, { rpm: e.target.value })} />
                  <Input placeholder="rph" value={row.rph} onChange={(e) => updateLimitRow(idx, { rph: e.target.value })} />
                  <Input placeholder="burst" value={row.burst} onChange={(e) => updateLimitRow(idx, { burst: e.target.value })} />
                </div>
              ))}
            </div>

            {SHOW_VIDEOFX_PROJECTS ? (
              <div className="space-y-4 rounded-lg border bg-muted/20 p-4">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <FolderKanban className="h-5 w-5 text-muted-foreground" />
                    <h3 className="text-sm font-semibold">VideoFX projects</h3>
                  </div>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={() => void loadKeyProjects()}
                    disabled={keyProjectsLoading}
                    className="h-8"
                  >
                    <RefreshCw className={`h-3.5 w-3.5 mr-1 ${keyProjectsLoading ? "animate-spin" : ""}`} />
                    Refresh
                  </Button>
                </div>
                <p className="text-xs text-muted-foreground">
                  Projects tagged with this key only. Account active status below is derived from this key&apos;s project rows (not global token cursor). Public API:{" "}
                  <code className="rounded bg-muted px-1 text-[10px]">POST /v1/projects</code> (requires{" "}
                  <code className="rounded bg-muted px-1 text-[10px]">projects:write</code> or <code className="rounded bg-muted px-1 text-[10px]">*</code>
                  ).
                </p>

                {keyProjectAccounts.length ? (
                  <div className="rounded-md border bg-background">
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHead className="text-xs">Account</TableHead>
                          <TableHead className="text-xs">Email</TableHead>
                          <TableHead className="text-xs">Key-active project ID</TableHead>
                          <TableHead className="text-xs">Key-active project name</TableHead>
                          <TableHead className="text-xs">Status</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {keyProjectAccounts.map((a) => {
                          const activeProjectId = a.active_project_id || a.current_project_id || null
                          const activeProjectName = a.active_project_name || a.current_project_name || null
                          return (
                            <TableRow key={a.token_id}>
                              <TableCell className="text-xs">#{a.token_id}</TableCell>
                              <TableCell className="max-w-[220px] truncate text-xs" title={a.email || ""}>
                                {a.email || "—"}
                              </TableCell>
                              <TableCell className="max-w-[280px] truncate font-mono text-[10px]" title={activeProjectId || ""}>
                                {activeProjectId || "—"}
                              </TableCell>
                              <TableCell className="max-w-[200px] truncate text-xs" title={activeProjectName || ""}>
                                {activeProjectName || "—"}
                              </TableCell>
                              <TableCell>
                                <Badge variant={activeProjectId ? "default" : "secondary"} className="text-[10px]">
                                  {activeProjectId ? "has active project" : "no active project"}
                                </Badge>
                              </TableCell>
                            </TableRow>
                          )
                        })}
                      </TableBody>
                    </Table>
                  </div>
                ) : (
                  <p className="text-xs text-muted-foreground">No native accounts assigned. Provider-only scopes like GeminiGen can still be used.</p>
                )}

                <div className="rounded-md border bg-background">
                  <Table>
                    <TableHeader>
                        <TableRow>
                        <TableHead className="text-xs">Project ID</TableHead>
                        <TableHead className="text-xs">Name</TableHead>
                        <TableHead className="text-xs">Token</TableHead>
                        <TableHead className="text-xs">Created</TableHead>
                          <TableHead className="text-xs">Status</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {keyProjectsLoading && !keyProjectRows.length ? (
                        <TableRow>
                          <TableCell colSpan={5} className="text-center text-muted-foreground py-6 text-xs">
                            Loading…
                          </TableCell>
                        </TableRow>
                      ) : !keyProjectRows.length ? (
                        <TableRow>
                          <TableCell colSpan={5} className="text-center text-muted-foreground py-6 text-xs">
                            No projects stored for this key yet
                          </TableCell>
                        </TableRow>
                      ) : (
                        keyProjectRows.map((p) => (
                          <TableRow key={p.project_id}>
                            <TableCell className="max-w-[140px] truncate font-mono text-[10px]" title={p.project_id}>
                              {p.project_id}
                            </TableCell>
                            <TableCell className="max-w-[120px] truncate text-xs" title={p.project_name}>
                              {p.project_name}
                            </TableCell>
                            <TableCell className="text-xs">
                              #{p.token_id ?? "—"} {typeof p.token_id === "number" ? tokenEmail(p.token_id) : ""}
                            </TableCell>
                            <TableCell className="text-[10px] text-muted-foreground">{p.created_at || "—"}</TableCell>
                            <TableCell>
                              {(() => {
                                const isActive =
                                  p.project_status === "active" ||
                                  p.is_current_for_token ||
                                  (typeof p.token_id === "number" &&
                                    activeProjectByToken.get(p.token_id) === p.project_id)
                                return (
                                  <Badge variant={isActive ? "default" : "secondary"} className="text-[10px]">
                                    {isActive ? "active" : "old"}
                                  </Badge>
                                )
                              })()}
                            </TableCell>
                          </TableRow>
                        ))
                      )}
                    </TableBody>
                  </Table>
                  <div className="flex flex-wrap items-center justify-between gap-2 border-t px-3 py-2 text-xs text-muted-foreground">
                    <span>
                      {keyProjectsTotal === 0
                        ? "No rows"
                        : `Showing ${keyProjectsPage * KEY_PROJECT_PAGE_SIZE + 1}–${Math.min(keyProjectsPage * KEY_PROJECT_PAGE_SIZE + keyProjectRows.length, keyProjectsTotal)} of ${keyProjectsTotal}`}
                    </span>
                    <div className="flex items-center gap-1">
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        className="h-8"
                        disabled={keyProjectsLoading || keyProjectsPage <= 0}
                        onClick={() => setKeyProjectsPage((p) => Math.max(0, p - 1))}
                      >
                        <ChevronLeft className="h-4 w-4" />
                        Previous
                      </Button>
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        className="h-8"
                        disabled={keyProjectsLoading || (keyProjectsPage + 1) * KEY_PROJECT_PAGE_SIZE >= keyProjectsTotal}
                        onClick={() => setKeyProjectsPage((p) => p + 1)}
                      >
                        Next
                        <ChevronRight className="h-4 w-4" />
                      </Button>
                    </div>
                  </div>
                </div>

                <div className="space-y-3 rounded-md border bg-background p-3">
                  <Label className="text-sm font-medium">Create project</Label>
                  <div className="grid gap-3 sm:grid-cols-2">
                    <div className="space-y-1">
                      <Label className="text-xs text-muted-foreground">Account</Label>
                      <Select value={newProjTokenId} onValueChange={setNewProjTokenId}>
                        <SelectTrigger className="h-9">
                          <SelectValue placeholder="Select account" />
                        </SelectTrigger>
                        <SelectContent>
                          {tokens
                            .filter((t) => selectedAccountIds.includes(t.id))
                            .map((t) => (
                              <SelectItem key={t.id} value={String(t.id)}>
                                #{t.id} {t.email || ""}
                              </SelectItem>
                            ))}
                        </SelectContent>
                      </Select>
                    </div>
                    <div className="space-y-1">
                      <Label className="text-xs text-muted-foreground">Title (optional)</Label>
                      <Input value={newProjTitle} onChange={(e) => setNewProjTitle(e.target.value)} placeholder="Custom project title" />
                    </div>
                  </div>
                  <label className="flex items-center gap-2 text-sm">
                    <input type="checkbox" checked={newProjSetCurrent} onChange={(e) => setNewProjSetCurrent(e.target.checked)} />
                    Set as current project for this account
                  </label>
                  <Button
                    type="button"
                    size="sm"
                    onClick={() => void createKeyProject()}
                    disabled={creatingProj || !newProjTokenId || !selectedAccountIds.length}
                  >
                    {creatingProj ? <Loader2 className="h-4 w-4 animate-spin" /> : "Create project"}
                  </Button>
                  {!selectedAccountIds.length ? (
                    <p className="text-xs text-amber-600 dark:text-amber-500">Assign at least one account above to create a project.</p>
                  ) : null}
                </div>
              </div>
            ) : null}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => handleEditOpenChange(false)}>
              Close
            </Button>
            <Button onClick={submitEditKey} disabled={saving}>
              {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : "Save changes"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={plainKeyOpen} onOpenChange={setPlainKeyOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Plain API key</DialogTitle>
          </DialogHeader>
          <div className="space-y-2">
            <Label>Copy and store safely</Label>
            <Input readOnly className="font-mono text-xs" value={plainKeyValue} />
          </div>
          <DialogFooter>
            <Button
              onClick={async () => {
                try {
                  await navigator.clipboard.writeText(plainKeyValue)
                  toast.success("Copied")
                } catch {
                  toast.error("Copy failed")
                }
              }}
            >
              Copy key
            </Button>
            <Button variant="outline" onClick={() => setPlainKeyOpen(false)}>
              Close
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
