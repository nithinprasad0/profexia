import type { AuthResponse, User } from "../types/api";
import { API_URL } from "./config";

export class ApiError extends Error {
  readonly status: number;
  readonly detail: unknown;

  constructor(status: number, detail: unknown, message = `API request failed with status ${status}`) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

/**
 * Human-readable message for any error thrown by the API client.
 * FastAPI returns `detail` as a string, or as a list of validation errors (422).
 */
export function describeApiError(error: unknown, fallback: string): string {
  if (error instanceof ApiError) {
    const { detail } = error;
    if (typeof detail === "string" && detail) return detail;
    if (Array.isArray(detail)) {
      const messages = detail
        .map((item) => (item && typeof item === "object" && "msg" in item ? String((item as { msg: unknown }).msg) : ""))
        .filter(Boolean);
      if (messages.length) return messages.join(". ");
    }
    return fallback;
  }
  if (error instanceof TypeError) {
    // fetch() rejects with TypeError on network failure or a blocked CORS request.
    return "Cannot reach the Profexia API. Check your connection and try again.";
  }
  return fallback;
}

export interface TokenStore {
  getAccessToken(): string | null;
  getRefreshToken(): string | null;
  setTokens(auth: Pick<AuthResponse, "access_token" | "refresh_token">): void;
  clear(): void;
}

const memoryStore: TokenStore = {
  getAccessToken: () => sessionStorage.getItem("profexia_access_token"),
  getRefreshToken: () => sessionStorage.getItem("profexia_refresh_token"),
  setTokens: ({ access_token, refresh_token }) => {
    sessionStorage.setItem("profexia_access_token", access_token);
    sessionStorage.setItem("profexia_refresh_token", refresh_token);
  },
  clear: () => {
    sessionStorage.removeItem("profexia_access_token");
    sessionStorage.removeItem("profexia_refresh_token");
  },
};

export const tokenStore = memoryStore;

type RequestOptions = RequestInit & { skipAuth?: boolean; retry?: boolean };

async function parseBody(response: Response): Promise<unknown> {
  // 204/205 responses (e.g. POST /auth/logout) have no body even when the server
  // labels them application/json; response.json() would throw on them.
  if (response.status === 204 || response.status === 205) return null;
  const text = await response.text();
  if (!text) return null;
  const contentType = response.headers.get("content-type") ?? "";
  if (contentType.includes("application/json")) {
    try {
      return JSON.parse(text) as unknown;
    } catch {
      return text;
    }
  }
  return text;
}

let refreshPromise: Promise<boolean> | null = null;

async function refreshAccessToken(): Promise<boolean> {
  if (refreshPromise) return refreshPromise;
  refreshPromise = (async () => {
    const refresh_token = tokenStore.getRefreshToken();
    if (!refresh_token) return false;
    try {
      const response = await fetch(`${API_URL}/auth/refresh`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh_token }),
      });
      if (!response.ok) {
        tokenStore.clear();
        return false;
      }
      const auth = (await response.json()) as AuthResponse;
      tokenStore.setTokens(auth);
      return true;
    } catch {
      tokenStore.clear();
      return false;
    } finally {
      refreshPromise = null;
    }
  })();
  return refreshPromise;
}

export async function apiFetch<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { skipAuth = false, retry = true, headers: providedHeaders, ...init } = options;
  const headers = new Headers(providedHeaders);
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");

  if (!skipAuth) {
    const token = tokenStore.getAccessToken();
    if (token) headers.set("Authorization", `Bearer ${token}`);
  }

  const response = await fetch(`${API_URL}${path.startsWith("/") ? path : `/${path}`}`, {
    ...init,
    headers,
  });
  const body = await parseBody(response);

  if (response.status === 401 && !skipAuth && retry && await refreshAccessToken()) {
    return apiFetch<T>(path, { ...options, retry: false });
  }

  if (!response.ok) {
    const detail = typeof body === "object" && body !== null && "detail" in body
      ? (body as { detail: unknown }).detail
      : body;
    throw new ApiError(response.status, detail);
  }

  return body as T;
}

export const api = {
  get: <T>(path: string, options?: RequestOptions) => apiFetch<T>(path, { ...options, method: "GET" }),
  post: <T>(path: string, body?: unknown, options?: RequestOptions) =>
    apiFetch<T>(path, { ...options, method: "POST", body: body === undefined ? undefined : JSON.stringify(body) }),
  patch: <T>(path: string, body?: unknown, options?: RequestOptions) =>
    apiFetch<T>(path, { ...options, method: "PATCH", body: body === undefined ? undefined : JSON.stringify(body) }),
  delete: <T>(path: string, options?: RequestOptions) => apiFetch<T>(path, { ...options, method: "DELETE" }),
};

export const authApi = {
  register: (body: { email: string; username: string; password: string; role?: "learner" | "teacher" }) =>
    api.post<AuthResponse>("/auth/register", body, { skipAuth: true }),
  login: (body: { email: string; password: string }) =>
    api.post<AuthResponse>("/auth/login", body, { skipAuth: true }),
  refresh: (refresh_token: string) =>
    api.post<AuthResponse>("/auth/refresh", { refresh_token }, { skipAuth: true }),
  logout: (refresh_token: string) =>
    api.post<{ logged_out: boolean }>("/auth/logout", { refresh_token }),
  me: () => api.get<User>("/users/me"),
};

export { API_URL };