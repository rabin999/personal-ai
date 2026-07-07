import { useCallback } from "react";
import { AuthPage } from "../components/AuthPage";
import { requestMicAccess } from "../lib/audio";
import { loginWithGoogle } from "../lib/session";
import { useTheme } from "../lib/theme";

// The /login route. Real Google SSO (sign-in AND sign-up are the same flow):
// "Continue with Google" warms the mic permission, then hands off to the server's
// OAuth start route (which redirects to Google and back to the app on success).
export default function LoginPage() {
  const { pref: themePref, setPref: setThemePref } = useTheme();

  const signIn = useCallback(() => {
    // Warm up the mic permission prompt before we leave (best-effort), then start
    // the OAuth flow — the browser navigates away to Google.
    void requestMicAccess();
    loginWithGoogle();
  }, []);

  return (
    <AuthPage
      themePref={themePref}
      onThemeChange={setThemePref}
      onGoogle={signIn}
    />
  );
}
