import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, errorMessage, type Schemas } from "../api/client";
import { Badge, Platforms } from "../components/bits";
import { ago } from "../lib/format";
import { useServerEvents } from "../lib/events";

type Recomp = Schemas["RecompSummary"];
type Game = Schemas["GameSummary"];

export function Library() {
  const [games, setGames] = useState<Game[] | null>(null);
  const [recomps, setRecomps] = useState<Recomp[]>([]);
  const [recipes, setRecipes] = useState<Schemas["RecipeSummary"][]>([]);

  const load = useCallback(async () => {
    const [g, r] = await Promise.all([api.GET("/api/v1/games"), api.GET("/api/v1/recomps")]);
    if (g.data) setGames(g.data);
    if (r.data) setRecomps(r.data);
  }, []);

  useEffect(() => {
    load();
    api.GET("/api/v1/recipes").then(({ data }) => data && setRecipes(data));
  }, [load]);

  useServerEvents((e) => {
    if (e.type === "game.updated" || e.type === "recomp.updated" || e.type === "recomp.update_available")
      load();
  });

  const byId = new Map(recomps.map((r) => [r.id, r]));
  const inLibrary = new Set(recomps.map((r) => r.repo?.toLowerCase()));
  const available = recipes.filter((r) => !inLibrary.has(r.repo.toLowerCase()));
  const unsorted = recomps.filter((r) => !r.game_id);

  return (
    <>
      <AddRecomp recipes={available} />
      <section>
        <h2>Library</h2>
        {games === null ? (
          <p className="muted">Loading…</p>
        ) : games.length === 0 && unsorted.length === 0 ? (
          <p className="muted">Nothing here yet. Add a recomp above.</p>
        ) : (
          <ul className="game-groups">
            {games.map((g) => (
              <li key={g.id} className="game-group">
                <div className="game-head">
                  <Link to={`/games/${g.id}`} className="game-title">
                    {g.title}
                  </Link>
                  {g.system && <span className="chip">{g.system}</span>}
                  <span className="spacer" />
                  <InputBadge game={g} />
                </div>
                <ul className="game-list">
                  {g.recomps.map((gr) => {
                    const r = byId.get(gr.id);
                    return r ? <RecompRow key={r.id} recomp={r} /> : null;
                  })}
                </ul>
              </li>
            ))}
            {unsorted.length > 0 && (
              <li className="game-group">
                <div className="game-head">
                  <span className="game-title muted">Game not known yet</span>
                </div>
                <ul className="game-list">
                  {unsorted.map((r) => (
                    <RecompRow key={r.id} recomp={r} />
                  ))}
                </ul>
              </li>
            )}
          </ul>
        )}
      </section>
    </>
  );
}

function InputBadge({ game }: { game: Game }) {
  const needs = game.recomps.length > 0;
  return (
    <span className="row small">
      {game.suggestions > 0 && <Badge tone="info">{game.suggestions} found in ROM library</Badge>}
      {game.inputs_ready > 0 ? (
        <Badge tone="ok">
          {game.inputs_ready} input{game.inputs_ready > 1 ? "s" : ""}
        </Badge>
      ) : needs ? (
        <Badge tone="warn">No input</Badge>
      ) : null}
    </span>
  );
}

function RecompRow({ recomp: r }: { recomp: Recomp }) {
  return (
    <li>
      <Link to={`/recomps/${r.id}`} className="game-row">
        <div className="game-main">
          <span className="game-name">{r.name}</span>
          <span className="muted small">{r.repo}</span>
        </div>
        <div className="game-state">
          <RecompStatus recomp={r} />
          <span className="muted small">
            {r.current_version ? `Have ${r.current_version}` : "Nothing downloaded"}
            {r.latest_version && r.latest_version !== r.current_version ? ` · Latest ${r.latest_version}` : ""}
          </span>
        </div>
        <div className="game-platforms">
          <Platforms keys={r.platforms} />
          <span className="muted small">Checked {ago(r.last_checked_at)}</span>
        </div>
      </Link>
    </li>
  );
}

export function RecompStatus({ recomp: r }: { recomp: Recomp }) {
  if (r.recipe_error) return <Badge tone="bad">Recipe error</Badge>;
  if (r.status === "draft") return <Badge tone="warn">Draft</Badge>;
  if (r.last_check_error) return <Badge tone="bad">Check failed</Badge>;
  if (r.update_available) return <Badge tone="info">{r.current_version ? "Update available" : "Not downloaded"}</Badge>;
  if (r.current_version) return <Badge tone="ok">Up to date</Badge>;
  return <Badge>No releases</Badge>;
}

function AddRecomp({ recipes }: { recipes: Schemas["RecipeSummary"][] }) {
  const navigate = useNavigate();
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function add(body: Schemas["RecompCreate"]) {
    setBusy(true);
    setError(null);
    const { data, error } = await api.POST("/api/v1/recomps", { body });
    setBusy(false);
    if (data) navigate(`/recomps/${data.id}`);
    else setError(errorMessage(error));
  }

  function submit(e: FormEvent) {
    e.preventDefault();
    add({ repo_url: url.trim() });
  }

  return (
    <section>
      <h2>Add a recomp</h2>
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
        Repos with a built-in recipe are ready right away. Any other repo becomes a draft: check the guessed game
        and asset rules, then confirm.
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
