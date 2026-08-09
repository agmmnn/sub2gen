export function isPrivateDevelopmentHost(hostname: string): boolean {
  const host = hostname.replace(/^\[|\]$/g, "").toLowerCase();
  if (host === "localhost" || host === "::1" || host === "127.0.0.1") return true;
  const octets = host.split(".").map(Number);
  if (octets.length !== 4 || octets.some((part) => !Number.isInteger(part) || part < 0 || part > 255)) return false;
  return (
    octets[0] === 10 ||
    (octets[0] === 172 && octets[1] >= 16 && octets[1] <= 31) ||
    (octets[0] === 192 && octets[1] === 168) ||
    octets[0] === 127
  );
}

export function normalizeBaseUrl(raw: string): string {
  let url: URL;
  try {
    url = new URL(raw.trim());
  } catch {
    throw new Error("Enter a valid sub2gen URL.");
  }
  if (url.username || url.password || url.search || url.hash) {
    throw new Error("The Base URL cannot contain credentials, a query, or a fragment.");
  }
  if (url.protocol !== "https:" && !(url.protocol === "http:" && isPrivateDevelopmentHost(url.hostname))) {
    throw new Error("Use HTTPS. HTTP is allowed only for localhost and private development addresses.");
  }
  url.pathname = url.pathname.replace(/\/+$/, "");
  return url.toString().replace(/\/$/, "");
}

export function permissionOrigin(baseUrl: string): string {
  return `${new URL(baseUrl).origin}/*`;
}

export async function ensureOriginPermission(baseUrl: string): Promise<boolean> {
  const origins = [permissionOrigin(baseUrl)];
  if (await chrome.permissions.contains({ origins })) return true;
  return chrome.permissions.request({ origins });
}
