// Client for the resolved-user profile endpoint (backend spec §26/§2). The UI
// reads `/api/me` with the bearer token the app already holds and renders the
// static UserRecord (companion name, comm/audio prefs, enabled traits) in the
// profile panel. Read-only — no writes, no auth flow here.

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

export interface CommPrefs {
  directness?: number;
  emotional_scaffolding?: number;
  [k: string]: unknown;
}

export interface UserProfile {
  user_id: string;
  companion_name: string | null;
  audio_prefs: AudioPrefs;
  traits_enabled: Record<string, boolean>;
  comm_prefs: CommPrefs;
}

/** Fetch the resolved user's profile. Throws on a non-2xx / network failure. */
export async function fetchProfile(token: string): Promise<UserProfile> {
  const res = await fetch("/api/me", {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!res.ok) {
    throw new Error(
      res.status === 401 || res.status === 403
        ? "That token isn't recognised."
        : `Couldn't load profile (${res.status}).`,
    );
  }
  const data = (await res.json()) as UserProfile;
  return {
    user_id: data.user_id,
    companion_name: data.companion_name ?? null,
    audio_prefs: data.audio_prefs ?? {},
    traits_enabled: data.traits_enabled ?? {},
    comm_prefs: data.comm_prefs ?? {},
  };
}
