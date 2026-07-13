import base64, binascii, hashlib, hmac, secrets, uuid

from datetime import timedelta
from typing import TYPE_CHECKING, Literal, Optional, get_args
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from fastapi.responses import JSONResponse

if TYPE_CHECKING:
    from helpers.models import OAuthAppWithSecret

OAuthScope = Literal[
    "chart:read",
    "chart:upload",
    "chart:edit",
    "chart:metadata",
    "chart:visibility",
    "chart:delete",
    "user:read",
]

SCOPE_DESCRIPTIONS: dict[OAuthScope, str] = {
    "chart:read": "List and open all of your charts, including the unlisted and private ones.",
    "chart:upload": "Upload new charts as you.",
    "chart:edit": "Replace the files (chart, audio, jacket, background, preview) of your charts.",
    "chart:metadata": "Edit the metadata (title, artists, tags, rating, description) of your charts.",
    "chart:visibility": "Publish, unlist, or hide your charts, and schedule them to publish.",
    "chart:delete": "Delete your charts.",
    "user:read": "Read your account information.",
}

ALL_SCOPES: tuple[OAuthScope, ...] = get_args(OAuthScope)

ACCESS_TOKEN_PREFIX = "uca_"
REFRESH_TOKEN_PREFIX = "ucr_"
AUTHORIZATION_CODE_PREFIX = "ucc_"
CLIENT_SECRET_PREFIX = "ucs_"

ACCESS_TOKEN_TTL = timedelta(hours=1)
REFRESH_TOKEN_TTL = timedelta(days=30)
AUTHORIZATION_CODE_TTL = timedelta(minutes=10)


def generate_client_id() -> str:
    return uuid.uuid4().hex


def generate_token(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(32)


def generate_client_secret() -> str:
    return generate_token(CLIENT_SECRET_PREFIX)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def verify_hash(token: str, token_hash: str) -> bool:
    return hmac.compare_digest(hash_token(token), token_hash)


def verify_code_challenge(code_verifier: str, code_challenge: str) -> bool:
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    expected = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return hmac.compare_digest(expected, code_challenge)


def client_authenticated(
    app: "OAuthAppWithSecret", client_secret: Optional[str]
) -> bool:
    """
    Public clients ship their client_id to users, so they can't prove who they are:
    PKCE is what protects them instead (enforced when the code is issued and redeemed).
    """
    if app.public:
        return True

    return (
        bool(client_secret)
        and bool(app.client_secret_hash)
        and verify_hash(client_secret, app.client_secret_hash)
    )


def parse_scopes(scope: Optional[str]) -> list[OAuthScope]:
    if not scope:
        return []
    return [s for s in scope.split(" ") if s in ALL_SCOPES]


def build_redirect(redirect_uri: str, params: dict[str, str]) -> str:
    parsed = urlparse(redirect_uri)
    query = dict(parse_qsl(parsed.query))
    query.update(params)
    return urlunparse(parsed._replace(query=urlencode(query)))


def oauth_error(error: str, description: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse(
        content={"error": error, "error_description": description},
        status_code=status_code,
    )


def basic_auth_credentials(
    authorization: Optional[str],
) -> tuple[Optional[str], Optional[str]]:
    if not authorization or not authorization.lower().startswith("basic "):
        return None, None
    try:
        decoded = base64.b64decode(authorization[len("basic ") :]).decode()
        client_id, client_secret = decoded.split(":", 1)
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None, None
    return client_id, client_secret
