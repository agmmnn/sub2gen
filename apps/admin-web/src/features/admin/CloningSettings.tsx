import { useCallback, useEffect, useMemo, useState } from "react"
import { useAuth } from "../../contexts/AuthContext"
import { adminFetch, adminJson } from "../../lib/adminApi"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../../components/ui/card"
import { Button } from "../../components/ui/button"
import { Input } from "../../components/ui/input"
import { Label } from "../../components/ui/label"
import { Textarea } from "../../components/ui/textarea"
import { toast } from "sonner"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../../components/ui/select"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../../components/ui/table"
import { Key } from "lucide-react"



const CLOUDFLARE_MODELS = [
  "@cf/moonshotai/kimi-k2.6",
  "@cf/zai-org/glm-4.7-flash",
  "@cf/meta/llama-4-scout-17b-16e-instruct",
  "@cf/google/gemma-4-26b-a4b-it",
]

const PRESET_MODELS: Record<string, string[]> = {
  cliproxy: [],
  gemini_native: ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.5-pro", "gemini-1.5-flash", "gemini-1.5-pro"],
  openai: ["gpt-4o-mini", "gpt-4o", "gpt-4-turbo", "gpt-4.1-mini", "gpt-4.1"],
  openrouter: ["moonshotai/kimi-k2.6", "google/gemma-4-26b-a4b-it"],
  third_party_gemini: ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.5-pro", "gemini-1.5-flash", "gemini-1.5-pro"],
  cloudflare: CLOUDFLARE_MODELS
}

const CLONING_BACKENDS = [
  "cliproxy",
  "gemini_native",
  "openai",
  "openrouter",
  "third_party_gemini",
  "cloudflare",
]

export function CloningSettings({ active }: { active: boolean }) {
  const { token } = useAuth()
  const [busy, setBusy] = useState(false)
  const [providerOrder, setProviderOrder] = useState<string[]>(CLONING_BACKENDS)
  const [enabledProviders, setEnabledProviders] = useState<string[]>(["gemini_native"])
  const [providerRetryCount, setProviderRetryCount] = useState(1)
  const [model, setModel] = useState("gemini-2.5-flash")
  const [gatewayModels, setGatewayModels] = useState<string[]>([])


  const [geminiKeys, setGeminiKeys] = useState("")
  const [openaiKeys, setOpenaiKeys] = useState("")
  const [thirdPartyKeys, setThirdPartyKeys] = useState("")
  const [thirdPartyBaseUrl, setThirdPartyBaseUrl] = useState("")
  const [openrouterKeys, setOpenrouterKeys] = useState("")
  const [cloudflareAccountId, setCloudflareAccountId] = useState("")
  const [cloudflareApiToken, setCloudflareApiToken] = useState("")

  const load = useCallback(async () => {
    if (!token || !active) return
    const [resp, gatewayResp] = await Promise.all([
      adminJson<{ success?: boolean; config?: Record<string, unknown> }>("/api/config/generation", token),
      adminJson<Array<{ id: string }>>("/api/admin/cliproxy/models", token),
    ])
    const gatewayIds = gatewayResp.ok && gatewayResp.data
      ? gatewayResp.data.map((entry) => entry.id).filter(Boolean)
      : []
    if (gatewayIds.length) setGatewayModels(gatewayIds)
    if (!resp.ok || !resp.data?.success || !resp.data.config) return
    const c = resp.data.config
    const legacyBackend = String(c.sub2gen_cloning_backend || "gemini_native").trim() || "gemini_native"
    const orderFromConfig = String(c.sub2gen_cloning_provider_order || "")
      .split(",")
      .map((x) => x.trim())
      .filter(Boolean)
      .filter((x) => CLONING_BACKENDS.includes(x))
    const configuredOrder = orderFromConfig.length ? orderFromConfig : [legacyBackend]
    const normalizedOrder = [
      "cliproxy",
      ...configuredOrder.filter((provider) => provider !== "cliproxy"),
      ...CLONING_BACKENDS.filter((provider) => provider !== "cliproxy" && !configuredOrder.includes(provider)),
    ]
    const enabledFromConfig = String(c.sub2gen_cloning_enabled_providers || "")
      .split(",")
      .map((x) => x.trim())
      .filter(Boolean)
      .filter((x) => normalizedOrder.includes(x))
    const normalizedEnabled = enabledFromConfig.length ? enabledFromConfig : [legacyBackend]
    const selectedProvider = normalizedOrder.find((p) => normalizedEnabled.includes(p)) || normalizedOrder[0] || "gemini_native"
    const presets = selectedProvider === "cliproxy" ? gatewayIds : (PRESET_MODELS[selectedProvider] || [])
    let modelStr = String(c.sub2gen_cloning_model || "").trim()
    if (!modelStr) {
      modelStr = presets[0] || "gemini-2.5-flash"
    }
    setProviderOrder(normalizedOrder)
    setEnabledProviders(normalizedEnabled)
    setProviderRetryCount(
      Math.max(0, Math.min(5, Number(c.sub2gen_cloning_provider_retry_count ?? 1) || 1))
    )
    setModel(modelStr)

    setGeminiKeys(String(c.sub2gen_cloning_gemini_api_keys || ""))
    setOpenaiKeys(String(c.sub2gen_cloning_openai_api_keys || ""))
    setThirdPartyKeys(String(c.sub2gen_cloning_third_party_gemini_api_keys || ""))
    setThirdPartyBaseUrl(String(c.sub2gen_cloning_third_party_gemini_base_url || ""))
    setOpenrouterKeys(String(c.sub2gen_cloning_openrouter_api_keys || ""))
    setCloudflareAccountId(String(c.sub2gen_cloning_cloudflare_account_id || ""))
    setCloudflareApiToken(String(c.sub2gen_cloning_cloudflare_api_token || ""))
  }, [token, active])

  const selectedProvider = useMemo(
    () => providerOrder.find((provider) => enabledProviders.includes(provider)) || providerOrder[0] || "gemini_native",
    [providerOrder, enabledProviders],
  )

  /** Include the persisted model so Radix Select never gets a value missing from items (fixes blank dropdown after load). */
  const backendModels = useMemo(() => {
    const base = [...(selectedProvider === "cliproxy" ? gatewayModels : (PRESET_MODELS[selectedProvider] || []))]
    const cur = model.trim()
    if (cur && !base.includes(cur)) base.unshift(cur)
    return base
  }, [gatewayModels, selectedProvider, model])

  useEffect(() => {
    const timer = window.setTimeout(() => {
      void load()
    }, 0)
    return () => window.clearTimeout(timer)
  }, [load])

  const toggleProvider = (providerName: string, checked: boolean) => {
    const value = providerName.trim()
    if (!value) return
    setEnabledProviders((prev) => {
      if (checked) return Array.from(new Set([...prev, value]))
      const next = prev.filter((p) => p !== value)
      return next.length ? next : prev
    })
  }

  const moveProvider = (providerName: string, direction: -1 | 1) => {
    setProviderOrder((prev) => {
      const idx = prev.indexOf(providerName)
      if (idx < 0) return prev
      const target = idx + direction
      if (target < 0 || target >= prev.length) return prev
      const next = [...prev]
      const tmp = next[idx]
      next[idx] = next[target]
      next[target] = tmp
      return next
    })
  }

  const save = async () => {
    if (!token) return
    const presets = selectedProvider === "cliproxy" ? gatewayModels : (PRESET_MODELS[selectedProvider] || [])
    let modelOut = model.trim()
    if (!modelOut) {
      modelOut = presets[0] || "gemini-2.5-flash"
      setModel(modelOut)
    }
    setBusy(true)
    try {
      const r = await adminFetch("/api/config/generation", token, {
        method: "POST",
        headers: {
          // Same URL as GET load; this header lets you spot the save in DevTools → Headers.
          "X-Sub2Gen-Client-Operation": "save-cloning-settings",
        },
        body: JSON.stringify({
          sub2gen_cloning_backend: selectedProvider,
          sub2gen_cloning_provider_order: providerOrder.join(","),
          sub2gen_cloning_enabled_providers: enabledProviders.join(","),
          sub2gen_cloning_provider_retry_count: providerRetryCount,
          sub2gen_cloning_model: modelOut,

          sub2gen_cloning_gemini_api_keys: geminiKeys,
          sub2gen_cloning_openai_api_keys: openaiKeys,
          sub2gen_cloning_third_party_gemini_api_keys: thirdPartyKeys,
          sub2gen_cloning_third_party_gemini_base_url: thirdPartyBaseUrl,
          sub2gen_cloning_openrouter_api_keys: openrouterKeys,
          sub2gen_cloning_cloudflare_account_id: cloudflareAccountId,
          sub2gen_cloning_cloudflare_api_token: cloudflareApiToken,
        }),
      })
      if (!r) {
        toast.error("Session expired or unauthorized — please log in again.")
        return
      }
      const d = await r.json().catch(() => null)
      if (!d) {
        toast.error(`Save failed (HTTP ${r.status}).`)
        return
      }
      if (!r.ok) {
        const msg =
          typeof d.detail === "string"
            ? d.detail
            : Array.isArray(d.detail)
              ? d.detail.map((x: { msg?: string }) => x?.msg).filter(Boolean).join("; ")
              : String(d.message || `HTTP ${r.status}`)
        toast.error(msg || `Save failed (HTTP ${r.status}).`)
        return
      }
      if (d.success) {
        toast.success("Cloning settings saved")
        await load()
      } else {
        const msg =
          typeof d.detail === "string"
            ? d.detail
            : Array.isArray(d.detail)
              ? d.detail.map((x: { msg?: string }) => x?.msg).filter(Boolean).join("; ")
              : String(d.message || "Failed to save")
        toast.error(msg || "Failed to save")
      }
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Network error while saving")
    } finally {
      setBusy(false)
    }
  }

  if (!active) return null

  return (
    <Card>
      <CardHeader>
        <CardTitle>Cloning Settings</CardTitle>
        <CardDescription>Configure cloning backend, model, and credentials.</CardDescription>
      </CardHeader>
      <CardContent>
        <form
          className="space-y-6"
          onSubmit={(e) => {
            e.preventDefault()
            void save()
          }}
        >
        <div className="space-y-2">
          <Label>Cloning Providers (ranked fallback)</Label>
          <div className="rounded-md border overflow-hidden">
            <div className="grid grid-cols-[1fr_90px_110px] gap-2 border-b bg-muted/40 px-3 py-2 text-xs font-medium">
              <span>Provider</span>
              <span className="text-center">Enabled</span>
              <span className="text-center">Order</span>
            </div>
            <div className="max-h-[260px] overflow-y-auto">
              {providerOrder.map((entry) => {
                const enabled = enabledProviders.includes(entry)
                return (
                  <div key={entry} className="grid grid-cols-[1fr_90px_110px] items-center gap-2 border-b last:border-0 px-3 py-2 text-sm hover:bg-muted/20 transition-colors">
                    <span className="font-mono truncate" title={entry}>{entry}</span>
                    <div className="flex justify-center">
                      <input
                        type="checkbox"
                        className="h-4 w-4 accent-primary rounded cursor-pointer"
                        checked={enabled}
                        onChange={(e) => toggleProvider(entry, e.target.checked)}
                      />
                    </div>
                    <div className="flex gap-1 justify-center">
                      <Button type="button" size="sm" variant="outline" onClick={() => moveProvider(entry, -1)} className="h-7 px-2">Up</Button>
                      <Button type="button" size="sm" variant="outline" onClick={() => moveProvider(entry, 1)} className="h-7 px-2">Down</Button>
                    </div>
                  </div>
                )
              })}
            </div>
          </div>
          <p className="text-xs text-muted-foreground">
            Requests use the first enabled provider, then fallback in order.
          </p>
          <div className="max-w-xs">
            <Label htmlFor="cloning-provider-retries">Retry per provider before fallback</Label>
            <Input
              id="cloning-provider-retries"
              type="number"
              min={0}
              max={5}
              className="mt-1 w-32 font-mono text-sm"
              value={providerRetryCount}
              onChange={(e) => setProviderRetryCount(Math.max(0, Math.min(5, Number(e.target.value) || 0)))}
            />
          </div>
        </div>

        <div className="space-y-2">
          <Label>Cloning Model</Label>
          <div className="flex gap-2 w-full sm:w-[400px]">
            <Select
              key={`${selectedProvider}:${model}`}
              value={model}
              onValueChange={setModel}
            >
              <SelectTrigger className="w-full">
                <SelectValue placeholder="Select model" />
              </SelectTrigger>
              <SelectContent>
                {backendModels.map(entry => (
                  <SelectItem key={entry} value={entry} className="font-mono">{entry}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

        </div>

        <div className="space-y-4 rounded-md border p-4 bg-muted/10">
          <div>
            <h3 className="font-semibold text-lg flex items-center gap-2">
              <Key className="w-5 h-5 text-primary" /> Provider Credentials
            </h3>
            <p className="text-xs text-muted-foreground mt-1">Cloning operates independently of Metadata and requires its own API keys. You can specify <strong>multiple API keys</strong> separated by commas for load balancing.</p>
          </div>
          
          <div className="rounded-md border bg-background overflow-hidden">
            <Table>
              <TableHeader>
                <TableRow className="bg-muted/50 hover:bg-muted/50">
                  <TableHead className="w-[200px]">Provider / Service</TableHead>
                  <TableHead className="w-[380px]">Configuration Key</TableHead>
                  <TableHead>Value / Token</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                <TableRow>
                  <TableCell className="font-medium">Google Gemini</TableCell>
                  <TableCell className="text-muted-foreground font-mono text-[11px]">SUB2GEN_CLONING_GEMINI_API_KEYS</TableCell>
                  <TableCell>
                    <Textarea className="font-mono text-xs min-h-[60px] bg-muted/20 resize-y" placeholder="AIzaSy..., AIzaSy..." value={geminiKeys} onChange={(e) => setGeminiKeys(e.target.value)} />
                  </TableCell>
                </TableRow>
                <TableRow>
                  <TableCell className="font-medium">OpenAI</TableCell>
                  <TableCell className="text-muted-foreground font-mono text-[11px]">SUB2GEN_CLONING_OPENAI_API_KEYS</TableCell>
                  <TableCell>
                    <Textarea className="font-mono text-xs min-h-[60px] bg-muted/20 resize-y" placeholder="sk-proj-..., sk-proj-..." value={openaiKeys} onChange={(e) => setOpenaiKeys(e.target.value)} />
                  </TableCell>
                </TableRow>
                <TableRow>
                  <TableCell className="font-medium">OpenRouter</TableCell>
                  <TableCell className="text-muted-foreground font-mono text-[11px]">SUB2GEN_CLONING_OPENROUTER_API_KEYS</TableCell>
                  <TableCell>
                    <Textarea className="font-mono text-xs min-h-[60px] bg-muted/20 resize-y" placeholder="sk-or-v1-..., sk-or-v1-..." value={openrouterKeys} onChange={(e) => setOpenrouterKeys(e.target.value)} />
                  </TableCell>
                </TableRow>
                <TableRow>
                  <TableCell className="font-medium">Third-Party Gemini</TableCell>
                  <TableCell className="text-muted-foreground font-mono text-[11px]">SUB2GEN_CLONING_THIRD_PARTY_GEMINI_API_KEYS</TableCell>
                  <TableCell>
                    <Textarea className="font-mono text-xs min-h-[60px] bg-muted/20 resize-y" placeholder="Key1, Key2..." value={thirdPartyKeys} onChange={(e) => setThirdPartyKeys(e.target.value)} />
                  </TableCell>
                </TableRow>
                <TableRow>
                  <TableCell className="font-medium text-muted-foreground text-xs pl-8">↳ Base URL</TableCell>
                  <TableCell className="text-muted-foreground font-mono text-[11px]">SUB2GEN_CLONING_THIRD_PARTY_GEMINI_BASE_URL</TableCell>
                  <TableCell>
                    <Input className="font-mono text-xs h-8 bg-muted/20" placeholder="https://..." value={thirdPartyBaseUrl} onChange={(e) => setThirdPartyBaseUrl(e.target.value)} />
                  </TableCell>
                </TableRow>
                <TableRow>
                  <TableCell className="font-medium">Cloudflare</TableCell>
                  <TableCell className="text-muted-foreground font-mono text-[11px]">CLONING_CLOUDFLARE_ACCOUNT_ID</TableCell>
                  <TableCell>
                    <Input className="font-mono text-xs h-8 bg-muted/20" placeholder="Account ID..." value={cloudflareAccountId} onChange={(e) => setCloudflareAccountId(e.target.value)} />
                  </TableCell>
                </TableRow>
                <TableRow>
                  <TableCell className="font-medium text-muted-foreground text-xs pl-8">↳ API Token</TableCell>
                  <TableCell className="text-muted-foreground font-mono text-[11px]">CLONING_CLOUDFLARE_API_TOKEN</TableCell>
                  <TableCell>
                    <Input className="font-mono text-xs h-8 bg-muted/20" placeholder="API Token..." value={cloudflareApiToken} onChange={(e) => setCloudflareApiToken(e.target.value)} />
                  </TableCell>
                </TableRow>
              </TableBody>
            </Table>
          </div>
        </div>


        <Button type="submit" disabled={busy} className="w-full sm:w-auto mt-4">
          Save cloning settings
        </Button>
        </form>
      </CardContent>
    </Card>
  )
}
