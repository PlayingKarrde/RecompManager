"""First-run admin token.

If there is no way in (no active admin API token and no web password), an admin
token is created and written to ``/config/admin-token.txt`` (mode 600). The token
itself is never logged; the log only says where to find it.

Recovery when locked out with a password set: ``recompmanager token create`` in
the container, then reset the password with that token.
"""

from __future__ import annotations

import contextlib
import logging
import os

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from recompmanager import passwords
from recompmanager.auth import create_token
from recompmanager.config import Env
from recompmanager.models import ApiToken, TokenKind, TokenScope

log = logging.getLogger(__name__)


def ensure_admin_token(env: Env, factory: sessionmaker[Session]) -> bool:
    """Returns True when a new bootstrap token was created."""
    with factory() as session:
        admin_token = session.scalar(
            select(ApiToken.id).where(
                ApiToken.scope == TokenScope.admin,
                ApiToken.kind == TokenKind.api,
                ApiToken.revoked_at.is_(None),
            )
        )
        if admin_token is not None or passwords.load(session) is not None:
            return False
        _row, plaintext = create_token(session, "bootstrap", TokenScope.admin)
        path = env.bootstrap_token_file
        path.write_text(
            "# Admin token for Recomp Manager. Log in with it, create your own\n"
            "# tokens in Settings, then delete this file.\n" + plaintext + "\n",
            encoding="utf-8",
        )
        with contextlib.suppress(OSError):  # chmod is a no-op on Windows dev boxes
            os.chmod(path, 0o600)
        session.commit()
    log.warning("No admin token existed, so one was created. Read it from %s", path)
    return True
