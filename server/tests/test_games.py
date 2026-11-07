"""Game → Recomp → Platform organisation."""

from __future__ import annotations

import os

from fastapi.testclient import TestClient

from recompmanager.config import Env
from recompmanager.main import create_app
from recompmanager.recipes.gameguess import guess_game_title
from tests.conftest import wait_idle, wait_job
from tests.fakegithub import FakeGitHub
from tests.test_auth import assert_error
from tests.test_library import BUILTIN_REPOS, add, blob, download


def test_guess_game_title():
    assert (
        guess_game_title(
            "Zelda64Recomp",
            "Static recompilation of Majora's Mask (and soon Ocarina of Time) for PC",
        )
        == "Majora's Mask"
    )
    assert guess_game_title("Starship", "A PC port of Star Fox 64.") == "Star Fox 64"
    assert guess_game_title("cool-recomp", None) == "Cool"


def test_builtin_recipes_create_their_games(client: TestClient, admin, github: FakeGitHub):
    github.load_all_fixtures()
    for url in BUILTIN_REPOS.values():
        add(client, admin, url)
    games = {g["id"]: g for g in client.get("/api/v1/games", headers=admin).json()}
    assert set(games) == {
        "virtua-fighter-2",
        "ocarina-of-time",
        "bloodborne",
        "dead-or-alive-xtreme-beach-volleyball",
        "tears-of-the-kingdom",
    }
    assert games["bloodborne"]["identifiers"] == {"ps4_title_id": "CUSA03173"}
    assert games["bloodborne"]["system"] == "ps4"
    assert [r["id"] for r in games["ocarina-of-time"]["recomps"]] == ["shipwright"]
    r = client.get("/api/v1/recomps/shipwright", headers=admin).json()
    assert r["game_id"] == "ocarina-of-time"
    assert r["game_title"] == "The Legend of Zelda: Ocarina of Time"


def test_draft_guesses_game_and_user_can_change_it(client, admin, github: FakeGitHub):
    github.add_repo("someone/mm-recomp", "Static recompilation of Majora's Mask for PC")
    github.publish("someone/mm-recomp", "v1", {"MM-windows.zip": blob("mm")})
    r = add(client, admin, "someone/mm-recomp")
    assert r["game_id"] == "majoras-mask" and r["game_title"] == "Majora's Mask"
    assert "game" in r["recipe"]["unverified"]

    # A second recomp of the same game joins it.
    github.add_repo("other/2ship", "A port of Majora's Mask")
    github.publish("other/2ship", "v1", {"2Ship-windows.zip": blob("2s")})
    r2 = add(client, admin, "other/2ship")
    assert r2["game_id"] == "majoras-mask"
    game = client.get("/api/v1/games/majoras-mask", headers=admin).json()
    assert {x["id"] for x in game["recomps"]} == {"mm-recomp", "2ship"}

    # Re-point one at another (new) game by title; confirming the game clears "unverified".
    r = client.patch(
        "/api/v1/recomps/2ship", json={"game_title": "Majora's Mask 3D"}, headers=admin
    ).json()
    assert r["game_id"] == "majoras-mask-3d"
    assert "game" not in r["recipe"]["unverified"]
    # ...and back by id.
    r = client.patch("/api/v1/recomps/2ship", json={"game_id": "majoras-mask"}, headers=admin)
    assert r.json()["game_id"] == "majoras-mask"
    assert_error(
        client.patch("/api/v1/recomps/2ship", json={"game_id": "nope"}, headers=admin),
        404,
        "not_found",
    )


def test_rename_game_updates_recipes(client, admin, github, env: Env):
    github.load_all_fixtures()
    add(client, admin, BUILTIN_REPOS["vf2-recomp"])
    r = client.patch(
        "/api/v1/games/virtua-fighter-2", json={"title": "Virtua Fighter 2 (Arcade)"}, headers=admin
    )
    assert r.json()["title"] == "Virtua Fighter 2 (Arcade)"
    assert "Virtua Fighter 2 (Arcade)" in (env.recipes_dir / "vf2-recomp.yaml").read_text("utf-8")
    assert_error(client.delete("/api/v1/games/virtua-fighter-2", headers=admin), 409, "in_use")
    client.delete("/api/v1/recomps/vf2-recomp", headers=admin)
    assert client.delete("/api/v1/games/virtua-fighter-2", headers=admin).status_code == 204


def _shared_asset_repo(github: FakeGitHub) -> None:
    github.add_repo("someone/shared", "A recomp of Shared Game")
    github.publish("someone/shared", "v1", {"Shared-1.zip": blob("s1")})


def test_one_file_for_two_platforms_is_linked(client, admin, github, env: Env):
    _shared_asset_repo(github)
    add(client, admin, "someone/shared")
    client.patch(
        "/api/v1/recomps/shared",
        json={
            "status": "active",
            "assets": {"windows-x64": {"match": "Shared"}, "linux-x64": {"match": "Shared"}},
        },
        headers=admin,
    )
    wait_idle(client, admin)
    assert download(client, admin, "shared")["status"] == "succeeded"
    base = env.data_dir / "library/shared-game/shared"
    win, lin = base / "windows-x64/v1/Shared-1.zip", base / "linux-x64/v1/Shared-1.zip"
    assert win.read_bytes() == lin.read_bytes() == blob("s1")
    arts = client.get("/api/v1/recomps/shared/versions/v1/artifacts", headers=admin).json()
    assert sorted(a["platform"] for a in arts) == ["linux-x64", "windows-x64"]
    # Only one download happened.
    gets = [r for r in github.requests if r.url.path.endswith("/Shared-1.zip")]
    assert len(gets) == 1


def test_changing_game_moves_files(client, admin, github, env: Env):
    _shared_asset_repo(github)
    add(client, admin, "someone/shared")
    client.patch(
        "/api/v1/recomps/shared",
        json={"status": "active", "assets": {"windows-x64": {"match": "Shared"}}},
        headers=admin,
    )
    wait_idle(client, admin)
    download(client, admin, "shared")
    old = env.data_dir / "library/shared-game/shared/windows-x64/v1/Shared-1.zip"
    assert old.is_file()
    client.patch("/api/v1/recomps/shared", json={"game_title": "Better Name"}, headers=admin)
    wait_idle(client, admin)
    new = env.data_dir / "library/better-name/shared/windows-x64/v1/Shared-1.zip"
    assert new.read_bytes() == blob("s1")
    assert not old.exists() and not (env.data_dir / "library/shared-game").exists()
    arts = client.get("/api/v1/recomps/shared/versions/v1/artifacts", headers=admin).json()
    r = client.get(f"/api/v1/artifacts/{arts[0]['id']}/download", headers=admin)
    assert r.content == blob("s1")


def test_upgrade_from_old_layout_relocates(env: Env, github: FakeGitHub, admin_token):
    """Files stored by the Phase 1 layout move on the next start."""
    _shared_asset_repo(github)
    headers = {"Authorization": f"Bearer {admin_token}"}
    app = create_app(env, http_transport=github.transport())
    with TestClient(app) as c:
        add(c, headers, "someone/shared")
        c.patch(
            "/api/v1/recomps/shared",
            json={
                "status": "active",
                "assets": {"windows-x64": {"match": "Shared"}, "linux-x64": {"match": "Shared"}},
            },
            headers=headers,
        )
        wait_idle(c, headers)
        job = c.post("/api/v1/recomps/shared/update", headers=headers).json()
        wait_job(c, job["id"], headers)
        # Rewind to how Phase 1 stored it: one file, both rows pointing at it.
        from recompmanager.models import Artifact

        lib = env.data_dir / "library"
        old_rel = "library/shared/v1/Shared-1.zip"
        (lib / "shared/v1").mkdir(parents=True)
        os.replace(
            lib / "shared-game/shared/windows-x64/v1/Shared-1.zip", lib / "shared/v1/Shared-1.zip"
        )
        (lib / "shared-game/shared/linux-x64/v1/Shared-1.zip").unlink()
        with c.app.state.sessionmaker() as s:
            for a in s.query(Artifact):
                a.rel_path = old_rel
            s.commit()

    with TestClient(create_app(env, http_transport=github.transport())) as c:
        wait_idle(c, headers)
        base = env.data_dir / "library/shared-game/shared"
        assert (base / "windows-x64/v1/Shared-1.zip").read_bytes() == blob("s1")
        assert (base / "linux-x64/v1/Shared-1.zip").read_bytes() == blob("s1")
        assert not (env.data_dir / "library/shared").exists()
        jobs = c.get("/api/v1/jobs?recomp_id=shared", headers=headers).json()
        assert jobs[0]["kind"] == "relocate" and jobs[0]["status"] == "succeeded"
