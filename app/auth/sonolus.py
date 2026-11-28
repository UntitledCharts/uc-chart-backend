import base64
import binascii
import hashlib
import time

from ecdsa import NIST256p, VerifyingKey, ellipticcurve
from ecdsa.keys import BadSignatureError
from ecdsa.util import sigdecode_string, string_to_number
from pydantic import BaseModel, ConfigDict, ValidationError

from app.errors import bad_request

SONOLUS_PUBLIC_KEY_X = "d2B14ZAn-zDsqY42rHofst8rw3XB90-a5lT80NFdXo0"
SONOLUS_PUBLIC_KEY_Y = "Hxzi9DHrlJ4CVSJVRnydxFWBZAgkFxZXbyxPSa8SJQw"


class ServiceUserProfile(BaseModel):
    """Sonolus account profile from a signed payload.

    Attributes:
        model_config: Ignores profile fields the backend doesn't use.
        id: Sonolus account id.
        handle: Sonolus handle number as text.
        name: Display name.
    """

    model_config = ConfigDict(extra="ignore")

    id: str
    handle: str
    name: str


class SignedAuthenticatePayload(BaseModel):
    """Body Sonolus signs when logging in to the Sonolus server.

    Attributes:
        model_config: Ignores unknown fields.
        type: Must be ``authenticateServer``.
        address: Server address the user is logging in to.
        time: Signing time in epoch milliseconds.
        userProfile: The account's profile.
    """

    model_config = ConfigDict(extra="ignore")

    type: str
    address: str
    time: int
    userProfile: ServiceUserProfile


class SignedExternalPayload(BaseModel):
    """Body Sonolus signs when approving a website login.

    Attributes:
        model_config: Ignores unknown fields.
        type: Must be ``authenticateExternal``.
        url: URL the approval was sent to.
        time: Signing time in epoch milliseconds.
        userProfile: The account's profile.
    """

    model_config = ConfigDict(extra="ignore")

    type: str
    url: str
    time: int
    userProfile: ServiceUserProfile


def _decode_base64url(value: str) -> bytes:
    """Decode unpadded or padded base64url.

    Args:
        value: Encoded text.

    Returns:
        bytes: Decoded bytes.
    """
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def load_sonolus_key() -> VerifyingKey:
    """Build Sonolus's ECDSA P-256 verifying key.

    Returns:
        VerifyingKey: The key.
    """
    x = string_to_number(_decode_base64url(SONOLUS_PUBLIC_KEY_X))
    y = string_to_number(_decode_base64url(SONOLUS_PUBLIC_KEY_Y))
    point = ellipticcurve.Point(NIST256p.curve, x, y)
    return VerifyingKey.from_public_point(point, curve=NIST256p)


def verify_signature(key: VerifyingKey, body: bytes, signature: str | None) -> None:
    """Check that Sonolus signed a request body.

    Args:
        key: Sonolus's verifying key.
        body: Raw request body exactly as received.
        signature: Value of the Sonolus-Signature header.
    """
    if not signature:
        raise bad_request("error.sonolus.signature_missing")
    try:
        decoded = _decode_base64url(signature.strip())
        key.verify(decoded, body, hashfunc=hashlib.sha256, sigdecode=sigdecode_string)
    except (BadSignatureError, binascii.Error, ValueError, AssertionError):
        raise bad_request("error.sonolus.signature_invalid") from None


def check_time(signed_time: int, window_seconds: int) -> None:
    """Check that a signed payload is recent.

    Args:
        signed_time: Signing time in epoch milliseconds.
        window_seconds: Accepted clock difference.
    """
    if abs(round(time.time() * 1000) - signed_time) > window_seconds * 1000:
        raise bad_request("error.sonolus.time_invalid")


def parse_authenticate(
    body: bytes, server_url: str, window_seconds: int
) -> SignedAuthenticatePayload:
    """Validate a signed Sonolus server login body.

    Args:
        body: Raw request body, already signature-checked.
        server_url: Public URL of the Sonolus server.
        window_seconds: Accepted clock difference.

    Returns:
        SignedAuthenticatePayload: The payload.
    """
    try:
        payload = SignedAuthenticatePayload.model_validate_json(body)
    except ValidationError:
        raise bad_request("error.sonolus.payload_invalid") from None
    if payload.type != "authenticateServer":
        raise bad_request("error.sonolus.wrong_type")
    if payload.address.lower() != server_url.lower():
        raise bad_request("error.sonolus.wrong_server")
    check_time(payload.time, window_seconds)
    return payload


def parse_external(
    body: bytes, server_url: str, request_id: str, window_seconds: int
) -> SignedExternalPayload:
    """Validate a signed Sonolus website login approval.

    Args:
        body: Raw request body, already signature-checked.
        server_url: Public URL of the Sonolus server.
        request_id: Login request id the approval must be bound to.
        window_seconds: Accepted clock difference.

    Returns:
        SignedExternalPayload: The payload.
    """
    try:
        payload = SignedExternalPayload.model_validate_json(body)
    except ValidationError:
        raise bad_request("error.sonolus.payload_invalid") from None
    if payload.type != "authenticateExternal":
        raise bad_request("error.sonolus.wrong_type")
    if payload.url != f"{server_url}/sonolus/authenticate_external?id={request_id}":
        raise bad_request("error.sonolus.wrong_server")
    check_time(payload.time, window_seconds)
    return payload
