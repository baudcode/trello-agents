"""HMAC-SHA1 webhook signature verification."""

from __future__ import annotations

import base64
import hashlib
import hmac


def verify_signature(
    payload_body: bytes,
    callback_url: str,
    secret: str,
    signature: str,
) -> bool:
    """Verify a Trello webhook HMAC-SHA1 signature.

    Trello signs: base64(HMAC-SHA1(secret, payload_body + callback_url))
    """
    mac = hmac.new(
        secret.encode("utf-8"),
        payload_body + callback_url.encode("utf-8"),
        hashlib.sha1,
    )
    expected = base64.b64encode(mac.digest()).decode("utf-8")
    return hmac.compare_digest(expected, signature)
