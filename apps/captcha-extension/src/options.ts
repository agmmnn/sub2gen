import { normalizeWebSocketUrl } from "@sub2gen/extension-core"

import {
  DEFAULT_SETTINGS,
  DEFAULT_WORKER_PAGE_URL,
  clampAccountInterval,
  clampWorkerRecaptchaSettleMs,
  loadSettings,
  normalizeWorkerPageUrl,
  type CaptchaExtensionSettings,
} from "./state/storage"
import { type WorkerMode } from "./state/worker-mode"
import { connectionPresentation, MODE_PRESENTATION } from "./ui-model"

interface RuntimeReply {
  success: boolean
  error?: string
  state?: Record<string, unknown>
}

let activeMode: WorkerMode = "endUser"

function element<T extends HTMLElement>(id: string): T {
  const node = document.getElementById(id)
  if (!node) throw new Error(`Missing settings element: ${id}`)
  return node as T
}

function runtimeMessage<T>(message: Record<string, unknown>): Promise<T> {
  return new Promise((resolve, reject) => {
    chrome.runtime.sendMessage(message, (response: T) => {
      const error = chrome.runtime.lastError
      if (error) reject(new Error(error.message))
      else resolve(response)
    })
  })
}

function saveStorage(values: Record<string, unknown>): Promise<void> {
  return new Promise((resolve, reject) => {
    chrome.storage.local.set(values, () => {
      const error = chrome.runtime.lastError
      if (error) reject(new Error(error.message))
      else resolve()
    })
  })
}

function setStatus(message: string, tone: "neutral" | "positive" | "negative" = "neutral"): void {
  const status = element<HTMLParagraphElement>("formStatus")
  status.textContent = message
  status.dataset.tone = tone
}

function setSelectValue(select: HTMLSelectElement, value: number): void {
  const stringValue = String(value)
  if (![...select.options].some((option) => option.value === stringValue)) {
    select.add(new Option(`Every ${value} minutes`, stringValue))
  }
  select.value = stringValue
}

function setMode(mode: WorkerMode): void {
  activeMode = mode
  const definitions: Array<[string, string, WorkerMode]> = [
    ["modeEndUser", "panelEndUser", "endUser"],
    ["modeCaptcha", "panelCaptcha", "captchaWorker"],
    ["modeRefresh", "panelRefresh", "refreshWorker"],
  ]

  for (const [buttonId, panelId, candidate] of definitions) {
    const selected = candidate === mode
    element<HTMLButtonElement>(buttonId).setAttribute("aria-selected", String(selected))
    element<HTMLElement>(panelId).hidden = !selected
  }

  element<HTMLParagraphElement>("modeDescription").textContent = MODE_PRESENTATION[mode].description
  element<HTMLElement>("accountSyncRow").classList.toggle("is-hidden", mode !== "endUser")
  element<HTMLElement>("accountIntervals").classList.toggle("is-hidden", mode !== "endUser")
  element<HTMLElement>("captchaReadyRow").classList.toggle("is-hidden", mode === "refreshWorker")
  element<HTMLElement>("captchaRecoveryRow").classList.toggle("is-hidden", mode === "refreshWorker")
}

function applySettings(settings: CaptchaExtensionSettings): void {
  element<HTMLInputElement>("serverUrl").value = settings.serverUrl
  element<HTMLInputElement>("clientLabel").value = settings.clientLabel
  element<HTMLInputElement>("apiKey").value = settings.apiKey
  element<HTMLInputElement>("captchaWorkerAuthKey").value = settings.captchaWorkerAuthKey
  element<HTMLInputElement>("refreshTokenId").value = settings.refreshTokenId
  element<HTMLInputElement>("accountAutoImportEnabled").checked = settings.accountAutoImportEnabled
  setSelectValue(
    element<HTMLSelectElement>("accountAutoImportIntervalMinutes"),
    settings.accountAutoImportIntervalMinutes,
  )
  setSelectValue(
    element<HTMLSelectElement>("accountRefreshIntervalMinutes"),
    settings.accountRefreshIntervalMinutes,
  )
  element<HTMLInputElement>("usePersistentWorkerTab").checked = settings.usePersistentWorkerTab
  element<HTMLInputElement>("autoRecycleWorkerTabOnCaptchaFailure").checked =
    settings.autoRecycleWorkerTabOnCaptchaFailure
  element<HTMLInputElement>("workerPageUrl").value = settings.workerPageUrl
  setSelectValue(
    element<HTMLSelectElement>("workerRecaptchaSettleMs"),
    settings.workerRecaptchaSettleMs,
  )
  setMode(settings.connectionMode)
}

async function updateConnectionStatus(): Promise<void> {
  const pill = element<HTMLElement>("connectionPill")
  const label = element<HTMLSpanElement>("connectionPillText")
  try {
    const response = await runtimeMessage<RuntimeReply>({ type: "get_status" })
    if (!response.success || !response.state) throw new Error(response.error || "status_unavailable")
    const presentation = connectionPresentation(response.state)
    pill.dataset.tone = presentation.tone
    label.textContent = presentation.label
  } catch {
    pill.dataset.tone = "negative"
    label.textContent = "Status unavailable"
  }
}

function validWebSocketUrl(value: string): boolean {
  try {
    const url = new URL(value)
    return url.protocol === "ws:" || url.protocol === "wss:"
  } catch {
    return false
  }
}

function readForm(): Record<string, unknown> {
  const serverUrl = normalizeWebSocketUrl(element<HTMLInputElement>("serverUrl").value.trim())
  if (!validWebSocketUrl(serverUrl)) throw new Error("Enter a valid ws:// or wss:// WebSocket URL.")

  const clientLabel = element<HTMLInputElement>("clientLabel").value.trim()
  const apiKey = element<HTMLInputElement>("apiKey").value.trim()
  const captchaWorkerAuthKey = element<HTMLInputElement>("captchaWorkerAuthKey").value.trim()
  const refreshTokenId = element<HTMLInputElement>("refreshTokenId").value.trim()

  if (activeMode === "endUser" && !apiKey) throw new Error("Enter a managed API key for My account mode.")
  if (activeMode === "captchaWorker" && !captchaWorkerAuthKey) {
    throw new Error("Enter a CAPTCHA worker key for CAPTCHA-only mode.")
  }
  if (activeMode === "refreshWorker" && !refreshTokenId) {
    throw new Error("Enter the token ID for Refresh-only mode.")
  }

  return {
    serverUrl,
    connectionMode: activeMode,
    clientLabel: activeMode === "endUser" ? clientLabel : "",
    apiKey: activeMode === "endUser" ? apiKey : "",
    captchaWorkerAuthKey: activeMode === "captchaWorker" ? captchaWorkerAuthKey : "",
    refreshTokenId: activeMode === "refreshWorker" ? refreshTokenId : "",
    accountAutoImportEnabled:
      activeMode === "endUser" && element<HTMLInputElement>("accountAutoImportEnabled").checked,
    accountAutoImportIntervalMinutes: clampAccountInterval(
      element<HTMLSelectElement>("accountAutoImportIntervalMinutes").value,
      DEFAULT_SETTINGS.accountAutoImportIntervalMinutes,
    ),
    accountRefreshIntervalMinutes: clampAccountInterval(
      element<HTMLSelectElement>("accountRefreshIntervalMinutes").value,
      DEFAULT_SETTINGS.accountRefreshIntervalMinutes,
    ),
    usePersistentWorkerTab: element<HTMLInputElement>("usePersistentWorkerTab").checked,
    autoRecycleWorkerTabOnCaptchaFailure: element<HTMLInputElement>(
      "autoRecycleWorkerTabOnCaptchaFailure",
    ).checked,
    workerPageUrl: normalizeWorkerPageUrl(
      element<HTMLInputElement>("workerPageUrl").value || DEFAULT_WORKER_PAGE_URL,
    ),
    workerRecaptchaSettleMs: clampWorkerRecaptchaSettleMs(
      element<HTMLSelectElement>("workerRecaptchaSettleMs").value,
    ),
  }
}

async function save(event: SubmitEvent): Promise<void> {
  event.preventDefault()
  const button = element<HTMLButtonElement>("saveButton")
  button.disabled = true
  try {
    const values = readForm()
    await saveStorage(values)
    const shouldKeepWorkerReady = values.usePersistentWorkerTab === true && activeMode !== "refreshWorker"
    await runtimeMessage<RuntimeReply>({
      type: shouldKeepWorkerReady ? "worker_tab_open" : "worker_tab_close",
    }).catch(() => undefined)
    setStatus("Saved. Reconnecting…", "positive")
    element<HTMLElement>("connectionPill").dataset.tone = "warning"
    element<HTMLSpanElement>("connectionPillText").textContent = "Connecting"
    window.setTimeout(() => void updateConnectionStatus(), 1000)
  } catch (error) {
    setStatus(error instanceof Error ? error.message : "Could not save settings.", "negative")
  } finally {
    button.disabled = false
  }
}

async function resetExtension(): Promise<void> {
  if (!window.confirm("Reset all extension settings, credentials, worker state, and history?")) return
  const button = element<HTMLButtonElement>("resetButton")
  button.disabled = true
  try {
    const response = await runtimeMessage<RuntimeReply>({ type: "reset_extension" })
    if (!response.success) throw new Error(response.error || "Reset failed")
    window.location.reload()
  } catch (error) {
    setStatus(error instanceof Error ? error.message : "Reset failed.", "negative")
    button.disabled = false
  }
}

async function initialize(): Promise<void> {
  for (const button of document.querySelectorAll<HTMLButtonElement>("[data-mode]")) {
    button.addEventListener("click", () => setMode(button.dataset.mode as WorkerMode))
  }
  element<HTMLFormElement>("settingsForm").addEventListener("submit", (event) => void save(event))
  element<HTMLButtonElement>("resetButton").addEventListener("click", () => void resetExtension())

  try {
    applySettings(await loadSettings(chrome.storage.local))
  } catch (error) {
    setStatus(error instanceof Error ? error.message : "Could not load settings.", "negative")
  }
  await updateConnectionStatus()
}

document.addEventListener("DOMContentLoaded", () => void initialize())
