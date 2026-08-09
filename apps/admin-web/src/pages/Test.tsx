import { useState, useEffect, useRef, useCallback } from "react"
import { Layout } from "../components/Layout"
import { Button } from "../components/ui/button"
import { Input } from "../components/ui/input"
import { Label } from "../components/ui/label"
import { ScrollArea } from "../components/ui/scroll-area"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../components/ui/select"
import { toast } from "sonner"
import { Play, UploadCloud, X, Beaker, CheckCircle2, Loader2, AlertCircle } from "lucide-react"
import { cn } from "@/lib/utils"

const MODEL_CATEGORIES = [
  { name: "Gemini 3.1 Flash Image", filter: (m: string) => m.startsWith("gemini-3.1-flash-image") },
  { name: "Gemini 3.0 Pro Image", filter: (m: string) => m.startsWith("gemini-3.0-pro-image") },
  { name: "Gemini 2.5 Flash Image", filter: (m: string) => m.startsWith("gemini-2.5-flash-image") },
  { name: "Imagen 4.0 Image", filter: (m: string) => m.startsWith("imagen-4.0") },
  { name: "Veo 3.1 Text-to-Video (T2V)", filter: (m: string) => m.startsWith("veo_3_1_t2v") && !m.includes("4k") && !m.includes("1080p") },
  { name: "Veo 3.1 Image-to-Video (I2V)", filter: (m: string) => m.startsWith("veo_3_1_i2v") && !m.includes("4k") && !m.includes("1080p") },
  { name: "Veo 3.1 Multi-Image-to-Video (R2V)", filter: (m: string) => m.startsWith("veo_3_1_r2v") && !m.includes("4k") && !m.includes("1080p") },
  { name: "Veo 2.x Video", filter: (m: string) => m.startsWith("veo_2") },
  { name: "Video Upsample", filter: (m: string) => m.includes("4k") || m.includes("1080p") },
]

const FALLBACK_MODELS: Record<string, string> = {
  "gemini-3.1-flash-image": "Image generation (alias) - aspects: landscape, portrait, square, four-three, three-four; sizes: 2k, 4k",
  "gemini-3.0-pro-image": "Image generation (alias) - aspects: landscape, portrait, square, four-three, three-four; sizes: 2k, 4k",
  "gemini-2.5-flash-image": "Image generation (alias) - aspects: landscape, portrait",
  "imagen-4.0-generate-preview": "Image generation (alias) - aspects: landscape, portrait",
  "veo_3_1_t2v_fast": "Video generation (alias) - supports landscape/portrait via generationConfig",
  "veo_3_1_t2v_fast_4s": "Video generation (alias) - 4s, supports landscape/portrait via generationConfig",
  "veo_3_1_t2v_fast_landscape_4s": "Video generation (alias) - 4s explicit landscape variant",
  "veo_3_1_t2v_fast_6s": "Video generation (alias) - 6s, supports landscape/portrait via generationConfig",
  "veo_3_1_t2v_fast_landscape_6s": "Video generation (alias) - 6s explicit landscape variant",
  "veo_3_1_t2v_fast_8s": "Video generation (alias) - 8s, supports landscape/portrait via generationConfig",
  "veo_3_1_t2v_fast_landscape_8s": "Video generation (alias) - 8s explicit landscape variant",
  "veo_3_1_t2v_fast_ultra": "Video generation (alias) - supports landscape/portrait via generationConfig",
  "veo_3_1_t2v_fast_ultra_relaxed": "Video generation (alias) - supports landscape/portrait via generationConfig",
  "veo_3_1_t2v": "Video generation (alias) - supports landscape/portrait via generationConfig",
  "veo_3_1_t2v_4s": "Video generation (alias) - 4s, supports landscape/portrait via generationConfig",
  "veo_3_1_t2v_landscape_4s": "Video generation (alias) - 4s explicit landscape variant",
  "veo_3_1_t2v_6s": "Video generation (alias) - 6s, supports landscape/portrait via generationConfig",
  "veo_3_1_t2v_landscape_6s": "Video generation (alias) - 6s explicit landscape variant",
  "veo_3_1_t2v_8s": "Video generation (alias) - 8s, supports landscape/portrait via generationConfig",
  "veo_3_1_t2v_landscape_8s": "Video generation (alias) - 8s explicit landscape variant",
  "veo_3_1_t2v_4k": "Video upsample alias - generate then upscale to 4K",
  "veo_3_1_t2v_landscape_4k": "Video upsample alias - explicit landscape 4K",
  "veo_3_1_t2v_1080p": "Video upsample alias - generate then upscale to 1080P",
  "veo_3_1_t2v_landscape_1080p": "Video upsample alias - explicit landscape 1080P",
  "veo_3_1_t2v_4s_4k": "Video upsample alias - generate 4s then upscale to 4K",
  "veo_3_1_t2v_landscape_4s_4k": "Video upsample alias - explicit landscape 4s 4K",
  "veo_3_1_t2v_4s_1080p": "Video upsample alias - generate 4s then upscale to 1080P",
  "veo_3_1_t2v_landscape_4s_1080p": "Video upsample alias - explicit landscape 4s 1080P",
  "veo_3_1_t2v_6s_4k": "Video upsample alias - generate 6s then upscale to 4K",
  "veo_3_1_t2v_landscape_6s_4k": "Video upsample alias - explicit landscape 6s 4K",
  "veo_3_1_t2v_6s_1080p": "Video upsample alias - generate 6s then upscale to 1080P",
  "veo_3_1_t2v_landscape_6s_1080p": "Video upsample alias - explicit landscape 6s 1080P",
  "veo_3_1_t2v_8s_4k": "Video upsample alias - generate 8s then upscale to 4K",
  "veo_3_1_t2v_landscape_8s_4k": "Video upsample alias - explicit landscape 8s 4K",
  "veo_3_1_t2v_8s_1080p": "Video upsample alias - generate 8s then upscale to 1080P",
  "veo_3_1_t2v_landscape_8s_1080p": "Video upsample alias - explicit landscape 8s 1080P",
  "veo_3_1_t2v_lite_8s": "Video generation (alias) - lite 8s, supports landscape/portrait via generationConfig",
  "veo_3_1_i2v_s_fast_fl": "Video generation (alias) - supports landscape/portrait via generationConfig",
  "veo_3_1_i2v_s_fast_4s_fl": "Video generation (alias) - 4s, supports landscape/portrait via generationConfig",
  "veo_3_1_i2v_s_fast_landscape_4s_fl": "Video generation (alias) - 4s explicit landscape variant",
  "veo_3_1_i2v_s_fast_6s_fl": "Video generation (alias) - 6s, supports landscape/portrait via generationConfig",
  "veo_3_1_i2v_s_fast_landscape_6s_fl": "Video generation (alias) - 6s explicit landscape variant",
  "veo_3_1_i2v_s_fast_8s_fl": "Video generation (alias) - 8s, supports landscape/portrait via generationConfig",
  "veo_3_1_i2v_s_fast_landscape_8s_fl": "Video generation (alias) - 8s explicit landscape variant",
  "veo_3_1_i2v_s_fast_ultra_fl": "Video generation (alias) - supports landscape/portrait via generationConfig",
  "veo_3_1_i2v_s_fast_ultra_relaxed": "Video generation (alias) - supports landscape/portrait via generationConfig",
  "veo_3_1_i2v_s": "Video generation (alias) - supports landscape/portrait via generationConfig",
  "veo_3_1_i2v_s_4s": "Video generation (alias) - 4s, supports landscape/portrait via generationConfig",
  "veo_3_1_i2v_s_landscape_4s": "Video generation (alias) - 4s explicit landscape variant",
  "veo_3_1_i2v_s_6s": "Video generation (alias) - 6s, supports landscape/portrait via generationConfig",
  "veo_3_1_i2v_s_landscape_6s": "Video generation (alias) - 6s explicit landscape variant",
  "veo_3_1_i2v_s_8s": "Video generation (alias) - 8s, supports landscape/portrait via generationConfig",
  "veo_3_1_i2v_s_landscape_8s": "Video generation (alias) - 8s explicit landscape variant",
  "veo_3_1_i2v_s_4k": "Video upsample alias - generate then upscale to 4K",
  "veo_3_1_i2v_s_landscape_4k": "Video upsample alias - explicit landscape 4K",
  "veo_3_1_i2v_s_1080p": "Video upsample alias - generate then upscale to 1080P",
  "veo_3_1_i2v_s_landscape_1080p": "Video upsample alias - explicit landscape 1080P",
  "veo_3_1_i2v_s_4s_4k": "Video upsample alias - generate 4s then upscale to 4K",
  "veo_3_1_i2v_s_landscape_4s_4k": "Video upsample alias - explicit landscape 4s 4K",
  "veo_3_1_i2v_s_4s_1080p": "Video upsample alias - generate 4s then upscale to 1080P",
  "veo_3_1_i2v_s_landscape_4s_1080p": "Video upsample alias - explicit landscape 4s 1080P",
  "veo_3_1_i2v_s_6s_4k": "Video upsample alias - generate 6s then upscale to 4K",
  "veo_3_1_i2v_s_landscape_6s_4k": "Video upsample alias - explicit landscape 6s 4K",
  "veo_3_1_i2v_s_6s_1080p": "Video upsample alias - generate 6s then upscale to 1080P",
  "veo_3_1_i2v_s_landscape_6s_1080p": "Video upsample alias - explicit landscape 6s 1080P",
  "veo_3_1_i2v_s_8s_4k": "Video upsample alias - generate 8s then upscale to 4K",
  "veo_3_1_i2v_s_landscape_8s_4k": "Video upsample alias - explicit landscape 8s 4K",
  "veo_3_1_i2v_s_8s_1080p": "Video upsample alias - generate 8s then upscale to 1080P",
  "veo_3_1_i2v_s_landscape_8s_1080p": "Video upsample alias - explicit landscape 8s 1080P",
  "veo_3_1_i2v_lite_8s": "Video generation (alias) - lite 8s, supports landscape/portrait via generationConfig",
  "veo_3_1_interpolation_lite_8s": "Video generation (alias) - interpolation lite 8s, supports landscape/portrait via generationConfig",
  "veo_3_1_r2v_fast": "Video generation (alias) - supports landscape/portrait via generationConfig",
  "veo_3_1_r2v_fast_landscape": "Video generation (alias) - explicit landscape variant",
  "veo_3_1_r2v_fast_8s": "Video generation (alias) - R2V fast 8s, supports landscape/portrait via generationConfig",
  "veo_3_1_r2v_fast_landscape_8s": "Video generation (alias) - R2V fast 8s explicit landscape variant",
  "veo_3_1_r2v_fast_ultra": "Video generation (alias) - supports landscape/portrait via generationConfig",
  "veo_3_1_r2v_fast_landscape_ultra": "Video generation (alias) - explicit landscape ultra variant",
  "veo_3_1_r2v_fast_ultra_8s": "Video generation (alias) - R2V fast ultra 8s, supports landscape/portrait via generationConfig",
  "veo_3_1_r2v_fast_landscape_ultra_8s": "Video generation (alias) - R2V fast ultra 8s explicit landscape variant",
  "veo_3_1_r2v_fast_ultra_relaxed": "Video generation (alias) - supports landscape/portrait via generationConfig",
  "veo_3_1_r2v_fast_landscape_ultra_relaxed": "Video generation (alias) - explicit landscape ultra relaxed variant",
  "veo_3_1_r2v_fast_ultra_relaxed_8s": "Video generation (alias) - R2V fast ultra relaxed 8s, supports landscape/portrait via generationConfig",
  "veo_3_1_r2v_fast_landscape_ultra_relaxed_8s": "Video generation (alias) - R2V fast ultra relaxed 8s explicit landscape variant",
  "veo_3_1_r2v_fast_landscape_ultra_4k": "Video upsample alias - explicit landscape ultra 4K",
  "veo_3_1_r2v_fast_landscape_ultra_1080p": "Video upsample alias - explicit landscape ultra 1080P",
}

type FlowProject = {
  project_id: string
  project_name: string
  token_id: number
  is_active: boolean
}

const AUTOMATIC_PROJECT = "__automatic__"

function getModelType(modelId: string) {
  if (modelId.includes("image") || modelId.startsWith("imagen")) return "image"
  return "video"
}

function getModelMeta(modelId: string) {
  const isI2V = modelId.includes("i2v")
  const isR2V = modelId.includes("r2v")
  const isImage = getModelType(modelId) === "image"

  if (isImage) return { type: "image" as const, supportsImages: true, minImages: 0, maxImages: 5 }
  if (isI2V) return { type: "video" as const, supportsImages: true, minImages: 1, maxImages: 2 }
  if (isR2V) return { type: "video" as const, supportsImages: true, minImages: 0, maxImages: 3 }
  return { type: "video" as const, supportsImages: false, minImages: 0, maxImages: 0 }
}

function shortModelLabel(modelId: string) {
  return modelId
    .replace(/^gemini-3\.1-flash-image-/, "")
    .replace(/^gemini-3\.0-pro-image-/, "")
    .replace(/^gemini-2\.5-flash-image-/, "")
    .replace(/^imagen-4\.0-generate-preview-/, "") || modelId
}

export default function TestPage() {
  const [baseUrl, setBaseUrl] = useState("")
  const [apiKey, setApiKey] = useState("")
  const [prompt, setPrompt] = useState(
    "A cute orange cat napping on a windowsill in the sun, cherry blossoms outside in spring."
  )
  const [models, setModels] = useState<Record<string, string>>({})
  const [selectedModel, setSelectedModel] = useState<string | null>(null)
  const [projects, setProjects] = useState<FlowProject[]>([])
  const [selectedProject, setSelectedProject] = useState(AUTOMATIC_PROJECT)
  const [projectsLoading, setProjectsLoading] = useState(false)
  const [projectsError, setProjectsError] = useState("")
  const [images, setImages] = useState<string[]>([])

  const [generating, setGenerating] = useState(false)
  const [outputLog, setOutputLog] = useState("Ready — pick a model or use the built-in list when API Key is missing.\n")
  const [outputResultHTML, setOutputResultHTML] = useState("")
  const [status, setStatus] = useState<"idle" | "running" | "done" | "error">("idle")
  const [timeElapsed, setTimeElapsed] = useState(0)
  const [uploadHover, setUploadHover] = useState(false)

  const fileInputRef = useRef<HTMLInputElement>(null)
  const logRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    setBaseUrl(window.location.origin)
  }, [])

  const loadModels = useCallback(async () => {
    const bu = baseUrl.trim()
    const key = apiKey.trim()
    if (!bu || !key) {
      setModels({ ...FALLBACK_MODELS })
      if (!key) console.warn("Using built-in candidate models (no API key)")
      return
    }
    try {
      const resp = await fetch(`${bu}/v1/models`, {
        headers: { Authorization: `Bearer ${key}` },
      })
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
      const data = await resp.json()
      const items = Array.isArray(data.data) ? data.data : []
      if (!items.length) {
        setModels({ ...FALLBACK_MODELS })
        toast.error("Empty catalog — using built-in candidates")
        return
      }
      const modelMap: Record<string, string> = {}
      items.forEach((m: { id: string; description?: string }) => {
        modelMap[m.id] = m.description || ""
      })
      setModels(modelMap)
    } catch (e) {
      console.warn("Load models failed, using fallback", e)
      setModels({ ...FALLBACK_MODELS })
      toast.error("Could not load /v1/models — using built-in candidates")
    }
  }, [baseUrl, apiKey])

  const loadProjects = useCallback(async () => {
    const bu = baseUrl.trim()
    const key = apiKey.trim()
    if (!bu || !key) {
      setProjects([])
      setSelectedProject(AUTOMATIC_PROJECT)
      setProjectsError("")
      return
    }
    setProjectsLoading(true)
    setProjectsError("")
    try {
      const resp = await fetch(`${bu}/v1/projects?limit=100`, {
        headers: { Authorization: `Bearer ${key}` },
      })
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
      const data = await resp.json()
      const items = (Array.isArray(data.data) ? data.data : []) as FlowProject[]
      const activeProjects = items.filter((project) => project.is_active)
      setProjects(activeProjects)
      setSelectedProject((current) =>
        current === AUTOMATIC_PROJECT || activeProjects.some((project) => project.project_id === current)
          ? current
          : AUTOMATIC_PROJECT
      )
    } catch (error) {
      console.warn("Load projects failed", error)
      setProjects([])
      setSelectedProject(AUTOMATIC_PROJECT)
      setProjectsError("Could not load projects. Enable the projects:read scope for this API key.")
    } finally {
      setProjectsLoading(false)
    }
  }, [baseUrl, apiKey])

  useEffect(() => {
    if (!baseUrl) return
    const t = window.setTimeout(() => {
      void loadModels()
      void loadProjects()
    }, 400)
    return () => window.clearTimeout(t)
  }, [baseUrl, apiKey, loadModels, loadProjects])

  const ingestFiles = (files: FileList | File[]) => {
    const meta = selectedModel ? getModelMeta(selectedModel) : null
    const max = meta?.maxImages ?? 5
    for (const file of Array.from(files)) {
      if (!file.type.startsWith("image/")) continue
      const reader = new FileReader()
      reader.onload = (ev) => {
        const r = ev.target?.result
        if (typeof r !== "string") return
        setImages((prev) => (prev.length < max ? [...prev, r] : prev))
      }
      reader.readAsDataURL(file)
    }
  }

  const handleFileUpload = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files) ingestFiles(e.target.files)
    e.target.value = ""
  }

  const appendLog = (text: string) => {
    setOutputLog((prev) => prev + text)
    requestAnimationFrame(() => {
      if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight
    })
  }

  const renderResult = (content: string) => {
    let html = ""
    const imgRegex = /!\[.*?\]\((.*?)\)/g
    let match
    let hasMedia = false
    while ((match = imgRegex.exec(content)) !== null) {
      html += `<img src="${match[1]}" alt="Generated" class="max-w-full rounded-lg mt-2 shadow-md" loading="lazy" />`
      hasMedia = true
    }

    const videoRegex = /<video[^>]+src=['"]([^'"]+)['"]/gi
    while ((match = videoRegex.exec(content)) !== null) {
      html += `<video src="${match[1]}" controls autoplay loop class="max-w-full rounded-lg mt-2 shadow-md"></video>`
      hasMedia = true
    }

    if (!hasMedia && content.trim()) {
      const esc = content.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      html += `<pre class="text-sm text-muted-foreground whitespace-pre-wrap break-all">${esc}</pre>`
    }
    setOutputResultHTML(html)
  }

  const generate = async () => {
    if (!selectedModel || generating) return
    if (!prompt.trim()) {
      toast.error("Enter a prompt")
      return
    }
    if (!apiKey.trim()) {
      toast.error("Enter API Key")
      return
    }
    if (!baseUrl.trim()) {
      toast.error("Enter base URL")
      return
    }

    setGenerating(true)
    setStatus("running")
    setOutputLog("")
    setOutputResultHTML("")
    setTimeElapsed(0)

    const startTime = Date.now()

    const contentArr: { type: string; text?: string; image_url?: { url: string } }[] = [{ type: "text", text: prompt }]
    images.forEach((img) => {
      contentArr.push({ type: "image_url", image_url: { url: img } })
    })

    const messages = [{ role: "user", content: contentArr.length === 1 ? prompt : contentArr }]
    const body = {
      model: selectedModel,
      messages,
      stream: true,
      ...(selectedProject !== AUTOMATIC_PROJECT ? { project_id: selectedProject } : {}),
    }

    const logTimeOpts = { hour: "2-digit" as const, minute: "2-digit" as const, second: "2-digit" as const, hour12: false }
    const logTime = new Date().toLocaleTimeString("en-US", logTimeOpts)
    appendLog(`[${logTime}] Model: ${selectedModel}\n`)
    appendLog(`[${logTime}] Project: ${selectedProject === AUTOMATIC_PROJECT ? "Automatic" : selectedProject}\n`)
    appendLog(`[${logTime}] Prompt: ${prompt.substring(0, 100)}${prompt.length > 100 ? "..." : ""}\n`)
    appendLog(`[${logTime}] Starting request...\n`)

    try {
      const resp = await fetch(`${baseUrl.trim()}/v1/chat/completions`, {
        method: "POST",
        headers: { Authorization: `Bearer ${apiKey.trim()}`, "Content-Type": "application/json" },
        body: JSON.stringify(body),
      })

      if (!resp.ok) {
        const errText = await resp.text()
        throw new Error(`HTTP ${resp.status}: ${errText}`)
      }

      if (!resp.body) throw new Error("No response body")

      const reader = resp.body.getReader()
      const decoder = new TextDecoder()
      let fullContent = ""
      let buffer = ""

      while (true) {
        const { done, value } = await reader.read()
        if (done) break

        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split("\n")
        buffer = lines.pop() || ""

        for (const line of lines) {
          if (!line.startsWith("data: ")) continue
          const data = line.slice(6).trim()
          if (data === "[DONE]") continue

          try {
            const parsed = JSON.parse(data) as { error?: unknown; choices?: { delta?: { reasoning_content?: string; content?: string } }[] }
            if (parsed.error) {
              appendLog(`\n❌ Error: ${JSON.stringify(parsed.error)}\n`)
              continue
            }
            const choices = parsed.choices || []
            if (choices.length > 0) {
              const delta = choices[0].delta || {}
              const c = delta.reasoning_content || delta.content || ""
              if (c) {
                fullContent += c
                appendLog(c)
              }
            }
          } catch {
            // skip chunk
          }
        }
      }

      const elapsed = ((Date.now() - startTime) / 1000).toFixed(1)
      appendLog(
        `\n\n[${new Date().toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false })}] Done in ${elapsed}s\n`
      )
      renderResult(fullContent)
      setStatus("done")
      setTimeElapsed(Number(elapsed))
    } catch (e: unknown) {
      appendLog(`\n❌ Request failed: ${e instanceof Error ? e.message : String(e)}\n`)
      setStatus("error")
    } finally {
      setGenerating(false)
    }
  }

  const activeMeta = selectedModel ? getModelMeta(selectedModel) : null

  return (
    <Layout>
      <div className="flex flex-col gap-6 animate-in fade-in duration-500">
        <div className="text-center">
          <div className="inline-flex items-center justify-center p-3 bg-primary/10 rounded-full mb-4">
            <Beaker className="h-8 w-8 text-primary" />
          </div>
          <h1 className="text-3xl font-bold tracking-tight">sub2gen model test</h1>
          <p className="text-muted-foreground mt-2">Pick a model, enter a prompt, and run a streamed completion.</p>
        </div>

        <div className="flex flex-wrap gap-4 items-end bg-card p-5 border rounded-xl shadow-xs">
          <div className="flex-1 min-w-[250px] space-y-2">
            <Label>API Key</Label>
            <Input type="password" value={apiKey} onChange={(e) => setApiKey(e.target.value)} placeholder="API key" />
          </div>
          <div className="flex-1 min-w-[250px] space-y-2">
            <Label>Base URL</Label>
            <Input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder="http://localhost:8000" />
          </div>
          <div className="flex-1 min-w-[250px] space-y-2">
            <Label>Flow project</Label>
            <Select value={selectedProject} onValueChange={setSelectedProject} disabled={projectsLoading || !apiKey.trim()}>
              <SelectTrigger>
                <SelectValue placeholder={projectsLoading ? "Loading projects…" : "Automatic"} />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={AUTOMATIC_PROJECT}>Automatic (recommended)</SelectItem>
                {projects.map((project) => (
                  <SelectItem key={project.project_id} value={project.project_id}>
                    {project.project_name || project.project_id} · account #{project.token_id}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {projectsError && <p className="text-xs text-destructive">{projectsError}</p>}
          </div>
          <Button type="button" onClick={() => { void loadModels(); void loadProjects() }} variant="secondary">
            Refresh
          </Button>
        </div>
        <p className="text-xs text-muted-foreground -mt-2">
          Without an API key or if the catalog request fails, the built-in candidate models are shown. You still need a valid API key to call{" "}
          <code className="bg-muted px-1 rounded">/v1/chat/completions</code>.
        </p>

        <div className="grid grid-cols-1 lg:grid-cols-4 gap-6">
          <div className="col-span-1 border rounded-xl bg-card shadow-xs overflow-hidden flex flex-col h-[700px]">
            <div className="p-4 bg-muted/50 border-b font-semibold">Models</div>
            <ScrollArea className="flex-1">
              <div className="p-2 space-y-4">
                {Object.keys(models).length === 0 ? (
                  <div className="text-sm text-muted-foreground p-4 text-center mt-10">No models — set Base URL</div>
                ) : (
                  MODEL_CATEGORIES.map((cat) => {
                    const catModels = Object.keys(models).filter(cat.filter).sort()
                    if (!catModels.length) return null
                    return (
                      <div key={cat.name} className="space-y-1">
                        <div className="px-2 py-1 text-xs font-semibold text-primary">
                          {cat.name} ({catModels.length})
                        </div>
                        {catModels.map((m) => (
                          <div
                            key={m}
                            role="button"
                            tabIndex={0}
                            onKeyDown={(e) => {
                              if (e.key === "Enter" || e.key === " ") {
                                e.preventDefault()
                                setSelectedModel(m)
                                setImages([])
                              }
                            }}
                            onClick={() => {
                              setSelectedModel(m)
                              setImages([])
                            }}
                            className={cn(
                              "px-3 py-2 text-sm rounded-md cursor-pointer transition-colors flex justify-between items-center gap-2",
                              selectedModel === m ? "bg-primary text-primary-foreground" : "hover:bg-muted"
                            )}
                          >
                            <span className="truncate pr-2 font-mono text-xs" title={m}>
                              {shortModelLabel(m)}
                            </span>
                            <span
                              className={cn(
                                "text-[10px] px-1.5 py-0.5 rounded shrink-0",
                                getModelType(m) === "image"
                                  ? "bg-green-500/20 text-green-700 dark:text-green-400"
                                  : "bg-orange-500/20 text-orange-600 dark:text-orange-400"
                              )}
                            >
                              {getModelType(m)}
                            </span>
                          </div>
                        ))}
                      </div>
                    )
                  })
                )}
              </div>
            </ScrollArea>
          </div>

          <div className="col-span-1 lg:col-span-3 space-y-6">
            <div className="border rounded-xl bg-card p-6 shadow-xs space-y-6">
              <div>
                <h3 className="font-semibold text-lg">{selectedModel || "Select a model"}</h3>
                {selectedModel && <p className="text-xs text-muted-foreground mt-1">{models[selectedModel]}</p>}
              </div>

              <div className="space-y-2">
                <Label>Prompt</Label>
                <textarea
                  className="w-full h-32 p-3 rounded-md border border-input bg-background text-sm focus:ring-2 focus:ring-primary focus:outline-hidden resize-y"
                  value={prompt}
                  onChange={(e) => setPrompt(e.target.value)}
                  placeholder="Describe what you want to generate…"
                />
              </div>

              {activeMeta?.supportsImages && (
                <div className="space-y-2">
                  <Label>
                    Images{" "}
                    <span className="text-xs text-muted-foreground font-normal">
                      (
                      {activeMeta.type === "image"
                        ? "Optional, image-to-image"
                        : `${activeMeta.minImages}-${activeMeta.maxImages} image(s)`}
                      )
                    </span>
                  </Label>
                  <div
                    role="presentation"
                    className={cn(
                      "border-2 border-dashed rounded-lg p-6 flex flex-col items-center justify-center text-center cursor-pointer transition-colors bg-muted/20",
                      uploadHover ? "border-primary" : "border-input"
                    )}
                    onClick={() => fileInputRef.current?.click()}
                    onDragOver={(e) => {
                      e.preventDefault()
                      setUploadHover(true)
                    }}
                    onDragLeave={() => setUploadHover(false)}
                    onDrop={(e) => {
                      e.preventDefault()
                      setUploadHover(false)
                      if (e.dataTransfer.files?.length) ingestFiles(e.dataTransfer.files)
                    }}
                  >
                    <UploadCloud className="h-8 w-8 text-muted-foreground mb-2" />
                    <span className="text-sm font-medium">Click or drop images here</span>
                    <span className="text-xs text-muted-foreground mt-1">PNG, JPG, WebP</span>
                  </div>
                  <input type="file" ref={fileInputRef} hidden accept="image/*" multiple onChange={handleFileUpload} />

                  {images.length > 0 && (
                    <div className="flex gap-4 mt-4 flex-wrap">
                      {images.map((img, i) => (
                        <div key={i} className="relative group rounded-md overflow-hidden border">
                          <img src={img} alt="" className="w-20 h-20 object-cover" />
                          <button
                            type="button"
                            className="absolute top-1 right-1 bg-black/60 text-white rounded-full p-1 opacity-0 group-hover:opacity-100 transition-opacity"
                            onClick={(e) => {
                              e.stopPropagation()
                              setImages((imgs) => imgs.filter((_, idx) => idx !== i))
                            }}
                          >
                            <X className="h-3 w-3" />
                          </button>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}

              <Button className="w-full h-12 text-md gap-2" onClick={() => void generate()} disabled={!selectedModel || generating}>
                {generating ? (
                  <>
                    <Loader2 className="h-5 w-5 animate-spin" /> Generating…
                  </>
                ) : (
                  <>
                    <Play className="h-5 w-5 fill-current" />{" "}
                    {activeMeta?.type === "image" ? "Generate image" : "Generate video"}
                  </>
                )}
              </Button>
            </div>

            <div className="border rounded-xl bg-card p-6 shadow-xs">
              <div className="flex items-center justify-between mb-4">
                <h3 className="font-semibold text-lg flex items-center gap-2">
                  Output
                  {status === "running" && (
                    <span className="flex h-3 w-3 relative">
                      <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-yellow-400 opacity-75" />
                      <span className="relative inline-flex rounded-full h-3 w-3 bg-yellow-500" />
                    </span>
                  )}
                  {status === "done" && <CheckCircle2 className="h-4 w-4 text-green-500" />}
                  {status === "error" && <AlertCircle className="h-4 w-4 text-red-500" />}
                </h3>
                {status === "done" && <span className="text-xs text-muted-foreground">{timeElapsed}s</span>}
              </div>
              <div
                ref={logRef}
                className="bg-black text-green-400 p-4 rounded-md font-mono text-xs h-48 overflow-y-auto whitespace-pre-wrap break-all"
              >
                {outputLog}
              </div>

              {outputResultHTML ? (
                <div className="mt-6 border-t pt-6">
                  <h3 className="font-semibold text-lg mb-4">Result</h3>
                  <div
                    dangerouslySetInnerHTML={{ __html: outputResultHTML }}
                    className="flex flex-col items-center justify-center min-h-[200px] bg-muted/20 rounded-lg p-4"
                  />
                </div>
              ) : null}
            </div>
          </div>
        </div>
      </div>
    </Layout>
  )
}
