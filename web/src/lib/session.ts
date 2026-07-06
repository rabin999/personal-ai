// Tiny client session flag used as the route guard. The demo app has no real
// auth (the WebSocket authenticates with a static bearer token); this only
// records whether the user has passed through the login screen so a refresh
// keeps them on the companion route instead of bouncing back to /login.

const KEY = "companion.entered";

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
