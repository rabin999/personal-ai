// Client for the resolved-user profile endpoint (backend spec §26/§2). The UI
// reads `/api/me` with the session cookie (Google SSO) and renders the resolved
// UserRecord (companion name, comm/audio prefs, enabled traits) in the profile
// panel. Read-only — no writes, no auth flow here.

export interface AudioPrefs {
  vad_threshold?: number;
  vad_min?: number;
  vad_max?: number;
  endpoint_short_pause_ms?: number;
  endpoint_long_pause_ms?: number;
  aec?: boolean;
  noise_suppress?: boolean;
  agc?: boolean;
  [k: string]: unknown;
}

export interface LocaleProfile {
  timezone?: string;
  city?: string;
  country?: string;
  units?: "metric" | "imperial" | "";
  currency?: string;
  language?: string;
}

export interface UserProfile {
  user_id: string;
  companion_name: string | null;
  audio_prefs: AudioPrefs;
  traits_enabled: Record<string, boolean>;
  locale: LocaleProfile;
}

/** Save voice speed (C7), locale (C5), and the U12 listening/privacy toggles.
 * Partial — only sent keys change; toggles take effect on the next reply (live). */
export async function updatePrefs(
  patch: {
    voice_speed?: number;
    locale?: LocaleProfile;
    ambient_mode?: "near" | "surroundings";
    transcribe_others?: boolean;
  },
): Promise<{ voice_speed: number; locale: LocaleProfile }> {
  const res = await fetch("/api/prefs", {
    method: "PATCH",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  if (!res.ok) throw new Error(`Couldn't save (${res.status}).`);
  return (await res.json()) as { voice_speed: number; locale: LocaleProfile };
}

/** Auto-detect the browser's IANA timezone and persist it to the profile so the
 * companion knows the user's real local time (time-of-day greetings, "tonight",
 * relative clocks). Runs once after sign-in; best-effort — never blocks the app.
 * Merges into the existing locale so other fields aren't wiped, and only PATCHes
 * when the timezone actually changed. This replaces the manual timezone field
 * (dynamic, not fixed) — the user should never have to type their timezone. */
export async function syncBrowserTimezone(): Promise<void> {
  try {
    const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
    if (!tz) return;
    const profile = await fetchProfile();
    if (profile.locale.timezone === tz) return;
    await updatePrefs({ locale: { ...profile.locale, timezone: tz } });
  } catch {
    // best-effort: a failed tz sync must never break the app
  }
}

/** Fetch the resolved user's profile. Throws on a non-2xx / network failure. */
export async function fetchProfile(): Promise<UserProfile> {
  const res = await fetch("/api/me", { credentials: "include" });
  if (!res.ok) {
    throw new Error(
      res.status === 401 || res.status === 403
        ? "Your session has expired — please sign in again."
        : `Couldn't load profile (${res.status}).`,
    );
  }
  const data = (await res.json()) as UserProfile;
  return {
    user_id: data.user_id,
    companion_name: data.companion_name ?? null,
    audio_prefs: data.audio_prefs ?? {},
    traits_enabled: data.traits_enabled ?? {},
    locale: data.locale ?? {},
  };
}
