import { HashRouter, Navigate, Route, Routes } from "react-router-dom";
import CompanionPage from "./pages/CompanionPage";
import LoginPage from "./pages/LoginPage";
import { isEntered } from "./lib/session";

// App router. HashRouter (not BrowserRouter) is deliberate: the production build
// is served by the FastAPI edge with explicit static paths only (api/app.py has
// no SPA catch-all), so hash routes keep every URL under "/" and survive a
// refresh without a server-side fallback.
//
// Routes:
//   /login  → sign in / sign up (presentation only)
//   /       → companion (voice session), guarded — redirects to /login until the
//             user has passed through the login screen.
export default function App() {
  return (
    <HashRouter>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route
          path="/"
          element={
            <RequireEntered>
              <CompanionPage />
            </RequireEntered>
          }
        />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </HashRouter>
  );
}

// Route guard: gate the companion behind the login screen. Mounting CompanionPage
// only when entered keeps its real-time session hooks from running on /login.
function RequireEntered({ children }: { children: React.ReactNode }) {
  return isEntered() ? <>{children}</> : <Navigate to="/login" replace />;
}
