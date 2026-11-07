"""rlclient command line.

rlclient login http://tower:8095            # asks for a token (read scope is enough)
rlclient list
rlclient install vf2-recomp [--dir ~/Games/VF2] [--platform linux-x64-steamdeck]
rlclient launch vf2-recomp
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import json
import platform as pyplatform
import sys
from pathlib import Path
from typing import Any

from rlclient import platforms
from rlclient.api import Api, ApiError
from rlclient.installer import Installer, InstallError, Places, launch


class Config:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or platforms.config_dir() / "config.json"
        self.data: dict[str, Any] = {}
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        self.data.setdefault("installs", {})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        with contextlib.suppress(OSError):  # no-op on Windows
            self.path.chmod(0o600)  # holds the token

    def api(self) -> Api:
        if not self.data.get("server") or not self.data.get("token"):
            raise SystemExit("Not logged in. Run: rlclient login <server-url>")
        return Api(self.data["server"], self.data["token"])


def cmd_login(cfg: Config, args, api: Api | None = None) -> int:
    token = args.token or getpass.getpass("API token (create one in the web UI, Settings): ")
    api = api or Api(args.server, token)
    me = api.get("/api/v1/auth/me")
    name = args.name or pyplatform.node() or "rlclient"
    plat = args.platform or platforms.detect()
    client = api.post("/api/v1/clients/register", {"name": name, "platform": plat})
    cfg.data.update(
        server=args.server.rstrip("/"), token=token, client_id=client["id"], platform=plat
    )
    cfg.save()
    print(
        f"Logged in to {args.server} as {me['token_name']} ({me['scope']}); this device is "
        f"{name!r}, {plat}"
    )
    return 0


def cmd_platform(cfg: Config, args, api: Api | None = None) -> int:
    print(platforms.detect())
    return 0


def cmd_list(cfg: Config, args, api: Api | None = None) -> int:
    api = api or cfg.api()
    for r in api.get("/api/v1/recomps"):
        have = cfg.data["installs"].get(r["id"], {}).get("version")
        cur = r["current_version"] or "-"
        mark = "" if not have else (" (installed)" if have == cur else f" (installed {have})")
        print(f"{r['id']:<24} {cur:<20} {r['name']}{mark}")
    return 0


def _places(cfg: Config, recomp: str, dir_: str | None) -> Places:
    prev = cfg.data["installs"].get(recomp, {}).get("dir")
    install = Path(dir_ or prev or platforms.default_games_dir() / recomp).expanduser()
    return Places(install=install.resolve(), data=platforms.data_dir())


def cmd_plan(cfg: Config, args, api: Api | None = None) -> int:
    api = api or cfg.api()
    plat = args.platform or cfg.data.get("platform") or platforms.detect()
    params = {"platform": plat} | ({"version": args.version} if args.version else {})
    print(json.dumps(api.get(f"/api/v1/recomps/{args.recomp}/install-plan", **params), indent=2))
    return 0


def cmd_install(cfg: Config, args, api: Api | None = None) -> int:
    api = api or cfg.api()
    plat = args.platform or cfg.data.get("platform") or platforms.detect()
    params = {"platform": plat} | ({"version": args.version} if args.version else {})
    plan = api.get(f"/api/v1/recomps/{args.recomp}/install-plan", **params)
    places = _places(cfg, args.recomp, args.dir)
    print(f"Installing {plan['name']} {plan['version']} ({plan['platform']}) into {places.install}")
    Installer(api, places, yes=args.yes).run(plan)
    cfg.data["installs"][args.recomp] = {
        "version": plan["version"],
        "dir": str(places.install),
        "platform": plan["platform"],
        "launch": plan["launch"],
        "saves": plan["saves"],
    }
    cfg.save()
    if cid := cfg.data.get("client_id"):
        installed = {k: v["version"] for k, v in cfg.data["installs"].items()}
        try:
            api.put(f"/api/v1/clients/{cid}/installed", {"installed": installed})
        except ApiError as e:
            print(f"Note: couldn't report this install to the server ({e.message})")
    print(f"Done. Start it with: rlclient launch {args.recomp}")
    return 0


def cmd_launch(cfg: Config, args, api: Api | None = None) -> int:
    inst = cfg.data["installs"].get(args.recomp)
    if not inst or not inst.get("launch"):
        raise SystemExit(f"{args.recomp} isn't installed (or has no launch info)")
    places = Places(install=Path(inst["dir"]), data=platforms.data_dir())
    launch(places, inst["launch"])
    return 0


def main(argv: list[str] | None = None, *, api: Api | None = None, config: Config | None = None):
    p = argparse.ArgumentParser(prog="rlclient", description="Recomp Manager reference client")
    sub = p.add_subparsers(dest="cmd", required=True)
    lp = sub.add_parser("login", help="Connect to a server")
    lp.add_argument("server")
    lp.add_argument("--token")
    lp.add_argument("--name", help="This device's name (default: hostname)")
    lp.add_argument("--platform", help="Override the detected platform key")
    lp.set_defaults(func=cmd_login)
    sub.add_parser("platform", help="Show this machine's platform key").set_defaults(
        func=cmd_platform
    )
    sub.add_parser("list", help="List recomps on the server").set_defaults(func=cmd_list)
    for name, fn, h in (
        ("plan", cmd_plan, "Show the install plan"),
        ("install", cmd_install, "Install or update a recomp"),
    ):
        sp = sub.add_parser(name, help=h)
        sp.add_argument("recomp")
        sp.add_argument("--platform")
        sp.add_argument("--version")
        if name == "install":
            sp.add_argument("--dir", help="Install folder (default: ~/Games/<recomp>)")
            sp.add_argument("--yes", action="store_true", help="Run commands without asking")
        sp.set_defaults(func=fn)
    la = sub.add_parser("launch", help="Start an installed recomp")
    la.add_argument("recomp")
    la.set_defaults(func=cmd_launch)

    args = p.parse_args(argv)
    cfg = config or Config()
    try:
        return args.func(cfg, args, api)
    except ApiError as e:
        print(f"Server said: {e.message}", file=sys.stderr)
        return 2
    except InstallError as e:
        print(str(e), file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
