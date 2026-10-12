const API_KEY_STORAGE = "tradeonbu_api_key";

export function getApiKey(): string {
  return sessionStorage.getItem(API_KEY_STORAGE) || "";
}

export function setApiKey(key: string) {
  sessionStorage.setItem(API_KEY_STORAGE, key);
}

export async function apiFetch(path: string, init: RequestInit = {}) {
  const headers = new Headers(init.headers || {});
  const key = getApiKey();
  if (key) headers.set("X-API-Key", key);
  if (!headers.has("Content-Type") && init.body) {
    headers.set("Content-Type", "application/json");
  }
  const resp = await fetch(path, { ...init, headers });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }));
    throw new Error(err.detail || "Request failed");
  }
  return resp.json();
}
