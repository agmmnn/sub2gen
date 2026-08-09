import { Sub2GenApiError, generateMetadata, validateSession } from "./api";
import { ADOBE_UPLOADS_URLS } from "./adobe-url";
import { applyTitleRules } from "./title";
import { clearConnection, DEFAULT_RUNTIME, getConnection, getPreferences, invalidateConnection, saveConnection, saveRuntimeState } from "./storage";
import { normalizeBaseUrl } from "./url-policy";

interface ExtensionMessage {
  type?: string;
  action?: string;
  baseUrl?: string;
  apiKey?: string;
  imageUrl?: string;
  fileType?: string;
  title?: string;
  message?: string;
}

chrome.action.onClicked.addListener((tab) => {
  if (tab.windowId !== undefined) void chrome.sidePanel.open({ windowId: tab.windowId });
});

chrome.runtime.onInstalled.addListener(() => {
  const uploadsUrls = ADOBE_UPLOADS_URLS.map((value) => new URL(value));
  chrome.declarativeContent.onPageChanged.removeRules(undefined, () => {
    chrome.declarativeContent.onPageChanged.addRules([
      {
        conditions: uploadsUrls.flatMap((uploadsUrl) => [
          new chrome.declarativeContent.PageStateMatcher({
            pageUrl: { hostEquals: uploadsUrl.hostname, pathEquals: uploadsUrl.pathname, schemes: ["https"] },
          }),
          new chrome.declarativeContent.PageStateMatcher({
            pageUrl: { hostEquals: uploadsUrl.hostname, pathEquals: `${uploadsUrl.pathname}/`, schemes: ["https"] },
          }),
        ]),
        actions: [new chrome.declarativeContent.ShowAction()],
      },
    ]);
  });
});

async function connectionStatus(revalidate: boolean) {
  const connection = await getConnection();
  if (!connection) return { connected: false };
  if (!revalidate) {
    return { connected: connection.validatedAt > 0, baseUrl: connection.baseUrl, keyLabel: connection.keyLabel };
  }
  try {
    const session = await validateSession(connection.baseUrl, connection.apiKey);
    const refreshed = { ...connection, keyLabel: session.keyLabel, validatedAt: Date.now() };
    await saveConnection(refreshed);
    return { connected: true, baseUrl: refreshed.baseUrl, keyLabel: refreshed.keyLabel };
  } catch (error) {
    await invalidateConnection();
    return {
      connected: false,
      baseUrl: connection.baseUrl,
      error: error instanceof Error ? error.message : "Connection validation failed.",
      status: error instanceof Sub2GenApiError ? error.status : 0,
    };
  }
}

async function handleMessage(message: ExtensionMessage): Promise<unknown> {
  if (message.type === "VALIDATE_CONNECTION") {
    const baseUrl = normalizeBaseUrl(message.baseUrl || "");
    const apiKey = (message.apiKey || "").trim();
    if (!apiKey) throw new Error("Enter a sub2gen key.");
    const session = await validateSession(baseUrl, apiKey);
    await saveConnection({ baseUrl, apiKey, keyLabel: session.keyLabel, validatedAt: Date.now() });
    return { success: true, connected: true, baseUrl, keyLabel: session.keyLabel };
  }

  if (message.type === "GET_CONNECTION_STATUS") return connectionStatus(Boolean(message.action === "revalidate"));
  if (message.type === "DISCONNECT") {
    await clearConnection();
    await saveRuntimeState({ ...DEFAULT_RUNTIME, activities: [] });
    return { success: true };
  }

  if (message.action === "processImage") {
    const connection = await getConnection();
    if (!connection || !connection.validatedAt) {
      return { success: false, error: "Connect a valid sub2gen key before processing.", isFatal: true };
    }
    if (!message.imageUrl) return { success: false, error: "Adobe image URL is missing." };
    try {
      const preferences = await getPreferences();
      const data = await generateMetadata(connection, message.imageUrl, message.fileType || "photo", preferences);
      data.title = applyTitleRules(data.title, preferences);
      return { success: true, data };
    } catch (error) {
      if (error instanceof Sub2GenApiError && (error.status === 401 || error.status === 403)) await invalidateConnection();
      return {
        success: false,
        error: error instanceof Error ? error.message : "Metadata generation failed.",
        status: error instanceof Sub2GenApiError ? error.status : 0,
        retryAfter: error instanceof Sub2GenApiError ? error.retryAfter : 0,
        isFatal: error instanceof Sub2GenApiError && (error.status === 401 || error.status === 403),
      };
    }
  }

  if (message.type === "NOTIFY") {
    await chrome.notifications.create(`sub2gen-metadata-${Date.now()}`, {
      type: "basic",
      iconUrl: "icons/icon128.png",
      title: message.title || "sub2gen Metadata",
      message: message.message || "",
    });
    return { success: true };
  }
  return { success: false, error: "Unknown extension message." };
}

chrome.runtime.onMessage.addListener((message: ExtensionMessage, _sender, sendResponse) => {
  void handleMessage(message)
    .then(sendResponse)
    .catch((error) => sendResponse({
      success: false,
      error: error instanceof Error ? error.message : "Unexpected extension error.",
      status: error instanceof Sub2GenApiError ? error.status : 0,
    }));
  return true;
});
