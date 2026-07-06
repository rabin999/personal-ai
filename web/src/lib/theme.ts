import { useCallback, useEffect, useState } from "react";

export type ThemePref = "light" | "dark" | "system";

const STORAGE_KEY = "companion.theme";

function readStored(): ThemePref {
  if (typeof localStorage === "undefined") return "system";
  const v = localStorage.getItem(STORAGE_KEY);
  return v === "light" || v === "dark" || v === "system" ? v : "system";
}

function systemPrefersDark(): boolean {
  return (
    typeof matchMedia !== "undefined" &&
    matchMedia("(prefers-color-scheme: dark)").matches
  );
}

/** Resolve a preference to the concrete theme actually applied to the root. */
function resolve(pref: ThemePref): "light" | "dark" {
  return pref === "system" ? (systemPrefersDark() ? "dark" : "light") : pref;
}

function apply(pref: ThemePref): void {
  document.documentElement.setAttribute("data-theme", resolve(pref));
}

/**
 * Theme controller: persists the user's light/dark/system choice and stamps the
 * resolved theme onto <html data-theme>. Defaults to "system" and tracks OS
 * changes live while "system" is selected.
 */
export function useTheme() {
  const [pref, setPref] = useState<ThemePref>(readStored);
  const [resolved, setResolved] = useState<"light" | "dark">(() =>
    resolve(readStored()),
  );

  useEffect(() => {
    apply(pref);
    setResolved(resolve(pref));
    localStorage.setItem(STORAGE_KEY, pref);
  }, [pref]);

  // While on "system", follow live OS theme changes.
  useEffect(() => {
    if (pref !== "system" || typeof matchMedia === "undefined") return;
    const mq = matchMedia("(prefers-color-scheme: dark)");
    const onChange = () => {
      apply("system");
      setResolved(resolve("system"));
    };
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, [pref]);

  const cycle = useCallback(() => {
    setPref((p) => (p === "light" ? "dark" : p === "dark" ? "system" : "light"));
  }, []);

  return { pref, resolved, setPref, cycle };
}
