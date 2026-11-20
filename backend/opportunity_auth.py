"""Authentication adapter for opportunity routes.

The reply server owns the session-token store. Importing it lazily keeps this
module safe to load while ``reply_server`` is registering routers.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer


security = HTTPBearer(auto_error=False)


def require_authenticated_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> Dict[str, Any]:
    from reply_server import verify_token

    user = verify_token(credentials)
    if not user:
        raise HTTPException(status_code=401, detail="未授权访问")
    return user


__all__ = ["require_authenticated_user"]
