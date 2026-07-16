import "server-only";

import { getServerBackendUrl } from "@/lib/apiClient";

let cachedAuthProvider: string | null = null;

/**
 * Fetches the auth provider from the backend health endpoint and caches it.
 * Falls back to 'local' on error.
 */
export async function getAuthProvider(): Promise<string> {
  if (cachedAuthProvider) {
    return cachedAuthProvider;
  }

  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 2000);

  try {
    const backendUrl = getServerBackendUrl();
    const res = await fetch(`${backendUrl}/api/v1/health`, {
      signal: controller.signal,
      cache: 'no-store',
    });
    if (res.ok) {
      const data = await res.json();
      cachedAuthProvider = (data.auth_provider as string) || "local";
      return cachedAuthProvider;
    }
  } catch {
    // Backend not reachable — fall back to local
  } finally {
    clearTimeout(timeoutId);
  }

  cachedAuthProvider = "local";
  return cachedAuthProvider;
}
