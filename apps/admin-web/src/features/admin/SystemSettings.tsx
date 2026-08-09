import { useState, useEffect, useCallback, useRef } from "react"
import { useAuth } from "../../contexts/AuthContext"
import { adminFetch, adminJson } from "../../lib/adminApi"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../../components/ui/card"
import { Button } from "../../components/ui/button"
import { Input } from "../../components/ui/input"
import { Label } from "../../components/ui/label"
import { Switch } from "../../components/ui/switch"
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectSeparator,
  SelectTrigger,
  SelectValue,
} from "../../components/ui/select"
import { toast } from "sonner"
import { Cloud, Download, Loader2, RefreshCw, Trash2, Upload } from "lucide-react"

type CaptchaForm = {
  captcha_method: string
  yescaptcha_api_key: string
  yescaptcha_base_url: string
  yescaptcha_task_type: string
  capmonster_api_key: string
  capmonster_base_url: string
  capmonster_min_score: number
  ezcaptcha_api_key: string
  ezcaptcha_base_url: string
  capsolver_api_key: string
  capsolver_base_url: string
  remote_browser_base_url: string
  remote_browser_api_key: string
  remote_browser_timeout: number
  browser_fallback_to_remote_browser: boolean
  browser_captcha_page_url: string
  browser_proxy_enabled: boolean
  browser_proxy_url: string
  browser_count: number
  browser_personal_fresh_restart_every_n_solves: number
  personal_project_pool_size: number
  personal_max_resident_tabs: number
  personal_idle_tab_ttl_seconds: number
  session_refresh_enabled: boolean
  session_refresh_browser_first: boolean
  session_refresh_inject_st_cookie: boolean
  session_refresh_warmup_urls: string
  session_refresh_wait_seconds_per_url: number
  session_refresh_overall_timeout_seconds: number
  session_refresh_update_st_from_cookie: boolean
  session_refresh_fail_if_st_refresh_fails: boolean
  session_refresh_local_only: boolean
  session_refresh_scheduler_enabled: boolean
  session_refresh_scheduler_interval_minutes: number
  session_refresh_scheduler_batch_size: number
  session_refresh_scheduler_only_expiring_within_minutes: number
  st_only_refresh_scheduler_enabled: boolean
  st_only_refresh_scheduler_interval_minutes: number
  st_only_refresh_scheduler_batch_size: number
  st_only_refresh_scheduler_expiring_within_minutes: number
  extension_queue_wait_timeout_seconds: number
  extension_fallback_to_managed_on_dedicated_failure: boolean
  personal_proxy_enabled: boolean
  personal_proxy_url: string
}

const defaultCaptcha: CaptchaForm = {
  captcha_method: "yescaptcha",
  yescaptcha_api_key: "",
  yescaptcha_base_url: "https://api.yescaptcha.com",
  yescaptcha_task_type: "RecaptchaV3TaskProxylessM1",
  capmonster_api_key: "",
  capmonster_base_url: "https://api.capmonster.cloud",
  capmonster_min_score: 0.9,
  ezcaptcha_api_key: "",
  ezcaptcha_base_url: "https://api.ez-captcha.com",
  capsolver_api_key: "",
  capsolver_base_url: "https://api.capsolver.com",
  remote_browser_base_url: "",
  remote_browser_api_key: "",
  remote_browser_timeout: 60,
  browser_fallback_to_remote_browser: true,
  browser_captcha_page_url: "https://labs.google/fx/api/auth/providers",
  browser_proxy_enabled: false,
  browser_proxy_url: "",
  browser_count: 1,
  browser_personal_fresh_restart_every_n_solves: 10,
  personal_project_pool_size: 4,
  personal_max_resident_tabs: 5,
  personal_idle_tab_ttl_seconds: 600,
  session_refresh_enabled: true,
  session_refresh_browser_first: true,
  session_refresh_inject_st_cookie: true,
  session_refresh_warmup_urls: "https://labs.google/fx/tools/flow,https://labs.google/fx",
  session_refresh_wait_seconds_per_url: 60,
  session_refresh_overall_timeout_seconds: 180,
  session_refresh_update_st_from_cookie: true,
  session_refresh_fail_if_st_refresh_fails: true,
  session_refresh_local_only: true,
  session_refresh_scheduler_enabled: false,
  session_refresh_scheduler_interval_minutes: 30,
  session_refresh_scheduler_batch_size: 10,
  session_refresh_scheduler_only_expiring_within_minutes: 60,
  st_only_refresh_scheduler_enabled: false,
  st_only_refresh_scheduler_interval_minutes: 5,
  st_only_refresh_scheduler_batch_size: 20,
  st_only_refresh_scheduler_expiring_within_minutes: 5,
  extension_queue_wait_timeout_seconds: 20,
  extension_fallback_to_managed_on_dedicated_failure: false,
  personal_proxy_enabled: false,
  personal_proxy_url: "",
}

type ExtensionWorkerRow = {
  worker_session_id: string
  instance_id?: string
  client_label: string
  managed_api_key_id: number | null
  binding_source: string
  connected_at: number
  captcha_worker_id?: number | null
  captcha_worker_key_label?: string | null
  captcha_worker_key_prefix?: string | null
  refresh_token_id?: number | null
  allow_captcha?: boolean
  allow_session_refresh?: boolean
  allow_generation?: boolean
}

type GeminiGenSystemStatus = {
  success?: boolean
  status?: string
  error?: string
  summary?: {
    operational?: number
    degraded?: number
    outage?: number
    unknown?: number
    matching_model_groups?: number
  }
  geminigen?: {
    enabled?: boolean
    active_account_count?: number
    image_in_flight?: number
    video_in_flight?: number
  }
}

type DriveBackupJob = {
  id: string
  kind: "backup" | "restore"
  status: string
  stage: string
  started_at?: string
  finished_at?: string
  bytes_total?: number
  bytes_transferred?: number
  error?: string | null
  restart_required?: boolean
}

type DriveBackupStatus = {
  database_backend?: "sqlite" | "postgres"
  database_revision?: string | null
  encryption_configured?: boolean
  encryption_key_id?: string | null
  oauth_configured: boolean
  connected: boolean
  account_email?: string | null
  enabled: boolean
  schedule_time: string
  timezone: string
  retention: number
  folder_configured: boolean
  last_backup_at?: string | null
  last_backup_status?: string | null
  last_backup_error?: string | null
  job?: DriveBackupJob | null
}

type DriveBackupFile = {
  id: string
  name: string
  size: number
  created_at?: string
  backup_type?: string
  database_backend?: "sqlite" | "postgres"
  database_revision?: string | null
  encryption_key_id?: string | null
  backup_format?: string
}

const formatBytes = (value?: number) => {
  const bytes = Math.max(0, Number(value || 0))
  if (bytes < 1024) return `${bytes} B`
  const units = ["KB", "MB", "GB", "TB"]
  let amount = bytes / 1024
  let unit = 0
  while (amount >= 1024 && unit < units.length - 1) {
    amount /= 1024
    unit += 1
  }
  return `${amount.toFixed(amount >= 10 ? 1 : 2)} ${units[unit]}`
}

export function SystemSettings({ active }: { active: boolean }) {
  const { token } = useAuth()

  const [adminUsername, setAdminUsername] = useState("")
  const [currentApiKey, setCurrentApiKey] = useState("")
  const [oldPwd, setOldPwd] = useState("")
  const [newPwd, setNewPwd] = useState("")
  const [newApiKeyInput, setNewApiKeyInput] = useState("")
  const [dbRestoreFile, setDbRestoreFile] = useState<File | null>(null)
  const [dbDownloadBusy, setDbDownloadBusy] = useState(false)
  const [dbRestoreBusy, setDbRestoreBusy] = useState(false)
  const [dbRestoreResult, setDbRestoreResult] = useState("")
  const [dbRestoreInputKey, setDbRestoreInputKey] = useState(0)
  const [driveStatus, setDriveStatus] = useState<DriveBackupStatus | null>(null)
  const [driveBackups, setDriveBackups] = useState<DriveBackupFile[]>([])
  const [driveBusy, setDriveBusy] = useState(false)
  const [driveEnabled, setDriveEnabled] = useState(false)
  const [driveSchedule, setDriveSchedule] = useState("03:00")
  const [driveTimezone, setDriveTimezone] = useState("Asia/Karachi")
  const [driveRetention, setDriveRetention] = useState("14")
  const [errorBan, setErrorBan] = useState("3")
  const [errorBanEnabled, setErrorBanEnabled] = useState(true)
  const [debugEnabled, setDebugEnabled] = useState(false)

  const [proxyEnabled, setProxyEnabled] = useState(false)
  const [proxyUrl, setProxyUrl] = useState("")
  const [mediaProxyEnabled, setMediaProxyEnabled] = useState(false)
  const [mediaProxyUrl, setMediaProxyUrl] = useState("")
  const [proxyTestMsg, setProxyTestMsg] = useState("")

  const [imgTimeout, setImgTimeout] = useState("300")
  const [vidTimeout, setVidTimeout] = useState("1500")
  const [maxRetries, setMaxRetries] = useState("3")
  const [extensionGenerationEnabled, setExtensionGenerationEnabled] = useState(false)
  const [extensionGenerationFallbackMode, setExtensionGenerationFallbackMode] = useState<"local_http_on_recaptcha" | "none">(
    "local_http_on_recaptcha"
  )

  const [callMode, setCallMode] = useState<"default" | "polling">("default")

  const [pluginUrl, setPluginUrl] = useState("")
  const [pluginToken, setPluginToken] = useState("")
  const [pluginAutoEnable, setPluginAutoEnable] = useState(false)

  const [captcha, setCaptcha] = useState<CaptchaForm>(defaultCaptcha)
  const [extensionWorkers, setExtensionWorkers] = useState<ExtensionWorkerRow[]>([])
  const [geminiGenStatus, setGeminiGenStatus] = useState<GeminiGenSystemStatus | null>(null)
  const [geminiGenStatusLoading, setGeminiGenStatusLoading] = useState(false)

  const [busy, setBusy] = useState(false)
  /** Bumps when leaving extension mode so in-flight worker list fetches do not repopulate state. */
  const extensionFetchGen = useRef(0)

  const loadAll = useCallback(async () => {
    if (!token || !active) return

    const [a, p, g, c, plug, cap, workersResp, geminiStatusResp, driveStatusResp, driveFilesResp] = await Promise.all([
      adminJson<{
        admin_username?: string
        api_key?: string
        error_ban_threshold?: number
        error_ban_enabled?: boolean
        debug_enabled?: boolean
      }>("/api/admin/config", token),
      adminJson<{ proxy_enabled?: boolean; proxy_url?: string; media_proxy_enabled?: boolean; media_proxy_url?: string }>(
        "/api/proxy/config",
        token
      ),
      adminJson<{
        success?: boolean
        config?: {
          image_timeout?: number
          video_timeout?: number
          max_retries?: number
          extension_generation_enabled?: boolean
          extension_generation_fallback_mode?: string
        }
      }>(
        "/api/generation/timeout",
        token
      ),
      adminJson<{ success?: boolean; config?: { call_mode?: string } }>("/api/call-logic/config", token),
      adminJson<{ success?: boolean; config?: { connection_url?: string; connection_token?: string; auto_enable_on_update?: boolean } }>(
        "/api/plugin/config",
        token
      ),
      adminJson<Record<string, unknown>>("/api/captcha/config", token),
      adminJson<{
        success?: boolean
        workers?: ExtensionWorkerRow[]
      }>("/api/admin/extension/workers", token),
      adminJson<GeminiGenSystemStatus>("/api/admin/geminigen/models/status?window=1h", token),
      adminJson<{ success?: boolean } & DriveBackupStatus>("/api/admin/backups/google-drive/status", token),
      adminJson<{ success?: boolean; backups?: DriveBackupFile[] }>("/api/admin/backups/google-drive/backups", token),
    ])

    if (a.data) {
      setAdminUsername(a.data.admin_username || "admin")
      setCurrentApiKey(a.data.api_key || "")
      setErrorBan(String(a.data.error_ban_threshold ?? 3))
      setErrorBanEnabled(a.data.error_ban_enabled !== false)
      setDebugEnabled(!!a.data.debug_enabled)
    }
    if (p.data) {
      setProxyEnabled(!!p.data.proxy_enabled)
      setProxyUrl(p.data.proxy_url || "")
      setMediaProxyEnabled(!!p.data.media_proxy_enabled)
      setMediaProxyUrl(p.data.media_proxy_url || "")
    }
    if (g.ok && g.data?.success && g.data.config) {
      setImgTimeout(String(g.data.config.image_timeout ?? 300))
      setVidTimeout(String(g.data.config.video_timeout ?? 1500))
      setMaxRetries(String(g.data.config.max_retries ?? 3))
      setExtensionGenerationEnabled(!!g.data.config.extension_generation_enabled)
      setExtensionGenerationFallbackMode(
        g.data.config.extension_generation_fallback_mode === "none" ? "none" : "local_http_on_recaptcha"
      )
    }
    if (c.ok && c.data?.success && c.data.config?.call_mode)
      setCallMode(c.data.config.call_mode === "polling" ? "polling" : "default")
    if (plug.ok && plug.data?.success && plug.data.config) {
      setPluginUrl(plug.data.config.connection_url || "")
      setPluginToken(plug.data.config.connection_token || "")
      setPluginAutoEnable(!!plug.data.config.auto_enable_on_update)
    }
    if (cap.ok && cap.data && typeof cap.data === "object") {
      const raw = cap.data as Record<string, unknown>
      setCaptcha(() => ({
        ...defaultCaptcha,
        captcha_method: String(raw.captcha_method || "yescaptcha"),
        yescaptcha_api_key: String(raw.yescaptcha_api_key ?? ""),
        yescaptcha_base_url: String(raw.yescaptcha_base_url || defaultCaptcha.yescaptcha_base_url),
        yescaptcha_task_type: String(raw.yescaptcha_task_type || defaultCaptcha.yescaptcha_task_type),
        capmonster_api_key: String(raw.capmonster_api_key ?? ""),
        capmonster_base_url: String(raw.capmonster_base_url || defaultCaptcha.capmonster_base_url),
        capmonster_min_score: Math.max(0.1, Math.min(0.9, Number(raw.capmonster_min_score ?? 0.9))),
        ezcaptcha_api_key: String(raw.ezcaptcha_api_key ?? ""),
        ezcaptcha_base_url: String(raw.ezcaptcha_base_url || defaultCaptcha.ezcaptcha_base_url),
        capsolver_api_key: String(raw.capsolver_api_key ?? ""),
        capsolver_base_url: String(raw.capsolver_base_url || defaultCaptcha.capsolver_base_url),
        remote_browser_base_url: String(raw.remote_browser_base_url ?? ""),
        remote_browser_api_key: String(raw.remote_browser_api_key ?? ""),
        remote_browser_timeout: Number(raw.remote_browser_timeout ?? 60),
        browser_fallback_to_remote_browser: raw.browser_fallback_to_remote_browser !== false,
        browser_captcha_page_url: String(
          raw.browser_captcha_page_url || "https://labs.google/fx/api/auth/providers"
        ),
        browser_proxy_enabled: !!raw.browser_proxy_enabled,
        browser_proxy_url: String(raw.browser_proxy_url ?? ""),
        browser_count: Math.max(1, Number(raw.browser_count ?? 1)),
        browser_personal_fresh_restart_every_n_solves: Math.max(
          0,
          Number(raw.browser_personal_fresh_restart_every_n_solves ?? 10)
        ),
        personal_project_pool_size: Number(raw.personal_project_pool_size ?? 4),
        personal_max_resident_tabs: Number(raw.personal_max_resident_tabs ?? 5),
        personal_idle_tab_ttl_seconds: Number(raw.personal_idle_tab_ttl_seconds ?? 600),
        session_refresh_enabled: raw.session_refresh_enabled !== false,
        session_refresh_browser_first: raw.session_refresh_browser_first !== false,
        session_refresh_inject_st_cookie: raw.session_refresh_inject_st_cookie !== false,
        session_refresh_warmup_urls: Array.isArray(raw.session_refresh_warmup_urls)
          ? raw.session_refresh_warmup_urls.join(",")
          : String(raw.session_refresh_warmup_urls ?? defaultCaptcha.session_refresh_warmup_urls),
        session_refresh_wait_seconds_per_url: Number(raw.session_refresh_wait_seconds_per_url ?? 60),
        session_refresh_overall_timeout_seconds: Number(raw.session_refresh_overall_timeout_seconds ?? 180),
        session_refresh_update_st_from_cookie: raw.session_refresh_update_st_from_cookie !== false,
        session_refresh_fail_if_st_refresh_fails: raw.session_refresh_fail_if_st_refresh_fails !== false,
        session_refresh_local_only: raw.session_refresh_local_only !== false,
        session_refresh_scheduler_enabled: !!raw.session_refresh_scheduler_enabled,
        session_refresh_scheduler_interval_minutes: Number(raw.session_refresh_scheduler_interval_minutes ?? 30),
        session_refresh_scheduler_batch_size: Number(raw.session_refresh_scheduler_batch_size ?? 10),
        session_refresh_scheduler_only_expiring_within_minutes: Number(
          raw.session_refresh_scheduler_only_expiring_within_minutes ?? 60
        ),
        st_only_refresh_scheduler_enabled: !!raw.st_only_refresh_scheduler_enabled,
        st_only_refresh_scheduler_interval_minutes: Number(raw.st_only_refresh_scheduler_interval_minutes ?? 5),
        st_only_refresh_scheduler_batch_size: Number(raw.st_only_refresh_scheduler_batch_size ?? 20),
        st_only_refresh_scheduler_expiring_within_minutes: Number(
          raw.st_only_refresh_scheduler_expiring_within_minutes ?? 5
        ),
        extension_queue_wait_timeout_seconds: Number(raw.extension_queue_wait_timeout_seconds ?? 20),
        extension_fallback_to_managed_on_dedicated_failure: !!raw.extension_fallback_to_managed_on_dedicated_failure,
        personal_proxy_enabled: !!raw.browser_proxy_enabled,
        personal_proxy_url: String(raw.browser_proxy_url ?? ""),
      }))
    }
    const loadedCaptchaMethod =
      cap.ok && cap.data && typeof cap.data === "object"
        ? String((cap.data as Record<string, unknown>).captcha_method || "yescaptcha")
        : ""
    if (workersResp.ok && workersResp.data?.success) {
      if (loadedCaptchaMethod === "extension") {
        setExtensionWorkers(Array.isArray(workersResp.data.workers) ? workersResp.data.workers : [])
      } else {
        setExtensionWorkers([])
      }
    }
    if (geminiStatusResp.data) setGeminiGenStatus(geminiStatusResp.data)
    if (driveStatusResp.data) {
      const status = driveStatusResp.data
      setDriveStatus(status)
      setDriveEnabled(Boolean(status.enabled))
      setDriveSchedule(status.schedule_time || "03:00")
      setDriveTimezone(status.timezone || "Asia/Karachi")
      setDriveRetention(String(status.retention || 14))
    }
    if (driveFilesResp.data?.backups) setDriveBackups(driveFilesResp.data.backups)
  }, [token, active])

  useEffect(() => {
    void loadAll()
  }, [loadAll])

  const refreshDrive = useCallback(async () => {
    if (!token) return
    const statusResp = await adminJson<{ success?: boolean } & DriveBackupStatus>(
      "/api/admin/backups/google-drive/status",
      token
    )
    if (statusResp.data) setDriveStatus(statusResp.data)
    if (statusResp.data?.connected) {
      const filesResp = await adminJson<{ backups?: DriveBackupFile[] }>(
        "/api/admin/backups/google-drive/backups",
        token
      )
      if (filesResp.data?.backups) setDriveBackups(filesResp.data.backups)
    } else {
      setDriveBackups([])
    }
  }, [token])

  useEffect(() => {
    const running = driveStatus?.job?.status === "running"
    if (!active || !token || !running) return
    const handle = window.setInterval(() => void refreshDrive(), 2000)
    return () => window.clearInterval(handle)
  }, [active, token, driveStatus?.job?.status, refreshDrive])

  const driveAction = async (action: () => Promise<void>) => {
    setDriveBusy(true)
    try {
      await action()
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Google Drive operation failed")
    } finally {
      setDriveBusy(false)
    }
  }

  const connectDrive = () => driveAction(async () => {
    const response = await adminJson<{ authorization_url?: string; detail?: string }>(
      "/api/admin/backups/google-drive/oauth/start",
      token,
      { method: "POST" }
    )
    if (!response.ok || !response.data?.authorization_url) {
      throw new Error(response.data?.detail || "Unable to start Google OAuth")
    }
    window.location.assign(response.data.authorization_url)
  })

  const testDrive = () => driveAction(async () => {
    const response = await adminJson<{ success?: boolean; account_email?: string; detail?: string }>(
      "/api/admin/backups/google-drive/test",
      token,
      { method: "POST" }
    )
    if (!response.ok || !response.data?.success) throw new Error(response.data?.detail || "Connection test failed")
    toast.success(`Google Drive connected${response.data.account_email ? ` as ${response.data.account_email}` : ""}`)
    await refreshDrive()
  })

  const saveDriveConfig = () => driveAction(async () => {
    const response = await adminJson<{ success?: boolean; detail?: string }>(
      "/api/admin/backups/google-drive/config",
      token,
      {
        method: "PUT",
        body: JSON.stringify({
          enabled: driveEnabled,
          schedule_time: driveSchedule,
          timezone: driveTimezone.trim(),
          retention: Number.parseInt(driveRetention, 10) || 14,
        }),
      }
    )
    if (!response.ok || !response.data?.success) throw new Error(response.data?.detail || "Backup settings were not saved")
    toast.success("Google Drive backup settings saved")
    await refreshDrive()
  })

  const startDriveBackup = () => driveAction(async () => {
    const response = await adminJson<{ success?: boolean; detail?: string }>(
      "/api/admin/backups/google-drive/backups",
      token,
      { method: "POST" }
    )
    if (!response.ok || !response.data?.success) throw new Error(response.data?.detail || "Backup could not start")
    toast.success("Google Drive backup started")
    await refreshDrive()
  })

  const disconnectDrive = () => driveAction(async () => {
    if (!window.confirm("Disconnect Google Drive and disable automatic backups? Remote backups will remain in Drive.")) return
    const response = await adminJson<{ success?: boolean; detail?: string }>(
      "/api/admin/backups/google-drive/disconnect",
      token,
      { method: "POST" }
    )
    if (!response.ok || !response.data?.success) throw new Error(response.data?.detail || "Disconnect failed")
    toast.success("Google Drive disconnected")
    await refreshDrive()
  })

  const deleteDriveBackup = (backup: DriveBackupFile) => driveAction(async () => {
    if (!window.confirm(`Delete ${backup.name} from Google Drive? This cannot be undone.`)) return
    const response = await adminJson<{ success?: boolean; detail?: string }>(
      `/api/admin/backups/google-drive/backups/${encodeURIComponent(backup.id)}`,
      token,
      { method: "DELETE" }
    )
    if (!response.ok || !response.data?.success) throw new Error(response.data?.detail || "Delete failed")
    toast.success("Remote backup deleted")
    await refreshDrive()
  })

  const restoreDriveBackup = (backup: DriveBackupFile) => driveAction(async () => {
    const confirmation = window.prompt(
      `Restore ${backup.name}? Current data will first be backed up to Drive. Type RESTORE to continue.`
    )
    if (confirmation !== "RESTORE") return
    const response = await adminJson<{ success?: boolean; detail?: string }>(
      `/api/admin/backups/google-drive/backups/${encodeURIComponent(backup.id)}/restore`,
      token,
      { method: "POST", body: JSON.stringify({ confirmation }) }
    )
    if (!response.ok || !response.data?.success) throw new Error(response.data?.detail || "Restore could not start")
    toast.success("Restore started. Keep this page open to monitor validation.")
    await refreshDrive()
  })

  const saveErrorBan = async () => {
    if (!token) return
    setBusy(true)
    try {
      const r = await adminFetch("/api/admin/config", token, {
        method: "POST",
        body: JSON.stringify({
          error_ban_threshold: parseInt(errorBan, 10) || 3,
          error_ban_enabled: errorBanEnabled,
        }),
      })
      if (!r) return
      const d = await r.json()
      if (d.success) toast.success("Saved")
      else toast.error("Save failed")
    } finally {
      setBusy(false)
    }
  }

  const savePassword = async () => {
    if (!token) return
    if (!oldPwd || !newPwd) {
      toast.error("Enter old and new password")
      return
    }
    if (newPwd.length < 4) {
      toast.error("New password at least 4 characters")
      return
    }
    setBusy(true)
    try {
      const r = await adminFetch("/api/admin/password", token, {
        method: "POST",
        body: JSON.stringify({
          username: adminUsername.trim() || undefined,
          old_password: oldPwd,
          new_password: newPwd,
        }),
      })
      if (!r) return
      const d = await r.json()
      if (d.success) {
        toast.success("Password updated — please sign in again")
        setTimeout(() => {
          localStorage.removeItem("adminToken")
          window.location.href = "/login"
        }, 1500)
      } else toast.error(d.detail || "Failed")
    } finally {
      setBusy(false)
    }
  }

  const saveApiKey = async () => {
    if (!token) return
    const k = newApiKeyInput.trim()
    if (!k || k.length < 6) {
      toast.error("New API key at least 6 characters")
      return
    }
    if (!confirm("Update API key? All clients must use the new key.")) return
    setBusy(true)
    try {
      const r = await adminFetch("/api/admin/apikey", token, {
        method: "POST",
        body: JSON.stringify({ new_api_key: k }),
      })
      if (!r) return
      const d = await r.json()
      if (d.success) {
        toast.success("API key updated")
        setCurrentApiKey(k)
        setNewApiKeyInput("")
      } else toast.error(d.detail || "Failed")
    } finally {
      setBusy(false)
    }
  }

  const downloadDatabase = async () => {
    if (!token) return
    setDbDownloadBusy(true)
    try {
      const r = await adminFetch("/api/admin/database/download", token)
      if (!r) return
      if (!r.ok) {
        const d = await r.json().catch(() => null)
        toast.error(d?.detail || "Database download failed")
        return
      }

      const blob = await r.blob()
      const disposition = r.headers.get("Content-Disposition") || ""
      const quotedName = disposition.match(/filename="([^"]+)"/i)?.[1]
      const rawName = quotedName || disposition.match(/filename=([^;]+)/i)?.[1]
      const filename = rawName?.trim() || `flow-${new Date().toISOString().slice(0, 10)}.db`
      const url = URL.createObjectURL(blob)
      const a = document.createElement("a")
      a.href = url
      a.download = filename
      document.body.appendChild(a)
      a.click()
      a.remove()
      URL.revokeObjectURL(url)
      toast.success("Database download started")
    } finally {
      setDbDownloadBusy(false)
    }
  }

  const restoreDatabase = async () => {
    if (!token) return
    if (!dbRestoreFile) {
      toast.error("Choose a sub2gen.db file first")
      return
    }
    if (!confirm("Replace the server database with this file? A backup will be created first.")) return

    setDbRestoreBusy(true)
    setDbRestoreResult("")
    try {
      const body = new FormData()
      body.append("database", dbRestoreFile, dbRestoreFile.name || "sub2gen.db")
      const r = await adminFetch("/api/admin/database/restore", token, {
        method: "POST",
        body,
      })
      if (!r) return
      const d = await r.json().catch(() => null)
      if (r.ok && d?.success) {
        const mb = typeof d.size === "number" ? `${(d.size / 1024 / 1024).toFixed(2)} MB` : "uploaded"
        toast.success("Database restored")
        setDbRestoreResult(`Restored ${mb}. Backup: ${d.backup_path || "none"}. Re-login if your session expires.`)
        setDbRestoreFile(null)
        setDbRestoreInputKey((value) => value + 1)
      } else {
        toast.error(d?.detail || "Database restore failed")
      }
    } finally {
      setDbRestoreBusy(false)
    }
  }

  const onDebugToggle = async (enabled: boolean) => {
    if (!token) return
    const prev = debugEnabled
    setDebugEnabled(enabled)
    const r = await adminFetch("/api/admin/debug", token, {
      method: "POST",
      body: JSON.stringify({ enabled }),
    })
    if (!r) return
    const d = await r.json()
    if (d.success) {
      const actualEnabled = !!d.enabled
      setDebugEnabled(actualEnabled)
      toast.success(d.message || (actualEnabled ? "Debug on" : "Debug off"))
    } else {
      setDebugEnabled(prev)
      toast.error(d.detail || "Failed")
    }
  }

  const saveProxy = async () => {
    if (!token) return
    setBusy(true)
    try {
      const r = await adminFetch("/api/proxy/config", token, {
        method: "POST",
        body: JSON.stringify({
          proxy_enabled: proxyEnabled,
          proxy_url: proxyUrl.trim(),
          media_proxy_enabled: mediaProxyEnabled,
          media_proxy_url: mediaProxyUrl.trim(),
        }),
      })
      if (!r) return
      const d = await r.json()
      if (d.success) toast.success("Proxy saved")
      else toast.error(d.message || "Failed")
    } finally {
      setBusy(false)
    }
  }

  const testProxy = async () => {
    if (!token) return
    const tests: { name: string; proxy_url: string }[] = []
    const pu = proxyUrl.trim()
    const mu = mediaProxyUrl.trim()
    if (pu) tests.push({ name: "Request proxy", proxy_url: pu })
    if (mu && mu !== pu) tests.push({ name: "Media proxy", proxy_url: mu })
    if (!tests.length) {
      setProxyTestMsg("Fill at least one proxy URL")
      toast.error("Fill at least one proxy URL")
      return
    }
    setBusy(true)
    setProxyTestMsg("")
    try {
      const parts: string[] = []
      let allOk = true
      for (const t of tests) {
        const r = await adminFetch("/api/proxy/test", token, {
          method: "POST",
          body: JSON.stringify({ proxy_url: t.proxy_url, test_url: "https://labs.google/" }),
        })
        if (!r) continue
        const d = await r.json()
        if (!d.success) allOk = false
        parts.push(`${t.name}: ${d.success ? "OK" : "Fail"} ${d.message || ""}${d.status_code != null ? ` (HTTP ${d.status_code})` : ""}`)
      }
      const summary = parts.join(" | ")
      setProxyTestMsg(summary)
      if (allOk) toast.success(summary)
      else toast.error(summary)
    } catch (e) {
      setProxyTestMsg(String(e))
      toast.error("Proxy test failed")
    } finally {
      setBusy(false)
    }
  }

  const saveGeneration = async () => {
    if (!token) return
    const it = parseInt(imgTimeout, 10) || 300
    const vt = parseInt(vidTimeout, 10) || 1500
    const mr = parseInt(maxRetries, 10) || 3
    if (it < 60 || it > 3600) return toast.error("Image timeout 60–3600s")
    if (vt < 60 || vt > 7200) return toast.error("Video timeout 60–7200s")
    if (mr < 1) return toast.error("Max retries ≥ 1")
    const extensionModeActive = captcha.captcha_method === "extension"
    setBusy(true)
    try {
      const r = await adminFetch("/api/generation/timeout", token, {
        method: "POST",
        body: JSON.stringify({
          image_timeout: it,
          video_timeout: vt,
          max_retries: mr,
          extension_generation_enabled: extensionModeActive ? extensionGenerationEnabled : false,
          extension_generation_fallback_mode: extensionModeActive ? extensionGenerationFallbackMode : "none",
        }),
      })
      if (!r) return
      const d = await r.json()
      if (d.success) toast.success("Generation config saved")
      else toast.error("Failed")
    } finally {
      setBusy(false)
    }
  }

  const saveCallLogic = async () => {
    if (!token) return
    setBusy(true)
    try {
      const r = await adminFetch("/api/call-logic/config", token, {
        method: "POST",
        body: JSON.stringify({ call_mode: callMode }),
      })
      if (!r) return
      const d = await r.json()
      if (d.success) toast.success("Call logic saved")
      else toast.error(d.message || "Failed")
    } finally {
      setBusy(false)
    }
  }

  const savePlugin = async () => {
    if (!token) return
    setBusy(true)
    try {
      const r = await adminFetch("/api/plugin/config", token, {
        method: "POST",
        body: JSON.stringify({
          connection_token: pluginToken.trim(),
          auto_enable_on_update: pluginAutoEnable,
        }),
      })
      if (!r) return
      const d = await r.json()
      if (d.success) {
        toast.success("Plugin config saved")
        if (d.connection_token) setPluginToken(d.connection_token)
        await loadAll()
      } else toast.error(d.message || "Failed")
    } finally {
      setBusy(false)
    }
  }

  const saveCaptcha = async () => {
    if (!token) return
    const method = captcha.captcha_method
    const finalProxyEnabled = method === "personal" ? captcha.personal_proxy_enabled : captcha.browser_proxy_enabled
    const finalProxyUrl = method === "personal" ? captcha.personal_proxy_url : captcha.browser_proxy_url
    setBusy(true)
    try {
      const r = await adminFetch("/api/captcha/config", token, {
        method: "POST",
        body: JSON.stringify({
          captcha_method: method,
          yescaptcha_api_key: captcha.yescaptcha_api_key.trim(),
          yescaptcha_base_url: captcha.yescaptcha_base_url.trim(),
          yescaptcha_task_type: captcha.yescaptcha_task_type,
          capmonster_api_key: captcha.capmonster_api_key.trim(),
          capmonster_base_url: captcha.capmonster_base_url.trim(),
          capmonster_min_score: captcha.capmonster_min_score,
          ezcaptcha_api_key: captcha.ezcaptcha_api_key.trim(),
          ezcaptcha_base_url: captcha.ezcaptcha_base_url.trim(),
          capsolver_api_key: captcha.capsolver_api_key.trim(),
          capsolver_base_url: captcha.capsolver_base_url.trim(),
          remote_browser_base_url: captcha.remote_browser_base_url.trim(),
          remote_browser_api_key: captcha.remote_browser_api_key.trim(),
          remote_browser_timeout: captcha.remote_browser_timeout,
          browser_fallback_to_remote_browser: captcha.browser_fallback_to_remote_browser,
          browser_captcha_page_url: captcha.browser_captcha_page_url.trim(),
          browser_proxy_enabled: finalProxyEnabled,
          browser_proxy_url: finalProxyUrl,
          browser_count: captcha.browser_count,
          browser_personal_fresh_restart_every_n_solves:
            captcha.browser_personal_fresh_restart_every_n_solves,
          personal_project_pool_size: captcha.personal_project_pool_size,
          personal_max_resident_tabs: captcha.personal_max_resident_tabs,
          personal_idle_tab_ttl_seconds: captcha.personal_idle_tab_ttl_seconds,
          session_refresh_enabled: captcha.session_refresh_enabled,
          session_refresh_browser_first: captcha.session_refresh_browser_first,
          session_refresh_inject_st_cookie: captcha.session_refresh_inject_st_cookie,
          session_refresh_warmup_urls: captcha.session_refresh_warmup_urls
            .split(",")
            .map((item) => item.trim())
            .filter(Boolean),
          session_refresh_wait_seconds_per_url: captcha.session_refresh_wait_seconds_per_url,
          session_refresh_overall_timeout_seconds: captcha.session_refresh_overall_timeout_seconds,
          session_refresh_update_st_from_cookie: captcha.session_refresh_update_st_from_cookie,
          session_refresh_fail_if_st_refresh_fails: captcha.session_refresh_fail_if_st_refresh_fails,
          session_refresh_local_only: captcha.session_refresh_local_only,
          session_refresh_scheduler_enabled: captcha.session_refresh_scheduler_enabled,
          session_refresh_scheduler_interval_minutes: captcha.session_refresh_scheduler_interval_minutes,
          session_refresh_scheduler_batch_size: captcha.session_refresh_scheduler_batch_size,
          session_refresh_scheduler_only_expiring_within_minutes:
            captcha.session_refresh_scheduler_only_expiring_within_minutes,
          st_only_refresh_scheduler_enabled: captcha.st_only_refresh_scheduler_enabled,
          st_only_refresh_scheduler_interval_minutes: captcha.st_only_refresh_scheduler_interval_minutes,
          st_only_refresh_scheduler_batch_size: captcha.st_only_refresh_scheduler_batch_size,
          st_only_refresh_scheduler_expiring_within_minutes:
            captcha.st_only_refresh_scheduler_expiring_within_minutes,
          extension_queue_wait_timeout_seconds: captcha.extension_queue_wait_timeout_seconds,
          extension_fallback_to_managed_on_dedicated_failure:
            captcha.extension_fallback_to_managed_on_dedicated_failure,
        }),
      })
      if (!r) return
      const d = await r.json()
      if (d.success) {
        toast.success("Captcha config saved")
        await loadAll()
      } else toast.error(d.message || "Failed")
    } finally {
      setBusy(false)
    }
  }

  const refreshExtensionWorkers = useCallback(async () => {
    if (!token) return
    const gen = ++extensionFetchGen.current
    const r = await adminJson<{ success?: boolean; workers?: ExtensionWorkerRow[] }>(
      "/api/admin/extension/workers",
      token
    )
    if (gen !== extensionFetchGen.current) return
    if (!r.ok || !r.data?.success) return
    setExtensionWorkers(Array.isArray(r.data.workers) ? r.data.workers : [])
  }, [token])

  useEffect(() => {
    if (captcha.captcha_method !== "extension") {
      extensionFetchGen.current++
      void Promise.resolve().then(() => {
        setExtensionWorkers([])
      })
      return
    }
    if (!token || !active) return
    void Promise.resolve().then(() => {
      void refreshExtensionWorkers()
    })
  }, [captcha.captcha_method, token, active, refreshExtensionWorkers])

  const killExtensionWorker = async (workerSessionId: string) => {
    if (!token) return
    const sessionId = (workerSessionId || "").trim()
    if (!sessionId) return toast.error("Worker session ID required")
    setBusy(true)
    try {
      const r = await adminFetch("/api/admin/extension/workers/kill", token, {
        method: "POST",
        body: JSON.stringify({ worker_session_id: sessionId }),
      })
      if (!r) return
      const d = await r.json()
      if (d.success) {
        toast.success("Worker terminated")
        await refreshExtensionWorkers()
      } else toast.error(d.message || "Failed")
    } finally {
      setBusy(false)
    }
  }

  const copyText = async (label: string, text: string) => {
    if (!text) return toast.error(`${label} is empty`)
    try {
      await navigator.clipboard.writeText(text)
      toast.success("Copied")
    } catch {
      toast.error("Copy failed")
    }
  }

  const refreshGeminiGenStatus = async () => {
    if (!token) return
    setGeminiGenStatusLoading(true)
    try {
      const resp = await adminJson<GeminiGenSystemStatus>("/api/admin/geminigen/models/status?window=1h", token)
      if (resp.data) setGeminiGenStatus(resp.data)
    } finally {
      setGeminiGenStatusLoading(false)
    }
  }

  const genPluginToken = () => {
    const chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    let out = ""
    for (let i = 0; i < 32; i++) out += chars[Math.floor(Math.random() * chars.length)]
    setPluginToken(out)
    toast.success("Random token generated")
  }

  if (!active) return null

  const m = captcha.captcha_method
  const extensionModeActive = m === "extension"
  const captchaWorkers = extensionWorkers.filter((w) => w.captcha_worker_id != null)
  const endUserWorkers = extensionWorkers.filter((w) => w.managed_api_key_id !== null)
  const refreshWorkers = extensionWorkers.filter(
    (w) => w.captcha_worker_id == null && w.managed_api_key_id === null && w.refresh_token_id != null
  )

  return (
    <div className="grid gap-6 lg:grid-cols-2">
      <Card>
        <CardHeader>
          <CardTitle>Security</CardTitle>
          <CardDescription>Admin username and password</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div>
            <Label>Admin username</Label>
            <Input className="mt-1" value={adminUsername} onChange={(e) => setAdminUsername(e.target.value)} />
          </div>
          <div>
            <Label>Old password</Label>
            <Input className="mt-1" type="password" value={oldPwd} onChange={(e) => setOldPwd(e.target.value)} />
          </div>
          <div>
            <Label>New password</Label>
            <Input className="mt-1" type="password" value={newPwd} onChange={(e) => setNewPwd(e.target.value)} />
          </div>
          <Button className="w-full" onClick={savePassword} disabled={busy}>
            {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : "Update password"}
          </Button>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Client API key</CardTitle>
          <CardDescription>Key used by OpenAI-compatible clients</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div>
            <Label>Current API key</Label>
            <Input className="mt-1 font-mono text-sm" readOnly disabled value={currentApiKey} />
          </div>
          <div>
            <Label>New API key</Label>
            <Input className="mt-1 font-mono text-sm" value={newApiKeyInput} onChange={(e) => setNewApiKeyInput(e.target.value)} />
          </div>
          <Button className="w-full" onClick={saveApiKey} disabled={busy}>
            Update API key
          </Button>
        </CardContent>
      </Card>

      <Card className="lg:col-span-2">
        <CardHeader>
          <CardTitle>Database migration</CardTitle>
          <CardDescription>
            {driveStatus?.database_backend === "postgres"
              ? "PostgreSQL is the durable database; legacy SQLite controls are retired"
              : "Download or restore the server SQLite database"}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
          {driveStatus?.database_backend === "postgres" ? (
            <div className="rounded-md border bg-muted/20 px-4 py-3 text-sm">
              <div className="font-medium">PostgreSQL · schema revision {driveStatus.database_revision || "unknown"}</div>
              <p className="mt-1 text-xs text-muted-foreground">
                Use encrypted Google Drive backups below. Old SQLite artifacts remain visible for rollback but cannot be restored into PostgreSQL.
              </p>
            </div>
          ) : (
          <>
          <div className="space-y-2">
            <Label>Current database</Label>
            <p className="text-xs text-muted-foreground">
              Download a fresh sub2gen.db snapshot for backup or migration to another server.
            </p>
            <Button variant="secondary" onClick={downloadDatabase} disabled={dbDownloadBusy || dbRestoreBusy}>
              {dbDownloadBusy ? <Loader2 className="h-4 w-4 animate-spin mr-2" /> : <Download className="h-4 w-4 mr-2" />}
              Download database
            </Button>
          </div>

          <div className="space-y-4 border-t border-border pt-4">
            <div>
              <Label htmlFor="db-restore-file">SQLite database file</Label>
              <Input
                key={dbRestoreInputKey}
                id="db-restore-file"
                className="mt-1"
                type="file"
                accept=".db,.sqlite,.sqlite3,application/octet-stream"
                onChange={(e) => setDbRestoreFile(e.target.files?.[0] || null)}
                disabled={dbDownloadBusy || dbRestoreBusy}
              />
              <p className="text-xs text-muted-foreground mt-1">
                The current database is backed up first. Active sessions may need to log in again after restore.
              </p>
            </div>
            <Button onClick={restoreDatabase} disabled={dbDownloadBusy || dbRestoreBusy || !dbRestoreFile}>
              {dbRestoreBusy ? <Loader2 className="h-4 w-4 animate-spin mr-2" /> : <Upload className="h-4 w-4 mr-2" />}
              Restore database
            </Button>
            {dbRestoreResult ? <p className="text-xs text-muted-foreground">{dbRestoreResult}</p> : null}
          </div>
          </>
          )}
        </CardContent>
      </Card>

      <Card className="lg:col-span-2">
        <CardHeader className="flex flex-row items-start justify-between gap-4">
          <div>
            <CardTitle className="flex items-center gap-2"><Cloud className="h-5 w-5" /> Google Drive backups</CardTitle>
            <CardDescription>
              Private database and browser-profile archives. BrowserMetrics and OAuth credentials are excluded.
            </CardDescription>
          </div>
          <Button size="sm" variant="outline" onClick={() => void refreshDrive()} disabled={driveBusy}>
            <RefreshCw className={`h-4 w-4 ${driveBusy ? "animate-spin" : ""}`} />
          </Button>
        </CardHeader>
        <CardContent className="space-y-6">
          <div className="grid gap-3 sm:grid-cols-4 text-sm">
            <div className="rounded-md border bg-muted/20 px-3 py-2">
              <div className="text-xs text-muted-foreground">Database</div>
              <div className="font-medium capitalize">{driveStatus?.database_backend || "unknown"}</div>
              {driveStatus?.database_revision ? <div className="text-xs text-muted-foreground">revision {driveStatus.database_revision}</div> : null}
            </div>
            <div className="rounded-md border bg-muted/20 px-3 py-2">
              <div className="text-xs text-muted-foreground">OAuth environment</div>
              <div className="font-medium">{driveStatus?.oauth_configured ? "Configured" : "Missing variables"}</div>
            </div>
            <div className="rounded-md border bg-muted/20 px-3 py-2">
              <div className="text-xs text-muted-foreground">Connection</div>
              <div className="font-medium">{driveStatus?.connected ? driveStatus.account_email || "Connected" : "Not connected"}</div>
            </div>
            <div className="rounded-md border bg-muted/20 px-3 py-2">
              <div className="text-xs text-muted-foreground">Last backup</div>
              <div className="font-medium">{driveStatus?.last_backup_status || "Never"}</div>
              {driveStatus?.last_backup_at ? <div className="text-xs text-muted-foreground">{new Date(driveStatus.last_backup_at).toLocaleString()}</div> : null}
            </div>
          </div>

          {!driveStatus?.oauth_configured ? (
            <p className="rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm text-amber-700 dark:text-amber-300">
              Configure SUB2GEN_GOOGLE_DRIVE_CLIENT_ID, SUB2GEN_GOOGLE_DRIVE_CLIENT_SECRET, and
              SUB2GEN_GOOGLE_DRIVE_REDIRECT_URI in Railway before connecting.
            </p>
          ) : null}
          <p className="text-xs text-muted-foreground">
            {driveStatus?.database_backend === "postgres"
              ? `PostgreSQL archives are AES-256-GCM encrypted before upload${driveStatus.encryption_key_id ? ` with key ${driveStatus.encryption_key_id}` : ""}. Keep every referenced key offline and in Railway.`
              : "SQLite bridge archives contain Flow tokens, Google cookies, and browser state. Keep the Drive folder private."}
          </p>
          {driveStatus?.database_backend === "postgres" && !driveStatus.encryption_configured ? (
            <p className="rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm text-amber-700 dark:text-amber-300">
              Configure SUB2GEN_BACKUP_ACTIVE_KEY_ID and SUB2GEN_BACKUP_KEYS_JSON before creating PostgreSQL backups.
            </p>
          ) : null}

          <div className="flex flex-wrap gap-2">
            {!driveStatus?.connected ? (
              <Button onClick={connectDrive} disabled={driveBusy || !driveStatus?.oauth_configured}>
                {driveBusy ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Cloud className="mr-2 h-4 w-4" />}
                Connect Google Drive
              </Button>
            ) : (
              <>
                <Button variant="secondary" onClick={testDrive} disabled={driveBusy}>Test connection</Button>
                <Button variant="outline" onClick={disconnectDrive} disabled={driveBusy}>Disconnect</Button>
              </>
            )}
          </div>

          <div className="grid gap-4 border-t border-border pt-4 md:grid-cols-4">
            <div className="flex items-center gap-2 md:col-span-4">
              <Switch
                id="drive-auto-backups"
                checked={driveEnabled}
                onCheckedChange={setDriveEnabled}
                disabled={!driveStatus?.connected || driveBusy}
              />
              <Label htmlFor="drive-auto-backups">Enable automatic backups</Label>
            </div>
            <div>
              <Label htmlFor="drive-schedule">Daily time</Label>
              <Input id="drive-schedule" className="mt-1" type="time" value={driveSchedule} onChange={(event) => setDriveSchedule(event.target.value)} />
            </div>
            <div className="md:col-span-2">
              <Label htmlFor="drive-timezone">Timezone</Label>
              <Input id="drive-timezone" className="mt-1" value={driveTimezone} onChange={(event) => setDriveTimezone(event.target.value)} />
            </div>
            <div>
              <Label htmlFor="drive-retention">Automatic backups retained</Label>
              <Input id="drive-retention" className="mt-1" type="number" min={1} max={365} value={driveRetention} onChange={(event) => setDriveRetention(event.target.value)} />
            </div>
            <div className="flex flex-wrap gap-2 md:col-span-4">
              <Button variant="secondary" onClick={saveDriveConfig} disabled={driveBusy || !driveStatus?.connected}>Save backup settings</Button>
              <Button onClick={startDriveBackup} disabled={driveBusy || !driveStatus?.connected || driveStatus?.job?.status === "running" || (driveStatus?.database_backend === "postgres" && !driveStatus.encryption_configured)}>
                <Upload className="mr-2 h-4 w-4" /> Backup now
              </Button>
            </div>
          </div>

          {driveStatus?.job ? (
            <div className="space-y-2 rounded-md border px-3 py-3 text-sm">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="font-medium capitalize">{driveStatus.job.kind}: {driveStatus.job.status}</span>
                <span className="text-xs text-muted-foreground">{driveStatus.job.stage.replaceAll("_", " ")}</span>
              </div>
              {Number(driveStatus.job.bytes_total || 0) > 0 ? (
                <>
                  <div className="h-2 overflow-hidden rounded-full bg-muted">
                    <div
                      className="h-full bg-primary transition-all"
                      style={{ width: `${Math.min(100, ((driveStatus.job.bytes_transferred || 0) / (driveStatus.job.bytes_total || 1)) * 100)}%` }}
                    />
                  </div>
                  <div className="text-xs text-muted-foreground">
                    {formatBytes(driveStatus.job.bytes_transferred)} / {formatBytes(driveStatus.job.bytes_total)}
                  </div>
                </>
              ) : null}
              {driveStatus.job.error ? <p className="text-xs text-destructive">{driveStatus.job.error}</p> : null}
              {driveStatus.job.restart_required ? <p className="text-xs text-amber-600">Restore completed. The single Railway replica is restarting and will clear maintenance after readiness checks.</p> : null}
            </div>
          ) : null}

          <div className="space-y-3 border-t border-border pt-4">
            <div className="flex items-center justify-between gap-3">
              <Label>Remote backup history</Label>
              <span className="text-xs text-muted-foreground">{driveBackups.length} backup(s)</span>
            </div>
            {driveBackups.length ? (
              <div className="max-h-72 space-y-2 overflow-y-auto pr-1">
                {driveBackups.map((backup) => (
                  <div key={backup.id} className="flex flex-col gap-3 rounded-md border px-3 py-2 sm:flex-row sm:items-center sm:justify-between">
                    <div className="min-w-0">
                      <div className="truncate text-sm font-medium">{backup.name}</div>
                      <div className="text-xs text-muted-foreground">
                        <span className="capitalize">{backup.database_backend || "sqlite"}</span>{" · "}
                        {backup.backup_type || "backup"} · {formatBytes(backup.size)}
                        {backup.created_at ? ` · ${new Date(backup.created_at).toLocaleString()}` : ""}
                      </div>
                    </div>
                    <div className="flex gap-2">
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() => restoreDriveBackup(backup)}
                        disabled={driveBusy || driveStatus?.job?.status === "running" || (driveStatus?.database_backend === "postgres" && backup.database_backend !== "postgres")}
                      >
                        Restore
                      </Button>
                      <Button size="sm" variant="ghost" onClick={() => deleteDriveBackup(backup)} disabled={driveBusy || driveStatus?.job?.status === "running"}>
                        <Trash2 className="h-4 w-4" />
                      </Button>
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <p className="text-sm text-muted-foreground">No remote backups found.</p>
            )}
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Error handling</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex items-center gap-2">
            <Switch checked={errorBanEnabled} onCheckedChange={setErrorBanEnabled} id="error-ban-enabled" />
            <Label htmlFor="error-ban-enabled">Auto-disable token after consecutive errors</Label>
          </div>
          <p className="text-xs text-muted-foreground">
            When off, tokens are not disabled by this counter. Other limits (for example 429) are unchanged.
          </p>
          <div>
            <Label>Error ban threshold</Label>
            <Input
              className="mt-1"
              type="number"
              value={errorBan}
              onChange={(e) => setErrorBan(e.target.value)}
              disabled={!errorBanEnabled}
            />
            <p className="text-xs text-muted-foreground mt-1">Disable token after this many consecutive errors</p>
          </div>
          <Button onClick={saveErrorBan} disabled={busy}>
            Save
          </Button>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Debug</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex items-center gap-2">
            <Switch checked={debugEnabled} onCheckedChange={onDebugToggle} />
            <Label>Enable debug logging</Label>
          </div>
          <p className="text-xs text-muted-foreground">Writes verbose upstream logs (disk usage).</p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="flex flex-row items-center justify-between gap-4">
          <div>
            <CardTitle>GeminiGen Status</CardTitle>
            <CardDescription>{geminiGenStatus?.status || "unavailable"}</CardDescription>
          </div>
          <Button size="sm" variant="outline" onClick={refreshGeminiGenStatus} disabled={geminiGenStatusLoading}>
            <RefreshCw className={`h-4 w-4 ${geminiGenStatusLoading ? "animate-spin" : ""}`} />
          </Button>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid grid-cols-2 gap-3 text-sm">
            <div className="rounded-md border bg-muted/20 px-3 py-2">
              <div className="text-xs text-muted-foreground">Enabled</div>
              <div className="font-medium">{geminiGenStatus?.geminigen?.enabled ? "Yes" : "No"}</div>
            </div>
            <div className="rounded-md border bg-muted/20 px-3 py-2">
              <div className="text-xs text-muted-foreground">Active accounts</div>
              <div className="font-medium tabular-nums">{geminiGenStatus?.geminigen?.active_account_count ?? 0}</div>
            </div>
            <div className="rounded-md border bg-muted/20 px-3 py-2">
              <div className="text-xs text-muted-foreground">Image in-flight</div>
              <div className="font-medium tabular-nums">{geminiGenStatus?.geminigen?.image_in_flight ?? 0}</div>
            </div>
            <div className="rounded-md border bg-muted/20 px-3 py-2">
              <div className="text-xs text-muted-foreground">Video in-flight</div>
              <div className="font-medium tabular-nums">{geminiGenStatus?.geminigen?.video_in_flight ?? 0}</div>
            </div>
          </div>
          <div className="grid grid-cols-3 gap-3 text-sm">
            <div>
              <div className="text-xs text-muted-foreground">Operational</div>
              <div className="font-medium tabular-nums">{geminiGenStatus?.summary?.operational ?? 0}</div>
            </div>
            <div>
              <div className="text-xs text-muted-foreground">Degraded</div>
              <div className="font-medium tabular-nums">{geminiGenStatus?.summary?.degraded ?? 0}</div>
            </div>
            <div>
              <div className="text-xs text-muted-foreground">Outage</div>
              <div className="font-medium tabular-nums">{geminiGenStatus?.summary?.outage ?? 0}</div>
            </div>
          </div>
          {geminiGenStatus?.error ? <p className="text-xs text-destructive">{geminiGenStatus.error}</p> : null}
        </CardContent>
      </Card>

      <Card className="lg:col-span-2">
        <CardHeader>
          <CardTitle>Proxy</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex items-center gap-2">
            <Switch checked={proxyEnabled} onCheckedChange={setProxyEnabled} />
            <Label>Enable request proxy</Label>
          </div>
          <div>
            <Label>Proxy URL</Label>
            <Input className="mt-1 font-mono text-sm" value={proxyUrl} onChange={(e) => setProxyUrl(e.target.value)} placeholder="http://127.0.0.1:7890" />
          </div>
          <div className="flex items-center gap-2 border-t pt-4">
            <Switch checked={mediaProxyEnabled} onCheckedChange={setMediaProxyEnabled} />
            <Label>Media upload/download proxy</Label>
          </div>
          {mediaProxyEnabled ? (
            <div>
              <Label>Media proxy URL</Label>
              <Input className="mt-1 font-mono text-sm" value={mediaProxyUrl} onChange={(e) => setMediaProxyUrl(e.target.value)} />
            </div>
          ) : null}
          <div className="flex flex-wrap gap-2">
            <Button variant="outline" onClick={testProxy} disabled={busy}>
              Test proxy
            </Button>
            <Button onClick={saveProxy} disabled={busy}>
              Save proxy
            </Button>
          </div>
          <p className={`text-xs ${proxyTestMsg.includes("Fail") ? "text-destructive" : "text-muted-foreground"}`}>
            Target: https://labs.google/ {proxyTestMsg ? `— ${proxyTestMsg}` : ""}
          </p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Generation timeouts</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div>
            <Label>Image timeout (s)</Label>
            <Input type="number" className="mt-1" value={imgTimeout} onChange={(e) => setImgTimeout(e.target.value)} min={60} max={3600} />
          </div>
          <div>
            <Label>Video timeout (s)</Label>
            <Input type="number" className="mt-1" value={vidTimeout} onChange={(e) => setVidTimeout(e.target.value)} min={60} max={7200} />
          </div>
          <div>
            <Label>Max retries</Label>
            <Input type="number" className="mt-1" value={maxRetries} onChange={(e) => setMaxRetries(e.target.value)} min={1} />
          </div>
          {extensionModeActive ? (
            <>
              <div className="flex items-center gap-2">
                <Switch checked={extensionGenerationEnabled} onCheckedChange={setExtensionGenerationEnabled} />
                <Label>Extension-first media generation</Label>
              </div>
              <div>
                <Label>Extension fallback mode</Label>
                <Select
                  value={extensionGenerationFallbackMode}
                  onValueChange={(v) => setExtensionGenerationFallbackMode(v as "local_http_on_recaptcha" | "none")}
                >
                  <SelectTrigger className="mt-1">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="local_http_on_recaptcha">Fallback to local HTTP on reCAPTCHA rejection</SelectItem>
                    <SelectItem value="none">No fallback</SelectItem>
                  </SelectContent>
                </Select>
              </div>
            </>
          ) : (
            <p className="text-xs text-muted-foreground">
              Extension generation settings are available only when captcha method is <code>extension</code>. In headed
              browser mode, extension path is not used.
            </p>
          )}
          <Button onClick={saveGeneration} disabled={busy}>
            Save
          </Button>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Token polling</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div>
            <Label>Mode</Label>
            <Select value={callMode} onValueChange={(v) => setCallMode(v as "default" | "polling")}>
              <SelectTrigger className="mt-1">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="default">Random (default)</SelectItem>
                <SelectItem value="polling">Sequential polling</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <Button onClick={saveCallLogic} disabled={busy}>
            Save
          </Button>
        </CardContent>
      </Card>

      <Card className="lg:col-span-2">
        <CardHeader>
          <CardTitle>Chrome plugin</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex gap-2">
            <Input readOnly className="font-mono text-sm" value={pluginUrl} />
            <Button type="button" variant="secondary" onClick={() => copyText("URL", pluginUrl)}>
              Copy URL
            </Button>
          </div>
          <div className="flex gap-2">
            <Input className="font-mono text-sm" value={pluginToken} onChange={(e) => setPluginToken(e.target.value)} placeholder="Connection token" />
            <Button type="button" variant="secondary" onClick={genPluginToken}>
              Random
            </Button>
            <Button type="button" variant="secondary" onClick={() => copyText("Token", pluginToken)}>
              Copy
            </Button>
          </div>
          <div className="flex items-center gap-2">
            <Switch checked={pluginAutoEnable} onCheckedChange={setPluginAutoEnable} />
            <Label>Auto-enable token on plugin update</Label>
          </div>
          <Button onClick={savePlugin} disabled={busy}>
            Save plugin settings
          </Button>
        </CardContent>
      </Card>

      <Card className="lg:col-span-2">
        <CardHeader>
          <CardTitle>Captcha</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4 max-h-[70vh] overflow-y-auto">
          <div>
            <Label>Method</Label>
            <Select value={captcha.captcha_method} onValueChange={(v) => setCaptcha((c) => ({ ...c, captcha_method: v }))}>
              <SelectTrigger className="mt-1">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectGroup>
                  <SelectLabel>Extension mode</SelectLabel>
                  <SelectItem value="extension">Extension mode (Chrome WebSocket)</SelectItem>
                </SelectGroup>
                <SelectSeparator />
                <SelectGroup>
                  <SelectLabel>Third-party solving APIs</SelectLabel>
                  <SelectItem value="yescaptcha">YesCaptcha</SelectItem>
                  <SelectItem value="capmonster">CapMonster</SelectItem>
                  <SelectItem value="ezcaptcha">EzCaptcha</SelectItem>
                  <SelectItem value="capsolver">CapSolver</SelectItem>
                </SelectGroup>
                <SelectSeparator />
                <SelectGroup>
                  <SelectLabel>Server / browser automation</SelectLabel>
                  <SelectItem value="browser">Headed browser</SelectItem>
                  <SelectItem value="personal">Built-in browser</SelectItem>
                  <SelectItem value="remote_browser">Remote browser gateway</SelectItem>
                </SelectGroup>
              </SelectContent>
            </Select>
            {m === "extension" ? (
              <p className="text-xs text-muted-foreground mt-1">
                Uses your Chrome extension connected to <code className="rounded bg-muted px-1">/captcha_ws</code> instead
                of headed Playwright/Chromium. reCAPTCHA requests use the shared captcha worker pool first. Token-bound
                refresh workers are reserved for ST refresh only.
              </p>
            ) : null}
          </div>
          {m === "extension" ? (
            <div>
              <Label>Extension queue wait timeout (s)</Label>
              <Input
                type="number"
                min={1}
                max={120}
                value={captcha.extension_queue_wait_timeout_seconds}
                onChange={(e) =>
                  setCaptcha((c) => ({
                    ...c,
                    extension_queue_wait_timeout_seconds: Math.max(1, Math.min(120, parseInt(e.target.value, 10) || 20)),
                  }))
                }
              />
              <p className="text-xs text-muted-foreground mt-1">
                Captcha requests wait for a healthy captcha worker up to this timeout. End user captcha workers are
                identified by managed API key only.
              </p>
            </div>
          ) : null}

          {(m === "yescaptcha" || !m) && (
            <div className="space-y-2 border rounded-md p-3">
              <Label>YesCaptcha API key</Label>
              <Input value={captcha.yescaptcha_api_key} onChange={(e) => setCaptcha((c) => ({ ...c, yescaptcha_api_key: e.target.value }))} />
              <Label>Base URL</Label>
              <Input value={captcha.yescaptcha_base_url} onChange={(e) => setCaptcha((c) => ({ ...c, yescaptcha_base_url: e.target.value }))} />
              <Label>YesCaptcha task type</Label>
              <Select
                value={captcha.yescaptcha_task_type}
                onValueChange={(v) => setCaptcha((c) => ({ ...c, yescaptcha_task_type: v }))}
              >
                <SelectTrigger>
                  <SelectValue placeholder="Task type" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="RecaptchaV3TaskProxyless">RecaptchaV3TaskProxyless — 20 points</SelectItem>
                  <SelectItem value="RecaptchaV3TaskProxylessM1">RecaptchaV3TaskProxylessM1 — 25 points</SelectItem>
                  <SelectItem value="RecaptchaV3TaskProxylessM1S7">RecaptchaV3TaskProxylessM1S7 — 30 points (minScore 0.7)</SelectItem>
                  <SelectItem value="RecaptchaV3TaskProxylessM1S9">RecaptchaV3TaskProxylessM1S9 — 35 points (minScore 0.9)</SelectItem>
                </SelectContent>
              </Select>
              <p className="text-xs text-muted-foreground">
                S7/S9 send <code className="text-xs">minScore</code> 0.7 / 0.9 on createTask.
              </p>
            </div>
          )}
          {m === "capmonster" && (
            <div className="space-y-2 border rounded-md p-3">
              <Label>CapMonster API key</Label>
              <Input value={captcha.capmonster_api_key} onChange={(e) => setCaptcha((c) => ({ ...c, capmonster_api_key: e.target.value }))} />
              <Label>Base URL</Label>
              <Input value={captcha.capmonster_base_url} onChange={(e) => setCaptcha((c) => ({ ...c, capmonster_base_url: e.target.value }))} />
              <Label>Minimum score</Label>
              <Input
                type="number"
                min={0.1}
                max={0.9}
                step={0.1}
                value={captcha.capmonster_min_score}
                onChange={(e) =>
                  setCaptcha((c) => ({
                    ...c,
                    capmonster_min_score: Number(e.target.value),
                  }))
                }
              />
              <p className="text-xs text-muted-foreground">
                Sent as <code className="text-xs">minScore</code> for CapMonster Enterprise tasks. Allowed range: 0.1–0.9.
              </p>
            </div>
          )}
          {m === "ezcaptcha" && (
            <div className="space-y-2 border rounded-md p-3">
              <Label>EzCaptcha API key</Label>
              <Input value={captcha.ezcaptcha_api_key} onChange={(e) => setCaptcha((c) => ({ ...c, ezcaptcha_api_key: e.target.value }))} />
              <Label>Base URL</Label>
              <Input value={captcha.ezcaptcha_base_url} onChange={(e) => setCaptcha((c) => ({ ...c, ezcaptcha_base_url: e.target.value }))} />
            </div>
          )}
          {m === "capsolver" && (
            <div className="space-y-2 border rounded-md p-3">
              <Label>CapSolver API key</Label>
              <Input value={captcha.capsolver_api_key} onChange={(e) => setCaptcha((c) => ({ ...c, capsolver_api_key: e.target.value }))} />
              <Label>Base URL</Label>
              <Input value={captcha.capsolver_base_url} onChange={(e) => setCaptcha((c) => ({ ...c, capsolver_base_url: e.target.value }))} />
            </div>
          )}
          {m === "browser" && (
            <div className="space-y-2 border rounded-md p-3">
              <Label>Captcha page URL</Label>
              <Input
                value={captcha.browser_captcha_page_url}
                onChange={(e) => setCaptcha((c) => ({ ...c, browser_captcha_page_url: e.target.value }))}
              />
              <div className="flex items-center gap-2">
                <Switch
                  checked={captcha.browser_fallback_to_remote_browser}
                  onCheckedChange={(v) =>
                    setCaptcha((c) => ({ ...c, browser_fallback_to_remote_browser: v }))
                  }
                />
                <Label>Fallback to gateway on browser failure</Label>
              </div>
              <div className="flex items-center gap-2">
                <Switch
                  checked={captcha.session_refresh_enabled}
                  onCheckedChange={(v) => setCaptcha((c) => ({ ...c, session_refresh_enabled: v }))}
                />
                <Label>Enable session refresh (ST warmup path)</Label>
              </div>
              <div className="flex items-center gap-2">
                <Switch
                  checked={captcha.browser_proxy_enabled}
                  onCheckedChange={(v) => setCaptcha((c) => ({ ...c, browser_proxy_enabled: v }))}
                />
                <Label>Browser proxy</Label>
              </div>
              {captcha.browser_proxy_enabled ? (
                <Input value={captcha.browser_proxy_url} onChange={(e) => setCaptcha((c) => ({ ...c, browser_proxy_url: e.target.value }))} />
              ) : null}
              <Label>Browser count</Label>
              <Input
                type="number"
                min={1}
                max={20}
                value={captcha.browser_count}
                onChange={(e) => setCaptcha((c) => ({ ...c, browser_count: parseInt(e.target.value, 10) || 1 }))}
              />
            </div>
          )}
          {m === "personal" && (
            <div className="space-y-2 border rounded-md p-3">
              <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                <div className="space-y-1">
                  <Label>Browser count</Label>
                  <Input
                    type="number"
                    min={1}
                    max={20}
                    value={captcha.browser_count}
                    onChange={(e) => setCaptcha((c) => ({ ...c, browser_count: parseInt(e.target.value, 10) || 1 }))}
                  />
                  <p className="text-xs text-muted-foreground mt-1">
                    Personal browser instances. Effective slots equal browser count × tabs per browser, capped at 50.
                  </p>
                </div>
                <div className="space-y-1">
                  <Label>Project pool size</Label>
                  <Input
                    type="number"
                    value={captcha.personal_project_pool_size}
                    onChange={(e) => setCaptcha((c) => ({ ...c, personal_project_pool_size: parseInt(e.target.value, 10) || 4 }))}
                  />
                </div>
                <div className="space-y-1">
                  <Label>Max tabs per browser</Label>
                  <Input
                    type="number"
                    min={1}
                    max={50}
                    value={captcha.personal_max_resident_tabs}
                    onChange={(e) => setCaptcha((c) => ({ ...c, personal_max_resident_tabs: parseInt(e.target.value, 10) || 5 }))}
                  />
                  <p className="text-xs text-muted-foreground mt-1">
                    Each browser keeps up to this many shared captcha tabs; total effective slots are capped at 50.
                  </p>
                </div>
              </div>
              <Label>Fresh restart every N solves (0 to disable)</Label>
              <Input
                type="number"
                min={0}
                max={999}
                value={captcha.browser_personal_fresh_restart_every_n_solves}
                onChange={(e) =>
                  setCaptcha((c) => ({
                    ...c,
                    browser_personal_fresh_restart_every_n_solves: Math.max(
                      0,
                      parseInt(e.target.value, 10) || 0
                    ),
                  }))
                }
              />
              <p className="text-xs text-muted-foreground">
                For high concurrency, use 0 or at least 50; values such as 5 or 10 can cause frequent restart queues.
              </p>
              <Label>Idle TTL (s)</Label>
              <Input
                type="number"
                value={captcha.personal_idle_tab_ttl_seconds}
                onChange={(e) => setCaptcha((c) => ({ ...c, personal_idle_tab_ttl_seconds: parseInt(e.target.value, 10) || 600 }))}
              />
              <div className="flex items-center gap-2">
                <Switch
                  checked={captcha.personal_proxy_enabled}
                  onCheckedChange={(v) => setCaptcha((c) => ({ ...c, personal_proxy_enabled: v }))}
                />
                <Label>Proxy</Label>
              </div>
              {captcha.personal_proxy_enabled ? (
                <Input value={captcha.personal_proxy_url} onChange={(e) => setCaptcha((c) => ({ ...c, personal_proxy_url: e.target.value }))} />
              ) : null}
            </div>
          )}
          {m === "remote_browser" && (
            <div className="space-y-2 border rounded-md p-3">
              <Label>Remote base URL</Label>
              <Input value={captcha.remote_browser_base_url} onChange={(e) => setCaptcha((c) => ({ ...c, remote_browser_base_url: e.target.value }))} />
              <Label>API key</Label>
              <Input value={captcha.remote_browser_api_key} onChange={(e) => setCaptcha((c) => ({ ...c, remote_browser_api_key: e.target.value }))} />
              <Label>Timeout (s)</Label>
              <Input
                type="number"
                value={captcha.remote_browser_timeout}
                onChange={(e) => setCaptcha((c) => ({ ...c, remote_browser_timeout: parseInt(e.target.value, 10) || 60 }))}
              />
              <div className="flex items-center gap-2">
                <Switch
                  checked={captcha.session_refresh_enabled}
                  onCheckedChange={(v) => setCaptcha((c) => ({ ...c, session_refresh_enabled: v }))}
                />
                <Label>Enable session refresh (ST warmup path)</Label>
              </div>
            </div>
          )}
          <Button onClick={saveCaptcha} disabled={busy}>
            Save captcha settings
          </Button>
        </CardContent>
      </Card>

      {(m === "browser" || m === "remote_browser") && captcha.session_refresh_enabled ? (
        <Card className="lg:col-span-2">
          <CardHeader>
            <CardTitle>Session refresh (ST warmup)</CardTitle>
            <CardDescription>Local headed Playwright warmup before AT refresh.</CardDescription>
          </CardHeader>
          <CardContent className="space-y-2">
            <div className="flex items-center gap-2">
              <Switch
                checked={captcha.session_refresh_browser_first}
                onCheckedChange={(v) => setCaptcha((c) => ({ ...c, session_refresh_browser_first: v }))}
              />
              <Label>Browser-first before AT refresh</Label>
            </div>
            <div className="flex items-center gap-2">
              <Switch
                checked={captcha.session_refresh_inject_st_cookie}
                onCheckedChange={(v) => setCaptcha((c) => ({ ...c, session_refresh_inject_st_cookie: v }))}
              />
              <Label>Inject current ST cookie before warmup</Label>
            </div>
            <div className="flex items-center gap-2">
              <Switch
                checked={captcha.session_refresh_update_st_from_cookie}
                onCheckedChange={(v) => setCaptcha((c) => ({ ...c, session_refresh_update_st_from_cookie: v }))}
              />
              <Label>Update ST from cookie after warmup</Label>
            </div>
            <div className="flex items-center gap-2">
              <Switch
                checked={captcha.session_refresh_fail_if_st_refresh_fails}
                onCheckedChange={(v) => setCaptcha((c) => ({ ...c, session_refresh_fail_if_st_refresh_fails: v }))}
              />
              <Label>Fail AT refresh if ST refresh fails (strict)</Label>
            </div>
            <Label>Warmup URLs (comma separated)</Label>
            <Input
              value={captcha.session_refresh_warmup_urls}
              onChange={(e) => setCaptcha((c) => ({ ...c, session_refresh_warmup_urls: e.target.value }))}
            />
            <div className="grid grid-cols-2 gap-2">
              <div>
                <Label>Wait per URL (s)</Label>
                <Input
                  type="number"
                  min={0}
                  value={captcha.session_refresh_wait_seconds_per_url}
                  onChange={(e) =>
                    setCaptcha((c) => ({ ...c, session_refresh_wait_seconds_per_url: parseInt(e.target.value, 10) || 0 }))
                  }
                />
              </div>
              <div>
                <Label>Overall timeout (s)</Label>
                <Input
                  type="number"
                  min={10}
                  value={captcha.session_refresh_overall_timeout_seconds}
                  onChange={(e) =>
                    setCaptcha((c) => ({
                      ...c,
                      session_refresh_overall_timeout_seconds: parseInt(e.target.value, 10) || 180,
                    }))
                  }
                />
              </div>
            </div>
          </CardContent>
        </Card>
      ) : null}

      <Card className="lg:col-span-2">
        <CardHeader>
          <CardTitle>AT auto refresh scheduler</CardTitle>
          <CardDescription>
            Background server loop that refreshes near-expiry tokens automatically. Works for extension mode and other
            captcha modes using the same AT refresh path.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="flex items-center gap-2">
            <Switch
              checked={captcha.session_refresh_scheduler_enabled}
              onCheckedChange={(v) => setCaptcha((c) => ({ ...c, session_refresh_scheduler_enabled: v }))}
            />
            <Label>Enable scheduled auto refresh</Label>
          </div>
          {captcha.session_refresh_scheduler_enabled ? (
            <div className="grid grid-cols-1 md:grid-cols-3 gap-2">
              <div>
                <Label>Interval (min)</Label>
                <Input
                  type="number"
                  min={1}
                  value={captcha.session_refresh_scheduler_interval_minutes}
                  onChange={(e) =>
                    setCaptcha((c) => ({
                      ...c,
                      session_refresh_scheduler_interval_minutes: parseInt(e.target.value, 10) || 30,
                    }))
                  }
                />
              </div>
              <div>
                <Label>Batch size</Label>
                <Input
                  type="number"
                  min={1}
                  value={captcha.session_refresh_scheduler_batch_size}
                  onChange={(e) =>
                    setCaptcha((c) => ({
                      ...c,
                      session_refresh_scheduler_batch_size: parseInt(e.target.value, 10) || 10,
                    }))
                  }
                />
              </div>
              <div>
                <Label>Expiring window (min)</Label>
                <Input
                  type="number"
                  min={1}
                  value={captcha.session_refresh_scheduler_only_expiring_within_minutes}
                  onChange={(e) =>
                    setCaptcha((c) => ({
                      ...c,
                      session_refresh_scheduler_only_expiring_within_minutes: parseInt(e.target.value, 10) || 60,
                    }))
                  }
                />
              </div>
            </div>
          ) : (
            <p className="text-xs text-muted-foreground">
              Scheduler is disabled. Enable it to run periodic automatic AT refresh checks in the server background.
            </p>
          )}
          <div>
            <Button onClick={saveCaptcha} disabled={busy}>
              Save refresh scheduler settings
            </Button>
          </div>
        </CardContent>
      </Card>

      <Card className="lg:col-span-2">
        <CardHeader>
          <CardTitle>ST-only refresh scheduler</CardTitle>
          <CardDescription>
            Pulls the latest session-token cookie from the bound extension worker (or local headed browser fallback)
            for any token whose AT is within the expiring window or already expired. Does NOT mint a new AT. Useful
            when AT auto refresh is intentionally off but the worker may have a fresh rotated cookie. Failures are
            retried on the next tick and never disable tokens.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="flex items-center gap-2">
            <Switch
              checked={captcha.st_only_refresh_scheduler_enabled}
              onCheckedChange={(v) => setCaptcha((c) => ({ ...c, st_only_refresh_scheduler_enabled: v }))}
            />
            <Label>Enable ST-only scheduled refresh</Label>
          </div>
          {captcha.st_only_refresh_scheduler_enabled ? (
            <div className="grid grid-cols-1 md:grid-cols-3 gap-2">
              <div>
                <Label>Interval (min)</Label>
                <Input
                  type="number"
                  min={1}
                  value={captcha.st_only_refresh_scheduler_interval_minutes}
                  onChange={(e) =>
                    setCaptcha((c) => ({
                      ...c,
                      st_only_refresh_scheduler_interval_minutes: parseInt(e.target.value, 10) || 5,
                    }))
                  }
                />
              </div>
              <div>
                <Label>Batch size</Label>
                <Input
                  type="number"
                  min={1}
                  value={captcha.st_only_refresh_scheduler_batch_size}
                  onChange={(e) =>
                    setCaptcha((c) => ({
                      ...c,
                      st_only_refresh_scheduler_batch_size: parseInt(e.target.value, 10) || 20,
                    }))
                  }
                />
              </div>
              <div>
                <Label>Expiring window (min)</Label>
                <Input
                  type="number"
                  min={1}
                  value={captcha.st_only_refresh_scheduler_expiring_within_minutes}
                  onChange={(e) =>
                    setCaptcha((c) => ({
                      ...c,
                      st_only_refresh_scheduler_expiring_within_minutes: parseInt(e.target.value, 10) || 5,
                    }))
                  }
                />
              </div>
            </div>
          ) : (
            <p className="text-xs text-muted-foreground">
              Disabled. Enable to periodically pull a fresh session-token cookie for tokens whose AT is near or past
              expiry, without minting a new AT.
            </p>
          )}
          <div>
            <Button onClick={saveCaptcha} disabled={busy}>
              Save ST-only scheduler settings
            </Button>
          </div>
        </CardContent>
      </Card>

      {m === "extension" ? (
        <Card className="lg:col-span-2">
          <CardHeader>
            <CardTitle>Extension worker routing</CardTitle>
            <CardDescription>
              Chrome extension mode uses exactly three worker types: server-side captcha workers, end user captcha
              workers, and token-bound refresh workers.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="flex justify-end">
              <Button variant="outline" onClick={refreshExtensionWorkers} disabled={busy}>
                Refresh
              </Button>
            </div>

            <div className="space-y-2">
              <Label>Active workers</Label>
              <p className="text-xs text-muted-foreground">
                Worker ID is unique per connection session and changes on reconnect.
              </p>
              <div className="space-y-3">
                <div className="rounded-md border">
                  <div className="px-3 py-2 text-xs font-medium border-b bg-muted/30">Captcha worker server side</div>
                  <div className="grid grid-cols-8 gap-2 px-3 py-2 text-xs font-medium border-b">
                    <span>Worker ID</span>
                    <span>Instance</span>
                    <span>Captcha key</span>
                    <span>Label</span>
                    <span>Work</span>
                    <span>Source</span>
                    <span>Connected at</span>
                    <span>Action</span>
                  </div>
                  {captchaWorkers.map((w) => (
                    <div key={w.worker_session_id} className="grid grid-cols-8 gap-2 px-3 py-2 text-xs border-b last:border-b-0 items-start">
                      <span className="font-mono min-w-0 break-all whitespace-normal">{w.worker_session_id || "-"}</span>
                      <span className="font-mono min-w-0 break-all whitespace-normal">{w.instance_id || "-"}</span>
                      <span>{w.captcha_worker_key_prefix || w.captcha_worker_id || "-"}</span>
                      <span className="min-w-0 wrap-break-word whitespace-normal">{w.captcha_worker_key_label || "-"}</span>
                      <span>reCAPTCHA only</span>
                      <span className="min-w-0 wrap-break-word whitespace-normal">{w.binding_source || "-"}</span>
                      <span>{w.connected_at ? new Date(w.connected_at * 1000).toLocaleTimeString() : "-"}</span>
                      <span>
                        <Button
                          size="sm"
                          variant="destructive"
                          onClick={() => killExtensionWorker(w.worker_session_id)}
                          disabled={busy}
                        >
                          Kill
                        </Button>
                      </span>
                    </div>
                  ))}
                  {captchaWorkers.length === 0 ? (
                    <div className="px-3 py-3 text-xs text-muted-foreground">No captcha workers</div>
                  ) : null}
                </div>
                <div className="rounded-md border">
                  <div className="px-3 py-2 text-xs font-medium border-b bg-muted/30">End user worker for captcha</div>
                  <div className="grid grid-cols-7 gap-2 px-3 py-2 text-xs font-medium border-b">
                    <span>Worker ID</span>
                    <span>Instance</span>
                    <span>Label</span>
                    <span>Managed key</span>
                    <span>Source</span>
                    <span>Connected at</span>
                    <span>Action</span>
                  </div>
                  {endUserWorkers.map((w) => (
                    <div key={w.worker_session_id} className="grid grid-cols-7 gap-2 px-3 py-2 text-xs border-b last:border-b-0 items-start">
                      <span className="font-mono min-w-0 break-all whitespace-normal">{w.worker_session_id || "-"}</span>
                      <span className="font-mono min-w-0 break-all whitespace-normal">{w.instance_id || "-"}</span>
                      <span className="min-w-0 wrap-break-word whitespace-normal">{w.client_label || "-"}</span>
                      <span>{w.managed_api_key_id ?? "-"}</span>
                      <span className="min-w-0 wrap-break-word whitespace-normal">{w.binding_source || "-"}</span>
                      <span>{w.connected_at ? new Date(w.connected_at * 1000).toLocaleTimeString() : "-"}</span>
                      <span>
                        <Button
                          size="sm"
                          variant="destructive"
                          onClick={() => killExtensionWorker(w.worker_session_id)}
                          disabled={busy}
                        >
                          Kill
                        </Button>
                      </span>
                    </div>
                  ))}
                  {endUserWorkers.length === 0 ? (
                    <div className="px-3 py-3 text-xs text-muted-foreground">No end user captcha workers</div>
                  ) : null}
                </div>
                <div className="rounded-md border">
                  <div className="px-3 py-2 text-xs font-medium border-b bg-muted/30">Refresh workers</div>
                  <div className="grid grid-cols-7 gap-2 px-3 py-2 text-xs font-medium border-b">
                    <span>Worker ID</span>
                    <span>Instance</span>
                    <span>Token ID</span>
                    <span>Work</span>
                    <span>Source</span>
                    <span>Connected at</span>
                    <span>Action</span>
                  </div>
                  {refreshWorkers.map((w) => (
                    <div key={w.worker_session_id} className="grid grid-cols-7 gap-2 px-3 py-2 text-xs border-b last:border-b-0 items-start">
                      <span className="font-mono min-w-0 break-all whitespace-normal">{w.worker_session_id || "-"}</span>
                      <span className="font-mono min-w-0 break-all whitespace-normal">{w.instance_id || "-"}</span>
                      <span>{w.refresh_token_id ?? "-"}</span>
                      <span>ST refresh only</span>
                      <span className="min-w-0 wrap-break-word whitespace-normal">{w.binding_source || "-"}</span>
                      <span>{w.connected_at ? new Date(w.connected_at * 1000).toLocaleTimeString() : "-"}</span>
                      <span>
                        <Button
                          size="sm"
                          variant="destructive"
                          onClick={() => killExtensionWorker(w.worker_session_id)}
                          disabled={busy}
                        >
                          Kill
                        </Button>
                      </span>
                    </div>
                  ))}
                  {refreshWorkers.length === 0 ? (
                    <div className="px-3 py-3 text-xs text-muted-foreground">No refresh worker connections</div>
                  ) : null}
                </div>
              </div>
            </div>
          </CardContent>
        </Card>
      ) : null}
    </div>
  )
}
