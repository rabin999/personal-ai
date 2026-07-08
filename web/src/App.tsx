import { useEffect, useState } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { AppHeader } from "./components/AppHeader";
import { ProfileButton } from "./components/ProfileButton";
import CompanionPage from "./pages/CompanionPage";
import LoginPage from "./pages/LoginPage";
import ConversationsPage from "./pages/ConversationsPage";
import ConversationDetailPage from "./pages/ConversationDetailPage";
import MemoriesPage from "./pages/MemoriesPage";
import { fetchMe } from "./lib/session";

// App router. BrowserRouter with REAL named paths (not a hash router): the FastAPI
// edge serves index.html for these client routes (api/app.py SPA fallback), so a
// refresh on /memories works. Routes:
//   /login          → sign in (presentation only)
//   /               → companion (voice session), guarded
//   /conversations  → the user's conversation history (paginated, date-filtered)
//   /memories       → the user's own memory space (semantic / episodic / procedural)
//   /conversations/:id → the conversation + its full per-turn trace timeline
export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/" element={<RequireAuth><CompanionPage /></RequireAuth>} />
        <Route
          path="/conversations"
          element={<RequireAuth><Shell><ConversationsPage /></Shell></RequireAuth>}
        />
        <Route
          path="/conversations/:sessionId"
          element={<RequireAuth><Shell><ConversationDetailPage /></Shell></RequireAuth>}
        />
        <Route
          path="/memories"
          element={<RequireAuth><Shell><MemoriesPage /></Shell></RequireAuth>}
        />
        {/* Traces are viewed per-conversation now (ConversationDetailPage renders
            the full trace timeline); the standalone /traces list was removed.
            Legacy /traces* links fall through to the catch-all → home. */}
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  );
}

// Real auth guard: ask the server who we are (session cookie). While checking,
// show a neutral splash; no session → bounce to /login (Google SSO).
function RequireAuth({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<"checking" | "in" | "out">("checking");
  useEffect(() => {
    fetchMe().then((u) => setState(u ? "in" : "out"));
  }, []);
  if (state === "checking") {
    return (
      <div className="grid min-h-screen place-items-center bg-slate-50 text-slate-400 dark:bg-slate-950 dark:text-slate-500">
        <div className="h-6 w-6 animate-spin rounded-full border-2 border-current border-t-transparent" />
      </div>
    );
  }
  return state === "in" ? <>{children}</> : <Navigate to="/login" replace />;
}

// Shared chrome for the per-user data pages: the single app header (brand, nav,
// external-tool links, theme) so every route looks the same (F10).
function Shell({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-screen bg-neutral-50 text-neutral-900 dark:bg-neutral-950 dark:text-neutral-100">
      <AppHeader right={<ProfileButton />} />
      <main className="mx-auto max-w-3xl px-4 py-6 pb-[max(1.5rem,env(safe-area-inset-bottom))]">
        {children}
      </main>
    </div>
  );
}
