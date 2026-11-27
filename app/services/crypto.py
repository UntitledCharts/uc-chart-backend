import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

NONCE_SIZE = 12


class TokenCipher:
    """AES-256-GCM encryption for provider tokens kept at rest.

    Attributes:
        aead: The AES-GCM primitive.
    """

    def __init__(self, base64_key: str) -> None:
        """Create a cipher from a base64 key.

        Args:
            base64_key: Base64 of exactly 32 random bytes.
        """
        key = base64.b64decode(base64_key)
        if len(key) != 32:
            raise ValueError("keys.encryption_key must be base64 of 32 bytes")
        self.aead = AESGCM(key)

    def encrypt(self, plaintext: str, context: str) -> bytes:
        """Encrypt a token.

        Args:
            plaintext: The token.
            context: Associated data binding the ciphertext to its row, such as the account id.

        Returns:
            bytes: Nonce followed by ciphertext.
        """
        nonce = os.urandom(NONCE_SIZE)
        return nonce + self.aead.encrypt(nonce, plaintext.encode(), context.encode())

    def decrypt(self, data: bytes, context: str) -> str:
        """Decrypt a token.

        Args:
            data: Nonce followed by ciphertext.
            context: The associated data used when encrypting.

        Returns:
            str: The token.
        """
        nonce, ciphertext = data[:NONCE_SIZE], data[NONCE_SIZE:]
        return self.aead.decrypt(nonce, ciphertext, context.encode()).decode()
