import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { useAuth } from "./auth";
import { Login } from "./pages/Login";
import { Library } from "./pages/Library";
import { Jobs } from "./pages/Jobs";
import { Settings } from "./pages/Settings";

export function App() {
  const { me, loading, logout } = useAuth();
  if (loading) return <div className="center muted">Loading…</div>;
  if (!me) return <Login />;

  return (
    <div className="shell">
      <header className="topbar">
        <span className="brand">Recomp Manager</span>
        <nav>
          <NavLink to="/library">Library</NavLink>
          <NavLink to="/jobs">Jobs</NavLink>
          <NavLink to="/settings">Settings</NavLink>
        </nav>
        <span className="spacer" />
        <span className="muted small">{me.token_name}</span>
        <button className="link" onClick={logout}>
          Log out
        </button>
      </header>
      <main>
        <Routes>
          <Route path="/library" element={<Library />} />
          <Route path="/jobs" element={<Jobs />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="*" element={<Navigate to="/library" replace />} />
        </Routes>
      </main>
    </div>
  );
}
