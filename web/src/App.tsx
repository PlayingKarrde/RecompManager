import { Link, NavLink, Navigate, Route, Routes } from "react-router-dom";
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
        <span className="muted small">{me.username ?? me.token_name}</span>
        <button className="link" onClick={logout}>
          Sign out
        </button>
      </header>
      <main>
        {!me.password_set && (
          <div className="banner">
            Set a username and password so you can sign in from any browser.{" "}
            <Link to="/settings#account">Set it up</Link>
          </div>
        )}
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
