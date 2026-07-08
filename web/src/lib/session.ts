// Real session-based auth (Google SSO). The session is a secure, httpOnly cookie
// the server sets on the OAuth callback — the client can't read it, so we learn
// who we are by asking /auth/me. All API/WS calls send the cookie automatically
// (same-origin); the WS handshake carries it too, so no bearer token anywhere.

export interface Me {
  user_id: string;
  email: string;
  name?: string | null;
  picture?: string | null;
}

/** Current signed-in user, or null if there's no valid session. */
export async function fetchMe(): Promise<Me | null> {
  try {
    const res = await fetch("/auth/me", { credentials: "include" });
    if (!res.ok) return null;
    return (await res.json()) as Me;
  } catch {
    return null;
  }
}

/** Start the Google sign-in / sign-up flow (same route does both). */
export function loginWithGoogle(): void {
  window.location.href = "/auth/google/login";
}

/** Clear the session and return to the login screen. */
export async function logout(): Promise<void> {
  try {
    await fetch("/auth/logout", { method: "POST", credentials: "include" });
  } catch {
    /* best-effort — navigate away regardless */
  }
  window.location.href = "/login";
}

/** Permanently delete the account + ALL data, then return to login. */
export async function deleteAccount(): Promise<void> {
  try {
    await fetch("/auth/account", { method: "DELETE", credentials: "include" });
  } catch {
    /* best-effort — navigate away regardless */
  }
  window.location.href = "/login";
}
