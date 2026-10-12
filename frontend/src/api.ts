const CSRF_STORAGE = "tradeonbu_csrf";
const LEGACY_KEY_STORAGE = "tradeonbu_api_key";

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

export async function apiFetch(path: string, init: RequestInit = {}) {
  const headers = new Headers(init.headers || {});
  const csrf = getCsrf();
  if (csrf) headers.set("X-CSRF-Token", csrf);
  const legacy = getApiKey();
  if (legacy) headers.set("X-API-Key", legacy);
  if (!headers.has("Content-Type") && init.body) {
    headers.set("Content-Type", "application/json");
  }
  const resp = await fetch(path, { ...init, headers, credentials: "include" });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }));
    throw new Error(err.detail || "Request failed");
  }
  return resp.json();
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
  await apiFetch("/api/auth/logout", { method: "POST" });
  sessionStorage.removeItem(CSRF_STORAGE);
}

export async function fetchMe() {
  return apiFetch("/api/auth/me");
}
