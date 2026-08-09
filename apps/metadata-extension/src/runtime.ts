import type { RunActivity, RuntimeState } from "./types";

export const ACTIVITY_LIMIT = 50;

export function sanitizeActivityMessage(message: string): string {
  return message
    .replace(/https?:\/\/\S+/gi, "remote service")
    .replace(/\bBearer\s+\S+/gi, "authentication token")
    .replace(/\bs2g_[A-Za-z0-9_-]+\b/g, "API key")
    .slice(0, 120);
}

export function upsertActivity(activities: RunActivity[], activity: RunActivity): RunActivity[] {
  const existingIndex = activities.findIndex((item) => item.id === activity.id);
  const next = existingIndex >= 0
    ? activities.map((item, index) => index === existingIndex ? activity : item)
    : [...activities, activity];
  return next.slice(-ACTIVITY_LIMIT);
}

export function activityFor(
  runtime: RuntimeState,
  assetNumber: number,
  phase: RunActivity["phase"],
  message: string,
): RunActivity {
  return {
    id: `${runtime.currentPage}:${assetNumber}`,
    assetNumber,
    page: runtime.currentPage,
    phase,
    message: sanitizeActivityMessage(message),
    updatedAt: Date.now(),
  };
}
