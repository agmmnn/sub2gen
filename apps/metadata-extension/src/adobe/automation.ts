import { activityFor, upsertActivity } from "../runtime";
import { getPreferences, getRuntimeState, saveRuntimeState } from "../storage";
import type { ActivityPhase, GeneratedMetadata, ProcessingMode, RuntimeState } from "../types";
import { addProcessingOverlay, applyPortfolioMetadata, applyUploadMetadata, assetImages, delay, detectAssetType, nextPageButton, openAsset } from "./dom";

const JOB_KEY = "sub2genMetadataJob";

interface JobState {
  active: boolean;
  navigating: boolean;
  ownerTabId: number | null;
  ownerWindowId: number | null;
  mode: ProcessingMode;
  startIndex: number;
  endIndex: number;
  nextIndex: number;
}

let running = false;
let stopRequested = false;
let stopAsError = false;
let stopMessage = "";

async function updateRuntime(patch: Partial<RuntimeState>): Promise<RuntimeState> {
  const state = { ...await getRuntimeState(), ...patch };
  await saveRuntimeState(state);
  void chrome.runtime.sendMessage({ type: "PROCESSING_UPDATE", state });
  return state;
}

async function saveJob(job: JobState): Promise<void> {
  await chrome.storage.local.set({ [JOB_KEY]: job });
}

async function getJob(): Promise<JobState | null> {
  const value = await chrome.storage.local.get(JOB_KEY);
  return (value[JOB_KEY] as JobState | undefined) ?? null;
}

async function updateActivity(
  assetNumber: number,
  phase: ActivityPhase,
  message: string,
  patch: Partial<RuntimeState> = {},
): Promise<RuntimeState> {
  const current = await getRuntimeState();
  const activity = activityFor(current, assetNumber, phase, message);
  return updateRuntime({ ...patch, activities: upsertActivity(current.activities, activity) });
}

async function generate(image: HTMLImageElement, mode: ProcessingMode): Promise<GeneratedMetadata> {
  const response = await chrome.runtime.sendMessage({ action: "processImage", imageUrl: image.src, fileType: detectAssetType(mode) });
  if (!response?.success) {
    const error = new Error(response?.error || "sub2gen metadata generation failed.");
    Object.assign(error, { fatal: Boolean(response?.isFatal), status: response?.status });
    throw error;
  }
  return response.data as GeneratedMetadata;
}

async function processPage(job: JobState): Promise<void> {
  const images = assetImages(job.mode);
  if (!images.length) throw new Error("No Adobe Stock images were found on this page.");
  const first = Math.max(job.nextIndex, job.startIndex - 1, 0);
  const limit = job.endIndex > 0 ? Math.min(job.endIndex, images.length) : images.length;
  let consecutiveFailures = 0;

  for (let index = first; index < limit && !stopRequested; index += 1) {
    const image = images[index];
    const assetNumber = index + 1;
    const removeOverlay = addProcessingOverlay(image, index, limit);
    await updateActivity(assetNumber, "generating", "Generating metadata", {
      processing: true,
      stopped: false,
      phase: "running",
      currentIndex: assetNumber,
      pageTotal: limit,
      message: `Generating metadata for asset ${assetNumber} of ${limit}`,
    });
    try {
      await openAsset(image, job.mode);
      const metadata = await generate(image, job.mode);
      if (stopRequested) {
        await updateActivity(assetNumber, "error", "Paused before Adobe was updated");
        break;
      }
      await updateActivity(assetNumber, "applying", "Applying metadata to Adobe", {
        message: `Applying metadata to asset ${assetNumber} of ${limit}`,
      });
      const preferences = await getPreferences();
      const onSaving = async () => {
        await updateActivity(assetNumber, "saving", "Saving work in Adobe", {
          message: `Saving asset ${assetNumber} of ${limit} in Adobe`,
        });
      };
      if (job.mode === "upload") await applyUploadMetadata(metadata, preferences, onSaving);
      else await applyPortfolioMetadata(metadata, onSaving);
      consecutiveFailures = 0;
      const current = await getRuntimeState();
      await updateActivity(assetNumber, "success", "Metadata applied", {
        processed: current.processed + 1,
        successes: current.successes + 1,
      });
    } catch (error) {
      consecutiveFailures += 1;
      const current = await getRuntimeState();
      const message = error instanceof Error ? error.message : "Asset processing failed.";
      await updateActivity(assetNumber, "error", message, {
        processed: current.processed + 1,
        message: `Asset ${assetNumber} failed: ${message}`,
      });
      if ((error as Error & { fatal?: boolean }).fatal) {
        void chrome.runtime.sendMessage({ type: "CONNECTION_INVALID" });
        stopRequested = true;
        stopAsError = true;
        stopMessage = message;
      } else if (consecutiveFailures >= 3) {
        stopRequested = true;
        stopAsError = true;
        stopMessage = "Stopped after three consecutive asset failures.";
        await updateRuntime({ message: stopMessage });
      }
    } finally {
      removeOverlay();
      job.nextIndex = index + 1;
      await saveJob(job);
    }
    await delay(250);
  }

  if (stopRequested || job.endIndex > 0) return;
  const next = nextPageButton();
  if (!next) return;
  job.navigating = true;
  job.nextIndex = 0;
  job.startIndex = 1;
  await saveJob(job);
  await updateRuntime({
    currentPage: (await getRuntimeState()).currentPage + 1,
    currentIndex: 0,
    pageTotal: 0,
    message: "Moving to the next page…",
  });
  const previous = images[0]?.src;
  next.click();
  for (let attempt = 0; attempt < 40; attempt += 1) {
    await delay(500);
    const current = assetImages(job.mode);
    if (current.length && current[0]?.src !== previous) {
      job.navigating = false;
      await saveJob(job);
      await processPage(job);
      return;
    }
  }
}

export async function startProcessing(
  mode: ProcessingMode,
  startIndex: number,
  endIndex: number,
  recovered = false,
  ownerTabId: number | null = null,
  ownerWindowId: number | null = null,
): Promise<void> {
  if (running) throw new Error("Processing is already running.");
  running = true;
  stopRequested = false;
  stopAsError = false;
  stopMessage = "";
  const existing = recovered ? await getJob() : null;
  const job: JobState = existing ?? {
    active: true,
    navigating: false,
    ownerTabId,
    ownerWindowId,
    mode,
    startIndex,
    endIndex,
    nextIndex: Math.max(0, startIndex - 1),
  };
  job.active = true;
  job.navigating = false;
  if (!recovered) {
    job.ownerTabId = ownerTabId;
    job.ownerWindowId = ownerWindowId;
  }
  await saveJob(job);
  if (!recovered) {
    await updateRuntime({
      processing: true,
      stopped: false,
      phase: "starting",
      ownerTabId: job.ownerTabId,
      ownerWindowId: job.ownerWindowId,
      processed: 0,
      successes: 0,
      currentPage: 1,
      currentIndex: 0,
      pageTotal: 0,
      targetTotal: endIndex > 0 ? Math.max(0, endIndex - startIndex + 1) : null,
      activities: [],
      message: "Preparing the run…",
    });
  } else {
    await updateRuntime({
      processing: true,
      stopped: false,
      phase: "starting",
      ownerTabId: job.ownerTabId,
      ownerWindowId: job.ownerWindowId,
      message: "Resuming the run…",
    });
  }
  try {
    await processPage(job);
    const state = await getRuntimeState();
    const stopped = stopRequested;
    await updateRuntime({
      processing: false,
      stopped,
      phase: stopped ? (stopAsError ? "error" : "paused") : "completed",
      message: stopped ? (stopMessage || "Run paused.") : `Complete: ${state.successes} of ${state.processed} assets updated.`,
    });
    if (!stopped) void chrome.runtime.sendMessage({ type: "NOTIFY", title: "sub2gen Metadata", message: `Completed ${state.successes} of ${state.processed} images.` });
  } catch (error) {
    await updateRuntime({ processing: false, stopped: true, phase: "error", message: error instanceof Error ? error.message : "Processing failed." });
  } finally {
    job.active = false;
    job.navigating = false;
    await saveJob(job);
    running = false;
  }
}

export async function stopProcessing(): Promise<void> {
  stopRequested = true;
  stopAsError = false;
  stopMessage = "Run paused.";
  const job = await getJob();
  if (job) await saveJob({ ...job, active: false, navigating: false });
  await updateRuntime({ processing: true, stopped: false, phase: "pausing", message: "Finishing the current step before pausing…" });
}

export async function recoverAfterNavigation(): Promise<void> {
  const job = await getJob();
  const runtime = await getRuntimeState();
  if (job?.active && job.navigating && runtime.processing) {
    await delay(700);
    void startProcessing(job.mode, 1, job.endIndex, true, job.ownerTabId, job.ownerWindowId);
  } else if (runtime.processing) {
    await updateRuntime({ processing: false, stopped: true, phase: "paused", message: "Page refresh detected. Processing paused safely." });
  }
}

export async function imageCount(mode: ProcessingMode): Promise<number> {
  return assetImages(mode).length;
}
