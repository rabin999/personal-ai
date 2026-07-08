import { useEffect, useState } from "react";
import { ProfilePanel } from "./ProfilePanel";
import { fetchMe, logout, type Me } from "../lib/session";

// The avatar + slide-over profile, shared by every data page's header (via
// <Shell>). Without this, the profile — and its voice-speed + locale settings
// (C5/C7) — was only reachable from the Companion page; now it's on every page.
export function ProfileButton() {
  const [me, setMe] = useState<Me | null>(null);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    fetchMe().then(setMe).catch(() => setMe(null));
  }, []);

  return (
    <>
      <button
        onClick={() => setOpen(true)}
        aria-label="Open your profile"
        title="Your profile"
        className="grid h-9 w-9 shrink-0 place-items-center rounded-full bg-gradient-to-br from-sky-500 to-cyan-500 text-xs font-semibold text-white shadow-sm outline-none transition-transform hover:scale-105 focus-visible:ring-2 focus-visible:ring-sky-500"
      >
        {me?.picture ? (
          <img
            src={me.picture}
            alt=""
            referrerPolicy="no-referrer"
            className="h-full w-full rounded-full object-cover"
          />
        ) : (
          initials(me?.name || me?.email || "U")
        )}
      </button>
      <ProfilePanel
        open={open}
        onClose={() => setOpen(false)}
        onSignOut={() => void logout()}
      />
    </>
  );
}

function initials(seed: string): string {
  const parts = seed.replace(/@.*/, "").split(/[\s._-]+/).filter(Boolean);
  const s = (parts[0]?.[0] ?? "U") + (parts[1]?.[0] ?? parts[0]?.[1] ?? "");
  return s.toUpperCase();
}
