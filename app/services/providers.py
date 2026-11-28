import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import urlencode

import aiohttp

from app.auth.tokens import hmac_hex, matches
from app.errors import bad_request, forbidden
from app.settings import ProviderConfig

Provider = Literal["discord", "google"]
Purpose = Literal["link", "sign_in"]


@dataclass(frozen=True)
class ProviderEndpoints:
    """OAuth endpoints and scopes of a provider.

    Attributes:
        authorize_url: Authorization page.
        token_url: Token endpoint.
        userinfo_url: Endpoint returning the signed-in user.
        scope: Requested scopes.
    """

    authorize_url: str
    token_url: str
    userinfo_url: str
    scope: str


ENDPOINTS: dict[str, ProviderEndpoints] = {
    "discord": ProviderEndpoints(
        authorize_url="https://discord.com/oauth2/authorize",
        token_url="https://discord.com/api/oauth2/token",
        userinfo_url="https://discord.com/api/users/@me",
        scope="identify",
    ),
    "google": ProviderEndpoints(
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        userinfo_url="https://openidconnect.googleapis.com/v1/userinfo",
        scope="openid profile",
    ),
}


@dataclass(frozen=True)
class ProviderIdentity:
    """Identity returned by a provider.

    Attributes:
        user_id: Account id at the provider.
        display_name: Display name at the provider.
        access_token: Provider access token.
        refresh_token: Provider refresh token, if issued.
        expires_at: Access token expiry, if known.
    """

    user_id: str
    display_name: str | None
    access_token: str
    refresh_token: str | None
    expires_at: datetime | None


@dataclass(frozen=True)
class SignedState:
    """Decoded sign-in state.

    Attributes:
        provider: Provider the flow is for.
        purpose: Linking or signing in.
        sonolus_id: Account being linked, for link flows.
        nonce: Random value the PKCE verifier is derived from.
    """

    provider: Provider
    purpose: Purpose
    sonolus_id: str | None
    nonce: str


def _b64(data: bytes) -> str:
    """Encode unpadded base64url.

    Args:
        data: Bytes to encode.

    Returns:
        str: Encoded text.
    """
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    """Decode unpadded base64url.

    Args:
        text: Encoded text.

    Returns:
        bytes: Decoded bytes.
    """
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def code_verifier(secret: str, nonce: str) -> str:
    """Derive the PKCE verifier of a flow from its nonce.

    Args:
        secret: Server secret.
        nonce: Flow nonce.

    Returns:
        str: The verifier.
    """
    return hmac_hex(secret, "pkce:" + nonce)


def encode_state(
    secret: str,
    provider: Provider,
    purpose: Purpose,
    sonolus_id: str | None,
    lifetime_seconds: int,
) -> str:
    """Create a signed, expiring state value.

    Args:
        secret: Server secret.
        provider: Provider name.
        purpose: Linking or signing in.
        sonolus_id: Account being linked.
        lifetime_seconds: How long the state is valid.

    Returns:
        str: The state.
    """
    payload = {
        "p": provider,
        "u": purpose,
        "a": sonolus_id,
        "n": secrets.token_urlsafe(24),
        "e": int(time.time()) + lifetime_seconds,
    }
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    return f"{body}.{hmac_hex(secret, 'state:' + body)}"


def decode_state(secret: str, state: str, provider: Provider, purpose: Purpose) -> SignedState:
    """Verify and decode a state value.

    Args:
        secret: Server secret.
        state: State from the provider redirect.
        provider: Provider expected.
        purpose: Purpose expected.

    Returns:
        SignedState: The decoded state.
    """
    body, _, signature = state.rpartition(".")
    if not body or not state.isascii() or not matches(signature, hmac_hex(secret, "state:" + body)):
        raise bad_request("error.connection.invalid_state")
    try:
        payload = json.loads(_unb64(body))
    except (ValueError, json.JSONDecodeError):
        raise bad_request("error.connection.invalid_state") from None
    if payload.get("e", 0) < time.time():
        raise bad_request("error.connection.state_expired")
    if payload.get("p") != provider or payload.get("u") != purpose:
        raise bad_request("error.connection.invalid_state")
    return SignedState(
        provider=provider,
        purpose=purpose,
        sonolus_id=payload.get("a"),
        nonce=payload["n"],
    )


def authorize_url(
    config: ProviderConfig, provider: Provider, state: str, secret: str, nonce: str
) -> str:
    """Build the provider's authorization URL.

    Args:
        config: Provider settings.
        provider: Provider name.
        state: Signed state.
        secret: Server secret.
        nonce: State nonce.

    Returns:
        str: The URL.
    """
    endpoints = ENDPOINTS[provider]
    challenge = _b64(hashlib.sha256(code_verifier(secret, nonce).encode()).digest())
    query = {
        "client_id": config.client_id,
        "redirect_uri": config.redirect_uri,
        "response_type": "code",
        "scope": endpoints.scope,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{endpoints.authorize_url}?{urlencode(query)}"


def state_nonce(state: str) -> str:
    """Read the nonce of a state this server just created.

    Args:
        state: Signed state.

    Returns:
        str: The nonce.
    """
    body = state.rpartition(".")[0]
    return json.loads(_unb64(body))["n"]


def _expiry(data: dict) -> datetime | None:
    """Turn an ``expires_in`` value into a time.

    Args:
        data: Token response.

    Returns:
        datetime | None: Expiry, or None when not given.
    """
    expires_in = data.get("expires_in")
    if not isinstance(expires_in, int | float):
        return None
    return datetime.now(timezone.utc) + timedelta(seconds=float(expires_in))


async def exchange_code(
    http: aiohttp.ClientSession,
    config: ProviderConfig,
    provider: Provider,
    code: str,
    verifier: str,
) -> ProviderIdentity:
    """Exchange an authorization code and read the provider identity.

    Args:
        http: Shared HTTP session.
        config: Provider settings.
        provider: Provider name.
        code: Authorization code.
        verifier: PKCE code verifier.

    Returns:
        ProviderIdentity: The identity and tokens.
    """
    endpoints = ENDPOINTS[provider]
    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with http.post(
            endpoints.token_url,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": config.redirect_uri,
                "client_id": config.client_id,
                "client_secret": config.client_secret,
                "code_verifier": verifier,
            },
            timeout=timeout,
        ) as response:
            if response.status != 200:
                raise forbidden("error.connection.exchange_failed", provider=provider)
            tokens = await response.json()
        async with http.get(
            endpoints.userinfo_url,
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
            timeout=timeout,
        ) as response:
            if response.status != 200:
                raise forbidden("error.connection.exchange_failed", provider=provider)
            user = await response.json()
    except (aiohttp.ClientError, TimeoutError, KeyError, ValueError):
        raise forbidden("error.connection.exchange_failed", provider=provider) from None
    if provider == "discord":
        user_id = str(user["id"])
        display_name = user.get("global_name") or user.get("username")
    else:
        user_id = str(user["sub"])
        display_name = user.get("name")
    return ProviderIdentity(
        user_id=user_id,
        display_name=display_name,
        access_token=tokens["access_token"],
        refresh_token=tokens.get("refresh_token"),
        expires_at=_expiry(tokens),
    )


async def refresh_tokens(
    http: aiohttp.ClientSession,
    config: ProviderConfig,
    provider: Provider,
    refresh_token: str,
) -> tuple[str, str | None, datetime | None]:
    """Refresh a stored provider access token.

    Args:
        http: Shared HTTP session.
        config: Provider settings.
        provider: Provider name.
        refresh_token: Stored refresh token.

    Returns:
        tuple[str, str | None, datetime | None]: New access token, new refresh token and expiry.
    """
    async with http.post(
        ENDPOINTS[provider].token_url,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": config.client_id,
            "client_secret": config.client_secret,
        },
        timeout=aiohttp.ClientTimeout(total=10),
    ) as response:
        if response.status != 200:
            raise forbidden("error.connection.refresh_failed", provider=provider)
        tokens = await response.json()
    return (
        tokens["access_token"],
        tokens.get("refresh_token", refresh_token),
        _expiry(tokens),
    )
