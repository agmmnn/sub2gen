import { useCallback, useEffect, useMemo, useState } from "react"
import { Activity, Copy, KeyRound, Pause, Play, RefreshCw, Unplug } from "lucide-react"
import { toast } from "sonner"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { useAuth } from "@/contexts/AuthContext"
import { adminJson } from "@/lib/adminApi"

type Provider = {
  id: string
  account_count: number
  enabled_accounts: number
  model_count: number
  execution_locations: string[]
  billing_pools: string[]
}
type Binding = {
  id: string
  credential_type: string
  storage_kind: string
  worker_id: string | null
  enabled: boolean
  expires_at: string | null
  last_error: string | null
}
type Account = {
  id: string
  provider: string
  label: string
  enabled: boolean
  health: string
  credential_locations: string[]
  bindings: Binding[]
}
type Worker = {
  id: string
  kind: string
  label: string
  enabled: boolean
  connected: boolean
  status: string
  capabilities: string[]
  last_seen_at: string | null
  credential_expires_at: string | null
  metadata: Record<string, unknown>
}
type Model = {
  id: string
  provider: string
  resolved_model: string
  kind: string
  billing_pool: string
  capability: string
  credential_kinds: string[]
  execution_location: string
}
type Job = {
  id: string
  status: string
  job_kind: string
  requested_model: string
  resolved_model: string | null
  provider: string | null
  billing_pool: string | null
  account_id: string | null
  worker_id: string | null
  error_code: string | null
  error_detail: string | null
  created_at: string | null
}
type Diagnostics = {
  chrome_relay: { status: string; connected_workers: number }
  login_state: Array<{ worker_id: string; state: string }>
  codex_oauth: { configured: boolean; healthy: boolean }
  supported_local_tools: string[]
}
type Overview = {
  providers: Provider[]
  accounts: Account[]
  workers: Worker[]
  models: Model[]
  jobs: Job[]
  diagnostics: Diagnostics
  warnings: Array<{ kind: string; target_id: string; message: string }>
}

const emptyOverview: Overview = {
  providers: [], accounts: [], workers: [], models: [], jobs: [], warnings: [],
  diagnostics: {
    chrome_relay: { status: "offline", connected_workers: 0 },
    login_state: [], codex_oauth: { configured: false, healthy: false }, supported_local_tools: [],
  },
}

function StatusBadge({ value }: { value: string }) {
  const variant = ["ready", "online", "succeeded"].includes(value)
    ? "default"
    : ["error", "failed", "revoked", "timed_out"].includes(value)
      ? "destructive"
      : "secondary"
  return <Badge variant={variant}>{value}</Badge>
}

function ShortId({ value }: { value: string }) {
  return <span className="font-mono text-xs" title={value}>{value.length > 24 ? `${value.slice(0, 12)}…${value.slice(-6)}` : value}</span>
}

export function ControlPlane() {
  const { token } = useAuth()
  const [data, setData] = useState<Overview>(emptyOverview)
  const [loading, setLoading] = useState(true)
  const [pairingCode, setPairingCode] = useState<string | null>(null)

  const load = useCallback(async () => {
    const response = await adminJson<Overview>("/api/admin/control-plane/overview", token)
    if (response.ok && response.data) setData(response.data)
    else toast.error("Could not load control-plane status")
    setLoading(false)
  }, [token])

  useEffect(() => {
    void load()
    const timer = window.setInterval(() => void load(), 10_000)
    return () => window.clearInterval(timer)
  }, [load])

  const mutate = useCallback(async (path: string, method: string, body: unknown) => {
    const response = await adminJson<{ success?: boolean; detail?: string }>(path, token, {
      method,
      body: JSON.stringify(body),
    })
    if (!response.ok) {
      toast.error(response.data?.detail || "Action failed")
      return false
    }
    await load()
    return true
  }, [load, token])

  const createPairingCode = async () => {
    const response = await adminJson<{ code: string }>("/api/admin/control-plane/pairing-codes", token, {
      method: "POST", body: JSON.stringify({ ttl_seconds: 300 }),
    })
    if (response.ok && response.data) setPairingCode(response.data.code)
    else toast.error("Could not create pairing code")
  }

  const totals = useMemo(() => ({
    providers: data.providers.length,
    accounts: data.accounts.filter((account) => account.enabled).length,
    workers: data.workers.filter((worker) => worker.connected).length,
    running: data.jobs.filter((job) => ["queued", "offered", "running"].includes(job.status)).length,
  }), [data])

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <p className="text-sm font-medium text-primary">Execution control plane</p>
          <h1 className="mt-1 text-2xl font-semibold tracking-tight">Generation infrastructure</h1>
          <p className="mt-1 text-sm text-muted-foreground">See exactly where each request can run and why.</p>
        </div>
        <Button variant="outline" onClick={() => void load()} disabled={loading}>
          <RefreshCw className={`mr-2 size-4 ${loading ? "animate-spin" : ""}`} /> Refresh
        </Button>
      </div>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {[
          ["Providers", totals.providers], ["Active accounts", totals.accounts],
          ["Workers online", totals.workers], ["Jobs in flight", totals.running],
        ].map(([label, value]) => (
          <Card key={label}><CardContent className="p-5"><p className="text-sm text-muted-foreground">{label}</p><p className="mt-1 text-2xl font-semibold">{value}</p></CardContent></Card>
        ))}
      </div>

      {data.warnings.length > 0 ? (
        <Card className="border-amber-500/40 bg-amber-500/5">
          <CardContent className="space-y-1 p-4 text-sm">
            {data.warnings.map((warning) => <p key={`${warning.target_id}-${warning.message}`}>{warning.message} · <ShortId value={warning.target_id} /></p>)}
          </CardContent>
        </Card>
      ) : null}

      <Tabs defaultValue="providers" className="space-y-4">
        <TabsList className="grid h-auto w-full grid-cols-5">
          {(["providers", "accounts", "workers", "models", "jobs"] as const).map((view) => (
            <TabsTrigger key={view} value={view} className="capitalize">{view}</TabsTrigger>
          ))}
        </TabsList>

        <TabsContent value="providers">
          <div className="grid gap-4 lg:grid-cols-2">
            {data.providers.map((provider) => (
              <Card key={provider.id}>
                <CardHeader><CardTitle>{provider.id}</CardTitle><CardDescription>{provider.model_count} models · {provider.enabled_accounts}/{provider.account_count} accounts active</CardDescription></CardHeader>
                <CardContent className="space-y-3 text-sm">
                  <div><span className="text-muted-foreground">Runs on </span>{provider.execution_locations.join(", ") || "not configured"}</div>
                  <div className="flex flex-wrap gap-2">{provider.billing_pools.map((pool) => <Badge key={pool} variant="outline">{pool}</Badge>)}</div>
                </CardContent>
              </Card>
            ))}
            <Card>
              <CardHeader><CardTitle className="flex items-center gap-2"><Activity className="size-4" /> ChatGPT diagnostics</CardTitle></CardHeader>
              <CardContent className="grid gap-3 text-sm sm:grid-cols-2">
                <div><p className="text-muted-foreground">Chrome relay</p><StatusBadge value={data.diagnostics.chrome_relay.status} /></div>
                <div><p className="text-muted-foreground">Codex OAuth</p><StatusBadge value={data.diagnostics.codex_oauth.healthy ? "ready" : data.diagnostics.codex_oauth.configured ? "error" : "not configured"} /></div>
                <div className="sm:col-span-2"><p className="text-muted-foreground">Local tools</p><p>{data.diagnostics.supported_local_tools.join(", ") || "None reported"}</p></div>
              </CardContent>
            </Card>
          </div>
        </TabsContent>

        <TabsContent value="accounts">
          <Card><Table><TableHeader><TableRow><TableHead>Account</TableHead><TableHead>Provider</TableHead><TableHead>Credential location</TableHead><TableHead>Health</TableHead><TableHead className="text-right">Action</TableHead></TableRow></TableHeader>
            <TableBody>{data.accounts.map((account) => <TableRow key={account.id}>
              <TableCell><div className="font-medium">{account.label}</div><ShortId value={account.id} /></TableCell>
              <TableCell>{account.provider}</TableCell><TableCell>{account.credential_locations.join(", ") || "legacy account store"}</TableCell>
              <TableCell><StatusBadge value={account.health} /></TableCell>
              <TableCell className="text-right"><Button size="sm" variant="outline" onClick={() => void mutate(`/api/admin/control-plane/accounts/${account.id}`, "PATCH", { enabled: !account.enabled })}>{account.enabled ? <Pause className="mr-2 size-3" /> : <Play className="mr-2 size-3" />}{account.enabled ? "Pause" : "Resume"}</Button></TableCell>
            </TableRow>)}</TableBody>
          </Table></Card>
        </TabsContent>

        <TabsContent value="workers" className="space-y-4">
          <Card>
            <CardHeader className="flex-row items-center justify-between space-y-0"><div><CardTitle>Worker devices</CardTitle><CardDescription>Pair, pause, restrict, or revoke local execution devices.</CardDescription></div><Button onClick={() => void createPairingCode()}><KeyRound className="mr-2 size-4" /> Pair worker</Button></CardHeader>
            {pairingCode ? <CardContent><div className="flex items-center justify-between rounded-lg border bg-muted/30 p-3"><div><p className="text-xs text-muted-foreground">Pairing code · expires in 5 minutes</p><code className="text-lg font-semibold">{pairingCode}</code></div><Button size="icon" variant="ghost" onClick={() => void navigator.clipboard.writeText(pairingCode)}><Copy className="size-4" /></Button></div></CardContent> : null}
            <Table><TableHeader><TableRow><TableHead>Worker</TableHead><TableHead>Status</TableHead><TableHead>Capabilities</TableHead><TableHead>Last seen</TableHead><TableHead className="text-right">Actions</TableHead></TableRow></TableHeader>
              <TableBody>{data.workers.map((worker) => <TableRow key={worker.id}>
                <TableCell><div className="font-medium">{worker.label}</div><ShortId value={worker.id} /></TableCell><TableCell><StatusBadge value={worker.status} /></TableCell>
                <TableCell className="max-w-xs"><div className="flex flex-wrap gap-1">{worker.capabilities.map((capability) => <Badge variant="outline" key={capability}>{capability}</Badge>)}</div></TableCell>
                <TableCell>{worker.last_seen_at ? new Date(worker.last_seen_at).toLocaleString() : "Never"}</TableCell>
                <TableCell><div className="flex justify-end gap-2"><Button size="sm" variant="outline" disabled={Boolean(worker.status === "revoked")} onClick={() => void mutate(`/api/admin/control-plane/workers/${worker.id}`, "PATCH", { enabled: !worker.enabled })}>{worker.enabled ? "Pause" : "Resume"}</Button><Button size="sm" variant="destructive" disabled={worker.status === "revoked"} onClick={() => { if (window.confirm(`Revoke ${worker.label}? This device must be paired again.`)) void mutate(`/api/admin/control-plane/workers/${worker.id}`, "DELETE", { confirm: worker.id }) }}><Unplug className="mr-2 size-3" />Revoke</Button></div></TableCell>
              </TableRow>)}</TableBody>
            </Table>
          </Card>
        </TabsContent>

        <TabsContent value="models">
          <Card><Table><TableHeader><TableRow><TableHead>Model</TableHead><TableHead>Provider</TableHead><TableHead>Type</TableHead><TableHead>Execution</TableHead><TableHead>Billing pool</TableHead><TableHead>Credential</TableHead></TableRow></TableHeader>
            <TableBody>{data.models.map((model) => <TableRow key={model.id}><TableCell><div className="font-mono text-xs">{model.id}</div><div className="text-xs text-muted-foreground">→ {model.resolved_model}</div></TableCell><TableCell>{model.provider}</TableCell><TableCell>{model.kind}</TableCell><TableCell>{model.execution_location}</TableCell><TableCell>{model.billing_pool}</TableCell><TableCell>{model.credential_kinds.join(", ")}</TableCell></TableRow>)}</TableBody>
          </Table></Card>
        </TabsContent>

        <TabsContent value="jobs">
          <Card><Table><TableHeader><TableRow><TableHead>Job</TableHead><TableHead>Status</TableHead><TableHead>Requested → resolved</TableHead><TableHead>Route</TableHead><TableHead>Created</TableHead><TableHead>Error</TableHead></TableRow></TableHeader>
            <TableBody>{data.jobs.map((job) => <TableRow key={job.id}><TableCell><ShortId value={job.id} /></TableCell><TableCell><StatusBadge value={job.status} /></TableCell><TableCell><div className="font-mono text-xs">{job.requested_model}</div><div className="text-xs text-muted-foreground">→ {job.resolved_model || "pending"}</div></TableCell><TableCell><div>{job.provider || "pending"}</div><div className="text-xs text-muted-foreground">{job.billing_pool || "—"}</div><div className="text-xs"><ShortId value={job.worker_id || job.account_id || "unassigned"} /></div></TableCell><TableCell>{job.created_at ? new Date(job.created_at).toLocaleString() : "—"}</TableCell><TableCell className="max-w-xs text-xs text-destructive">{job.error_code || job.error_detail || "—"}</TableCell></TableRow>)}</TableBody>
          </Table></Card>
        </TabsContent>
      </Tabs>
    </div>
  )
}
