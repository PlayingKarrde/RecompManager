import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, errorMessage, type Schemas } from "../api/client";
import { Badge, Platforms } from "../components/bits";
import { ago } from "../lib/format";
import { useServerEvents } from "../lib/events";

type Game = Schemas["GameSummary"];

export function Library() {
  const [games, setGames] = useState<Game[] | null>(null);
  const [recipes, setRecipes] = useState<Schemas["RecipeSummary"][]>([]);

  const load = useCallback(async () => {
    const { data } = await api.GET("/api/v1/games");
    if (data) setGames(data);
  }, []);

  useEffect(() => {
    load();
    api.GET("/api/v1/recipes").then(({ data }) => data && setRecipes(data));
  }, [load]);

  useServerEvents((e) => {
    if (e.type === "game.updated" || e.type === "game.update_available") load();
  });

  const inLibrary = new Set((games ?? []).map((g) => g.repo?.toLowerCase()));
  const available = recipes.filter((r) => !inLibrary.has(r.repo.toLowerCase()));

  return (
    <>
      <AddGame recipes={available} />
      <section>
        <h2>Library</h2>
        {games === null ? (
          <p className="muted">Loading…</p>
        ) : games.length === 0 ? (
          <p className="muted">No games yet. Add one above.</p>
        ) : (
          <ul className="game-list">
            {games.map((g) => (
              <GameRow key={g.id} game={g} />
            ))}
          </ul>
        )}
      </section>
    </>
  );
}

function GameRow({ game: g }: { game: Game }) {
  return (
    <li>
      <Link to={`/games/${g.id}`} className="game-row">
        <div className="game-main">
          <span className="game-name">{g.name}</span>
          <span className="muted small">{g.repo}</span>
        </div>
        <div className="game-state">
          <GameStatus game={g} />
          <span className="muted small">
            {g.current_version ? `Have ${g.current_version}` : "Nothing downloaded"}
            {g.latest_version && g.latest_version !== g.current_version
              ? ` · Latest ${g.latest_version}`
              : ""}
          </span>
        </div>
        <div className="game-platforms">
          <Platforms keys={g.platforms} />
          <span className="muted small">Checked {ago(g.last_checked_at)}</span>
        </div>
      </Link>
    </li>
  );
}

export function GameStatus({ game: g }: { game: Game }) {
  if (g.recipe_error) return <Badge tone="bad">Recipe error</Badge>;
  if (g.status === "draft") return <Badge tone="warn">Draft</Badge>;
  if (g.last_check_error) return <Badge tone="bad">Check failed</Badge>;
  if (g.update_available) return <Badge tone="info">{g.current_version ? "Update available" : "Not downloaded"}</Badge>;
  if (g.current_version) return <Badge tone="ok">Up to date</Badge>;
  return <Badge>No releases</Badge>;
}

function AddGame({ recipes }: { recipes: Schemas["RecipeSummary"][] }) {
  const navigate = useNavigate();
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function add(body: Schemas["GameCreate"]) {
    setBusy(true);
    setError(null);
    const { data, error } = await api.POST("/api/v1/games", { body });
    setBusy(false);
    if (data) navigate(`/games/${data.id}`);
    else setError(errorMessage(error));
  }

  function submit(e: FormEvent) {
    e.preventDefault();
    add({ repo_url: url.trim() });
  }

  return (
    <section>
      <h2>Add a game</h2>
      <form className="row" onSubmit={submit}>
        <input
          className="grow"
          aria-label="GitHub repository"
          placeholder="https://github.com/owner/repo"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
        />
        <button type="submit" disabled={busy || !url.trim()}>
          {busy ? "Adding…" : "Add"}
        </button>
      </form>
      <p className="muted small">
        Repos with a built-in recipe are ready right away. Any other repo becomes a draft: check the
        guessed asset rules, then confirm.
      </p>
      {recipes.length > 0 && (
        <div className="row">
          <span className="muted small">Built-in:</span>
          {recipes.map((r) => (
            <button
              key={r.id}
              className="secondary small-btn"
              disabled={busy}
              title={r.description}
              onClick={() => add({ recipe_id: r.id })}
            >
              {r.name}
            </button>
          ))}
        </div>
      )}
      {error && <p className="error">{error}</p>}
    </section>
  );
}
