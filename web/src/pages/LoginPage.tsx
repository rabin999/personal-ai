import { useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { AuthPage } from "../components/AuthPage";
import { requestMicAccess } from "../lib/audio";
import { setEntered } from "../lib/session";
import { useTheme } from "../lib/theme";

// The /login route. Presentation-only auth (see AuthPage); "continue" records
// the session flag, warms up mic permission, and routes into the companion.
export default function LoginPage() {
  const navigate = useNavigate();
  const { pref: themePref, setPref: setThemePref } = useTheme();

  const enter = useCallback(() => {
    setEntered(true);
    // Warm up the mic permission prompt before the companion mounts so the
    // pipeline works on first Start (best-effort — ignore the result here).
    void requestMicAccess();
    navigate("/", { replace: true });
  }, [navigate]);

  return (
    <AuthPage
      themePref={themePref}
      onThemeChange={setThemePref}
      onContinue={enter}
    />
  );
}
