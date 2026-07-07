import { BrowserRouter, Navigate, NavLink, Route, Routes } from "react-router-dom";
import CompanionPage from "./pages/CompanionPage";
import LoginPage from "./pages/LoginPage";
import ConversationsPage from "./pages/ConversationsPage";
import MemoriesPage from "./pages/MemoriesPage";
import TracesPage from "./pages/TracesPage";
import { isEntered } from "./lib/session";

// App router. BrowserRouter with REAL named paths (not a hash router): the FastAPI
// edge serves index.html for these client routes (api/app.py SPA fallback), so a
// refresh on /memories works. Routes:
//   /login          → sign in (presentation only)
//   /               → companion (voice session), guarded
//   /conversations  → the user's conversation history (paginated, date-filtered)
//   /memories       → the user's own memory space (semantic / episodic / procedural)
//   /traces         → what happened per turn (readable view of the trace)
export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/" element={<RequireEntered><CompanionPage /></RequireEntered>} />
        <Route
          path="/conversations"
          element={<RequireEntered><Shell><ConversationsPage /></Shell></RequireEntered>}
        />
        <Route
          path="/memories"
          element={<RequireEntered><Shell><MemoriesPage /></Shell></RequireEntered>}
        />
        <Route
          path="/traces"
          element={<RequireEntered><Shell><TracesPage /></Shell></RequireEntered>}
        />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  );
}

function RequireEntered({ children }: { children: React.ReactNode }) {
  return isEntered() ? <>{children}</> : <Navigate to="/login" replace />;
}

// Shared chrome for the per-user data pages: a top nav with real links.
function Shell({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-screen bg-neutral-50 text-neutral-900 dark:bg-neutral-950 dark:text-neutral-100">
      <nav className="sticky top-0 z-10 flex gap-1 border-b border-neutral-200 bg-white/80 px-4 py-3 backdrop-blur dark:border-neutral-800 dark:bg-neutral-900/80">
        <NavItem to="/">Companion</NavItem>
        <NavItem to="/conversations">Conversations</NavItem>
        <NavItem to="/memories">Memories</NavItem>
        <NavItem to="/traces">Traces</NavItem>
      </nav>
      <main className="mx-auto max-w-3xl px-4 py-6">{children}</main>
    </div>
  );
}

function NavItem({ to, children }: { to: string; children: React.ReactNode }) {
  return (
    <NavLink
      to={to}
      end={to === "/"}
      className={({ isActive }) =>
        `rounded-md px-3 py-1.5 text-sm font-medium transition-colors ${
          isActive
            ? "bg-neutral-900 text-white dark:bg-white dark:text-neutral-900"
            : "text-neutral-600 hover:bg-neutral-100 dark:text-neutral-300 dark:hover:bg-neutral-800"
        }`
      }
    >
      {children}
    </NavLink>
  );
}
