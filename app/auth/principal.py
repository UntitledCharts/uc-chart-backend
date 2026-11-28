from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from fastapi import Request

from app.auth.catalog import ALL_STAFF_PERMISSIONS
from app.auth.tokens import ACCESS_TOKEN_PREFIX, hash_token, matches
from app.context import get_context
from app.database import oauth as oauth_db
from app.database import permissions as permissions_db
from app.database import sessions as sessions_db
from app.database.accounts import AccountRow
from app.database.sessions import SessionRow, SessionType
from app.errors import ApiError, forbidden, unauthorized

PrincipalKind = Literal["anonymous", "session", "oauth", "admin_key"]

ADMIN_KEY_HEADER = "x-admin-key"
SONOSERVER_KEY_HEADER = "x-sonoserver-key"


@dataclass(frozen=True)
class Principal:
    """Who is making a request.

    Attributes:
        kind: How the caller authenticated.
        account: The caller's account, None for anonymous callers and the admin key.
        permissions: Staff permissions the caller can use.
        session: The session used, for session callers.
        oauth_scopes: Granted scopes, for OAuth callers.
        oauth_client_id: App acting for the account, for OAuth callers.
        sonoserver: Whether a valid X-SonoServer-Key was sent.
    """

    kind: PrincipalKind
    account: AccountRow | None = None
    permissions: frozenset[str] = frozenset()
    session: SessionRow | None = None
    oauth_scopes: frozenset[str] | None = None
    oauth_client_id: str | None = None
    sonoserver: bool = False

    @property
    def is_admin_key(self) -> bool:
        """Whether the admin key authenticated the request.

        Returns:
            bool: True for the admin key.
        """
        return self.kind == "admin_key"

    @property
    def is_oauth(self) -> bool:
        """Whether a third-party OAuth token authenticated the request.

        Returns:
            bool: True for OAuth tokens.
        """
        return self.kind == "oauth"

    @property
    def sonolus_id(self) -> str | None:
        """The caller's account id.

        Returns:
            str | None: Account id, or None without an account.
        """
        return self.account.sonolus_id if self.account else None

    def require_account(self) -> AccountRow:
        """Return the caller's account or fail.

        Returns:
            AccountRow: The account.
        """
        if self.account is not None:
            return self.account
        if self.is_admin_key:
            raise forbidden("error.auth.account_required")
        raise unauthorized()

    def has_permission(self, permission: str) -> bool:
        """Check a staff permission.

        Args:
            permission: Permission name.

        Returns:
            bool: Whether the caller may use it.
        """
        if self.is_admin_key:
            return True
        if self.is_oauth:
            return False
        return permission in self.permissions

    def require_permission(self, permission: str) -> None:
        """Fail unless the caller holds a staff permission.

        Args:
            permission: Permission name.
        """
        if not self.has_permission(permission):
            if self.kind == "anonymous":
                raise unauthorized()
            raise forbidden("error.staff.permission_required", permission=permission)

    def outranks(self, target_permissions: frozenset[str]) -> bool:
        """Check the staff hierarchy against a target account.

        Args:
            target_permissions: Staff permissions of the target account.

        Returns:
            bool: Whether the caller holds every permission the target holds.
        """
        if self.is_admin_key:
            return True
        return target_permissions <= self.permissions

    def require_outranks(self, target_permissions: frozenset[str]) -> None:
        """Fail when the target holds a permission the caller lacks.

        Args:
            target_permissions: Staff permissions of the target account.
        """
        if not self.outranks(target_permissions):
            raise forbidden("error.staff.target_outranks")

    def has_scope(self, scope: str) -> bool:
        """Check an OAuth scope; non-OAuth callers always pass.

        Args:
            scope: Scope name.

        Returns:
            bool: Whether the scope is available.
        """
        return not self.is_oauth or scope in (self.oauth_scopes or frozenset())

    def require_scope(self, scope: str) -> None:
        """Fail when an OAuth caller lacks a scope.

        Args:
            scope: Scope name.
        """
        if not self.has_scope(scope):
            raise ApiError(
                403,
                "error.oauth.insufficient_scope",
                {"scope": scope},
                {"WWW-Authenticate": f'Bearer error="insufficient_scope", scope="{scope}"'},
            )


async def resolve_principal(request: Request) -> Principal:
    """Authenticate the request from its headers, once per request.

    Args:
        request: Current request.

    Returns:
        Principal: The caller.
    """
    cached = getattr(request.state, "principal", None)
    if isinstance(cached, Principal):
        return cached
    context = get_context(request)
    keys = context.config.keys

    sonoserver_header = request.headers.get(SONOSERVER_KEY_HEADER)
    sonoserver = False
    if sonoserver_header is not None:
        if not matches(sonoserver_header, keys.sonoserver_key):
            raise unauthorized("error.auth.invalid_sonoserver_key")
        sonoserver = True

    admin_header = request.headers.get(ADMIN_KEY_HEADER)
    if admin_header is not None:
        if not matches(admin_header, keys.admin_key):
            raise unauthorized("error.auth.invalid_admin_key")
        principal = Principal(
            kind="admin_key", permissions=ALL_STAFF_PERMISSIONS, sonoserver=sonoserver
        )
        request.state.principal = principal
        return principal

    authorization = request.headers.get("authorization")
    if not authorization:
        principal = Principal(kind="anonymous", sonoserver=sonoserver)
        request.state.principal = principal
        return principal

    scheme, _, token = authorization.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        raise unauthorized("error.auth.invalid_token", bearer_error="invalid_token")

    token_hash = hash_token(token)
    async with context.database.connection() as connection:
        if token.startswith(ACCESS_TOKEN_PREFIX):
            resolved = await oauth_db.get_access_token_principal(connection, token_hash)
            if resolved is None:
                raise unauthorized("error.auth.invalid_token", bearer_error="invalid_token")
            principal = Principal(
                kind="oauth",
                account=resolved.account,
                oauth_scopes=frozenset(resolved.scopes),
                oauth_client_id=resolved.client_id,
                sonoserver=sonoserver,
            )
        else:
            resolved_session = await sessions_db.get_session_by_token_hash(connection, token_hash)
            if resolved_session is None:
                raise unauthorized("error.auth.invalid_token", bearer_error="invalid_token")
            permissions = (
                frozenset()
                if resolved_session.account.banned
                else await permissions_db.get_permissions(
                    connection, resolved_session.account.sonolus_id
                )
            )
            principal = Principal(
                kind="session",
                account=resolved_session.account,
                permissions=permissions,
                session=resolved_session.session,
                sonoserver=sonoserver,
            )
    request.state.principal = principal
    return principal


def auth(
    *,
    account: bool = False,
    scope: str | None = None,
    oauth: bool = True,
    banned: bool = True,
    session_types: tuple[SessionType, ...] | None = None,
    sonoserver: bool = False,
) -> Callable[[Request], Awaitable[Principal]]:
    """Build a dependency that authenticates and checks the caller for a route.

    Args:
        account: Require an identified account (the admin key alone is rejected).
        scope: OAuth scope required when an OAuth token is used.
        oauth: Allow OAuth tokens at all.
        banned: Allow banned accounts.
        session_types: Session types allowed, None for any.
        sonoserver: Require a valid X-SonoServer-Key.

    Returns:
        Callable[[Request], Awaitable[Principal]]: The dependency.
    """

    async def dependency(request: Request) -> Principal:
        """Authenticate the request and apply the route's requirements.

        Args:
            request: Current request.

        Returns:
            Principal: The caller.
        """
        principal = await resolve_principal(request)
        if sonoserver and not principal.sonoserver:
            raise forbidden("error.auth.sonoserver_required")
        if principal.is_oauth:
            if not oauth:
                raise forbidden("error.oauth.route_not_allowed")
            if scope is not None:
                principal.require_scope(scope)
        if account:
            principal.require_account()
        if not banned and principal.account is not None and principal.account.banned:
            raise forbidden("error.account.banned")
        if (
            session_types is not None
            and principal.session is not None
            and principal.session.type not in session_types
        ):
            raise forbidden("error.auth.wrong_session_type")
        return principal

    return dependency
