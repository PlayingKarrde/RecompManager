"""Command line: ``recompmanager serve | migrate | token ... | openapi``.

Inside the container: ``docker exec -it recompmanager recompmanager token create --name me``.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from sqlalchemy import select


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(), format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )


def cmd_serve(_args) -> int:
    import uvicorn

    from recompmanager.config import Env
    from recompmanager.main import create_app

    env = Env()
    _setup_logging(env.log_level)
    uvicorn.run(
        create_app(env),
        host=env.host,
        port=env.port,
        log_level=env.log_level.lower(),
        proxy_headers=True,
    )
    return 0


def cmd_migrate(_args) -> int:
    from recompmanager.config import Env
    from recompmanager.db import run_migrations

    env = Env()
    env.ensure_dirs()
    run_migrations(env.database_url)
    print("Database is up to date")
    return 0


def _sessionmaker():
    from recompmanager.config import Env
    from recompmanager.db import make_engine, make_sessionmaker, run_migrations

    env = Env()
    env.ensure_dirs()
    run_migrations(env.database_url)
    return make_sessionmaker(make_engine(env.database_url))


def cmd_token_create(args) -> int:
    from recompmanager.auth import create_token
    from recompmanager.models import TokenScope

    with _sessionmaker()() as session:
        _row, plaintext = create_token(session, args.name, TokenScope(args.scope))
        session.commit()
    print(plaintext)
    return 0


def cmd_token_list(_args) -> int:
    from recompmanager.models import ApiToken

    with _sessionmaker()() as session:
        for t in session.scalars(select(ApiToken).order_by(ApiToken.id)):
            state = "revoked" if t.revoked_at else "active"
            print(f"{t.id:>4}  {t.scope:<5}  {state:<7}  {t.prefix}...  {t.name}")
    return 0


def cmd_openapi(args) -> int:
    from recompmanager.config import Env
    from recompmanager.main import create_app

    spec = create_app(Env(static_dir=None)).openapi()
    text = json.dumps(spec, indent=2, sort_keys=False) + "\n"
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8", newline="\n")
    else:
        sys.stdout.write(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="recompmanager")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="Run the server").set_defaults(func=cmd_serve)
    sub.add_parser("migrate", help="Apply database migrations").set_defaults(func=cmd_migrate)

    tok_parser = sub.add_parser("token", help="Manage API tokens")
    tok = tok_parser.add_subparsers(dest="tcmd", required=True)
    tc = tok.add_parser("create", help="Create a token and print it")
    tc.add_argument("--name", required=True)
    tc.add_argument("--scope", choices=["read", "admin"], default="admin")
    tc.set_defaults(func=cmd_token_create)
    tok.add_parser("list", help="List tokens").set_defaults(func=cmd_token_list)

    oa = sub.add_parser("openapi", help="Print the OpenAPI spec")
    oa.add_argument("-o", "--output")
    oa.set_defaults(func=cmd_openapi)

    args = parser.parse_args(argv)
    return args.func(args)
