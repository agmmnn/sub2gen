import { imageCount, recoverAfterNavigation, startProcessing, stopProcessing } from "./adobe/automation";
import { isSupportedAdobeUrl, normalizedAdobeUploadsRoute } from "./adobe-url";
import type { ProcessingMode } from "./types";

interface ContentMessage {
  action?: string;
  mode?: ProcessingMode;
  startIndex?: number;
  endIndex?: number;
  ownerTabId?: number | null;
  ownerWindowId?: number | null;
}

declare global {
  interface Window {
    __sub2genMetadataContentLoaded?: boolean;
    __sub2genMetadataLastHref?: string;
    __sub2genMetadataLocationTimer?: number;
  }
}

function supportedNow(): boolean {
  return isSupportedAdobeUrl(location.href);
}

function sendPageContext(): void {
  void chrome.runtime.sendMessage({
    type: "PAGE_CONTEXT_CHANGED",
    supported: supportedNow(),
    route: normalizedAdobeUploadsRoute(location.href),
  }).catch(() => undefined);
}

function watchLocationChanges(): void {
  const check = () => {
    if (window.__sub2genMetadataLastHref === location.href) return;
    window.__sub2genMetadataLastHref = location.href;
    sendPageContext();
    if (supportedNow()) void recoverAfterNavigation();
  };
  window.addEventListener("popstate", check);
  window.addEventListener("hashchange", check);
  window.__sub2genMetadataLocationTimer = window.setInterval(check, 700);
  check();
}

if (!window.__sub2genMetadataContentLoaded) {
  window.__sub2genMetadataContentLoaded = true;
  chrome.runtime.onMessage.addListener((message: ContentMessage, _sender, sendResponse) => {
  if (message.action === "ping") {
    sendResponse({ success: true, status: "alive", supported: supportedNow() });
    return false;
  }
  if (!supportedNow() && message.action !== "stopProcessing") {
    sendResponse({ success: false, error: "Open the English or Canadian Adobe Uploads page first." });
    return false;
  }
  if (message.action === "getImageCount") {
    void imageCount(message.mode || "upload").then((count) => sendResponse({ success: true, count }));
    return true;
  }
  if (message.action === "startProcessing") {
    void startProcessing(
      message.mode || "upload",
      message.startIndex || 1,
      message.endIndex || 0,
      false,
      message.ownerTabId ?? null,
      message.ownerWindowId ?? null,
    )
      .then(() => undefined)
      .catch(() => undefined);
    sendResponse({ success: true });
    return false;
  }
  if (message.action === "stopProcessing") {
    void stopProcessing().then(() => sendResponse({ success: true }));
    return true;
  }
  if (message.action === "resumeProcessing") {
    void startProcessing(message.mode || "upload", 1, 0, true, message.ownerTabId ?? null, message.ownerWindowId ?? null)
      .then(() => undefined)
      .catch(() => undefined);
    sendResponse({ success: true });
    return false;
  }
  return false;
  });

  watchLocationChanges();
}
