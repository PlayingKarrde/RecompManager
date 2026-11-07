"""First-run admin token.

If no usable admin token exists, one is created and written to
``/config/admin-token.txt`` (mode 600). The token itself is never logged; the
log only says where to find it. Revoking every admin token and restarting the
container creates a fresh one, which doubles as account recovery.
"""

from __future__ import annotations

import contextlib
import logging
import os

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from recompmanager.auth import create_token
from recompmanager.config import Env
from recompmanager.models import ApiToken, TokenScope

log = logging.getLogger(__name__)


def ensure_admin_token(env: Env, factory: sessionmaker[Session]) -> bool:
    """Returns True when a new bootstrap token was created."""
    with factory() as session:
        exists = session.scalar(
            select(ApiToken.id).where(
                ApiToken.scope == TokenScope.admin, ApiToken.revoked_at.is_(None)
            )
        )
        if exists is not None:
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
