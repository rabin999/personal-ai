// Tiny client session flag used as the route guard. The demo app has no real
// auth (the WebSocket authenticates with a static bearer token); this only
// records whether the user has passed through the login screen so a refresh
// keeps them on the companion route instead of bouncing back to /login.

const KEY = "companion.entered";
const TOKEN_KEY = "companion.token";
const DEFAULT_TOKEN = "static_token_abc";

/** The bearer token the app authenticates API + WS calls with. */
export function getToken(): string {
  try {
    return localStorage.getItem(TOKEN_KEY) || DEFAULT_TOKEN;
  } catch {
    return DEFAULT_TOKEN;
  }
}

export function setToken(token: string): void {
  try {
    localStorage.setItem(TOKEN_KEY, token || DEFAULT_TOKEN);
  } catch {
    /* storage unavailable — non-fatal */
  }
}

export function isEntered(): boolean {
  try {
    return localStorage.getItem(KEY) === "1";
  } catch {
    return false;
  }
}

export function setEntered(entered: boolean): void {
  try {
    if (entered) localStorage.setItem(KEY, "1");
    else localStorage.removeItem(KEY);
  } catch {
    /* storage unavailable — non-fatal */
  }
}
