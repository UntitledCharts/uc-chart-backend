import base64
import hashlib
import hmac
import secrets
import uuid

SESSION_TOKEN_PREFIX = "uct_"
ACCESS_TOKEN_PREFIX = "uca_"
REFRESH_TOKEN_PREFIX = "ucr_"
AUTHORIZATION_CODE_PREFIX = "ucc_"
CLIENT_SECRET_PREFIX = "ucs_"


def generate_token(prefix: str) -> str:
    """Generate a random opaque token.

    Args:
        prefix: Prefix identifying the token kind.

    Returns:
        str: The token.
    """
    return prefix + secrets.token_urlsafe(32)


def generate_client_id() -> str:
    """Generate an OAuth client id.

    Returns:
        str: A 32-character hex id.
    """
    return uuid.uuid4().hex


def hash_token(token: str) -> str:
    """Hash a token for storage.

    Args:
        token: The token.

    Returns:
        str: SHA-256 hex digest.
    """
    return hashlib.sha256(token.encode()).hexdigest()


def matches(value: str | None, expected: str) -> bool:
    """Compare a secret in constant time.

    Args:
        value: Value supplied by the client.
        expected: Expected secret.

    Returns:
        bool: Whether they are equal.
    """
    if value is None:
        return False
    return hmac.compare_digest(value.encode(), expected.encode())


def hmac_hex(secret: str, message: str) -> str:
    """Sign a message with HMAC-SHA256.

    Args:
        secret: Signing key.
        message: Message to sign.

    Returns:
        str: Hex digest.
    """
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def generate_signed_id(secret: str) -> str:
    """Generate a random id carrying its own HMAC signature.

    Args:
        secret: Signing key.

    Returns:
        str: Text like ``<random>.<signature>``.
    """
    random_part = secrets.token_urlsafe(24)
    return f"{random_part}.{hmac_hex(secret, 'id:' + random_part)}"


def signed_id_valid(secret: str, value: str) -> bool:
    """Check the signature of an id from ``generate_signed_id``.

    Args:
        secret: Signing key.
        value: The id.

    Returns:
        bool: Whether the signature matches.
    """
    random_part, _, signature = value.rpartition(".")
    if not random_part or not signature:
        return False
    return hmac.compare_digest(hmac_hex(secret, "id:" + random_part), signature)


def pkce_challenge_valid(code_verifier: str, code_challenge: str) -> bool:
    """Check a PKCE S256 code verifier against its challenge.

    Args:
        code_verifier: Verifier sent when redeeming the code.
        code_challenge: Challenge sent when the code was issued.

    Returns:
        bool: Whether they match.
    """
    try:
        digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    except UnicodeEncodeError:
        return False
    expected = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return hmac.compare_digest(expected, code_challenge)
