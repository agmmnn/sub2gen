import type { Connection, Preferences, RuntimeState } from "./types";
import { DEFAULT_PREFERENCES, migratePreferences } from "./preferences";
export { DEFAULT_PREFERENCES } from "./preferences";

const CONNECTION_KEY = "sub2genMetadataConnection";
const PREFERENCES_KEY = "sub2genMetadataPreferences";
const RUNTIME_KEY = "sub2genMetadataRuntime";

export const DEFAULT_BASE_URL = "http://localhost:8000";

export const DEFAULT_RUNTIME: RuntimeState = {
  processing: false,
  stopped: false,
  phase: "idle",
  ownerTabId: null,
  ownerWindowId: null,
  processed: 0,
  successes: 0,
  currentPage: 1,
  currentIndex: 0,
  pageTotal: 0,
  targetTotal: null,
  activities: [],
  message: "Ready to start",
};

export async function getConnection(): Promise<Connection | null> {
  const result = await chrome.storage.local.get(CONNECTION_KEY);
  return (result[CONNECTION_KEY] as Connection | undefined) ?? null;
}

export async function saveConnection(connection: Connection): Promise<void> {
  await chrome.storage.local.set({ [CONNECTION_KEY]: connection });
}

export async function invalidateConnection(): Promise<void> {
  const connection = await getConnection();
  if (connection) await saveConnection({ ...connection, validatedAt: 0 });
}

export async function clearConnection(): Promise<void> {
  await chrome.storage.local.remove(CONNECTION_KEY);
}

export async function getPreferences(): Promise<Preferences> {
  const result = await chrome.storage.local.get(PREFERENCES_KEY);
  return migratePreferences(result[PREFERENCES_KEY]);
}

export async function savePreferences(preferences: Preferences): Promise<void> {
  await chrome.storage.local.set({ [PREFERENCES_KEY]: preferences });
}

export async function getRuntimeState(): Promise<RuntimeState> {
  const result = await chrome.storage.local.get(RUNTIME_KEY);
  const stored = result[RUNTIME_KEY] as Partial<RuntimeState> | undefined;
  const state = { ...DEFAULT_RUNTIME, ...stored };
  if (!stored?.phase) state.phase = state.processing ? "running" : state.stopped ? "paused" : "idle";
  if (!Array.isArray(state.activities)) state.activities = [];
  return state;
}

export async function saveRuntimeState(state: RuntimeState): Promise<void> {
  await chrome.storage.local.set({ [RUNTIME_KEY]: state });
}
