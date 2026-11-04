/**
 * Runtime API/WebSocket endpoint resolution.
 *
 * VITE_API_URL may be given either as the backend origin
 * ("https://profexia-api.onrender.com") or including the API prefix
 * ("https://profexia-api.onrender.com/api/v1"). Both resolve to the same
 * base so request paths are never duplicated (no ".../api/v1/api/v1/...").
 *
 * Development falls back to the local FastAPI server. Production builds never
 * fall back to localhost: without VITE_API_URL they use same-origin "/api/v1"
 * (useful behind a reverse proxy), and vite.config.ts warns at build time.
 */

export const API_PREFIX = "/api/v1";

function stripTrailingSlashes(value: string): string {
  return value.replace(/\/+$/, "");
}

export function normalizeApiBase(raw: string | undefined, fallbackOrigin: string): string {
  const value = stripTrailingSlashes((raw ?? "").trim());
  const base = value || stripTrailingSlashes(fallbackOrigin);
  return base.endsWith(API_PREFIX) ? base : `${base}${API_PREFIX}`;
}

/** Derive the WebSocket base (ws:// or wss://) from the HTTP API base. */
export function deriveWsBase(apiBase: string, rawWsUrl: string | undefined, pageOrigin: string): string {
  const override = stripTrailingSlashes((rawWsUrl ?? "").trim());
  if (override) {
    return override.endsWith(API_PREFIX) ? override : `${override}${API_PREFIX}`;
  }
  const url = new URL(apiBase, pageOrigin);
  const wsProtocol = url.protocol === "https:" ? "wss:" : "ws:";
  return `${wsProtocol}//${url.host}${stripTrailingSlashes(url.pathname)}`;
}

// `import.meta.env.DEV` is a build-time constant, so the localhost default is
// removed from production bundles entirely.
const fallbackOrigin = import.meta.env.DEV ? "http://localhost:8000" : "";

export const API_URL = normalizeApiBase(import.meta.env.VITE_API_URL, fallbackOrigin);
export const WS_URL = deriveWsBase(API_URL, import.meta.env.VITE_WS_URL, window.location.origin);
