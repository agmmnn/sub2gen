import { useEffect, useState, useMemo } from "react"
import { useAuth } from "@/contexts/AuthContext"
import { adminFetch, adminJson } from "@/lib/adminApi"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { toast } from "sonner"
import { Loader2, CheckCircle2, XCircle, Copy } from "lucide-react"
import { Link } from "react-router-dom"

type GatewayAuthMode = "legacy" | "keygen" | "dual" | "unknown"
type GatewayConnection = {
  auth_method?: string
  subject?: string
  machine_id?: string
  license_id?: string
  account_id?: string
  claimed_token_ids?: number[]
  authorized_token_ids?: number[]
  connected_at?: number
}

function toWssBase(publicBase: string): string {
  const t = publicBase.trim()
  if (!t) return ""
  try {
    const u = new URL(t.startsWith("http") ? t : `https://${t}`)
    u.protocol = u.protocol === "https:" ? "wss:" : "ws:"
    return u.origin
  } catch {
    return ""
  }
}

export function AgentGateway() {
  const { token } = useAuth()
  const [captchaMethod, setCaptchaMethod] = useState<string>("")
  const [captchaConfigRaw, setCaptchaConfigRaw] = useState<Record<string, unknown> | null>(null)
  const [remoteBase, setRemoteBase] = useState("")
  const [remoteApiKey, setRemoteApiKey] = useState("")
  const [remoteTimeout, setRemoteTimeout] = useState(60)
  const [publicAgentUrl, setPublicAgentUrl] = useState("")
  const [captchaLoaded, setCaptchaLoaded] = useState(false)
  const [savingRemoteConfig, setSavingRemoteConfig] = useState(false)
  const [healthStatus, setHealthStatus] = useState<"idle" | "loading" | "ok" | "err">("idle")
  const [healthBody, setHealthBody] = useState<string>("")
  const [gatewayMode, setGatewayMode] = useState<GatewayAuthMode>("unknown")
  const [gatewayVerifyMode, setGatewayVerifyMode] = useState("")
  const [gatewayReachable, setGatewayReachable] = useState<boolean | null>(null)
  const [gatewayModeMsg, setGatewayModeMsg] = useState("")
  const [connectionsLoading, setConnectionsLoading] = useState(false)
  const [gatewayConnections, setGatewayConnections] = useState<GatewayConnection[]>([])

  useEffect(() => {
    if (!token) return
    let cancelled = false
    ;(async () => {
      try {
        const captchaResp = await adminJson<Record<string, unknown>>("/api/captcha/config", token)
        if (!cancelled && captchaResp.ok && captchaResp.data) {
          setCaptchaConfigRaw(captchaResp.data)
          const m = captchaResp.data.captcha_method
          setCaptchaMethod(typeof m === "string" ? m : "")
          const rb = captchaResp.data.remote_browser_base_url
          setRemoteBase(typeof rb === "string" ? rb : "")
          const rk = captchaResp.data.remote_browser_api_key
          setRemoteApiKey(typeof rk === "string" ? rk : "")
          const rt = Number(captchaResp.data.remote_browser_timeout ?? 60)
          setRemoteTimeout(Number.isFinite(rt) ? rt : 60)
        }
      } finally {
        if (!cancelled) setCaptchaLoaded(true)
      }

      // Keep mode probe independent so captcha fields still populate even if probe fails.
      try {
        const modeResp = await adminJson<Record<string, unknown>>("/api/agent-gateway/mode", token)
        if (cancelled || !modeResp.data) return
        const mode = String(modeResp.data.agent_auth_mode || "unknown").toLowerCase()
        const safeMode: GatewayAuthMode =
          mode === "legacy" || mode === "keygen" || mode === "dual" ? mode : "unknown"
        setGatewayMode(safeMode)
        setGatewayVerifyMode(String(modeResp.data.keygen_verify_mode || ""))
        if (typeof modeResp.data.gateway_reachable === "boolean") {
          setGatewayReachable(modeResp.data.gateway_reachable)
        } else {
          setGatewayReachable(null)
        }
        setGatewayModeMsg(String(modeResp.data.message || ""))
      } catch {
        if (cancelled) return
        setGatewayMode("unknown")
        setGatewayVerifyMode("")
        setGatewayReachable(null)
        setGatewayModeMsg("Gateway mode probe unavailable on this backend")
      }

      try {
        const conResp = await adminJson<Record<string, unknown>>("/api/agent-gateway/connections", token)
        if (cancelled || !conResp.data) return
        const items = Array.isArray(conResp.data.connections) ? conResp.data.connections : []
        setGatewayConnections(items as GatewayConnection[])
      } catch {
        if (!cancelled) setGatewayConnections([])
      }
    })()
    return () => {
      cancelled = true
    }
  }, [token])

  const wssUrl = useMemo(() => {
    const o = toWssBase(publicAgentUrl)
    return o ? `${o}/ws/agents` : ""
  }, [publicAgentUrl])

  const registerJson = useMemo(() => {
    if (gatewayMode === "keygen") {
      if (gatewayVerifyMode === "introspection") {
        return `{
  "type": "register",
  "agent_token": "<keygen-token>",
  "agent_token_id": "<keygen-token-uuid>",
  "token_ids": [1]
}`
      }
      return `{
  "type": "register",
  "agent_token": "<keygen-token>",
  "token_ids": [1]
}`
    }
    if (gatewayMode === "dual") {
      return `{
  "type": "register",
  "agent_token": "<keygen-token>",
  "token_ids": [1]
}

// Also accepted in dual mode:
{
  "type": "register",
  "device_token": "<GATEWAY_AGENT_DEVICE_TOKEN>",
  "token_ids": [1]
}`
    }
    return `{
  "type": "register",
  "device_token": "<GATEWAY_AGENT_DEVICE_TOKEN>",
  "token_ids": [1]
}`
  }, [gatewayMode, gatewayVerifyMode])

  const modeBadgeText = useMemo(() => {
    if (gatewayMode === "unknown") return "Detected mode: unknown"
    if (gatewayMode === "keygen" && gatewayVerifyMode) {
      return `Detected mode: keygen (${gatewayVerifyMode})`
    }
    if (gatewayMode === "dual" && gatewayVerifyMode) {
      return `Detected mode: dual (${gatewayVerifyMode})`
    }
    return `Detected mode: ${gatewayMode}`
  }, [gatewayMode, gatewayVerifyMode])

  const copy = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text)
      toast.success("Copied")
    } catch {
      toast.error("Copy failed")
    }
  }

  const refreshConnections = async () => {
    if (!token) return
    setConnectionsLoading(true)
    try {
      const conResp = await adminJson<Record<string, unknown>>("/api/agent-gateway/connections", token)
      const items = Array.isArray(conResp.data?.connections) ? conResp.data.connections : []
      setGatewayConnections(items as GatewayConnection[])
      toast.success("Connections refreshed")
    } catch {
      toast.error("Failed to refresh connections")
    } finally {
      setConnectionsLoading(false)
    }
  }

  const saveRemoteGatewayConfig = async () => {
    if (!token) return
    if (!captchaConfigRaw) {
      toast.error("Captcha config not loaded yet")
      return
    }
    setSavingRemoteConfig(true)
    try {
      const payload: Record<string, unknown> = {
        ...captchaConfigRaw,
        captcha_method: String(captchaConfigRaw.captcha_method || "browser"),
        remote_browser_base_url: remoteBase.trim(),
        remote_browser_api_key: remoteApiKey.trim(),
        remote_browser_timeout: Math.max(5, Number(remoteTimeout) || 60),
      }
      const res = await adminFetch("/api/captcha/config", token, {
        method: "POST",
        body: JSON.stringify(payload),
      })
      if (!res) return
      const body = await res.json()
      if (!body?.success) {
        toast.error(String(body?.message || body?.detail || "Save failed"))
        return
      }
      toast.success("Remote gateway config saved")
      setCaptchaConfigRaw((prev) =>
        prev
          ? {
              ...prev,
              remote_browser_base_url: payload.remote_browser_base_url,
              remote_browser_api_key: payload.remote_browser_api_key,
              remote_browser_timeout: payload.remote_browser_timeout,
            }
          : prev
      )
    } catch {
      toast.error("Failed to save remote gateway config")
    } finally {
      setSavingRemoteConfig(false)
    }
  }

  const pingHealth = async () => {
    const base = publicAgentUrl.trim()
    if (!base) {
      toast.error("Set the public agent URL first")
      return
    }
    let url: string
    try {
      url = new URL("/health", base.startsWith("http") ? base : `https://${base}`).toString()
    } catch {
      toast.error("Invalid URL")
      return
    }
    setHealthStatus("loading")
    setHealthBody("")
    try {
      const r = await fetch(url, { method: "GET" })
      const text = await r.text()
      setHealthBody(text || `HTTP ${r.status}`)
      setHealthStatus(r.ok ? "ok" : "err")
      if (r.ok) toast.success("Gateway reachable")
      else toast.error(`HTTP ${r.status}`)
    } catch (e) {
      setHealthStatus("err")
      setHealthBody(e instanceof Error ? e.message : String(e))
      toast.error("Health check failed (CORS or network)")
    }
  }

  if (!token) {
    return null
  }
  if (!captchaLoaded) {
    return (
      <div className="flex items-center gap-2 text-muted-foreground">
        <Loader2 className="h-4 w-4 animate-spin" />
        Loading captcha config…
      </div>
    )
  }

  return (
    <div className="space-y-6 max-w-3xl">
      <p className="text-sm text-muted-foreground">
        Operator UI for the <strong>remote captcha agent</strong> lives here (this admin app in{" "}
        <code className="text-xs bg-muted px-1 rounded">apps/admin-web/</code>
        ), not on the gateway port’s API docs. Connect PCs over WebSocket to your public{" "}
        <code className="text-xs bg-muted px-1 rounded">agents.*</code> hostname.
      </p>

      {captchaMethod && captchaMethod !== "remote_browser" && (
        <p className="text-sm text-amber-800 dark:text-amber-200 bg-amber-50 dark:bg-amber-950/40 border border-amber-200/60 dark:border-amber-800/60 rounded-md px-3 py-2">
          Captcha method is <strong>{captchaMethod}</strong>. For agents, set{" "}
          <Link to="/manage?tab=settings" className="underline font-medium">
            System settings
          </Link>{" "}
          → captcha to <strong>Remote headed</strong> (
          <code className="text-xs">remote_browser</code>).
        </p>
      )}

      <Card>
        <CardHeader>
          <CardTitle>sub2gen → gateway (HTTP)</CardTitle>
          <CardDescription>
            Uses the same captcha config values as <strong>System settings</strong>. Inside Docker this is usually{" "}
            <code className="text-xs">http://agent-gateway:9080</code>.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="space-y-2">
            <Label htmlFor="ag-remote-base">Remote base URL</Label>
            <Input
              id="ag-remote-base"
              placeholder="http://agent-gateway:9080"
              value={remoteBase}
              onChange={(e) => setRemoteBase(e.target.value)}
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="ag-remote-key">API key</Label>
            <Input
              id="ag-remote-key"
              value={remoteApiKey}
              onChange={(e) => setRemoteApiKey(e.target.value)}
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="ag-remote-timeout">Timeout (s)</Label>
            <Input
              id="ag-remote-timeout"
              type="number"
              min={5}
              value={remoteTimeout}
              onChange={(e) => setRemoteTimeout(parseInt(e.target.value, 10) || 60)}
            />
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Button type="button" onClick={saveRemoteGatewayConfig} disabled={savingRemoteConfig}>
              {savingRemoteConfig ? "Saving…" : "Save gateway config"}
            </Button>
            {remoteBase ? (
              <Button type="button" variant="outline" size="sm" onClick={() => copy(remoteBase)}>
                <Copy className="h-3.5 w-3.5" />
              </Button>
            ) : null}
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Gateway mode detection</CardTitle>
          <CardDescription>
            Auto-detected from configured gateway health endpoint.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-xs bg-muted px-2 py-1 rounded">{modeBadgeText}</span>
            {gatewayReachable === true && <CheckCircle2 className="h-4 w-4 text-green-600" />}
            {gatewayReachable === false && <XCircle className="h-4 w-4 text-destructive" />}
          </div>
          {gatewayModeMsg ? (
            <p className="text-xs text-muted-foreground">{gatewayModeMsg}</p>
          ) : null}
          {gatewayMode === "keygen" ? (
            <p className="text-xs text-muted-foreground">
              Agent clients should send <code className="text-xs">agent_token</code>
              {gatewayVerifyMode === "introspection" ? (
                <>
                  {" "}
                  + <code className="text-xs">agent_token_id</code>
                </>
              ) : null}
              . Server must have
              <code className="text-xs"> KEYGEN_*</code> configured.
            </p>
          ) : null}
          {gatewayMode === "legacy" ? (
            <p className="text-xs text-muted-foreground">
              Agent clients should send <code className="text-xs">device_token</code> matching
              <code className="text-xs"> GATEWAY_AGENT_DEVICE_TOKEN</code>.
            </p>
          ) : null}
          {gatewayMode === "dual" ? (
            <p className="text-xs text-muted-foreground">
              Both auth payloads work. Prefer <code className="text-xs">agent_token</code> for forward compatibility.
            </p>
          ) : null}
          {gatewayReachable === false ? (
            <div className="text-xs text-amber-800 dark:text-amber-200 bg-amber-50 dark:bg-amber-950/40 border border-amber-200/60 dark:border-amber-800/60 rounded-md px-3 py-2 space-y-1">
              <p>Gateway is unreachable from sub2gen.</p>
              <p>
                Check <code className="text-xs">remote_browser_base_url</code>, Docker service name, and tunnel/hostname routing.
              </p>
            </div>
          ) : null}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Connected agents</CardTitle>
          <CardDescription>
            Live sessions currently registered in gateway memory.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="flex items-center gap-2">
            <Button type="button" variant="secondary" onClick={refreshConnections} disabled={connectionsLoading}>
              {connectionsLoading ? (
                <>
                  <Loader2 className="h-3.5 w-3.5 mr-1 animate-spin" />
                  Refreshing…
                </>
              ) : (
                "Refresh"
              )}
            </Button>
            <span className="text-xs text-muted-foreground">
              {gatewayConnections.length} connected
            </span>
          </div>
          {gatewayConnections.length === 0 ? (
            <p className="text-xs text-muted-foreground">No agents connected.</p>
          ) : (
            <div className="space-y-2">
              {gatewayConnections.map((c, idx) => (
                <div key={`${c.subject || "unknown"}-${idx}`} className="rounded-md border p-2 text-xs space-y-1">
                  <div>
                    <strong>subject:</strong> {c.subject || "-"}
                  </div>
                  <div>
                    <strong>auth:</strong> {c.auth_method || "-"}
                  </div>
                  <div>
                    <strong>machine:</strong> {c.machine_id || "-"}
                  </div>
                  <div>
                    <strong>license:</strong> {c.license_id || "-"}
                  </div>
                  <div>
                    <strong>authorized token_ids:</strong>{" "}
                    {Array.isArray(c.authorized_token_ids) && c.authorized_token_ids.length
                      ? c.authorized_token_ids.join(", ")
                      : "-"}
                  </div>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>PC agents → public URL</CardTitle>
          <CardDescription>
            The hostname you set in Cloudflare (e.g. <code className="text-xs">https://agents.example.com</code> →
            <code className="text-xs">http://agent-gateway:9080</code>). Used to build the WebSocket URL and test
            health.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="public-agent-url">Public agent base URL</Label>
            <Input
              id="public-agent-url"
              placeholder="https://agents.example.com"
              value={publicAgentUrl}
              onChange={(e) => setPublicAgentUrl(e.target.value)}
            />
          </div>
          {wssUrl ? (
            <div className="space-y-2">
              <Label>WebSocket (register agents here)</Label>
              <div className="flex flex-wrap items-center gap-2">
                <code className="text-sm break-all bg-muted px-2 py-1 rounded flex-1 min-w-0">{wssUrl}</code>
                <Button type="button" variant="outline" size="sm" onClick={() => copy(wssUrl)}>
                  <Copy className="h-3.5 w-3.5" />
                </Button>
              </div>
            </div>
          ) : null}
          <div className="flex flex-wrap items-center gap-2">
            <Button type="button" variant="secondary" onClick={pingHealth} disabled={healthStatus === "loading"}>
              {healthStatus === "loading" ? (
                <>
                  <Loader2 className="h-3.5 w-3.5 mr-1 animate-spin" />
                  Checking…
                </>
              ) : (
                "GET /health (browser)"
              )}
            </Button>
            {healthStatus === "ok" && <CheckCircle2 className="h-4 w-4 text-green-600" />}
            {healthStatus === "err" && <XCircle className="h-4 w-4 text-destructive" />}
          </div>
          {healthBody ? (
            <pre className="text-xs bg-muted p-3 rounded-md overflow-x-auto max-h-40">{healthBody}</pre>
          ) : null}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Register message (first frame after connect)</CardTitle>
          <CardDescription>
            <code className="text-xs">token_ids</code> are sub2gen token row IDs to serve.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <pre className="text-xs bg-muted p-3 rounded-md overflow-x-auto">
            {registerJson}
          </pre>
          {gatewayMode === "keygen" ? (
            <p className="text-xs text-muted-foreground mt-2">
              Keygen mode mismatch tip: if you send <code className="text-xs">device_token</code>, registration will fail.
            </p>
          ) : null}
          {gatewayMode === "legacy" ? (
            <p className="text-xs text-muted-foreground mt-2">
              Legacy mode mismatch tip: if you send <code className="text-xs">agent_token</code>, registration will fail.
            </p>
          ) : null}
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="mt-2"
            onClick={() => copy(registerJson)}
          >
            <Copy className="h-3.5 w-3.5 mr-1" />
            Copy JSON
          </Button>
        </CardContent>
      </Card>
    </div>
  )
}
