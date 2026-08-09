import { useCallback, useEffect, useMemo, useState } from "react"
import { useAuth } from "../../contexts/AuthContext"
import { adminFetch, adminJson } from "../../lib/adminApi"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../../components/ui/card"
import { Button } from "../../components/ui/button"
import { Input } from "../../components/ui/input"
import { Label } from "../../components/ui/label"
import { Textarea } from "../../components/ui/textarea"
import { Switch } from "../../components/ui/switch"
import { toast } from "sonner"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../../components/ui/table"
import { Key } from "lucide-react"


const CLOUDFLARE_MODELS = [
  "@cf/moonshotai/kimi-k2.6",
  "@cf/zai-org/glm-4.7-flash",
  "@cf/meta/llama-4-scout-17b-16e-instruct",
  "@cf/google/gemma-4-26b-a4b-it",
]

/** Models sent to csvgen.com /api/generate-metadata (Workers AI ids). */
const CSVGEN_METADATA_MODELS = [
  "@cf/meta/llama-4-scout-17b-16e-instruct",
  "@cf/mistralai/mistral-small-3.1-24b-instruct",
  "@cf/moonshotai/kimi-k2.5",
]

const PRESET_MODELS: Record<string, string[]> = {
  cliproxy: [],
  gemini_native: ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.5-pro", "gemini-1.5-flash", "gemini-1.5-pro"],
  openai: ["gpt-4o-mini", "gpt-4o", "gpt-4-turbo", "gpt-4.1-mini", "gpt-4.1"],
  openrouter: ["moonshotai/kimi-k2.6", "google/gemma-4-26b-a4b-it"],
  third_party_gemini: ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.5-pro", "gemini-1.5-flash", "gemini-1.5-pro"],
  cloudflare: CLOUDFLARE_MODELS,
  csvgen: [...CSVGEN_METADATA_MODELS],
}

const GLOBAL_BACKENDS = [
  "cliproxy",
  "gemini_native",
  "openai",
  "openrouter",
  "third_party_gemini",
  "cloudflare",
]

export function AdobeConfigurations({ active }: { active: boolean }) {
  const { token } = useAuth()
  const [busy, setBusy] = useState(false)
  const [providerOrder, setProviderOrder] = useState<string[]>(GLOBAL_BACKENDS)
  const [enabledProviders, setEnabledProviders] = useState<string[]>(["gemini_native"])
  const [useCsvgenOnly, setUseCsvgenOnly] = useState(false)
  const [providerRetryCount, setProviderRetryCount] = useState(1)
  const [model, setModel] = useState("gemini-2.5-flash")
  const [enabledModels, setEnabledModels] = useState<string[]>(["gemini-2.5-flash"])
  const [primaryModel, setPrimaryModel] = useState("gemini-2.5-flash")
  const [gatewayModels, setGatewayModels] = useState<string[]>([])

  const [csvgenCookie, setCsvgenCookie] = useState("")
  const [csvgenApiKeys, setCsvgenApiKeys] = useState("")

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
    if (gatewayResp.ok && gatewayResp.data) {
      setGatewayModels(gatewayResp.data.map((entry) => entry.id).filter(Boolean))
    }
    if (!resp.ok || !resp.data?.success || !resp.data.config) return
    const c = resp.data.config
    
    // Check if csvgen is currently active for metadata
    const legacyBackend = String(c.sub2gen_metadata_backend || "gemini_native").trim()
    const isCsvgenOnly = legacyBackend === "csvgen"
    setUseCsvgenOnly(isCsvgenOnly)

    // Load global provider order (metadata is canonical; market keys are not persisted by API yet)
    const orderFromConfig = String(c.sub2gen_market_provider_order || c.sub2gen_metadata_provider_order || "")
      .split(",")
      .map((x) => x.trim())
      .filter(Boolean)
      .filter((x) => GLOBAL_BACKENDS.includes(x))
    
    const configuredOrder = orderFromConfig.length ? orderFromConfig : ["gemini_native"]
    const normalizedOrder = [
      "cliproxy",
      ...configuredOrder.filter((provider) => provider !== "cliproxy"),
      ...GLOBAL_BACKENDS.filter((provider) => provider !== "cliproxy" && !configuredOrder.includes(provider)),
    ]
    
    const enabledFromConfig = String(
      c.sub2gen_market_enabled_providers ||
        c.sub2gen_metadata_enabled_providers ||
        c.sub2gen_cloning_enabled_providers ||
        "",
    )
      .split(",")
      .map((x) => x.trim())
      .filter(Boolean)
      .filter((x) => normalizedOrder.includes(x))

    const normalizedEnabledProviders = enabledFromConfig.length ? enabledFromConfig : ["gemini_native"]

    setProviderOrder(normalizedOrder)
    setEnabledProviders(normalizedEnabledProviders)
    const retryRaw =
      c.sub2gen_market_provider_retry_count ??
      c.sub2gen_metadata_provider_retry_count ??
      c.sub2gen_cloning_provider_retry_count
    setProviderRetryCount(Math.max(0, Math.min(5, Number(retryRaw) || 1)))
    
    // Model configuration
    const configuredEnabledRaw = String(c.sub2gen_market_enabled_models || c.sub2gen_metadata_enabled_models || "").trim()
    const configuredPrimary = String(c.sub2gen_market_primary_model || c.sub2gen_metadata_primary_model || "").trim()
    const configuredFallbackRaw = String(c.sub2gen_market_fallback_models || c.sub2gen_metadata_fallback_models || "").trim()
    const legacyModel = String(c.sub2gen_market_model || c.sub2gen_metadata_model || "gemini-2.5-flash").trim() || "gemini-2.5-flash"
    const enabled = (configuredEnabledRaw ? configuredEnabledRaw.split(",") : [])
      .map((m) => m.trim())
      .filter(Boolean)
    const fallback = (configuredFallbackRaw ? configuredFallbackRaw.split(",") : [])
      .map((m) => m.trim())
      .filter(Boolean)
    const primary = configuredPrimary || legacyModel
    const normalizedEnabled = Array.from(new Set([...(enabled.length ? enabled : [legacyModel]), primary, ...fallback]))
    setEnabledModels(normalizedEnabled)
    setPrimaryModel(primary)
    setModel(primary)
    
    setCsvgenCookie(String(c.sub2gen_csvgen_cookie || ""))
    setCsvgenApiKeys(String(c.sub2gen_csvgen_api_keys || ""))

    setGeminiKeys(String(c.sub2gen_gemini_api_keys || ""))
    setOpenaiKeys(String(c.sub2gen_openai_api_keys || ""))
    setThirdPartyKeys(String(c.sub2gen_third_party_gemini_api_keys || ""))
    setThirdPartyBaseUrl(String(c.sub2gen_third_party_gemini_base_url || ""))
    setOpenrouterKeys(String(c.sub2gen_openrouter_api_keys || ""))
    setCloudflareAccountId(String(c.cloudflare_account_id || ""))
    setCloudflareApiToken(String(c.cloudflare_api_token || ""))
  }, [token, active])

  const selectedProvider = useMemo(
    () => providerOrder.find((provider) => enabledProviders.includes(provider)) || providerOrder[0] || "gemini_native",
    [providerOrder, enabledProviders],
  )

  const backendModels = useMemo(
    () => selectedProvider === "cliproxy" ? gatewayModels : (PRESET_MODELS[selectedProvider] || []),
    [gatewayModels, selectedProvider],
  )

  const allModels = useMemo(() => {
    const fromEnabled = enabledModels.map((m) => m.trim()).filter(Boolean)
    const remainingPresetModels = backendModels.filter((m) => !fromEnabled.includes(m))
    return [...fromEnabled, ...remainingPresetModels]
  }, [enabledModels, backendModels])

  const fallbackModels = useMemo(
    () => enabledModels.filter((m) => m !== primaryModel),
    [enabledModels, primaryModel],
  )

  const toggleModel = (modelName: string, checked: boolean) => {
    const value = modelName.trim()
    if (!value) return
    setEnabledModels((prev) => {
      if (checked) return Array.from(new Set([...prev, value]))
      const next = prev.filter((m) => m !== value)
      if (next.length === 0) return prev
      if (!next.includes(primaryModel)) {
        setPrimaryModel(next[0])
        setModel(next[0])
      }
      return next
    })
  }

  const moveModel = (modelName: string, direction: -1 | 1) => {
    setEnabledModels((prev) => {
      const idx = prev.indexOf(modelName)
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

  const toggleProvider = (providerName: string, checked: boolean) => {
    const value = providerName.trim()
    if (!value) return
    setEnabledProviders((prev) => {
      if (checked) return Array.from(new Set([...prev, value]))
      const next = prev.filter((p) => p !== value)
      return next.length ? next : prev
    })
  }

  const toggleCsvgenOnly = (checked: boolean) => {
    setUseCsvgenOnly(checked)
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

  useEffect(() => {
    const timer = window.setTimeout(() => {
      void load()
    }, 0)
    return () => window.clearTimeout(timer)
  }, [load])

  const save = async () => {
    if (!token) return
    setBusy(true)
    try {
      const metadataProvider = useCsvgenOnly ? "csvgen" : selectedProvider
      const metadataModel = useCsvgenOnly ? CSVGEN_METADATA_MODELS[2] : model
      
      const r = await adminFetch("/api/config/generation", token, {
        method: "POST",
        body: JSON.stringify({
          // Metadata overrides if any
          sub2gen_metadata_backend: metadataProvider,
          sub2gen_metadata_provider_order: providerOrder.join(","),
          sub2gen_metadata_enabled_providers: enabledProviders.join(","),
          sub2gen_metadata_provider_retry_count: providerRetryCount,
          sub2gen_metadata_model: metadataModel,
          sub2gen_metadata_enabled_models: enabledModels.join(","),
          sub2gen_metadata_primary_model: primaryModel,
          sub2gen_metadata_fallback_models: fallbackModels.join(","),

          // Cloning (same routing as metadata for this screen)
          sub2gen_cloning_backend: selectedProvider,
          sub2gen_cloning_provider_order: providerOrder.join(","),
          sub2gen_cloning_enabled_providers: enabledProviders.join(","),
          sub2gen_cloning_provider_retry_count: providerRetryCount,
          sub2gen_cloning_model: model,

          // CSVGen specific keys
          sub2gen_csvgen_cookie: csvgenCookie,
          sub2gen_csvgen_api_keys: csvgenApiKeys,

          // Global API Keys
          sub2gen_gemini_api_keys: geminiKeys,
          sub2gen_openai_api_keys: openaiKeys,
          sub2gen_openrouter_api_keys: openrouterKeys,
          sub2gen_third_party_gemini_api_keys: thirdPartyKeys,
          sub2gen_third_party_gemini_base_url: thirdPartyBaseUrl,
          cloudflare_account_id: cloudflareAccountId,
          cloudflare_api_token: cloudflareApiToken,
          
          // Also set the specific keys requested by the original APIs (e.g., Cloning uses specific gemini keys)
          // To unify them, we simply copy the global keys into the specialized ones so legacy APIs keep working.
          sub2gen_cloning_gemini_api_keys: geminiKeys,
          sub2gen_cloning_openai_api_keys: openaiKeys,
          sub2gen_cloning_third_party_gemini_api_keys: thirdPartyKeys,
          sub2gen_cloning_third_party_gemini_base_url: thirdPartyBaseUrl,
          sub2gen_cloning_openrouter_api_keys: openrouterKeys,
          sub2gen_cloning_cloudflare_account_id: cloudflareAccountId,
          sub2gen_cloning_cloudflare_api_token: cloudflareApiToken,
          
        }),
      })
      if (!r) return
      const d = await r.json()
      if (d.success) toast.success("Adobe configurations saved")
      else toast.error(d.message || "Failed")
    } finally {
      setBusy(false)
    }
  }

  if (!active) return null

  return (
    <Card>
      <CardHeader>
        <CardTitle>Adobe Configurations</CardTitle>
        <CardDescription>Configure the global generation backend, models, and credentials.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-6">
        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <Label>Providers (ranked fallback)</Label>
            <div className="flex items-center gap-2">
              <Label htmlFor="csvgen-only" className="cursor-pointer">Use CSVGEN Only for Metadata</Label>
              <Switch id="csvgen-only" checked={useCsvgenOnly} onCheckedChange={toggleCsvgenOnly} />
            </div>
          </div>
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
          <div className="max-w-xs pt-2">
            <Label htmlFor="metadata-provider-retries">Retry per provider before fallback</Label>
            <Input
              id="metadata-provider-retries"
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
          <Label>Models (preset)</Label>
          <div className="rounded-md border overflow-hidden">
            <div className="grid grid-cols-[1fr_90px_90px_110px] gap-2 border-b bg-muted/40 px-3 py-2 text-xs font-medium">
              <span>Model</span>
              <span className="text-center">Enabled</span>
              <span className="text-center">Primary</span>
              <span className="text-center">Order</span>
            </div>
            <div className="max-h-[300px] overflow-y-auto">
              {allModels.map((entry) => {
                const enabled = enabledModels.includes(entry)
                return (
                  <div key={entry} className="grid grid-cols-[1fr_90px_90px_110px] items-center gap-2 border-b last:border-0 px-3 py-2 text-sm hover:bg-muted/20 transition-colors">
                    <span className="font-mono truncate" title={entry}>{entry}</span>
                    <div className="flex justify-center">
                      <input
                        type="checkbox"
                        className="h-4 w-4 accent-primary rounded cursor-pointer"
                        checked={enabled}
                        onChange={(e) => toggleModel(entry, e.target.checked)}
                      />
                    </div>
                    <div className="flex justify-center">
                      <input
                        type="radio"
                        name="primary-model"
                        className="h-4 w-4 accent-primary"
                        checked={primaryModel === entry}
                        disabled={!enabled}
                        onChange={() => {
                          setPrimaryModel(entry)
                          setModel(entry)
                        }}
                      />
                    </div>
                    <div className="flex gap-1 justify-center">
                      <Button type="button" size="sm" variant="outline" disabled={!enabled} onClick={() => moveModel(entry, -1)} className="h-7 px-2">Up</Button>
                      <Button type="button" size="sm" variant="outline" disabled={!enabled} onClick={() => moveModel(entry, 1)} className="h-7 px-2">Down</Button>
                    </div>
                  </div>
                )
              })}
            </div>
          </div>

          <p className="text-xs text-muted-foreground mt-2">
            Fallback order follows enabled model order excluding primary.
          </p>
        </div>





        <div className="space-y-4 rounded-md border p-4 bg-muted/10">
          <div>
            <h3 className="font-semibold text-lg flex items-center gap-2">
              <Key className="w-5 h-5 text-primary" /> Provider Credentials
            </h3>
            <p className="text-xs text-muted-foreground mt-1">Configure your generation provider credentials globally. You can specify <strong>multiple API keys</strong> separated by commas for load balancing.</p>
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
                  <TableCell className="text-muted-foreground font-mono text-[11px]">SUB2GEN_GEMINI_API_KEYS</TableCell>
                  <TableCell>
                    <Textarea className="font-mono text-xs min-h-[60px] bg-muted/20 resize-y" placeholder="AIzaSy..., AIzaSy..." value={geminiKeys} onChange={(e) => setGeminiKeys(e.target.value)} />
                  </TableCell>
                </TableRow>
                <TableRow>
                  <TableCell className="font-medium">OpenAI</TableCell>
                  <TableCell className="text-muted-foreground font-mono text-[11px]">SUB2GEN_OPENAI_API_KEYS</TableCell>
                  <TableCell>
                    <Textarea className="font-mono text-xs min-h-[60px] bg-muted/20 resize-y" placeholder="sk-proj-..., sk-proj-..." value={openaiKeys} onChange={(e) => setOpenaiKeys(e.target.value)} />
                  </TableCell>
                </TableRow>
                <TableRow>
                  <TableCell className="font-medium">OpenRouter</TableCell>
                  <TableCell className="text-muted-foreground font-mono text-[11px]">SUB2GEN_OPENROUTER_API_KEYS</TableCell>
                  <TableCell>
                    <Textarea className="font-mono text-xs min-h-[60px] bg-muted/20 resize-y" placeholder="sk-or-v1-..., sk-or-v1-..." value={openrouterKeys} onChange={(e) => setOpenrouterKeys(e.target.value)} />
                  </TableCell>
                </TableRow>
                <TableRow>
                  <TableCell className="font-medium">Third-Party Gemini</TableCell>
                  <TableCell className="text-muted-foreground font-mono text-[11px]">SUB2GEN_THIRD_PARTY_GEMINI_API_KEYS</TableCell>
                  <TableCell>
                    <Textarea className="font-mono text-xs min-h-[60px] bg-muted/20 resize-y" placeholder="Key1, Key2..." value={thirdPartyKeys} onChange={(e) => setThirdPartyKeys(e.target.value)} />
                  </TableCell>
                </TableRow>
                <TableRow>
                  <TableCell className="font-medium text-muted-foreground text-xs pl-8">↳ Base URL</TableCell>
                  <TableCell className="text-muted-foreground font-mono text-[11px]">SUB2GEN_THIRD_PARTY_GEMINI_BASE_URL</TableCell>
                  <TableCell>
                    <Input className="font-mono text-xs h-8 bg-muted/20" placeholder="https://..." value={thirdPartyBaseUrl} onChange={(e) => setThirdPartyBaseUrl(e.target.value)} />
                  </TableCell>
                </TableRow>
                <TableRow>
                  <TableCell className="font-medium">Cloudflare</TableCell>
                  <TableCell className="text-muted-foreground font-mono text-[11px]">CLOUDFLARE_ACCOUNT_ID</TableCell>
                  <TableCell>
                    <Input className="font-mono text-xs h-8 bg-muted/20" placeholder="Account ID..." value={cloudflareAccountId} onChange={(e) => setCloudflareAccountId(e.target.value)} />
                  </TableCell>
                </TableRow>
                <TableRow>
                  <TableCell className="font-medium text-muted-foreground text-xs pl-8">↳ API Token</TableCell>
                  <TableCell className="text-muted-foreground font-mono text-[11px]">CLOUDFLARE_API_TOKEN</TableCell>
                  <TableCell>
                    <Input className="font-mono text-xs h-8 bg-muted/20" placeholder="API Token..." value={cloudflareApiToken} onChange={(e) => setCloudflareApiToken(e.target.value)} />
                  </TableCell>
                </TableRow>
                {useCsvgenOnly && (
                  <>
                    <TableRow className="bg-primary/5">
                      <TableCell className="font-medium text-primary">CSVGEN</TableCell>
                      <TableCell className="text-muted-foreground font-mono text-[11px]">SUB2GEN_CSVGEN_COOKIE</TableCell>
                      <TableCell>
                        <Input className="font-mono text-xs h-8 bg-muted/20" placeholder="Cookie string..." value={csvgenCookie} onChange={(e) => setCsvgenCookie(e.target.value)} />
                      </TableCell>
                    </TableRow>
                    <TableRow className="bg-primary/5">
                      <TableCell className="font-medium text-primary text-xs pl-8">↳ Native API keys</TableCell>
                      <TableCell className="text-muted-foreground font-mono text-[11px]">SUB2GEN_CSVGEN_API_KEYS</TableCell>
                      <TableCell>
                        <Textarea
                          className="font-mono text-xs min-h-[60px] bg-muted/20 resize-y"
                          placeholder="Comma-separated keys (csvgen Settings → Metadata, Native tab)…"
                          value={csvgenApiKeys}
                          onChange={(e) => setCsvgenApiKeys(e.target.value)}
                        />
                        <p className="text-[11px] text-muted-foreground mt-1">Required if csvgen returns &quot;At least one API key is required&quot;. Same keys as in the csvgen web app.</p>
                      </TableCell>
                    </TableRow>
                  </>
                )}
              </TableBody>
            </Table>
          </div>
        </div>
        
        <Button onClick={save} disabled={busy} className="w-full sm:w-auto mt-4">Save configurations</Button>
      </CardContent>
    </Card>
  )
}
