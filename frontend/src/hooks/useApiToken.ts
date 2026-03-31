/**
 * Fetches the API bearer token from the backend on startup.
 *
 * The token is served at GET /api/auth/token (unauthenticated) and is
 * used for all subsequent API calls and WebSocket connections.
 */

// Use relative URL so it works through Vite proxy (dev) and
// direct static serving (production) without hardcoding a port.
const _loc = typeof window !== "undefined" ? window.location : { protocol: "http:", host: "localhost:8080" };
const API_BASE = `${_loc.protocol}//${_loc.host}/api`;

let _token: string | null = null;
let _fetchPromise: Promise<string | null> | null = null;

export async function getApiToken(): Promise<string | null> {
  if (_token) return _token;

  // Avoid duplicate fetches if called concurrently
  if (_fetchPromise) return _fetchPromise;

  _fetchPromise = (async () => {
    try {
      const resp = await fetch(`${API_BASE}/auth/token`);
      if (!resp.ok) return null;
      const data = await resp.json();
      _token = data.token ?? null;
      return _token;
    } catch {
      return null;
    } finally {
      // Allow retry if the fetch failed (server may not have been ready)
      if (!_token) _fetchPromise = null;
    }
  })();

  return _fetchPromise;
}

/**
 * Wrapper around fetch() that automatically injects the bearer token.
 *
 * Accepts paths like "/api/sessions" (keeps as-is, uses relative URL
 * so Vite proxy or same-origin works) or "/sessions" (prepends /api).
 */
export async function apiFetch(
  path: string,
  options: RequestInit = {},
): Promise<Response> {
  const token = await getApiToken();
  const headers = new Headers(options.headers);
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  // Use relative URLs so the Vite dev proxy handles routing
  const url = path.startsWith("/api/") ? path : `/api${path}`;

  let resp: Response;
  try {
    resp = await fetch(url, { ...options, headers });
  } catch (err) {
    // Network error (server down, CORS, etc.) — log and re-throw
    console.warn(`[MUSE] API request failed: ${options.method ?? "GET"} ${url}`, err);
    throw err;
  }

  // If 401, the token may be stale (server restarted). Retry once with
  // a fresh token.
  if (resp.status === 401 && token) {
    _token = null;
    _fetchPromise = null;
    const newToken = await getApiToken();
    if (newToken && newToken !== token) {
      const retryHeaders = new Headers(options.headers);
      retryHeaders.set("Authorization", `Bearer ${newToken}`);
      return fetch(url, { ...options, headers: retryHeaders });
    }
  }

  // Log non-OK responses (4xx, 5xx) as warnings
  if (!resp.ok && resp.status !== 401) {
    console.warn(`[MUSE] API ${resp.status}: ${options.method ?? "GET"} ${url}`);
  }

  return resp;
}
