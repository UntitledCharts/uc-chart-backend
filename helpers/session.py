from fastapi import Header, HTTPException, status, Request
from core import ChartFastAPI
from typing import Literal, Optional
from database import accounts, oauth
from fastapi import Depends
from helpers.models import Account
from helpers.oauth import ACCESS_TOKEN_PREFIX, OAuthScope, hash_token


def get_session(
    enforce_auth: bool = False,
    enforce_type: Literal["game", "external", False] = False,
    allow_banned_users: bool = True,
    scopes: Optional[list[OAuthScope]] = None,
):
    """
    scopes=None    -> oauth access tokens are rejected outright
    scopes=[...]   -> oauth access tokens need every listed scope
    scopes=[]      -> oauth access tokens are allowed, route decides via Session.require_scopes

    enforce_type is ignored for oauth access tokens, scopes gate them instead.
    """

    async def dependency(request: Request, authorization: str = Header(None)):
        session = Session(
            enforce_auth=enforce_auth,
            enforce_type=enforce_type,
            allow_banned_users=allow_banned_users,
            scopes=scopes,
        )
        await session(request, authorization)
        return session

    return Depends(dependency)


class Session:
    def __init__(
        self,
        enforce_auth: bool = False,
        enforce_type: Literal["game", "external", False] = False,
        allow_banned_users: bool = True,
        scopes: Optional[list[OAuthScope]] = None,
    ):
        self.enforce_auth = enforce_auth
        self.enforce_type = enforce_type
        self.allow_banned_users = allow_banned_users
        self.required_scopes = scopes

        self.is_oauth: bool = False
        self.client_id: Optional[str] = None
        self.scopes: list[OAuthScope] = []

        self._user_fetched = False
        self._user = None

    def require_scopes(self, *scopes: OAuthScope) -> None:
        if not self.is_oauth:
            return

        missing = [scope for scope in scopes if scope not in self.scopes]
        if missing:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing scope(s): {' '.join(missing)}",
            )

    async def user(self) -> Optional[Account]:
        if not self._user_fetched:
            if not self.auth:
                return None

            if self.is_oauth:
                query = oauth.get_account_from_access_token(hash_token(self.auth))
            else:
                query = accounts.get_account_from_session(
                    self.session_data.user_id, self.auth, self.session_data.type
                )

            async with self.app.db_acquire() as conn:
                result = await conn.fetchrow(query)

                if not result and self.enforce_auth:
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        detail="Not logged in.",
                    )

                if result and self.is_oauth:
                    self.client_id = result.client_id
                    self.scopes = result.scopes

                self._user = result
                self._user_fetched = True

        return self._user

    async def __call__(
        self, request: Request, authorization: Optional[str] = Header(None)
    ):
        self.app: ChartFastAPI = request.app

        if authorization and authorization.lower().startswith("bearer "):
            authorization = authorization[len("bearer ") :]
        self.auth = authorization

        if not authorization and self.enforce_auth:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Not logged in."
            )

        if authorization and authorization.startswith(ACCESS_TOKEN_PREFIX):
            if self.required_scopes is None:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="This endpoint can't be used with an OAuth token.",
                )

            self.is_oauth = True
            self.session_data = None

            user = await self.user()
            if not user:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED, detail="Not logged in."
                )

            self.sonolus_id = user.sonolus_id
            self.require_scopes(*self.required_scopes)

            if not self.allow_banned_users and user.banned:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN, detail="User banned."
                )
        elif authorization:
            self.session_data = self.app.decode_key(authorization)
            self.sonolus_id = self.session_data.user_id

            if self.enforce_type and self.session_data.type != self.enforce_type:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN, detail="Invalid token type."
                )
            user = await self.user()
            if not user:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED, detail="Not logged in."
                )
            if not self.allow_banned_users and user.banned:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN, detail="User banned."
                )
        else:
            self.session_data = None
            self.sonolus_id = None
        return self
