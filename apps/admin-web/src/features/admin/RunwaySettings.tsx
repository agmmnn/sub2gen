import { useCallback, useEffect, useMemo, useState } from "react"
import { useAuth } from "../../contexts/AuthContext"
import { adminFetch, adminJson } from "../../lib/adminApi"
import { Badge } from "../../components/ui/badge"
import { Button } from "../../components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "../../components/ui/card"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "../../components/ui/dialog"
import { Input } from "../../components/ui/input"
import { Label } from "../../components/ui/label"
import { ScrollArea } from "../../components/ui/scroll-area"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../../components/ui/select"
import { Switch } from "../../components/ui/switch"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../../components/ui/table"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "../../components/ui/tabs"
import { Textarea } from "../../components/ui/textarea"
import { toast } from "sonner"
import {
  CheckCircle2,
  Edit3,
  FileJson,
  Plus,
  RefreshCw,
  RotateCcw,
  Save,
  Search,
  Trash2,
} from "lucide-react"

type RunwayConfig = {
  enabled: boolean
  base_url: string
  poll_interval_sec: number
  timeout_sec: number
  cache_outputs: boolean
}

type RunwayAccount = {
  id: number
  label: string
  raw_credential: string
  is_active: boolean
  workspace_id: string
  team_id: string
  concurrency_limit: number
  in_flight: number
  last_status: string
  last_error: string
}

type RunwayTeam = {
  id: string
  username: string
  team_name: string
  first_name: string
  last_name: string
  email: string
  role: string
  current_plan: string
  plan_expiration?: string | null
  gpu_credits: number
  organization_id: string
  organization_name: string
}

type RunwayModelKind = "image" | "video" | "audio" | "upscale"
type KindFilter = "all" | RunwayModelKind | "blocked"
type StatusFilter = "all" | "enabled" | "disabled" | "available" | "blocked"

type RunwayModel = {
  id: number
  public_model_id: string
  display_name: string
  kind: RunwayModelKind
  task_type: string
  builder_key: string
  default_options: string
  request_mapping: string
  capability_schema: string
  media_roles: string
  supported_modes: string
  limits: string
  feature_flags: string
  cost_feature: string
  source_version: string
  live_available: boolean
  disabled_reason: string
  last_synced_at?: string | null
  is_enabled: boolean
}

type RunwayAccountDraft = {
  id?: number
  label: string
  raw_credential: string
  is_active: boolean
  workspace_id: string
  team_id: string
  concurrency_limit: string
}

type RunwayModelDraft = Omit<RunwayModel, "id"> & { id?: number }
type JsonFieldKey =
  | "default_options"
  | "request_mapping"
  | "capability_schema"
  | "limits"
  | "media_roles"
  | "supported_modes"
  | "feature_flags"

const DEFAULT_CONFIG: RunwayConfig = {
  enabled: false,
  base_url: "https://api.runwayml.com/v1",
  poll_interval_sec: 3,
  timeout_sec: 600,
  cache_outputs: true,
}

const EMPTY_ACCOUNT: RunwayAccountDraft = {
  label: "",
  raw_credential: "",
  is_active: true,
  workspace_id: "",
  team_id: "",
  concurrency_limit: "1",
}

const EMPTY_MODEL: RunwayModelDraft = {
  public_model_id: "runway-",
  display_name: "",
  kind: "image",
  task_type: "",
  builder_key: "",
  default_options: "{}",
  request_mapping: "{}",
  capability_schema: "{}",
  media_roles: "[]",
  supported_modes: "[]",
  limits: "{}",
  feature_flags: "[]",
  cost_feature: "",
  source_version: "",
  live_available: true,
  disabled_reason: "",
  is_enabled: true,
}

function cloneModel(model: RunwayModel | RunwayModelDraft): RunwayModelDraft {
  return { ...model }
}

function modelPayload(model: RunwayModelDraft) {
  return {
    ...model,
    display_name: model.display_name.trim() || model.public_model_id.trim(),
    public_model_id: model.public_model_id.trim(),
    task_type: model.task_type.trim(),
    builder_key: model.builder_key.trim(),
    cost_feature: model.cost_feature.trim(),
    source_version: model.source_version.trim(),
    disabled_reason: model.disabled_reason.trim(),
  }
}

function jsonText(value: string, fallback: "{}" | "[]") {
  const raw = (value || "").trim() || fallback
  return JSON.stringify(JSON.parse(raw), null, 2)
}

function validateModelJson(model: RunwayModelDraft) {
  try {
    jsonText(model.default_options, "{}")
    jsonText(model.request_mapping, "{}")
    jsonText(model.capability_schema, "{}")
    jsonText(model.limits, "{}")
    const arrayFields: JsonFieldKey[] = ["media_roles", "supported_modes", "feature_flags"]
    for (const key of arrayFields) {
      const parsed = JSON.parse((model[key] || "").trim() || "[]")
      if (!Array.isArray(parsed)) throw new Error(`${key} must be an array`)
    }
    return true
  } catch (error) {
    toast.error(error instanceof Error ? error.message : "Model JSON is invalid")
    return false
  }
}

function parseStringArray(value: string) {
  try {
    const parsed = JSON.parse((value || "").trim() || "[]")
    return Array.isArray(parsed) ? parsed.map(String).filter(Boolean) : []
  } catch {
    return []
  }
}

function stringArrayJson(value: string) {
  const parts = value
    .split(",")
    .map((part) => part.trim())
    .filter(Boolean)
  return JSON.stringify(parts, null, 2)
}

function kindBadgeVariant(kind: RunwayModelKind) {
  return kind === "video" ? "default" : kind === "audio" ? "secondary" : "outline"
}

function availabilityBadge(model: RunwayModel) {
  if (!model.live_available) return <Badge variant="destructive">blocked</Badge>
  return <Badge variant="secondary">available</Badge>
}

function enabledBadge(model: RunwayModel) {
  return model.is_enabled ? <Badge>enabled</Badge> : <Badge variant="outline">disabled</Badge>
}

function StatBox({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="rounded-md border bg-muted/20 px-3 py-2">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="text-lg font-semibold tabular-nums">{value}</div>
    </div>
  )
}

function teamOptionLabel(team: RunwayTeam) {
  const pieces = [
    team.team_name || team.username || `Team ${team.id}`,
    team.username ? `@${team.username}` : "",
    team.role,
    team.current_plan,
    `${Number(team.gpu_credits || 0).toLocaleString()} credits`,
  ].filter(Boolean)
  return `${pieces.join(" - ")} (${team.id})`
}

function JsonEditor({
  label,
  value,
  fallback,
  onChange,
  onFormat,
}: {
  label: string
  value: string
  fallback: "{}" | "[]"
  onChange: (value: string) => void
  onFormat: () => void
}) {
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between gap-2">
        <Label>{label}</Label>
        <Button type="button" size="sm" variant="outline" onClick={onFormat}>
          <FileJson className="mr-2 h-4 w-4" />
          Format
        </Button>
      </div>
      <Textarea
        className="min-h-[220px] resize-y font-mono text-xs leading-5"
        value={value || fallback}
        spellCheck={false}
        onChange={(event) => onChange(event.target.value)}
      />
    </div>
  )
}

function ChipListEditor({
  label,
  value,
  onChange,
}: {
  label: string
  value: string
  onChange: (value: string) => void
}) {
  const items = parseStringArray(value)
  return (
    <div className="space-y-2">
      <Label>{label}</Label>
      <Input
        value={items.join(", ")}
        onChange={(event) => onChange(stringArrayJson(event.target.value))}
        placeholder="comma, separated, values"
      />
      <div className="flex min-h-7 flex-wrap gap-1.5">
        {items.length ? (
          items.map((item) => (
            <Badge key={item} variant="outline" className="font-mono font-normal">
              {item}
            </Badge>
          ))
        ) : (
          <span className="text-xs text-muted-foreground">No values</span>
        )}
      </div>
    </div>
  )
}

export function RunwaySettings({ active }: { active: boolean }) {
  const { token } = useAuth()
  const [busy, setBusy] = useState(false)
  const [config, setConfig] = useState<RunwayConfig>(DEFAULT_CONFIG)
  const [accounts, setAccounts] = useState<RunwayAccount[]>([])
  const [models, setModels] = useState<RunwayModel[]>([])
  const [accountOpen, setAccountOpen] = useState(false)
  const [accountDraft, setAccountDraft] = useState<RunwayAccountDraft>(EMPTY_ACCOUNT)
  const [accountTeams, setAccountTeams] = useState<RunwayTeam[]>([])
  const [teamsLoading, setTeamsLoading] = useState(false)
  const [modelOpen, setModelOpen] = useState(false)
  const [modelDraft, setModelDraft] = useState<RunwayModelDraft>(EMPTY_MODEL)
  const [modelSearch, setModelSearch] = useState("")
  const [kindFilter, setKindFilter] = useState<KindFilter>("all")
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all")

  const load = useCallback(async () => {
    if (!token || !active) return
    const resp = await adminJson<{
      success?: boolean
      config?: Partial<RunwayConfig>
      accounts?: RunwayAccount[]
      models?: RunwayModel[]
    }>("/api/admin/runway/config", token)
    if (!resp.ok || !resp.data?.success) return
    setConfig({ ...DEFAULT_CONFIG, ...(resp.data.config || {}) })
    setAccounts(Array.isArray(resp.data.accounts) ? resp.data.accounts : [])
    setModels(Array.isArray(resp.data.models) ? resp.data.models : [])
  }, [token, active])

  useEffect(() => {
    void load()
  }, [load])

  const stats = useMemo(() => {
    return {
      accounts: accounts.length,
      activeAccounts: accounts.filter((account) => account.is_active).length,
      enabledModels: models.filter((model) => model.is_enabled).length,
      blockedModels: models.filter((model) => !model.live_available).length,
    }
  }, [accounts, models])

  const filteredModels = useMemo(() => {
    const query = modelSearch.trim().toLowerCase()
    return models.filter((model) => {
      const searchable = [
        model.display_name,
        model.public_model_id,
        model.task_type,
        model.builder_key,
      ].join(" ").toLowerCase()
      if (query && !searchable.includes(query)) return false
      if (kindFilter === "blocked" && model.live_available) return false
      if (kindFilter !== "all" && kindFilter !== "blocked" && model.kind !== kindFilter) return false
      if (statusFilter === "enabled" && !model.is_enabled) return false
      if (statusFilter === "disabled" && model.is_enabled) return false
      if (statusFilter === "available" && !model.live_available) return false
      if (statusFilter === "blocked" && model.live_available) return false
      return true
    })
  }, [kindFilter, modelSearch, models, statusFilter])

  const selectedAccountTeam = useMemo(() => {
    const selectedId = accountDraft.team_id || accountDraft.workspace_id
    return accountTeams.find((team) => team.id === selectedId)
  }, [accountDraft.team_id, accountDraft.workspace_id, accountTeams])

  const saveConfig = async () => {
    if (!token) return
    setBusy(true)
    try {
      const r = await adminFetch("/api/admin/runway/config", token, {
        method: "POST",
        body: JSON.stringify({
          enabled: config.enabled,
          base_url: config.base_url.trim(),
          poll_interval_sec: Number(config.poll_interval_sec) || 3,
          timeout_sec: Number(config.timeout_sec) || 600,
          cache_outputs: config.cache_outputs,
        }),
      })
      if (!r) return
      const d = await r.json()
      if (d.success) {
        toast.success("Runway config saved")
        await load()
      } else toast.error(d.detail || "Save failed")
    } finally {
      setBusy(false)
    }
  }

  const openAddAccount = () => {
    setAccountDraft({ ...EMPTY_ACCOUNT })
    setAccountTeams([])
    setAccountOpen(true)
  }

  const openEditAccount = (account: RunwayAccount) => {
    setAccountDraft({
      id: account.id,
      label: account.label,
      raw_credential: account.raw_credential,
      is_active: account.is_active,
      workspace_id: account.workspace_id || "",
      team_id: account.team_id || "",
      concurrency_limit: String(account.concurrency_limit || 1),
    })
    setAccountTeams([])
    setAccountOpen(true)
  }

  const loadAccountTeams = async () => {
    if (!token) return
    const credential = accountDraft.raw_credential.trim()
    if (!credential) return toast.error("Credential required before loading teams")
    setTeamsLoading(true)
    try {
      const r = await adminFetch("/api/admin/runway/accounts/teams", token, {
        method: "POST",
        body: JSON.stringify({ raw_credential: credential }),
      })
      if (!r) return
      const d = await r.json()
      if (!d.success) {
        toast.error(d.detail || "Could not load Runway teams")
        return
      }
      const teams = (Array.isArray(d.teams) ? d.teams : []) as RunwayTeam[]
      setAccountTeams(teams)
      if (!teams.length) {
        toast.info("No teams were returned for this credential")
        return
      }
      const currentId = accountDraft.team_id || accountDraft.workspace_id
      const fallbackId = String(d.team_id || d.workspace_id || "")
      const selected =
        teams.find((team) => team.id === currentId) ||
        teams.find((team) => team.id === fallbackId) ||
        teams[0]
      if (selected?.id) {
        setAccountDraft((draft) => ({ ...draft, workspace_id: selected.id, team_id: selected.id }))
      }
      toast.success(`Loaded ${teams.length} Runway team${teams.length === 1 ? "" : "s"}`)
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Could not load Runway teams")
    } finally {
      setTeamsLoading(false)
    }
  }

  const saveAccount = async () => {
    if (!token) return
    if (!accountDraft.raw_credential.trim()) return toast.error("Credential required")
    setBusy(true)
    try {
      const payload = {
        label: accountDraft.label.trim() || "Runway account",
        raw_credential: accountDraft.raw_credential.trim(),
        is_active: accountDraft.is_active,
        workspace_id: accountDraft.workspace_id.trim(),
        team_id: accountDraft.team_id.trim(),
        concurrency_limit: Number(accountDraft.concurrency_limit) || 1,
      }
      const r = await adminFetch(
        accountDraft.id ? `/api/admin/runway/accounts/${accountDraft.id}` : "/api/admin/runway/accounts",
        token,
        {
          method: accountDraft.id ? "PATCH" : "POST",
          body: JSON.stringify(payload),
        }
      )
      if (!r) return
      const d = await r.json()
      if (d.success) {
        toast.success(accountDraft.id ? "Account saved" : "Runway account added")
        setAccountOpen(false)
        await load()
      } else toast.error(d.detail || "Save failed")
    } finally {
      setBusy(false)
    }
  }

  const patchAccount = async (account: RunwayAccount, patch: Partial<RunwayAccount>) => {
    if (!token) return
    const r = await adminFetch(`/api/admin/runway/accounts/${account.id}`, token, {
      method: "PATCH",
      body: JSON.stringify(patch),
    })
    if (!r) return
    const d = await r.json()
    if (d.success) await load()
    else toast.error(d.detail || "Update failed")
  }

  const deleteAccount = async (account: RunwayAccount) => {
    if (!token) return
    if (!confirm(`Delete ${account.label || account.id}?`)) return
    const r = await adminFetch(`/api/admin/runway/accounts/${account.id}`, token, { method: "DELETE" })
    if (!r) return
    const d = await r.json()
    if (d.success) {
      toast.success("Account deleted")
      await load()
    } else toast.error(d.detail || "Delete failed")
  }

  const testAccount = async (account: RunwayAccount) => {
    if (!token) return
    setBusy(true)
    try {
      const r = await adminFetch(`/api/admin/runway/accounts/${account.id}/test`, token, { method: "POST" })
      if (!r) return
      const d = await r.json()
      if (d.success) toast.success("Runway account healthy")
      else toast.error(d.error || "Runway account test failed")
      await load()
    } finally {
      setBusy(false)
    }
  }

  const openAddModel = () => {
    setModelDraft({ ...EMPTY_MODEL })
    setModelOpen(true)
  }

  const openEditModel = (model: RunwayModel) => {
    setModelDraft(cloneModel(model))
    setModelOpen(true)
  }

  const resetModelDraft = () => {
    if (!modelDraft.id) {
      setModelDraft({ ...EMPTY_MODEL })
      return
    }
    const current = models.find((model) => model.id === modelDraft.id)
    if (current) setModelDraft(cloneModel(current))
  }

  const saveModel = async (model: RunwayModelDraft, isNew = !model.id) => {
    if (!token) return false
    if (!model.public_model_id.trim().startsWith("runway-")) {
      toast.error("Model id must start with runway-")
      return false
    }
    if (!model.task_type.trim()) {
      toast.error("Task type required")
      return false
    }
    if (!validateModelJson(model)) return false
    const r = await adminFetch(isNew ? "/api/admin/runway/models" : `/api/admin/runway/models/${model.id}`, token, {
      method: isNew ? "POST" : "PATCH",
      body: JSON.stringify(modelPayload(model)),
    })
    if (!r) return false
    const d = await r.json()
    if (d.success) {
      toast.success(isNew ? "Model added" : "Model saved")
      await load()
      return true
    }
    toast.error(d.detail || "Save failed")
    return false
  }

  const saveModelDialog = async () => {
    setBusy(true)
    try {
      const ok = await saveModel(modelDraft, !modelDraft.id)
      if (ok) setModelOpen(false)
    } finally {
      setBusy(false)
    }
  }

  const toggleModelEnabled = async (model: RunwayModel, is_enabled: boolean) => {
    setModels((rows) => rows.map((row) => (row.id === model.id ? { ...row, is_enabled } : row)))
    const ok = await saveModel({ ...model, is_enabled }, false)
    if (!ok) await load()
  }

  const deleteModel = async (model: RunwayModel) => {
    if (!token) return
    if (!confirm(`Delete ${model.public_model_id}?`)) return
    const r = await adminFetch(`/api/admin/runway/models/${model.id}`, token, { method: "DELETE" })
    if (!r) return
    const d = await r.json()
    if (d.success) {
      toast.success("Model deleted")
      await load()
    } else toast.error(d.detail || "Delete failed")
  }

  const syncModels = async () => {
    if (!token) return
    setBusy(true)
    try {
      const r = await adminFetch("/api/admin/runway/models/sync", token, { method: "POST" })
      if (!r) return
      const d = await r.json()
      if (d.success) {
        const blocked = Number(d.blocked || 0)
        const disabledTaskTypes = Number(d.disabled_task_types || 0)
        const suffix = blocked
          ? `, ${blocked} blocked by Runway features${disabledTaskTypes ? ` (${disabledTaskTypes} disabled task types)` : ""}`
          : ""
        toast.success(`Synced ${d.synced || 0} models${suffix}`)
        await load()
      } else toast.error(d.detail || "Sync failed")
    } finally {
      setBusy(false)
    }
  }

  const updateDraft = <K extends keyof RunwayModelDraft>(key: K, value: RunwayModelDraft[K]) => {
    setModelDraft((draft) => ({ ...draft, [key]: value }))
  }

  const formatDraftJson = (key: JsonFieldKey, fallback: "{}" | "[]") => {
    try {
      updateDraft(key, jsonText(modelDraft[key], fallback) as RunwayModelDraft[JsonFieldKey])
    } catch {
      toast.error(`${key} is not valid JSON`)
    }
  }

  if (!active) return null

  return (
    <div className="space-y-6 overflow-x-hidden">
      <Card>
        <CardHeader className="flex flex-col gap-3 border-b pb-4 lg:flex-row lg:items-center lg:justify-between">
          <CardTitle>Runway Overview</CardTitle>
          <div className="flex flex-wrap gap-2">
            <Button variant="outline" onClick={syncModels} disabled={busy}>
              <RefreshCw className="mr-2 h-4 w-4" />
              Sync models
            </Button>
            <Button onClick={saveConfig} disabled={busy}>
              <Save className="mr-2 h-4 w-4" />
              Save config
            </Button>
          </div>
        </CardHeader>
        <CardContent className="space-y-4 pt-4">
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-6">
            <StatBox label="Active accounts" value={`${stats.activeAccounts}/${stats.accounts}`} />
            <StatBox label="Enabled models" value={stats.enabledModels} />
            <StatBox label="Blocked models" value={stats.blockedModels} />
            <div className="rounded-md border bg-muted/20 px-3 py-2">
              <div className="text-xs text-muted-foreground">Runway</div>
              <div className="mt-1 flex items-center gap-2">
                <Switch checked={config.enabled} onCheckedChange={(enabled) => setConfig((c) => ({ ...c, enabled }))} />
                <span className="text-sm font-medium">{config.enabled ? "Enabled" : "Disabled"}</span>
              </div>
            </div>
            <div className="rounded-md border bg-muted/20 px-3 py-2">
              <div className="text-xs text-muted-foreground">Cache outputs</div>
              <div className="mt-1 flex items-center gap-2">
                <Switch checked={config.cache_outputs} onCheckedChange={(cache_outputs) => setConfig((c) => ({ ...c, cache_outputs }))} />
                <span className="text-sm font-medium">{config.cache_outputs ? "On" : "Off"}</span>
              </div>
            </div>
          </div>
          <div className="grid gap-3 lg:grid-cols-[minmax(260px,1fr)_140px_160px]">
            <div className="space-y-2">
              <Label>Base URL</Label>
              <Input value={config.base_url} onChange={(event) => setConfig((c) => ({ ...c, base_url: event.target.value }))} />
            </div>
            <div className="space-y-2">
              <Label>Poll seconds</Label>
              <Input
                type="number"
                min={1}
                value={config.poll_interval_sec}
                onChange={(event) => setConfig((c) => ({ ...c, poll_interval_sec: Number(event.target.value) || 3 }))}
              />
            </div>
            <div className="space-y-2">
              <Label>Timeout seconds</Label>
              <Input
                type="number"
                min={10}
                value={config.timeout_sec}
                onChange={(event) => setConfig((c) => ({ ...c, timeout_sec: Number(event.target.value) || 600 }))}
              />
            </div>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="flex flex-row items-center justify-between gap-3 border-b pb-4">
          <CardTitle>Runway Accounts</CardTitle>
          <Button onClick={openAddAccount}>
            <Plus className="mr-2 h-4 w-4" />
            Add account
          </Button>
        </CardHeader>
        <CardContent className="p-0">
          <div className="overflow-x-auto">
            <Table>
              <TableHeader className="sticky top-0 z-10 bg-background">
                <TableRow>
                  <TableHead className="min-w-[180px]">Label</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Workspace</TableHead>
                  <TableHead>Team</TableHead>
                  <TableHead className="text-center">Limit</TableHead>
                  <TableHead className="text-center">In flight</TableHead>
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {!accounts.length ? (
                  <TableRow>
                    <TableCell colSpan={7} className="py-8 text-center text-sm text-muted-foreground">
                      No Runway accounts configured.
                    </TableCell>
                  </TableRow>
                ) : (
                  accounts.map((account) => (
                    <TableRow key={account.id}>
                      <TableCell className="font-medium">{account.label || `Account ${account.id}`}</TableCell>
                      <TableCell>
                        <div className="flex flex-wrap items-center gap-2">
                          <Switch checked={account.is_active} onCheckedChange={(is_active) => void patchAccount(account, { is_active })} />
                          <Badge variant={account.last_status === "failed" ? "destructive" : "secondary"}>
                            {account.last_status || "new"}
                          </Badge>
                          {account.last_error ? (
                            <span className="max-w-[240px] truncate text-xs text-destructive" title={account.last_error}>
                              {account.last_error}
                            </span>
                          ) : null}
                        </div>
                      </TableCell>
                      <TableCell className="font-mono text-xs">{account.workspace_id || "-"}</TableCell>
                      <TableCell className="font-mono text-xs">{account.team_id || "-"}</TableCell>
                      <TableCell className="text-center tabular-nums">{account.concurrency_limit}</TableCell>
                      <TableCell className="text-center tabular-nums">{account.in_flight}</TableCell>
                      <TableCell className="text-right">
                        <div className="flex justify-end gap-2">
                          <Button size="sm" variant="outline" onClick={() => void testAccount(account)} disabled={busy} title="Test account">
                            <CheckCircle2 className="h-4 w-4" />
                          </Button>
                          <Button size="sm" variant="outline" onClick={() => openEditAccount(account)} title="Edit account">
                            <Edit3 className="h-4 w-4" />
                          </Button>
                          <Button size="sm" variant="outline" onClick={() => void deleteAccount(account)} title="Delete account">
                            <Trash2 className="h-4 w-4" />
                          </Button>
                        </div>
                      </TableCell>
                    </TableRow>
                  ))
                )}
              </TableBody>
            </Table>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="space-y-4 border-b pb-4">
          <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
            <CardTitle>Runway Models</CardTitle>
            <div className="flex flex-wrap gap-2">
              <Button variant="outline" onClick={syncModels} disabled={busy}>
                <RefreshCw className="mr-2 h-4 w-4" />
                Sync models
              </Button>
              <Button onClick={openAddModel}>
                <Plus className="mr-2 h-4 w-4" />
                Add model
              </Button>
            </div>
          </div>
          <div className="flex flex-col gap-3 xl:flex-row xl:items-center">
            <div className="relative min-w-[240px] flex-1">
              <Search className="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-muted-foreground" />
              <Input
                className="pl-9"
                placeholder="Search models, task types, builders"
                value={modelSearch}
                onChange={(event) => setModelSearch(event.target.value)}
              />
            </div>
            <Tabs value={kindFilter} onValueChange={(value) => setKindFilter(value as KindFilter)}>
              <TabsList className="flex h-auto flex-wrap justify-start">
                <TabsTrigger value="all">All</TabsTrigger>
                <TabsTrigger value="image">Image</TabsTrigger>
                <TabsTrigger value="video">Video</TabsTrigger>
                <TabsTrigger value="audio">Audio</TabsTrigger>
                <TabsTrigger value="upscale">Upscale</TabsTrigger>
                <TabsTrigger value="blocked">Blocked</TabsTrigger>
              </TabsList>
            </Tabs>
            <Select value={statusFilter} onValueChange={(value) => setStatusFilter(value as StatusFilter)}>
              <SelectTrigger className="w-full xl:w-[170px]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All status</SelectItem>
                <SelectItem value="enabled">Enabled</SelectItem>
                <SelectItem value="disabled">Disabled</SelectItem>
                <SelectItem value="available">Available</SelectItem>
                <SelectItem value="blocked">Blocked</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </CardHeader>
        <CardContent className="p-0">
          <div className="overflow-x-auto">
            <Table className="min-w-[980px]">
              <TableHeader className="sticky top-0 z-10 bg-background">
                <TableRow>
                  <TableHead className="w-[260px]">Model</TableHead>
                  <TableHead className="w-[120px]">Kind</TableHead>
                  <TableHead>Task type</TableHead>
                  <TableHead>Builder</TableHead>
                  <TableHead className="w-[120px]">Availability</TableHead>
                  <TableHead className="w-[120px]">Enabled</TableHead>
                  <TableHead className="w-[150px] text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {!models.length ? (
                  <TableRow>
                    <TableCell colSpan={7} className="py-8 text-center text-sm text-muted-foreground">
                      No Runway models found. Sync presets to load the manifest.
                    </TableCell>
                  </TableRow>
                ) : !filteredModels.length ? (
                  <TableRow>
                    <TableCell colSpan={7} className="py-8 text-center text-sm text-muted-foreground">
                      No models match the current search and filters.
                    </TableCell>
                  </TableRow>
                ) : (
                  filteredModels.map((model) => (
                    <TableRow key={model.id}>
                      <TableCell>
                        <div className="max-w-[250px]">
                          <div className="truncate font-medium" title={model.display_name || model.public_model_id}>
                            {model.display_name || model.public_model_id}
                          </div>
                          <div className="truncate font-mono text-xs text-muted-foreground" title={model.public_model_id}>
                            {model.public_model_id}
                          </div>
                        </div>
                      </TableCell>
                      <TableCell>
                        <Badge variant={kindBadgeVariant(model.kind)}>{model.kind}</Badge>
                      </TableCell>
                      <TableCell className="max-w-[220px] truncate font-mono text-xs" title={model.task_type}>
                        {model.task_type || "-"}
                      </TableCell>
                      <TableCell className="max-w-[190px] truncate font-mono text-xs" title={model.builder_key}>
                        {model.builder_key || "-"}
                      </TableCell>
                      <TableCell>{availabilityBadge(model)}</TableCell>
                      <TableCell>
                        <div className="flex items-center gap-2">
                          <Switch checked={model.is_enabled} onCheckedChange={(checked) => void toggleModelEnabled(model, checked)} />
                          {enabledBadge(model)}
                        </div>
                      </TableCell>
                      <TableCell className="text-right">
                        <div className="flex justify-end gap-2">
                          <Button size="sm" variant="outline" onClick={() => openEditModel(model)} title="Edit model">
                            <Edit3 className="h-4 w-4" />
                          </Button>
                          <Button size="sm" variant="outline" onClick={() => void deleteModel(model)} title="Delete model">
                            <Trash2 className="h-4 w-4" />
                          </Button>
                        </div>
                      </TableCell>
                    </TableRow>
                  ))
                )}
              </TableBody>
            </Table>
          </div>
        </CardContent>
      </Card>

      <Dialog open={accountOpen} onOpenChange={setAccountOpen}>
        <DialogContent className="max-h-[92vh] max-w-2xl overflow-y-auto">
          <DialogHeader>
            <DialogTitle>{accountDraft.id ? "Edit Runway Account" : "Add Runway Account"}</DialogTitle>
            <DialogDescription>Credentials stay hidden in the account table and are only editable here.</DialogDescription>
          </DialogHeader>
          <div className="grid gap-4">
            <div className="grid gap-3 md:grid-cols-[1fr_140px]">
              <div className="space-y-2">
                <Label>Label</Label>
                <Input value={accountDraft.label} onChange={(event) => setAccountDraft((draft) => ({ ...draft, label: event.target.value }))} />
              </div>
              <div className="space-y-2">
                <Label>Concurrency</Label>
                <Input
                  type="number"
                  min={-1}
                  value={accountDraft.concurrency_limit}
                  onChange={(event) => setAccountDraft((draft) => ({ ...draft, concurrency_limit: event.target.value }))}
                />
              </div>
            </div>
            <div className="flex items-center gap-3">
              <Switch
                checked={accountDraft.is_active}
                onCheckedChange={(is_active) => setAccountDraft((draft) => ({ ...draft, is_active }))}
              />
              <Label>Active</Label>
            </div>
            <div className="space-y-2">
              <Label>JWT or cookie string</Label>
              <Textarea
                className="min-h-[150px] font-mono text-xs"
                value={accountDraft.raw_credential}
                spellCheck={false}
                onChange={(event) => setAccountDraft((draft) => ({ ...draft, raw_credential: event.target.value }))}
              />
            </div>
            <div className="space-y-3 rounded-md border bg-muted/20 p-3">
              <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                <div>
                  <Label>Team / workspace</Label>
                  <div className="mt-1 text-xs text-muted-foreground">
                    Load the teams available to this Runway account, then choose which one sub2gen should use.
                  </div>
                </div>
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => void loadAccountTeams()}
                  disabled={teamsLoading || !accountDraft.raw_credential.trim()}
                >
                  <RefreshCw className={`mr-2 h-4 w-4 ${teamsLoading ? "animate-spin" : ""}`} />
                  {teamsLoading ? "Loading" : "Load teams"}
                </Button>
              </div>
              {accountTeams.length ? (
                <div className="space-y-3">
                  <Select
                    value={accountDraft.team_id || accountDraft.workspace_id}
                    onValueChange={(id) => setAccountDraft((draft) => ({ ...draft, workspace_id: id, team_id: id }))}
                  >
                    <SelectTrigger>
                      <SelectValue placeholder="Select Runway team" />
                    </SelectTrigger>
                    <SelectContent>
                      {accountTeams.map((team) => (
                        <SelectItem key={team.id} value={team.id}>
                          {teamOptionLabel(team)}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  {selectedAccountTeam ? (
                    <div className="flex flex-wrap gap-2 text-xs">
                      <Badge variant="secondary">{selectedAccountTeam.role || "role unknown"}</Badge>
                      {selectedAccountTeam.current_plan ? <Badge variant="outline">{selectedAccountTeam.current_plan}</Badge> : null}
                      <Badge variant="outline">{Number(selectedAccountTeam.gpu_credits || 0).toLocaleString()} credits</Badge>
                      {selectedAccountTeam.organization_name ? (
                        <Badge variant="outline">{selectedAccountTeam.organization_name}</Badge>
                      ) : null}
                    </div>
                  ) : null}
                </div>
              ) : (
                <div className="text-xs text-muted-foreground">
                  No teams loaded yet. If you do not load teams, sub2gen will fall back to the ID decoded from the JWT.
                </div>
              )}
              <div className="grid gap-3 md:grid-cols-2">
                <div className="space-y-2">
                  <Label>Workspace header ID</Label>
                  <Input
                    className="font-mono text-xs"
                    value={accountDraft.workspace_id}
                    placeholder="x-runway-workspace"
                    onChange={(event) => setAccountDraft((draft) => ({ ...draft, workspace_id: event.target.value }))}
                  />
                </div>
                <div className="space-y-2">
                  <Label>Team ID / asTeamId</Label>
                  <Input
                    className="font-mono text-xs"
                    value={accountDraft.team_id}
                    placeholder="asTeamId"
                    onChange={(event) => setAccountDraft((draft) => ({ ...draft, team_id: event.target.value }))}
                  />
                </div>
              </div>
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setAccountOpen(false)}>Cancel</Button>
            <Button onClick={saveAccount} disabled={busy}>
              <Save className="mr-2 h-4 w-4" />
              Save account
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={modelOpen} onOpenChange={setModelOpen}>
        <DialogContent className="max-h-[92vh] max-w-5xl overflow-hidden p-0">
          <DialogHeader className="border-b p-6 pr-12">
            <DialogTitle>{modelDraft.id ? "Edit Runway Model" : "Add Runway Model"}</DialogTitle>
            <DialogDescription>
              Keep the table clean; advanced Runway fields live in this editor.
            </DialogDescription>
          </DialogHeader>
          <Tabs defaultValue="general" className="min-h-0">
            <div className="border-b px-6 pt-4">
              <TabsList className="flex h-auto flex-wrap justify-start">
                <TabsTrigger value="general">General</TabsTrigger>
                <TabsTrigger value="capabilities">Capabilities</TabsTrigger>
                <TabsTrigger value="defaults">Defaults</TabsTrigger>
                <TabsTrigger value="schema">Schema</TabsTrigger>
              </TabsList>
            </div>
            <ScrollArea className="h-[62vh] px-6 pb-6">
              <TabsContent value="general" className="space-y-4 pt-4">
                <div className="grid gap-4 md:grid-cols-2">
                  <div className="space-y-2">
                    <Label>Public model ID</Label>
                    <Input value={modelDraft.public_model_id} onChange={(event) => updateDraft("public_model_id", event.target.value)} />
                  </div>
                  <div className="space-y-2">
                    <Label>Display name</Label>
                    <Input value={modelDraft.display_name} onChange={(event) => updateDraft("display_name", event.target.value)} />
                  </div>
                  <div className="space-y-2">
                    <Label>Kind</Label>
                    <Select value={modelDraft.kind} onValueChange={(kind) => updateDraft("kind", kind as RunwayModelKind)}>
                      <SelectTrigger><SelectValue /></SelectTrigger>
                      <SelectContent>
                        <SelectItem value="image">image</SelectItem>
                        <SelectItem value="video">video</SelectItem>
                        <SelectItem value="audio">audio</SelectItem>
                        <SelectItem value="upscale">upscale</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                  <div className="space-y-2">
                    <Label>Task type</Label>
                    <Input value={modelDraft.task_type} onChange={(event) => updateDraft("task_type", event.target.value)} />
                  </div>
                  <div className="space-y-2">
                    <Label>Builder key</Label>
                    <Input value={modelDraft.builder_key} onChange={(event) => updateDraft("builder_key", event.target.value)} />
                  </div>
                  <div className="space-y-2">
                    <Label>Disabled reason</Label>
                    <Input value={modelDraft.disabled_reason} onChange={(event) => updateDraft("disabled_reason", event.target.value)} />
                  </div>
                </div>
                <div className="flex flex-wrap gap-6 rounded-md border bg-muted/20 p-4">
                  <div className="flex items-center gap-3">
                    <Switch checked={modelDraft.is_enabled} onCheckedChange={(checked) => updateDraft("is_enabled", checked)} />
                    <Label>Admin enabled</Label>
                  </div>
                  <div className="flex items-center gap-3">
                    <Switch checked={modelDraft.live_available} onCheckedChange={(checked) => updateDraft("live_available", checked)} />
                    <Label>Live available</Label>
                  </div>
                </div>
              </TabsContent>

              <TabsContent value="capabilities" className="space-y-5 pt-4">
                <div className="grid gap-5 md:grid-cols-2">
                  <ChipListEditor label="Supported modes" value={modelDraft.supported_modes} onChange={(value) => updateDraft("supported_modes", value)} />
                  <ChipListEditor label="Media roles" value={modelDraft.media_roles} onChange={(value) => updateDraft("media_roles", value)} />
                  <ChipListEditor label="Feature flags" value={modelDraft.feature_flags} onChange={(value) => updateDraft("feature_flags", value)} />
                  <div className="space-y-2">
                    <Label>Cost feature</Label>
                    <Input value={modelDraft.cost_feature} onChange={(event) => updateDraft("cost_feature", event.target.value)} />
                  </div>
                  <div className="space-y-2">
                    <Label>Source version</Label>
                    <Input value={modelDraft.source_version} onChange={(event) => updateDraft("source_version", event.target.value)} />
                  </div>
                  <div className="space-y-2">
                    <Label>Last synced</Label>
                    <Input value={modelDraft.last_synced_at || "Not synced"} readOnly />
                  </div>
                </div>
              </TabsContent>

              <TabsContent value="defaults" className="grid gap-5 pt-4 lg:grid-cols-2">
                <JsonEditor
                  label="Default options"
                  value={modelDraft.default_options}
                  fallback="{}"
                  onChange={(value) => updateDraft("default_options", value)}
                  onFormat={() => formatDraftJson("default_options", "{}")}
                />
                <JsonEditor
                  label="Request mapping"
                  value={modelDraft.request_mapping}
                  fallback="{}"
                  onChange={(value) => updateDraft("request_mapping", value)}
                  onFormat={() => formatDraftJson("request_mapping", "{}")}
                />
              </TabsContent>

              <TabsContent value="schema" className="grid gap-5 pt-4 lg:grid-cols-2">
                <JsonEditor
                  label="Capability schema"
                  value={modelDraft.capability_schema}
                  fallback="{}"
                  onChange={(value) => updateDraft("capability_schema", value)}
                  onFormat={() => formatDraftJson("capability_schema", "{}")}
                />
                <JsonEditor
                  label="Limits"
                  value={modelDraft.limits}
                  fallback="{}"
                  onChange={(value) => updateDraft("limits", value)}
                  onFormat={() => formatDraftJson("limits", "{}")}
                />
              </TabsContent>
            </ScrollArea>
          </Tabs>
          <DialogFooter className="border-t p-4">
            <Button variant="outline" onClick={resetModelDraft}>
              <RotateCcw className="mr-2 h-4 w-4" />
              Reset
            </Button>
            <Button variant="outline" onClick={() => setModelOpen(false)}>Cancel</Button>
            <Button onClick={saveModelDialog} disabled={busy}>
              <Save className="mr-2 h-4 w-4" />
              Save model
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
