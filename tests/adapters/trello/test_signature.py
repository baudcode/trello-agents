"""Tests for Trello HMAC-SHA1 webhook signature verification."""

from __future__ import annotations

import base64
import hashlib
import hmac

from agents_trello.adapters.trello.signature import verify_signature


def _make_signature(payload_body: bytes, callback_url: str, secret: str) -> str:
    """Produce a valid Trello-style signature for test purposes."""
    mac = hmac.new(
        secret.encode("utf-8"),
        payload_body + callback_url.encode("utf-8"),
        hashlib.sha1,
    )
    return base64.b64encode(mac.digest()).decode("utf-8")


class TestVerifySignature:
    PAYLOAD = b'{"action":{"type":"createCard"}}'
    CALLBACK = "https://example.com/trello/webhook"
    SECRET = "my-trello-secret"

    def test_valid_signature(self) -> None:
        sig = _make_signature(self.PAYLOAD, self.CALLBACK, self.SECRET)
        assert verify_signature(self.PAYLOAD, self.CALLBACK, self.SECRET, sig) is True

    def test_wrong_signature(self) -> None:
        assert verify_signature(self.PAYLOAD, self.CALLBACK, self.SECRET, "badsig==") is False

    def test_wrong_secret(self) -> None:
        sig = _make_signature(self.PAYLOAD, self.CALLBACK, "wrong-secret")
        assert verify_signature(self.PAYLOAD, self.CALLBACK, self.SECRET, sig) is False

    def test_wrong_callback_url(self) -> None:
        sig = _make_signature(self.PAYLOAD, "https://other.example.com/hook", self.SECRET)
        assert verify_signature(self.PAYLOAD, self.CALLBACK, self.SECRET, sig) is False

    def test_tampered_payload(self) -> None:
        sig = _make_signature(self.PAYLOAD, self.CALLBACK, self.SECRET)
        tampered = b'{"action":{"type":"deleteCard"}}'
        assert verify_signature(tampered, self.CALLBACK, self.SECRET, sig) is False

    def test_empty_payload(self) -> None:
        sig = _make_signature(b"", self.CALLBACK, self.SECRET)
        assert verify_signature(b"", self.CALLBACK, self.SECRET, sig) is True
