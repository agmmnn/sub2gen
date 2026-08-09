export const MANAGE_TABS = [
  "platform",
  "tokens",
  "apikeys",
  "settings",
  "logs",
  "adobe",
  "gateway",
  "runway",
  "geminigen",
  "cache",
] as const

export type ManageTab = (typeof MANAGE_TABS)[number]

export function parseManageTab(raw: string | null): ManageTab {
  if (raw && (MANAGE_TABS as readonly string[]).includes(raw)) return raw as ManageTab
  return "platform"
}
