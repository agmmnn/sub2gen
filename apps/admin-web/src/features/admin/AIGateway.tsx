import { useCallback, useEffect, useMemo, useState } from "react"
import {
  Activity,
  Boxes,
  CircleAlert,
  ExternalLink,
  KeyRound,
  Network,
  Plus,
  Power,
  PowerOff,
  RefreshCw,
  Route,
  ShieldCheck,
  TestTube2,
  Trash2,
  Upload,
  Users,
} from "lucide-react"
import { toast } from "sonner"

import { useAuth } from "../../contexts/AuthContext"
import { adminFetch, adminJson } from "../../lib/adminApi"
import { Badge } from "../../components/ui/badge"
import { Button } from "../../components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../../components/ui/card"
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

type GatewayStatus = {
  configured: boolean
  reachable: boolean
  version: string
  public_url: string
  routing_strategy: string
  platform_count: number
  account_count: number
  healthy_count: number
  unavailable_count: number
  disabled_count: number
  message: string
}

type GatewayModel = {
  id: string
  raw_id: string
  platform: string
  display_name: string
  owned_by: string
  capabilities: string[]
  excluded: boolean
}

type GatewayAccount = {
  id: string
  auth_index: string
  name: string
  platform: string
  label: string
  email: string
  account_type: string
  status: string
  status_message: string
  disabled: boolean
  unavailable: boolean
  runtime_only: boolean
  source: string
  last_refresh: string | null
  success_count: number
  failure_count: number
  models: GatewayModel[]
}

type GatewayPlatform = {
  id: string
  label: string
  namespace: string
  oauth: boolean
  import_types: string[]
  account_count: number
  healthy_count: number
  models: GatewayModel[]
  error: string
}

type GatewayRouting = {
  strategy: string
  supported_strategies: string[]
  session_affinity: boolean
  retry_count: number
  max_retry_credentials: number
  max_retry_interval_seconds: number
}

type GatewayAlias = {
  name: string
  alias: string
  fork: boolean
  display_name: string
  force_mapping: boolean
}

type GatewayLogs = {
  lines: string[]
  cursor: string
  cursor_reset: boolean
  line_count: number
}

type OAuthSession = {
  provider: string
  status: string
  state: string
  url: string
  flow: string
  user_code: string
  expires_in: number | null
  error: string
}

type CredentialImportResponse = {
  success: boolean
  platform: string
  source_name: string
  total: number
  imported: number
  failed: number
  items: Array<{
    name: string
    email: string
    status: string
    error: string
  }>
}

const API = "/api/admin/cliproxy"
const EMPTY_STATUS: GatewayStatus = {
  configured: false,
  reachable: false,
  version: "v7.2.120",
  public_url: "",
  routing_strategy: "unknown",
  platform_count: 0,
  account_count: 0,
  healthy_count: 0,
  unavailable_count: 0,
  disabled_count: 0,
  message: "Checking gateway configuration…",
}

function errorMessage(data: unknown, fallback: string) {
  if (data && typeof data === "object" && "detail" in data) {
    const detail = (data as { detail?: unknown }).detail
    if (typeof detail === "string") return detail
  }
  return fallback
}

function formatTime(value: string | null) {
  if (!value) return "Never"
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}

function healthBadge(account: GatewayAccount) {
  if (account.disabled) return <Badge variant="outline">Disabled</Badge>
  if (account.unavailable) return <Badge variant="destructive">Unavailable</Badge>
  if (account.status === "ready" || account.status === "ok") {
    return <Badge className="border-emerald-500/20 bg-emerald-500/10 text-emerald-600 hover:bg-emerald-500/10">Healthy</Badge>
  }
  return <Badge variant="secondary">{account.status || "Unknown"}</Badge>
}

export function AIGateway({ active }: { active: boolean }) {
  const { token } = useAuth()
  const [section, setSection] = useState("overview")
  const [status, setStatus] = useState<GatewayStatus>(EMPTY_STATUS)
  const [accounts, setAccounts] = useState<GatewayAccount[]>([])
  const [platforms, setPlatforms] = useState<GatewayPlatform[]>([])
  const [routing, setRouting] = useState<GatewayRouting | null>(null)
  const [aliases, setAliases] = useState<Record<string, GatewayAlias[]>>({})
  const [exclusions, setExclusions] = useState<Record<string, string[]>>({})
  const [logs, setLogs] = useState<GatewayLogs>({ lines: [], cursor: "", cursor_reset: false, line_count: 0 })
  const [loading, setLoading] = useState(false)
  const [actionKey, setActionKey] = useState("")
  const [error, setError] = useState("")
  const [accountFilter, setAccountFilter] = useState("all")
  const [search, setSearch] = useState("")
  const [importOpen, setImportOpen] = useState(false)
  const [importMode, setImportMode] = useState("oauth")
  const [importPlatform, setImportPlatform] = useState("codex")
  const [credentialFile, setCredentialFile] = useState<File | null>(null)
  const [credentialImportResult, setCredentialImportResult] = useState<CredentialImportResponse | null>(null)
  const [vertexLocation, setVertexLocation] = useState("us-central1")
  const [apiKey, setApiKey] = useState("")
  const [providerName, setProviderName] = useState("")
  const [providerBaseUrl, setProviderBaseUrl] = useState("")
  const [providerModels, setProviderModels] = useState("")
  const [oauth, setOauth] = useState<OAuthSession | null>(null)
  const [aliasPlatform, setAliasPlatform] = useState("codex")
  const [aliasRaw, setAliasRaw] = useState("")
  const [aliasValue, setAliasValue] = useState("")

  const loadLogs = useCallback(async (incremental = false) => {
    if (!token) return
    const cursor = incremental ? logs.cursor : ""
    const path = `${API}/logs?limit=250${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`
    const response = await adminJson<GatewayLogs>(path, token)
    if (!response.ok || !response.data) return
    setLogs((previous) => ({
      ...response.data!,
      lines: incremental && !response.data!.cursor_reset
        ? [...previous.lines, ...response.data!.lines].slice(-1000)
        : response.data!.lines,
    }))
  }, [logs.cursor, token])

  const load = useCallback(async (quiet = false) => {
    if (!token || !active) return
    if (!quiet) setLoading(true)
    setError("")
    try {
      const statusResponse = await adminJson<GatewayStatus>(`${API}/status`, token)
      if (!statusResponse.ok || !statusResponse.data) {
        setError(errorMessage(statusResponse.data, `Unable to read gateway status (HTTP ${statusResponse.status})`))
        return
      }
      setStatus(statusResponse.data)
      if (!statusResponse.data.configured || !statusResponse.data.reachable) return
      const [accountResponse, platformResponse, routingResponse, aliasResponse, exclusionResponse] = await Promise.all([
        adminJson<GatewayAccount[]>(`${API}/accounts?include_models=true`, token),
        adminJson<GatewayPlatform[]>(`${API}/platforms`, token),
        adminJson<GatewayRouting>(`${API}/routing`, token),
        adminJson<Record<string, GatewayAlias[]>>(`${API}/aliases`, token),
        adminJson<Record<string, string[]>>(`${API}/exclusions`, token),
      ])
      if (accountResponse.ok && accountResponse.data) setAccounts(accountResponse.data)
      if (platformResponse.ok && platformResponse.data) setPlatforms(platformResponse.data)
      if (routingResponse.ok && routingResponse.data) setRouting(routingResponse.data)
      if (aliasResponse.ok && aliasResponse.data) setAliases(aliasResponse.data)
      if (exclusionResponse.ok && exclusionResponse.data) setExclusions(exclusionResponse.data)
    } finally {
      if (!quiet) setLoading(false)
    }
  }, [active, token])

  useEffect(() => {
    if (!active) return
    const initial = window.setTimeout(() => void load(), 0)
    const timer = window.setInterval(() => void load(true), 20_000)
    return () => {
      window.clearTimeout(initial)
      window.clearInterval(timer)
    }
  }, [active, load])

  useEffect(() => {
    if (!active || section !== "logs" || !status.reachable) return
    const initial = window.setTimeout(() => void loadLogs(false), 0)
    const timer = window.setInterval(() => void loadLogs(true), 5_000)
    return () => {
      window.clearTimeout(initial)
      window.clearInterval(timer)
    }
  }, [active, loadLogs, section, status.reachable])

  useEffect(() => {
    if (!oauth?.state || oauth.status !== "wait" || !token) return
    const timer = window.setInterval(async () => {
      const response = await adminJson<OAuthSession>(
        `${API}/oauth/status?state=${encodeURIComponent(oauth.state)}&provider=${encodeURIComponent(oauth.provider)}`,
        token,
      )
      if (!response.ok || !response.data) return
      setOauth((previous) => previous ? { ...previous, ...response.data } : response.data)
      if (response.data.status === "ok") {
        toast.success(`${oauth.provider} account connected`)
        window.clearInterval(timer)
        await load(true)
      } else if (response.data.status === "error") {
        toast.error(response.data.error || "OAuth login failed")
        window.clearInterval(timer)
      }
    }, 2_000)
    return () => window.clearInterval(timer)
  }, [load, oauth?.provider, oauth?.state, oauth?.status, token])

  const filteredAccounts = useMemo(() => {
    const needle = search.trim().toLowerCase()
    return accounts.filter((account) => {
      if (accountFilter !== "all" && account.platform !== accountFilter) return false
      if (!needle) return true
      return [account.email, account.label, account.name, account.platform]
        .some((value) => value.toLowerCase().includes(needle))
    })
  }, [accountFilter, accounts, search])

  const mutate = async (key: string, path: string, init: RequestInit, success: string) => {
    if (!token) return false
    setActionKey(key)
    try {
      const response = await adminJson<Record<string, unknown>>(path, token, init)
      if (!response.ok) {
        toast.error(errorMessage(response.data, `Operation failed (HTTP ${response.status})`))
        return false
      }
      toast.success(success)
      await load(true)
      return true
    } finally {
      setActionKey("")
    }
  }

  const toggleAccount = async (account: GatewayAccount, enabled: boolean) => {
    await mutate(
      `toggle:${account.name}`,
      `${API}/accounts/${encodeURIComponent(account.name)}/status`,
      { method: "PATCH", body: JSON.stringify({ enabled }) },
      `${account.email || account.name} ${enabled ? "enabled" : "disabled"}`,
    )
  }

  const deleteAccount = async (account: GatewayAccount) => {
    if (!window.confirm(`Delete ${account.email || account.name} from CLIProxy? This cannot be undone.`)) return
    await mutate(
      `delete:${account.name}`,
      `${API}/accounts/${encodeURIComponent(account.name)}`,
      { method: "DELETE" },
      "Account deleted",
    )
  }

  const resetQuota = async (account: GatewayAccount) => {
    if (!account.auth_index) return
    await mutate(
      `quota:${account.auth_index}`,
      `${API}/routing/reset-quota`,
      { method: "POST", body: JSON.stringify({ auth_index: account.auth_index }) },
      "Local cooldown state cleared",
    )
  }

  const startOAuth = async () => {
    if (!token) return
    setActionKey("oauth")
    try {
      const response = await adminJson<OAuthSession>(`${API}/oauth/${importPlatform}/start`, token, { method: "POST" })
      if (!response.ok || !response.data) {
        toast.error(errorMessage(response.data, "Unable to start OAuth login"))
        return
      }
      setOauth(response.data)
      if (response.data.url) window.open(response.data.url, "_blank", "noopener,noreferrer")
    } finally {
      setActionKey("")
    }
  }

  const cancelOAuth = async () => {
    if (!token || !oauth?.state) return
    await adminFetch(`${API}/oauth/session?state=${encodeURIComponent(oauth.state)}`, token, { method: "DELETE" })
    setOauth(null)
  }

  const importCredential = async () => {
    if (!token || !credentialFile) {
      toast.error("Choose a credential JSON file")
      return
    }
    setActionKey("credential")
    const form = new FormData()
    form.set("platform", importPlatform)
    form.set("file", credentialFile)
    form.set("location", vertexLocation)
    try {
      const response = await adminJson<CredentialImportResponse>(`${API}/accounts/import`, token, { method: "POST", body: form })
      if (!response.ok || !response.data) {
        toast.error(errorMessage(response.data, "Credential import failed"))
        return
      }
      setCredentialImportResult(response.data)
      if (response.data.failed === 0) {
        toast.success(`Imported ${response.data.imported} gateway account${response.data.imported === 1 ? "" : "s"}`)
        setCredentialFile(null)
        setImportOpen(false)
      } else if (response.data.imported > 0) {
        toast.warning(`Imported ${response.data.imported}; ${response.data.failed} failed`)
      } else {
        toast.error(`All ${response.data.failed} account imports failed`)
      }
      await load(true)
    } finally {
      setActionKey("")
    }
  }

  const importApiKey = async () => {
    const models = providerModels.split(",").map((item) => item.trim()).filter(Boolean)
    const ok = await mutate(
      "api-key",
      `${API}/accounts/api-key`,
      {
        method: "POST",
        body: JSON.stringify({
          provider: importPlatform,
          api_key: apiKey,
          name: providerName,
          base_url: providerBaseUrl,
          models,
        }),
      },
      "API key imported",
    )
    if (ok) {
      setApiKey("")
      setImportOpen(false)
    }
  }

  const changeRouting = async (strategy: string) => {
    if (!token) return
    setActionKey("routing")
    try {
      const response = await adminJson<GatewayRouting>(`${API}/routing`, token, {
        method: "PATCH",
        body: JSON.stringify({ strategy }),
      })
      if (!response.ok || !response.data) {
        toast.error(errorMessage(response.data, "Unable to update routing"))
        return
      }
      setRouting(response.data)
      setStatus((previous) => ({ ...previous, routing_strategy: response.data!.strategy }))
      toast.success("Routing updated")
    } finally {
      setActionKey("")
    }
  }

  const saveRetryPolicy = async () => {
    if (!token || !routing) return
    setActionKey("retry-policy")
    try {
      const response = await adminJson<GatewayRouting>(`${API}/routing`, token, {
        method: "PATCH",
        body: JSON.stringify({
          retry_count: routing.retry_count,
          max_retry_interval_seconds: routing.max_retry_interval_seconds,
        }),
      })
      if (!response.ok || !response.data) {
        toast.error(errorMessage(response.data, "Unable to update retry policy"))
        return
      }
      setRouting(response.data)
      toast.success("Retry policy updated")
    } finally {
      setActionKey("")
    }
  }

  const testModel = async (model: string) => {
    await mutate(
      `test:${model}`,
      `${API}/models/test`,
      { method: "POST", body: JSON.stringify({ model }) },
      `${model} responded successfully`,
    )
  }

  const setModelExcluded = async (platform: string, rawModel: string, excluded: boolean) => {
    const current = exclusions[platform] || []
    const next = excluded
      ? Array.from(new Set([...current, rawModel]))
      : current.filter((model) => model !== rawModel)
    const ok = await mutate(
      `exclude:${platform}:${rawModel}`,
      `${API}/exclusions/${platform}`,
      { method: "PATCH", body: JSON.stringify({ models: next }) },
      excluded ? "Model excluded" : "Model restored",
    )
    if (ok) setExclusions((previous) => ({ ...previous, [platform]: next }))
  }

  const setAllModelsEnabled = async (platform: GatewayPlatform, enabled: boolean) => {
    const current = exclusions[platform.id] || []
    const next = enabled
      ? []
      : Array.from(new Set([...current, ...platform.models.map((model) => model.raw_id).filter(Boolean)]))
    const ok = await mutate(
      `exclude:${platform.id}:all`,
      `${API}/exclusions/${platform.id}`,
      { method: "PATCH", body: JSON.stringify({ models: next }) },
      `${platform.label} models ${enabled ? "enabled" : "disabled"}`,
    )
    if (ok) setExclusions((previous) => ({ ...previous, [platform.id]: next }))
  }

  const saveAliases = async (platform: string, next: GatewayAlias[]) => {
    const ok = await mutate(
      `alias:${platform}`,
      `${API}/aliases/${platform}`,
      { method: "PATCH", body: JSON.stringify({ aliases: next }) },
      "Model aliases updated",
    )
    if (ok) setAliases((previous) => ({ ...previous, [platform]: next }))
  }

  const addAlias = async () => {
    if (!aliasRaw.trim() || !aliasValue.trim()) {
      toast.error("Enter both the upstream model and namespaced alias")
      return
    }
    const next = [
      ...(aliases[aliasPlatform] || []).filter((item) => item.alias !== aliasValue.trim()),
      {
        name: aliasRaw.trim(),
        alias: aliasValue.trim(),
        fork: false,
        display_name: aliasValue.trim(),
        force_mapping: true,
      },
    ]
    await saveAliases(aliasPlatform, next)
    setAliasRaw("")
    setAliasValue("")
  }

  if (!active) return null

  return (
    <div className="space-y-6">
      <div className="relative overflow-hidden rounded-xl border bg-card px-5 py-5 shadow-xs sm:px-7">
        <div className="absolute inset-y-0 right-0 w-1/2 bg-[radial-gradient(circle_at_center,hsl(var(--primary)/0.12),transparent_68%)]" />
        <div className="relative flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex items-start gap-4">
            <div className="rounded-xl border bg-background p-3 shadow-xs"><Network className="h-6 w-6 text-primary" /></div>
            <div>
              <div className="flex flex-wrap items-center gap-2">
                <h1 className="text-2xl font-semibold tracking-tight">AI Gateway</h1>
                <Badge variant={status.reachable ? "default" : "secondary"} className={status.reachable ? "bg-emerald-600 hover:bg-emerald-600" : ""}>
                  {status.reachable ? "Online" : status.configured ? "Unavailable" : "Not configured"}
                </Badge>
                <Badge variant="outline">{status.version}</Badge>
              </div>
              <p className="mt-1 max-w-2xl text-sm text-muted-foreground">
                Multi-account text and vision-input routing for prompt cloning and metadata. Gateway secrets stay server-side.
              </p>
            </div>
          </div>
          <div className="flex gap-2">
            {status.public_url ? (
              <Button variant="outline" size="sm" render={<a href={`${status.public_url}/management.html`} target="_blank" rel="noreferrer" />}>
                Break-glass UI <ExternalLink className="ml-2 h-3.5 w-3.5" />
              </Button>
            ) : null}
            <Button variant="outline" size="sm" onClick={() => void load()} disabled={loading}>
              <RefreshCw className={`mr-2 h-4 w-4 ${loading ? "animate-spin" : ""}`} /> Refresh
            </Button>
          </div>
        </div>
      </div>

      {error ? (
        <div className="flex items-start gap-3 rounded-lg border border-destructive/30 bg-destructive/5 p-4 text-sm">
          <CircleAlert className="mt-0.5 h-4 w-4 shrink-0 text-destructive" /><span>{error}</span>
        </div>
      ) : null}

      {!status.configured ? (
        <Card>
          <CardHeader><CardTitle>Connect the Railway service</CardTitle><CardDescription>{status.message}</CardDescription></CardHeader>
          <CardContent className="space-y-3 text-sm">
            <p>Set the following variables on sub2gen, then redeploy:</p>
            <pre className="overflow-x-auto rounded-lg border bg-muted/50 p-4 font-mono text-xs">{`SUB2GEN_CLIPROXY_BASE_URL=http://cliproxy.railway.internal:8317
SUB2GEN_CLIPROXY_PUBLIC_URL=https://<public-domain>
SUB2GEN_CLIPROXY_API_KEY=<client-key>
SUB2GEN_CLIPROXY_MANAGEMENT_KEY=<management-key>`}</pre>
          </CardContent>
        </Card>
      ) : (
        <Tabs value={section} onValueChange={setSection}>
          <TabsList className="h-auto w-full justify-start overflow-x-auto p-1">
            <TabsTrigger value="overview">Overview</TabsTrigger>
            <TabsTrigger value="accounts">Accounts</TabsTrigger>
            <TabsTrigger value="models">Platforms & Models</TabsTrigger>
            <TabsTrigger value="routing">Routing</TabsTrigger>
            <TabsTrigger value="logs">Logs</TabsTrigger>
          </TabsList>

          <TabsContent value="overview" className="mt-5 space-y-5">
            <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
              {[
                { label: "Accounts", value: status.account_count, icon: Users, note: `${status.healthy_count} healthy` },
                { label: "Platforms", value: status.platform_count, icon: Boxes, note: "Extensible providers" },
                { label: "Unavailable", value: status.unavailable_count, icon: CircleAlert, note: `${status.disabled_count} disabled` },
                { label: "Routing", value: status.routing_strategy, icon: Route, note: "Session affinity off" },
              ].map((metric) => (
                <Card key={metric.label}>
                  <CardContent className="flex items-start justify-between p-5">
                    <div><p className="text-sm text-muted-foreground">{metric.label}</p><p className="mt-1 text-2xl font-semibold capitalize">{metric.value}</p><p className="mt-1 text-xs text-muted-foreground">{metric.note}</p></div>
                    <metric.icon className="h-5 w-5 text-primary" />
                  </CardContent>
                </Card>
              ))}
            </div>
            <div className="grid gap-4 lg:grid-cols-3">
              {platforms.filter((platform) => platform.account_count > 0).map((platform) => (
                <Card key={platform.id}>
                  <CardHeader className="pb-3">
                    <div className="flex items-center justify-between"><CardTitle className="text-base">{platform.label}</CardTitle><Badge variant="outline">{platform.namespace}/</Badge></div>
                    <CardDescription>{platform.healthy_count} of {platform.account_count} accounts ready</CardDescription>
                  </CardHeader>
                  <CardContent><div className="h-1.5 overflow-hidden rounded-full bg-muted"><div className="h-full bg-emerald-500" style={{ width: `${platform.account_count ? (platform.healthy_count / platform.account_count) * 100 : 0}%` }} /></div></CardContent>
                </Card>
              ))}
            </div>
          </TabsContent>

          <TabsContent value="accounts" className="mt-5 space-y-4">
            <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
              <div className="flex flex-1 flex-col gap-2 sm:flex-row">
                <Input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search email, label, or file…" className="sm:max-w-xs" />
                <Select value={accountFilter} onValueChange={setAccountFilter}>
                  <SelectTrigger className="sm:w-52"><SelectValue placeholder="All platforms" /></SelectTrigger>
                  <SelectContent><SelectItem value="all">All platforms</SelectItem>{Array.from(new Set(accounts.map((account) => account.platform))).map((platform) => <SelectItem key={platform} value={platform}>{platform}</SelectItem>)}</SelectContent>
                </Select>
              </div>
              <Button onClick={() => { setCredentialImportResult(null); setImportOpen(true) }}><Plus className="mr-2 h-4 w-4" /> Add account</Button>
            </div>
            <Card>
              <CardContent className="p-0">
                <div className="overflow-x-auto">
                  <Table>
                    <TableHeader><TableRow><TableHead>Account</TableHead><TableHead>Platform</TableHead><TableHead>Health</TableHead><TableHead>Models</TableHead><TableHead>Requests</TableHead><TableHead>Last refresh</TableHead><TableHead className="text-right">Control</TableHead></TableRow></TableHeader>
                    <TableBody>
                      {filteredAccounts.map((account) => (
                        <TableRow key={account.id}>
                          <TableCell><div className="font-medium">{account.label || account.email || account.name}</div><div className="max-w-[240px] truncate text-xs text-muted-foreground" title={account.name}>{account.email && account.label ? account.email : account.name}</div></TableCell>
                          <TableCell><Badge variant="outline" className="font-mono">{account.platform}</Badge></TableCell>
                          <TableCell>{healthBadge(account)}{account.status_message && account.status_message !== "ok" ? <div className="mt-1 max-w-40 truncate text-xs text-muted-foreground" title={account.status_message}>{account.status_message}</div> : null}</TableCell>
                          <TableCell><div className="max-w-[220px] text-xs text-muted-foreground">{account.models.length ? account.models.slice(0, 3).map((model) => model.id).join(", ") : "Not reported"}{account.models.length > 3 ? ` +${account.models.length - 3}` : ""}</div></TableCell>
                          <TableCell><span className="text-emerald-600">{account.success_count}</span><span className="mx-1 text-muted-foreground">/</span><span className="text-destructive">{account.failure_count}</span></TableCell>
                          <TableCell className="whitespace-nowrap text-xs text-muted-foreground">{formatTime(account.last_refresh)}</TableCell>
                          <TableCell><div className="flex items-center justify-end gap-2"><Switch checked={!account.disabled} disabled={actionKey === `toggle:${account.name}`} onCheckedChange={(enabled) => void toggleAccount(account, enabled)} aria-label={`Enable ${account.email || account.name}`} /><Button size="sm" variant="ghost" onClick={() => void resetQuota(account)} disabled={!account.auth_index || actionKey === `quota:${account.auth_index}`} title="Reset local cooldown"><RefreshCw className="h-4 w-4" /></Button><Button size="sm" variant="ghost" className="text-destructive hover:text-destructive" onClick={() => void deleteAccount(account)} disabled={account.runtime_only || actionKey === `delete:${account.name}`} title={account.runtime_only ? "Runtime-only accounts cannot be deleted here" : "Delete account"}><Trash2 className="h-4 w-4" /></Button></div></TableCell>
                        </TableRow>
                      ))}
                      {!filteredAccounts.length ? <TableRow><TableCell colSpan={7} className="h-32 text-center text-muted-foreground">No accounts match this view.</TableCell></TableRow> : null}
                    </TableBody>
                  </Table>
                </div>
              </CardContent>
            </Card>
          </TabsContent>

          <TabsContent value="models" className="mt-5 space-y-5">
            <div className="grid gap-4 lg:grid-cols-2">
              {platforms.map((platform) => {
                const platformExclusions = exclusions[platform.id] || []
                const excludedModels = new Set(platformExclusions)
                const enabledCount = platform.models.filter((model) => !excludedModels.has(model.raw_id)).length
                const allEnabled = platformExclusions.length === 0
                const allDisabled = platform.models.length > 0 && enabledCount === 0
                const modelUpdatePending = actionKey.startsWith(`exclude:${platform.id}:`)

                return (
                  <Card key={platform.id}>
                    <CardHeader className="pb-3">
                      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                        <div>
                          <CardTitle className="text-base">{platform.label}</CardTitle>
                          <CardDescription>{platform.account_count} accounts · namespace <code>{platform.namespace}/</code></CardDescription>
                        </div>
                        <div className="flex flex-wrap items-center gap-2 sm:justify-end">
                          <Badge variant={platform.error ? "secondary" : "outline"}>
                            {enabledCount} of {platform.models.length} on
                          </Badge>
                          <div className="inline-flex" role="group" aria-label={`${platform.label} bulk model controls`}>
                            <Button
                              size="sm"
                              variant="outline"
                              className="rounded-r-none"
                              onClick={() => void setAllModelsEnabled(platform, true)}
                              disabled={!platform.models.length || allEnabled || modelUpdatePending}
                            >
                              <Power className="h-3.5 w-3.5" />
                              All on
                            </Button>
                            <Button
                              size="sm"
                              variant="outline"
                              className="-ml-px rounded-l-none"
                              onClick={() => void setAllModelsEnabled(platform, false)}
                              disabled={!platform.models.length || allDisabled || modelUpdatePending}
                            >
                              <PowerOff className="h-3.5 w-3.5" />
                              All off
                            </Button>
                          </div>
                        </div>
                      </div>
                    </CardHeader>
                    <CardContent>
                      {platform.error ? <p className="mb-3 text-xs text-muted-foreground">{platform.error}</p> : null}
                      <ScrollArea className="h-56 pr-3">
                        <div className="space-y-2">
                          {platform.models.map((model) => {
                            const excluded = excludedModels.has(model.raw_id)
                            return <div key={model.id} className="flex items-center justify-between gap-3 rounded-lg border px-3 py-2"><div className="min-w-0"><div className="truncate font-mono text-xs" title={model.id}>{model.id}</div><div className="mt-1 flex gap-1">{model.capabilities.map((capability) => <Badge key={capability} variant="secondary" className="px-1.5 py-0 text-[10px]">{capability}</Badge>)}</div></div><div className="flex items-center gap-1"><Button size="sm" variant="ghost" onClick={() => void testModel(model.id)} disabled={actionKey === `test:${model.id}`} title="Connectivity test"><TestTube2 className="h-4 w-4" /></Button><Switch checked={!excluded} onCheckedChange={(enabled) => void setModelExcluded(platform.id, model.raw_id, !enabled)} disabled={modelUpdatePending} aria-label={`Allow ${model.id}`} /></div></div>
                          })}
                          {!platform.models.length ? <p className="py-12 text-center text-sm text-muted-foreground">No model definitions discovered.</p> : null}
                        </div>
                      </ScrollArea>
                    </CardContent>
                  </Card>
                )
              })}
            </div>
            <Card>
              <CardHeader><CardTitle className="text-base">Namespaced aliases</CardTitle><CardDescription>Bind an unambiguous sub2gen model ID to a model exposed by one CLIProxy channel.</CardDescription></CardHeader>
              <CardContent className="space-y-4">
                <div className="grid gap-3 md:grid-cols-[180px_1fr_1fr_auto]">
                  <Select value={aliasPlatform} onValueChange={setAliasPlatform}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent>{platforms.map((platform) => <SelectItem key={platform.id} value={platform.id}>{platform.id}</SelectItem>)}</SelectContent></Select>
                  <Input value={aliasRaw} onChange={(event) => setAliasRaw(event.target.value)} placeholder="Upstream model, e.g. gpt-5.6" />
                  <Input value={aliasValue} onChange={(event) => setAliasValue(event.target.value)} placeholder="Alias, e.g. codex/gpt-5.6" />
                  <Button onClick={() => void addAlias()} disabled={actionKey === `alias:${aliasPlatform}`}><Plus className="mr-2 h-4 w-4" /> Add</Button>
                </div>
                <div className="rounded-lg border">
                  {(aliases[aliasPlatform] || []).map((alias) => <div key={`${alias.name}:${alias.alias}`} className="flex items-center justify-between gap-3 border-b px-3 py-2 last:border-0"><div className="min-w-0 text-sm"><span className="font-mono">{alias.name}</span><span className="mx-2 text-muted-foreground">→</span><span className="font-mono text-primary">{alias.alias}</span></div><Button size="sm" variant="ghost" className="text-destructive hover:text-destructive" onClick={() => void saveAliases(aliasPlatform, (aliases[aliasPlatform] || []).filter((item) => item !== alias))}><Trash2 className="h-4 w-4" /></Button></div>)}
                  {!(aliases[aliasPlatform] || []).length ? <p className="p-4 text-center text-sm text-muted-foreground">No custom aliases for this channel.</p> : null}
                </div>
              </CardContent>
            </Card>
          </TabsContent>

          <TabsContent value="routing" className="mt-5 space-y-5">
            <div className="grid gap-4 lg:grid-cols-2">
              <Card><CardHeader><CardTitle className="text-base">Credential selection</CardTitle><CardDescription>Changes apply to new calls immediately.</CardDescription></CardHeader><CardContent className="space-y-4"><div className="space-y-2"><Label>Strategy</Label><Select value={routing?.strategy || "round-robin"} onValueChange={(value) => void changeRouting(value)} disabled={actionKey === "routing"}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectItem value="round-robin">Round robin</SelectItem><SelectItem value="fill-first">Fill first</SelectItem></SelectContent></Select></div><div className="rounded-lg border bg-muted/30 p-3 text-sm"><div className="flex items-center gap-2 font-medium"><ShieldCheck className="h-4 w-4 text-emerald-600" /> Session affinity is off</div><p className="mt-1 text-xs text-muted-foreground">Each cloning or metadata call may use the next eligible account. Disabled, unavailable, cooling, or incompatible accounts are skipped.</p></div></CardContent></Card>
              <Card><CardHeader><CardTitle className="text-base">Retry & cooldown policy</CardTitle><CardDescription>Bounded gateway failover. Changes hot-reload in CLIProxy.</CardDescription></CardHeader><CardContent className="space-y-4"><div className="grid grid-cols-2 gap-3"><div className="space-y-2"><Label htmlFor="gateway-retries">Request retries</Label><Input id="gateway-retries" type="number" min={0} max={10} value={routing?.retry_count ?? 2} onChange={(event) => setRouting((previous) => previous ? { ...previous, retry_count: Math.max(0, Math.min(10, Number(event.target.value) || 0)) } : previous)} /></div><div className="space-y-2"><Label htmlFor="gateway-retry-interval">Max cooldown wait (s)</Label><Input id="gateway-retry-interval" type="number" min={0} max={300} value={routing?.max_retry_interval_seconds ?? 15} onChange={(event) => setRouting((previous) => previous ? { ...previous, max_retry_interval_seconds: Math.max(0, Math.min(300, Number(event.target.value) || 0)) } : previous)} /></div></div><dl className="grid grid-cols-2 gap-3 text-sm"><div className="rounded-lg border p-3"><dt className="text-muted-foreground">Credentials per call</dt><dd className="mt-1 text-xl font-semibold">{routing?.max_retry_credentials ?? 3}</dd></div><div className="rounded-lg border p-3"><dt className="text-muted-foreground">Provider fallback</dt><dd className="mt-1 text-xl font-semibold">Enabled</dd></div></dl><Button variant="outline" onClick={() => void saveRetryPolicy()} disabled={!routing || actionKey === "retry-policy"}>{actionKey === "retry-policy" ? "Saving…" : "Save retry policy"}</Button></CardContent></Card>
            </div>
            <Card><CardHeader><CardTitle className="text-base">Local cooldown state</CardTitle><CardDescription>Reset resumes an account in routing; it does not replenish upstream quota.</CardDescription></CardHeader><CardContent className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">{accounts.filter((account) => account.auth_index).map((account) => <div key={account.auth_index} className="flex items-center justify-between rounded-lg border px-3 py-2"><div className="min-w-0"><div className="truncate text-sm font-medium">{account.label || account.email || account.name}</div><div className="truncate font-mono text-[10px] text-muted-foreground">{account.auth_index}</div></div><Button size="sm" variant="outline" onClick={() => void resetQuota(account)} disabled={actionKey === `quota:${account.auth_index}`}><RefreshCw className="mr-2 h-3.5 w-3.5" /> Reset</Button></div>)}</CardContent></Card>
          </TabsContent>

          <TabsContent value="logs" className="mt-5">
            <Card><CardHeader className="flex-row items-center justify-between space-y-0"><div><CardTitle className="text-base">Sanitized gateway logs</CardTitle><CardDescription>Incremental lines only; credentials, authorization headers, and data URLs are redacted.</CardDescription></div><Button size="sm" variant="outline" onClick={() => void loadLogs(false)}><RefreshCw className="mr-2 h-4 w-4" /> Reload</Button></CardHeader><CardContent><ScrollArea className="h-[520px] rounded-lg border bg-zinc-950"><pre className="min-h-full p-4 font-mono text-xs leading-5 text-zinc-300">{logs.lines.length ? logs.lines.join("\n") : "Waiting for gateway log lines…"}</pre></ScrollArea><div className="mt-2 flex items-center justify-between text-xs text-muted-foreground"><span>{logs.lines.length} visible lines</span><span>Cursor {logs.cursor ? "active" : "not established"}</span></div></CardContent></Card>
          </TabsContent>
        </Tabs>
      )}

      <Dialog open={importOpen} onOpenChange={(open) => { setImportOpen(open); if (!open) { setOauth(null); setCredentialImportResult(null) } }}>
        <DialogContent className="max-w-xl">
          <DialogHeader><DialogTitle>Add gateway account</DialogTitle><DialogDescription>OAuth tokens and imported credentials are stored only in CLIProxy's Railway volume.</DialogDescription></DialogHeader>
          <Tabs value={importMode} onValueChange={(mode) => {
            setImportMode(mode)
            const importType = mode === "api-key" ? "api_key" : "credential"
            const eligible = platforms.filter((platform) => mode === "oauth" ? platform.oauth : platform.import_types.includes(importType))
            if (!eligible.some((platform) => platform.id === importPlatform) && eligible[0]) {
              setImportPlatform(eligible[0].id)
            }
            setOauth(null)
          }}>
            <TabsList className="grid w-full grid-cols-3"><TabsTrigger value="oauth">OAuth</TabsTrigger><TabsTrigger value="credential">Credential file</TabsTrigger><TabsTrigger value="api-key">API key</TabsTrigger></TabsList>
            <div className="mt-4 space-y-4">
              <div className="space-y-2"><Label>Platform</Label><Select value={importPlatform} onValueChange={(value) => { setImportPlatform(value); setOauth(null) }}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent>{platforms.filter((platform) => importMode === "oauth" ? platform.oauth : platform.import_types.includes(importMode === "api-key" ? "api_key" : "credential")).map((platform) => <SelectItem key={platform.id} value={platform.id}>{platform.label}</SelectItem>)}</SelectContent></Select></div>
              <TabsContent value="oauth" className="m-0 space-y-4">
                {oauth ? <div className="rounded-lg border bg-muted/30 p-4"><div className="flex items-center gap-2 font-medium"><Activity className={`h-4 w-4 ${oauth.status === "wait" ? "animate-pulse text-amber-500" : oauth.status === "ok" ? "text-emerald-500" : "text-destructive"}`} /> OAuth {oauth.status}</div>{oauth.user_code ? <div className="mt-3"><p className="text-xs text-muted-foreground">Device code</p><code className="mt-1 block rounded border bg-background p-3 text-center text-lg tracking-widest">{oauth.user_code}</code></div> : null}{oauth.url ? <Button className="mt-3" variant="outline" size="sm" render={<a href={oauth.url} target="_blank" rel="noreferrer" />}>Open login page <ExternalLink className="ml-2 h-3.5 w-3.5" /></Button> : null}{oauth.error ? <p className="mt-2 text-sm text-destructive">{oauth.error}</p> : null}</div> : <div className="rounded-lg border border-dashed p-5 text-center"><KeyRound className="mx-auto h-6 w-6 text-muted-foreground" /><p className="mt-2 text-sm">Start a secure provider login in a new tab.</p></div>}
              </TabsContent>
              <TabsContent value="credential" className="m-0 space-y-4"><div className="space-y-2"><Label htmlFor="gateway-credential">Credential JSON or Cockpit bundle</Label><Input id="gateway-credential" type="file" accept="application/json,.json" onChange={(event) => { setCredentialFile(event.target.files?.[0] || null); setCredentialImportResult(null) }} /></div>{importPlatform === "vertex" ? <div className="space-y-2"><Label>Vertex location</Label><Input value={vertexLocation} onChange={(event) => setVertexLocation(event.target.value)} /></div> : null}<p className="text-xs text-muted-foreground">Maximum 2 MiB. For Codex or Antigravity, choose one Cockpit Tools export containing all accounts; sub2gen imports the whole array in one action.</p>{credentialImportResult ? <div className={`rounded-lg border p-3 text-sm ${credentialImportResult.failed ? "border-amber-500/30 bg-amber-500/5" : "border-emerald-500/30 bg-emerald-500/5"}`}><p className="font-medium">Imported {credentialImportResult.imported} of {credentialImportResult.total} accounts</p>{credentialImportResult.failed ? <div className="mt-2 max-h-32 space-y-1 overflow-y-auto text-xs text-muted-foreground">{credentialImportResult.items.filter((item) => item.status === "failed").map((item) => <p key={item.name}><span className="font-medium text-foreground">{item.email || item.name}:</span> {item.error || "Import failed"}</p>)}</div> : null}</div> : null}</TabsContent>
              <TabsContent value="api-key" className="m-0 space-y-4"><div className="space-y-2"><Label>API key</Label><Input type="password" value={apiKey} onChange={(event) => setApiKey(event.target.value)} autoComplete="off" /></div>{importPlatform === "openai-compatible" ? <div className="space-y-3"><div className="space-y-2"><Label>Provider name</Label><Input value={providerName} onChange={(event) => setProviderName(event.target.value)} placeholder="openrouter" /></div><div className="space-y-2"><Label>Base URL</Label><Input value={providerBaseUrl} onChange={(event) => setProviderBaseUrl(event.target.value)} placeholder="https://api.example.com/v1" /></div></div> : null}<div className="space-y-2"><Label>Models (comma separated)</Label><Textarea value={providerModels} onChange={(event) => setProviderModels(event.target.value)} placeholder="model-a, model-b" rows={3} /></div></TabsContent>
            </div>
          </Tabs>
          <DialogFooter>
            {importMode === "oauth" ? <>{oauth?.state && oauth.status === "wait" ? <Button variant="outline" onClick={() => void cancelOAuth()}>Cancel session</Button> : null}<Button onClick={() => void startOAuth()} disabled={actionKey === "oauth"}>{actionKey === "oauth" ? "Starting…" : "Start OAuth"}</Button></> : null}
            {importMode === "credential" ? <Button onClick={() => void importCredential()} disabled={actionKey === "credential"}><Upload className="mr-2 h-4 w-4" /> {actionKey === "credential" ? "Importing accounts…" : "Import account(s)"}</Button> : null}
            {importMode === "api-key" ? <Button onClick={() => void importApiKey()} disabled={!apiKey || actionKey === "api-key"}><KeyRound className="mr-2 h-4 w-4" /> {actionKey === "api-key" ? "Importing…" : "Import API key"}</Button> : null}
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
