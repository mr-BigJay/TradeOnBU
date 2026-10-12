const CSRF_STORAGE = "tradeonbu_csrf";
const LEGACY_KEY_STORAGE = "tradeonbu_api_key";
const DEFAULT_TIMEOUT_MS = 25_000;

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export function isAuthError(err: unknown): boolean {
  return err instanceof ApiError && (err.status === 401 || err.status === 403);
}

export function getCsrf(): string {
  return sessionStorage.getItem(CSRF_STORAGE) || "";
}

export function setCsrf(token: string) {
  sessionStorage.setItem(CSRF_STORAGE, token);
}

/** Legacy dev fallback when SESSION_AUTH_ENABLED=0 */
export function getApiKey(): string {
  return sessionStorage.getItem(LEGACY_KEY_STORAGE) || "";
}

export function setApiKey(key: string) {
  sessionStorage.setItem(LEGACY_KEY_STORAGE, key);
}

export async function apiFetch(path: string, init: RequestInit = {}, timeoutMs = DEFAULT_TIMEOUT_MS) {
  const headers = new Headers(init.headers || {});
  const csrf = getCsrf();
  if (csrf) headers.set("X-CSRF-Token", csrf);
  const legacy = getApiKey();
  if (legacy) headers.set("X-API-Key", legacy);
  if (!headers.has("Content-Type") && init.body) {
    headers.set("Content-Type", "application/json");
  }

  const timeoutController = new AbortController();
  const timer = window.setTimeout(() => timeoutController.abort(), timeoutMs);
  const signal = init.signal ?? timeoutController.signal;

  try {
    const resp = await fetch(path, { ...init, headers, credentials: "include", signal });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ detail: resp.statusText }));
      const detail = err.detail || resp.statusText || "Request failed";
      throw new ApiError(typeof detail === "string" ? detail : JSON.stringify(detail), resp.status);
    }
    return resp.json();
  } catch (e) {
    if (e instanceof DOMException && e.name === "AbortError") {
      throw new ApiError("درخواست زمان‌دار شد — اتصال را بررسی کنید", 408);
    }
    throw e;
  } finally {
    window.clearTimeout(timer);
  }
}

/** Session probe: never throws; suitable for bootstrapping UI auth state. */
export async function fetchMe(timeoutMs = 12_000): Promise<{
  authenticated: boolean;
  username?: string;
  csrf_token?: string | null;
  onboarding?: Record<string, boolean>;
  error?: string;
}> {
  try {
    const data = await apiFetch("/api/auth/me", {}, timeoutMs);
    return data;
  } catch (e) {
    if (isAuthError(e)) {
      return { authenticated: false };
    }
    const msg = e instanceof Error ? e.message : "Session check failed";
    return { authenticated: false, error: msg };
  }
}

export async function login(username: string, password: string) {
  const data = await apiFetch("/api/auth/login", {
    method: "POST",
    body: JSON.stringify({ username, password }),
  });
  if (data.csrf_token) setCsrf(data.csrf_token);
  return data;
}

export async function logout() {
  try {
    await apiFetch("/api/auth/logout", { method: "POST" });
  } finally {
    sessionStorage.removeItem(CSRF_STORAGE);
  }
}
