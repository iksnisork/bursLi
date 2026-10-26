"""Zero-Database Stateless Token Cryptography (AES-256-GCM + optional Cloud KMS).

Seals user-provided Librus account credentials and alert preferences into a
compact URL-safe token (JWE-style sealed box) that can be stored in the user's
browser or embedded in private `.ics` Google Calendar URLs without storing any
passwords in a server database.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger("librus-hub.crypto")

TOKEN_PREFIX = "lfh1."
REPLY_PREFIX = "rpl1."
DEFAULT_TOKEN_TTL_SECONDS = 365 * 24 * 3600  # 1 year for .ics calendar feeds
REPLY_TOKEN_TTL_SECONDS = 5 * 60             # 5 minutes for 2-step message send


class TokenValidationError(ValueError):
    """Raised when a sealed token is malformed, tampered with, or expired."""


@dataclass(frozen=True)
class SealedAccount:
    alias: str
    username: str
    password: str
    messaging_backend: str = "modern"
    student_username: str = ""
    student_password: str = ""

    def to_dict(self) -> dict[str, str]:
        data = {
            "alias": self.alias,
            "username": self.username,
            "password": self.password,
            "messaging_backend": self.messaging_backend,
        }
        if self.student_username and self.student_password:
            data["student_username"] = self.student_username
            data["student_password"] = self.student_password
        return data


@dataclass(frozen=True)
class UnsealedVaultPayload:
    accounts: tuple[SealedAccount, ...]
    alert_email: str | None = None
    whatsapp_webhook: str | None = None
    issued_at: int = field(default_factory=lambda: int(time.time()))
    expires_at: int = field(
        default_factory=lambda: int(time.time()) + DEFAULT_TOKEN_TTL_SECONDS
    )

    def fingerprint(self) -> str:
        """Deterministic non-reversible SHA-256 hash of credentials for RAM session pooling."""
        h = hashlib.sha256()
        for acc in sorted(self.accounts, key=lambda a: a.alias):
            h.update(
                f"{acc.alias}:{acc.username}:{acc.password}:{acc.messaging_backend}:{acc.student_username}:{acc.student_password}|".encode(
                    "utf-8"
                )
            )
        return h.hexdigest()


def _b64u_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64u_decode(text: str) -> bytes:
    pad = "=" * ((4 - len(text) % 4) % 4)
    return base64.urlsafe_b64decode((text + pad).encode("ascii"))


def _load_or_derive_master_key() -> bytes:
    """Resolve a 32-byte AES-256 master key from env or local context key."""
    env_key = (
        os.environ.get("LIBRUS_MASTER_KEY")
        or os.environ.get("HUB_MASTER_KEY")
        or ""
    ).strip()
    if env_key:
        if len(env_key) == 64:
            try:
                return bytes.fromhex(env_key)
            except ValueError:
                pass
        return hashlib.sha256(env_key.encode("utf-8")).digest()

    # Check if local librus-mcp state context.key exists for deterministic local restarts
    local_ctx_key = Path.home() / ".librus-mcp" / "state" / "context.key"
    if local_ctx_key.exists():
        try:
            raw = local_ctx_key.read_text(encoding="utf-8").strip()
            if len(raw) == 64:
                return bytes.fromhex(raw)
        except OSError:
            pass

    # Fallback deterministic machine-local key or ephemeral random key
    seed_file = Path.home() / ".config" / "librus-mcp" / "config.json"
    if seed_file.exists():
        try:
            return hashlib.sha256(seed_file.read_bytes() + b":librus-family-hub-v1").digest()
        except OSError:
            pass

    logger.warning(
        "LIBRUS_MASTER_KEY not set; generating an ephemeral process master key."
    )
    return secrets.token_bytes(32)


class TokenVault:
    """Stateless AES-256-GCM (plus optional GCP Cloud KMS envelope) token sealer."""

    def __init__(
        self,
        master_key: bytes | None = None,
        kms_key_name: str | None = None,
    ) -> None:
        self._master_key = master_key or _load_or_derive_master_key()
        if len(self._master_key) != 32:
            raise ValueError("Master key must be exactly 32 bytes (256 bits).")
        self._aesgcm = AESGCM(self._master_key)
        self._kms_key_name = (
            kms_key_name
            if kms_key_name is not None
            else os.environ.get("GCP_KMS_KEY_NAME", "").strip() or None
        )

    def seal_vault(
        self,
        accounts: list[dict[str, Any]] | tuple[SealedAccount, ...],
        *,
        alert_email: str | None = None,
        whatsapp_webhook: str | None = None,
        ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS,
    ) -> str:
        """Encrypt account credentials into a compact URL-safe token."""
        normalized_accounts: list[dict[str, str]] = []
        seen_aliases: set[str] = set()
        for item in accounts:
            if isinstance(item, SealedAccount):
                acc_dict = item.to_dict()
            elif isinstance(item, dict):
                alias = str(item.get("alias") or "").strip()
                username = str(item.get("username") or "").strip()
                password = str(item.get("password") or "")
                backend = str(item.get("messaging_backend") or "modern").strip().lower()
                student_username = str(item.get("student_username") or item.get("student_login") or "").strip()
                student_password = str(item.get("student_password") or "")
                if not alias or not username or not password:
                    raise TokenValidationError(
                        "Każde konto musi zawierać alias, login (username) i hasło (password)."
                    )
                acc_dict = {
                    "alias": alias,
                    "username": username,
                    "password": password,
                    "messaging_backend": backend if backend in ("modern", "legacy") else "modern",
                }
                if student_username and student_password:
                    acc_dict["student_username"] = student_username
                    acc_dict["student_password"] = student_password
            else:
                raise TokenValidationError("Nieprawidłowy format konta.")

            if acc_dict["alias"] in seen_aliases:
                raise TokenValidationError(f"Zduplikowany alias konta: {acc_dict['alias']}")
            seen_aliases.add(acc_dict["alias"])
            normalized_accounts.append(acc_dict)

        if not normalized_accounts:
            raise TokenValidationError("Wymagane jest co najmniej jedno konto Librus.")

        now = int(time.time())
        payload = {
            "v": 1,
            "iat": now,
            "exp": now + int(ttl_seconds),
            "acc": normalized_accounts,
        }
        if alert_email:
            payload["em"] = alert_email.strip()
        if whatsapp_webhook:
            payload["wa"] = whatsapp_webhook.strip()

        raw_json = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        compressed = zlib.compress(raw_json, level=9)

        if self._kms_key_name:
            return self._seal_with_gcp_kms(compressed)

        nonce = secrets.token_bytes(12)
        ciphertext = self._aesgcm.encrypt(nonce, compressed, b"librus-family-hub:v1")
        return TOKEN_PREFIX + _b64u_encode(nonce + ciphertext)

    def unseal_vault(self, token: str) -> UnsealedVaultPayload:
        """Decrypt and validate a sealed token in RAM."""
        if not token or not isinstance(token, str):
            raise TokenValidationError("Brak tokenu autoryzacyjnego.")
        clean = token.strip()
        if clean.startswith("kms1."):
            compressed = self._unseal_with_gcp_kms(clean)
        elif clean.startswith(TOKEN_PREFIX):
            raw = _b64u_decode(clean[len(TOKEN_PREFIX) :])
            if len(raw) < 28:
                raise TokenValidationError("Uszkodzony token autoryzacyjny.")
            nonce, ciphertext = raw[:12], raw[12:]
            try:
                compressed = self._aesgcm.decrypt(nonce, ciphertext, b"librus-family-hub:v1")
            except InvalidTag as exc:
                raise TokenValidationError(
                    "Nieprawidłowy lub wygasły klucz tokenu autoryzacyjnego."
                ) from exc
        else:
            raise TokenValidationError("Nieznany format tokenu autoryzacyjnego.")

        try:
            data = json.loads(zlib.decompress(compressed).decode("utf-8"))
        except Exception as exc:
            raise TokenValidationError("Nie udało się zdekodować zawartości tokenu.") from exc

        now = int(time.time())
        exp = int(data.get("exp") or 0)
        if exp and now > exp:
            raise TokenValidationError("Token autoryzacyjny wygasł. Wygeneruj nowy link w aplikacji.")

        raw_accounts = data.get("acc") or []
        accounts = tuple(
            SealedAccount(
                alias=str(a["alias"]),
                username=str(a["username"]),
                password=str(a["password"]),
                messaging_backend=str(a.get("messaging_backend") or "modern"),
                student_username=str(a.get("student_username") or a.get("student_login") or ""),
                student_password=str(a.get("student_password") or ""),
            )
            for a in raw_accounts
            if isinstance(a, dict) and a.get("alias") and a.get("username") and a.get("password")
        )
        if not accounts:
            raise TokenValidationError("Token nie zawiera żadnych kont Librus.")

        return UnsealedVaultPayload(
            accounts=accounts,
            alert_email=data.get("em"),
            whatsapp_webhook=data.get("wa"),
            issued_at=int(data.get("iat") or now),
            expires_at=exp,
        )

    def seal_reply_confirmation(self, data: dict[str, Any]) -> str:
        """Seal a short-lived 5-minute confirmation token for 2-step message sending."""
        now = int(time.time())
        envelope = {
            "iat": now,
            "exp": now + REPLY_TOKEN_TTL_SECONDS,
            "d": data,
        }
        raw = zlib.compress(
            json.dumps(envelope, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        )
        nonce = secrets.token_bytes(12)
        ct = self._aesgcm.encrypt(nonce, raw, b"librus-reply-confirm:v1")
        return REPLY_PREFIX + _b64u_encode(nonce + ct)

    def unseal_reply_confirmation(self, token: str) -> dict[str, Any]:
        """Unseal and verify a 2-step message send confirmation token."""
        if not token or not token.startswith(REPLY_PREFIX):
            raise TokenValidationError("Nieprawidłowy token potwierdzenia wysyłki.")
        raw = _b64u_decode(token[len(REPLY_PREFIX) :])
        if len(raw) < 28:
            raise TokenValidationError("Uszkodzony token potwierdzenia wysyłki.")
        nonce, ct = raw[:12], raw[12:]
        try:
            decompressed = zlib.decompress(
                self._aesgcm.decrypt(nonce, ct, b"librus-reply-confirm:v1")
            )
            envelope = json.loads(decompressed.decode("utf-8"))
        except Exception as exc:
            raise TokenValidationError("Token potwierdzenia wysyłki jest nieprawidłowy.") from exc

        if int(time.time()) > int(envelope.get("exp") or 0):
            raise TokenValidationError(
                "Token potwierdzenia wysyłki wygasł (ważny 5 minut). Wygeneruj podgląd ponownie."
            )
        return dict(envelope.get("d") or {})

    def _seal_with_gcp_kms(self, plaintext: bytes) -> str:
        """Envelope encryption using Google Cloud KMS when GCP_KMS_KEY_NAME is set."""
        from google.cloud import kms  # type: ignore[import-untyped]

        dek = secrets.token_bytes(32)
        nonce = secrets.token_bytes(12)
        ct = AESGCM(dek).encrypt(nonce, plaintext, b"librus-kms-envelope:v1")

        client = kms.KeyManagementServiceClient()
        resp = client.encrypt(request={"name": self._kms_key_name, "plaintext": dek})
        wrapped_dek = resp.ciphertext
        envelope = struct_pack_envelope(wrapped_dek, nonce, ct)
        return "kms1." + _b64u_encode(envelope)

    def _unseal_with_gcp_kms(self, token: str) -> bytes:
        from google.cloud import kms  # type: ignore[import-untyped]

        raw = _b64u_decode(token[len("kms1.") :])
        wrapped_dek, nonce, ct = struct_unpack_envelope(raw)
        client = kms.KeyManagementServiceClient()
        resp = client.decrypt(request={"name": self._kms_key_name, "ciphertext": wrapped_dek})
        dek = resp.plaintext
        return AESGCM(dek).decrypt(nonce, ct, b"librus-kms-envelope:v1")


def struct_pack_envelope(wrapped_dek: bytes, nonce: bytes, ct: bytes) -> bytes:
    dek_len = len(wrapped_dek).to_bytes(2, "big")
    return dek_len + wrapped_dek + nonce + ct


def struct_unpack_envelope(raw: bytes) -> tuple[bytes, bytes, bytes]:
    if len(raw) < 16:
        raise TokenValidationError("Uszkodzona koperta KMS.")
    dek_len = int.from_bytes(raw[:2], "big")
    if len(raw) < 2 + dek_len + 12:
        raise TokenValidationError("Uszkodzona koperta KMS.")
    wrapped_dek = raw[2 : 2 + dek_len]
    nonce = raw[2 + dek_len : 2 + dek_len + 12]
    ct = raw[2 + dek_len + 12 :]
    return wrapped_dek, nonce, ct
