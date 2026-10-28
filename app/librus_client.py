"""Stateless In-Memory Librus Synergia Client Pool & Multi-Module Data Engine.

Uses `librus_python_api.LibrusService` directly in RAM with zero disk writes.
Supports both:
- Stateless multi-tenant credentials from `UnsealedVaultPayload` (browser/JWE token)
- Automatic local fallback to `~/.config/librus-mcp/config.json` for home/Nest Hub usage
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import aiohttp

from librus_python_api import (
    AccountCredentials,
    AttendanceView,
    GradeView,
    LibrusService,
    MessageAttachmentReference,
    MessageFolder,
    MessageReference,
    MessagingBackend,
    ModernMessageAttachmentReference,
    ModernMessageReference,
    ModernRecipientReference,
    RecipientReference,
    SchoolReference,
)
from librus_python_api.config import SchedulerLimits
from pydantic import SecretStr

from app.crypto_tokens import SealedAccount, UnsealedVaultPayload
from app.study_assistant import (
    DEFAULT_LESSON_INTERVALS,
    WEEKDAYS_PL,
    StudyAssistant,
    build_deterministic_checklist,
    build_synthesized_requirements,
    classify_agenda_event,
    clean_verbatim_description,
    extract_verbatim_requirements,
    match_completed_lessons_for_subject,
    match_messages_and_materials,
    normalize_teacher_tokens,
    synthesize_action_item,
)

logger = logging.getLogger("librus-hub.client")

SESSION_TTL_SECONDS = 15 * 60  # Keep authenticated HTTP session in RAM for 15 min
DATA_CACHE_TTL_SECONDS = 5 * 60
MAX_POOL_SIZE = 64
MAX_DOCX_XML_UNCOMPRESSED_BYTES = 10 * 1024 * 1024  # 10 MB anti-Zip-Bomb limit
MAX_ATTACHMENT_PARSE_BYTES = 15 * 1024 * 1024       # 15 MB max raw attachment size

_ROOM_RE = re.compile(r"^(?P<head>.*?)\s+s\.\s*(?P<room>.+?)\s*$")
_GROUP_RE = re.compile(r"^(?P<teacher>.*?)\s*\((?P<group>[^()]+)\)\s*$")
_CODE_RE = re.compile(r"\b(?:MULT-[A-Z0-9]{4}-[A-Z0-9]{4}|[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4})\b")
_URL_RE = re.compile(r"https?://[^\s<>()\"']+")


def parse_teacher_and_classroom(raw: str | None) -> dict[str, str | None]:
    """Split Librus `teacher_and_classroom` string into teacher, group, and room."""
    if not raw:
        return {"teacher": None, "group": None, "room": None}
    text = raw.strip()
    room: str | None = None
    m_room = _ROOM_RE.match(text)
    if m_room:
        text = m_room.group("head").strip()
        raw_room = m_room.group("room").strip()
        room = f"Sala {raw_room}" if raw_room.isdigit() else raw_room

    group: str | None = None
    m_group = _GROUP_RE.match(text)
    if m_group:
        teacher = m_group.group("teacher").strip() or None
        group = m_group.group("group").strip() or None
    else:
        teacher = text or None

    return {"teacher": teacher, "group": group, "room": room}


def summarize_change(change_label: str, meta_pairs: list[list[str]] | tuple[tuple[str, str], ...]) -> dict[str, Any]:
    """Enrich a Librus timetable change entry with structured metadata."""
    label = str(change_label or "zmiana").strip()
    meta_dict: dict[str, str] = {}
    serializable_pairs: list[list[str]] = []
    for item in meta_pairs or []:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            k, v = str(item[0]), str(item[1])
            meta_dict[k] = v
            serializable_pairs.append([k, v])

    summary_parts: list[str] = []
    subj_change = meta_dict.get("Przedmiot")
    teacher_change = meta_dict.get("Nauczyciel")
    room_change = meta_dict.get("Sala")

    if subj_change and "->" in subj_change:
        orig_subj = subj_change.split("->", 1)[0].strip()
        summary_parts.append(f"Za: {orig_subj}")
    if teacher_change and "->" in teacher_change:
        orig_t, new_t = [p.strip() for p in teacher_change.split("->", 1)]
        summary_parts.append(f"{orig_t} → {new_t}")
    if room_change and "->" in room_change:
        orig_r, new_r = [p.strip() for p in room_change.split("->", 1)]
        if new_r and new_r != "[brak]":
            summary_parts.append(f"Sala: {new_r}")

    return {
        "label": label,
        "metadata": serializable_pairs,
        "meta_map": meta_dict,
        "summary": " · ".join(summary_parts) if summary_parts else label.capitalize(),
    }


def load_local_config_vault(config_path: Path) -> UnsealedVaultPayload | None:
    """Load local `~/.config/librus-mcp/config.json` (plus optional `student_accounts.json`) for home mode."""
    if os.environ.get("K_SERVICE"):
        return None
    if not config_path.exists():
        return None
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        student_sidecar_path = config_path.parent / "student_accounts.json"
        student_sidecar: dict[str, dict[str, str]] = {}
        if student_sidecar_path.exists():
            with contextlib.suppress(Exception):
                student_sidecar = json.loads(student_sidecar_path.read_text(encoding="utf-8")) or {}

        accounts_list: list[SealedAccount] = []
        for item in raw.get("accounts") or []:
            alias = str(item.get("alias") or "").strip()
            username = str(item.get("username") or "").strip()
            password = str(item.get("password") or "")
            backend = str(item.get("messaging_backend") or "modern").strip().lower()
            side = student_sidecar.get(alias) or {}
            student_username = str(
                item.get("student_username")
                or item.get("student_login")
                or side.get("student_username")
                or ""
            ).strip()
            student_password = str(
                item.get("student_password") or side.get("student_password") or ""
            )
            if alias and username and password:
                accounts_list.append(
                    SealedAccount(
                        alias=alias,
                        username=username,
                        password=password,
                        messaging_backend=backend,
                        student_username=student_username,
                        student_password=student_password,
                    )
                )
        if not accounts_list:
            return None
        return UnsealedVaultPayload(accounts=tuple(accounts_list))
    except Exception as exc:
        logger.warning("Could not load local Librus config %s: %s", config_path, exc)
        return None


def save_local_student_account(
    config_path: Path,
    alias: str,
    student_username: str,
    student_password: str,
) -> None:
    """Save or remove a paired child/student Librus account in `~/.config/librus-mcp/student_accounts.json`."""
    if os.environ.get("K_SERVICE"):
        return
    sidecar_path = config_path.parent / "student_accounts.json"
    data: dict[str, dict[str, str]] = {}
    if sidecar_path.exists():
        with contextlib.suppress(Exception):
            data = json.loads(sidecar_path.read_text(encoding="utf-8")) or {}
    clean_alias = alias.strip()
    if student_username.strip() and student_password:
        data[clean_alias] = {
            "student_username": student_username.strip(),
            "student_password": student_password,
        }
    else:
        data.pop(clean_alias, None)
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(sidecar_path, 0o600)


def extract_text_from_bytes(filename: str, content_type: str | None, data: bytes) -> str | None:
    """Extract human-readable text in RAM from PDF, DOCX, TXT, CSV, or MD attachments."""
    if not data or len(data) > MAX_ATTACHMENT_PARSE_BYTES:
        return None
    fname_low = (filename or "").lower()
    ctype_low = (content_type or "").lower()

    try:
        if fname_low.endswith(".pdf") or "pdf" in ctype_low:
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(data))
            pages_text: list[str] = []
            for idx, page in enumerate(reader.pages[:25]):
                txt = (page.extract_text() or "").strip()
                if txt:
                    pages_text.append(f"--- Strona {idx + 1} ---\n{txt}")
            return "\n\n".join(pages_text) if pages_text else "(Plik PDF nie zawiera warstwy tekstowej — skan obrazkowy)"

        if fname_low.endswith(".docx") or "wordprocessingml" in ctype_low:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                if "word/document.xml" in zf.namelist():
                    info = zf.getinfo("word/document.xml")
                    if info.file_size > MAX_DOCX_XML_UNCOMPRESSED_BYTES:
                        logger.warning(
                            "Refusing to decompress oversized word/document.xml (%d bytes) in %s",
                            info.file_size,
                            filename,
                        )
                        return None
                    xml_bytes = zf.read("word/document.xml")
                    xml_head_upper = xml_bytes[:4096].upper()
                    if b"<!ENTITY" in xml_head_upper or b"<!DOCTYPE" in xml_head_upper:
                        logger.warning("Rejected DOCX XML with DOCTYPE/ENTITY declarations in %s", filename)
                        return None
                    root = ET.fromstring(xml_bytes)
                    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
                    paragraphs: list[str] = []
                    num_counters: dict[str, int] = {}
                    for p in root.iter(f"{ns}p"):
                        texts = [
                            t.text
                            for t in p.iter(f"{ns}t")
                            if t.text
                        ]
                        p_text = "".join(texts).strip()
                        if not p_text:
                            continue
                        num_pr = p.find(f".//{ns}numPr")
                        if num_pr is not None:
                            ilvl_el = num_pr.find(f"{ns}ilvl")
                            num_id_el = num_pr.find(f"{ns}numId")
                            ilvl = ilvl_el.get(f"{ns}val", "0") if ilvl_el is not None else "0"
                            num_id = num_id_el.get(f"{ns}val", "1") if num_id_el is not None else "1"
                            if ilvl == "0":
                                idx_val = num_counters.get(num_id, 0) + 1
                                num_counters[num_id] = idx_val
                                if not re.match(r"^(\d+[\.\)]|[\-\•\*–])\s", p_text):
                                    p_text = f"{idx_val}. {p_text}"
                            else:
                                if not re.match(r"^([a-zA-Z][\.\)]|[\-\•\*–])\s", p_text):
                                    p_text = f"- {p_text}"
                        paragraphs.append(p_text)
                    return "\n".join(paragraphs)

        if any(fname_low.endswith(ext) for ext in (".txt", ".csv", ".md", ".html")) or ctype_low.startswith("text/"):
            for enc in ("utf-8", "cp1250", "iso-8859-2", "latin-1"):
                try:
                    return data.decode(enc)
                except UnicodeDecodeError:
                    continue
    except Exception as exc:
        logger.warning("Failed to extract text from attachment %s: %s", filename, exc)
    return None


class LibrusCaptchaRequiredError(ValueError):
    """Raised when Librus Synergia requires Cloudflare Turnstile CAPTCHA unlock for a login."""

    def __init__(self, username: str, message: str) -> None:
        super().__init__(message)
        self.username = username
        self.captcha_required = True


@dataclass
class PooledServiceEntry:
    service: LibrusService
    backends: dict[str, MessagingBackend]
    created_at: float
    last_used_at: float
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    cache: dict[str, tuple[float, Any]] = field(default_factory=dict)


class StatelessLibrusHub:
    """In-memory multi-tenant LibrusService manager with short-lived RAM caching."""

    def __init__(self, local_config_path: Path | None = None) -> None:
        self.local_config_path = local_config_path or (
            Path.home() / ".config" / "librus-mcp" / "config.json"
        )
        self._pool: dict[str, PooledServiceEntry] = {}
        self._pool_lock = asyncio.Lock()
        self._bad_credentials_cache: dict[str, tuple[float, str]] = {}
        self.study_assistant = StudyAssistant()

    @staticmethod
    def _cred_fingerprint(username: str, password: str) -> str:
        u = (username or "").strip().lower()
        p = password or ""
        return hashlib.sha256(f"{u}\0{p}".encode("utf-8")).hexdigest()

    @staticmethod
    def _is_auth_failure(exc: BaseException) -> bool:
        if isinstance(exc, LibrusCaptchaRequiredError):
            return True
        name = exc.__class__.__name__
        if name in (
            "AccessDeniedError",
            "CredentialsRejectedError",
            "AccountActionRequiredError",
            "LibrusCaptchaRequiredError",
        ):
            return True
        text = str(exc).lower()
        return any(
            k in text
            for k in (
                "access_denied",
                "credentials_rejected",
                "account_action_required",
                "captcha_required",
                "blokadę captcha",
                "zablokowane przez captcha",
            )
        )

    def _mark_bad_credentials(
        self, username: str, password: str, exc: BaseException
    ) -> None:
        if not username or not password:
            return
        fp = self._cred_fingerprint(username, password)
        self._bad_credentials_cache[fp] = (time.monotonic(), exc.__class__.__name__)

    def _clear_bad_credentials(self, username: str, password: str) -> None:
        if not username or not password:
            return
        fp = self._cred_fingerprint(username, password)
        self._bad_credentials_cache.pop(fp, None)

    def _is_bad_credentials(
        self, username: str, password: str, *, ttl_seconds: float = 43200.0
    ) -> bool:
        if not username or not password:
            return False
        fp = self._cred_fingerprint(username, password)
        item = self._bad_credentials_cache.get(fp)
        if item is None:
            return False
        ts, _ = item
        if (time.monotonic() - ts) < ttl_seconds:
            return True
        self._bad_credentials_cache.pop(fp, None)
        return False

    @staticmethod
    def _reset_client_session(client: Any, *, full: bool = True) -> bool:
        """Clear client cooldowns and invalidate upstream session state so the next call re-authenticates.

        Never clears authentication cooldowns if the client never established an initial Synergia identity,
        preventing failed credential submissions from being replayed and locking out the user's Librus account.
        """
        cooldowns = getattr(client, "_cooldowns", None)
        if getattr(client, "_identity", None) is None:
            if isinstance(cooldowns, dict) and "authentication" in cooldowns:
                return False
        if isinstance(cooldowns, dict):
            auth_cd = cooldowns.get("authentication")
            cooldowns.clear()
            if auth_cd is not None and getattr(client, "_identity", None) is None:
                cooldowns["authentication"] = auth_cd
                return False
        if full:
            inv = getattr(client, "_invalidate", None)
            if callable(inv):
                inv()
                return True
        inv_mod = getattr(client, "_invalidate_modern", None)
        if callable(inv_mod):
            inv_mod()
        return True

    async def close_all(self) -> None:
        async with self._pool_lock:
            entries = list(self._pool.values())
            self._pool.clear()
        for entry in entries:
            with contextlib.suppress(BaseException):
                await entry.service.__aexit__(None, None, None)

    async def _acquire_entry(self, vault: UnsealedVaultPayload) -> PooledServiceEntry:
        now = time.monotonic()
        fp = vault.fingerprint()

        async with self._pool_lock:
            # Evict expired entries
            expired_keys = [
                k
                for k, v in self._pool.items()
                if (now - v.last_used_at) > SESSION_TTL_SECONDS
            ]
            for k in expired_keys:
                old = self._pool.pop(k, None)
                if old is not None:
                    asyncio.create_task(self._safe_close_service(old.service))

            existing = self._pool.get(fp)
            if existing is not None:
                existing.last_used_at = now
                # Proactively invalidate upstream Librus cookies older than 10 minutes
                # while keeping the in-memory data cache warm.
                if (now - existing.created_at) > 600.0:
                    existing.created_at = now
                    for acc_key in existing.backends:
                        with contextlib.suppress(Exception):
                            self._reset_client_session(existing.service.account(acc_key), full=True)
                return existing

            if len(self._pool) >= MAX_POOL_SIZE:
                lru_key = min(self._pool.items(), key=lambda kv: kv[1].last_used_at)[0]
                evicted = self._pool.pop(lru_key, None)
                if evicted is not None:
                    asyncio.create_task(self._safe_close_service(evicted.service))

            creds_map: dict[str, AccountCredentials] = {}
            backends: dict[str, MessagingBackend] = {}
            for acc in vault.accounts:
                mb = (
                    MessagingBackend.LEGACY
                    if acc.messaging_backend == "legacy"
                    else MessagingBackend.MODERN
                )
                creds_map[acc.alias] = AccountCredentials(
                    login=SecretStr(acc.username),
                    password=SecretStr(acc.password),
                )
                backends[acc.alias] = mb
                if (
                    acc.student_username
                    and acc.student_password
                    and acc.student_username != acc.username
                ):
                    student_key = f"{acc.alias}__student"
                    creds_map[student_key] = AccountCredentials(
                        login=SecretStr(acc.student_username),
                        password=SecretStr(acc.student_password),
                    )
                    backends[student_key] = mb
            context_key = hashlib.sha256(f"ctx:{fp}".encode("utf-8")).digest()
            scheduler_limits = SchedulerLimits(
                requests_per_second=15.0,
                burst=30,
                active_requests=4,
                active_requests_per_account=1,
                queued_requests=128,
                queued_requests_per_account=64,
                accounts=32,
                operations=128,
                operations_per_account=64,
            )
            service = LibrusService(
                creds_map,
                context_key=context_key,
                scheduler_limits=scheduler_limits,
            )
            await service.__aenter__()
            entry = PooledServiceEntry(
                service=service,
                backends=backends,
                created_at=now,
                last_used_at=now,
            )
            self._pool[fp] = entry
            return entry

    @staticmethod
    def _format_sent_at(raw_val: Any, dt_obj: Any = None) -> str:
        s = str(raw_val or "").strip()
        if not s and dt_obj is not None:
            s = dt_obj.isoformat(sep=" ") if hasattr(dt_obj, "isoformat") else str(dt_obj)
        s = s.replace("T", " ")
        # Trim trailing timezone offset or seconds if YYYY-MM-DD HH:MM:SS
        if len(s) >= 16 and s[4:5] == "-" and s[7:8] == "-" and s[10:11] == " ":
            return s[:16]
        return s

    async def check_librus_captcha_needed(self, username: str) -> bool:
        """Read-only check against https://api.librus.pl/OAuth/Captcha (is_needed=1).

        Mirrors the checkCaptcha() call in Librus's official Authorization.js on #Login blur
        and after login errors. Does not submit a password or increment failed login counters.
        """
        u = (username or "").strip()
        if not u or getattr(self, "_demo_mode", False):
            return False
        timeout = aiohttp.ClientTimeout(total=4.0, connect=2.5)
        headers = {
            "Origin": "https://api.librus.pl",
            "Referer": "https://api.librus.pl/OAuth/Authorization?client_id=46",
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        }
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    "https://api.librus.pl/OAuth/Captcha",
                    data={"username": u, "is_needed": "1"},
                    headers=headers,
                ) as resp:
                    if resp.status != 200:
                        return False
                    raw = await resp.text()
                    data = json.loads(raw)
                    return bool(isinstance(data, dict) and data.get("is_needed"))
        except Exception:
            return False

    async def verify_student_account(
        self, student_username: str, student_password: str
    ) -> dict[str, Any]:
        """Verify parent or student Librus Synergia credentials before saving."""
        u = (student_username or "").strip()
        p = student_password or ""
        if not u or not p:
            return {"verified": False}
        if "@" in u:
            raise ValueError(
                f"Wpisano adres e-mail („{u}”) zamiast loginu Synergia. Wpisz cyfrowy login do dziennika Librus Synergia (np. 8090546 dla rodzica lub 8090546u dla ucznia)."
            )
        # In demo/mock mode, accept without live Librus network call
        if getattr(self, "_demo_mode", False):
            return {"verified": True, "student_name": u}
        role_label = "dziecka/ucznia" if u.lower().endswith("u") else "rodzica"
        # 1. Pre-check /OAuth/Captcha (is_needed=1) before sending password so we never hit a CAPTCHA-locked account
        if await self.check_librus_captcha_needed(u):
            raise LibrusCaptchaRequiredError(
                u,
                f"Konto {role_label} „{u}” ma aktywną blokadę CAPTCHA w Librus Synergia („Przekroczono limit prób logowania”). "
                "Otwórz w przeglądarce stronę https://portal.librus.pl/rodzina/synergia/loguj, wybierz „Cloudflare Turnstile” i zaloguj się jeden raz, aby zdjąć blokadę, a następnie wróć do bursLi i kliknij zapisz.",
            )
        if self._is_bad_credentials(u, p, ttl_seconds=60.0):
            raise ValueError(
                f"Librus Synergia przed chwilą odrzucił tę kombinację loginu i hasła dla konta {role_label} „{u}”. "
                "Odczekaj chwilę lub upewnij się, że wpisujesz poprawne hasło do dziennika Synergia."
            )
        creds_map = {
            "student_verify": AccountCredentials(
                login=SecretStr(u),
                password=SecretStr(p),
            )
        }
        context_key = hashlib.sha256(f"verify:{u}".encode("utf-8")).digest()
        service = LibrusService(creds_map, context_key=context_key)
        try:
            await service.__aenter__()
            client = service.account("student_verify")
            info = await client.student_information(max_age_seconds=0.0)
            self._clear_bad_credentials(u, p)
            student_name = str(getattr(info, "name", None) or getattr(info, "student_name", "") or u).strip()
            class_name = str(getattr(info, "class_name", "") or "").strip()
            return {
                "verified": True,
                "student_name": student_name,
                "class_name": class_name,
            }
        except Exception as exc:
            if self._is_auth_failure(exc):
                self._mark_bad_credentials(u, p, exc)
                if await self.check_librus_captcha_needed(u):
                    raise LibrusCaptchaRequiredError(
                        u,
                        f"Konto {role_label} „{u}” zostało zablokowane przez CAPTCHA w Librus Synergia („Przekroczono limit prób logowania”). "
                        "Otwórz w przeglądarce stronę https://portal.librus.pl/rodzina/synergia/loguj, wybierz „Cloudflare Turnstile” i zaloguj się jeden raz, aby zdjąć blokadę, a następnie wróć do bursLi.",
                    ) from exc
            logger.warning("Librus account verification failed for %s: %s", u, type(exc).__name__)
            raise ValueError(
                f"Librus Synergia odrzucił hasło dla konta {role_label} „{u}” (błędny login lub hasło — blokada CAPTCHA nie jest aktywna). "
                "Upewnij się, że podajesz hasło do dziennika Librus Synergia (portal.librus.pl/rodzina/synergia/loguj), a nie hasło do konta e-mail z aplikacji Konto LIBRUS."
            ) from exc
        finally:
            await self._safe_close_service(service)

    @staticmethod
    async def _safe_close_service(service: LibrusService) -> None:
        with contextlib.suppress(BaseException):
            await service.__aexit__(None, None, None)

    async def get_profiles(
        self, vault: UnsealedVaultPayload, *, force: bool = False
    ) -> list[dict[str, Any]]:
        entry = await self._acquire_entry(vault)
        cache_key = "profiles"
        now = time.monotonic()
        if not force and cache_key in entry.cache:
            ts, val = entry.cache[cache_key]
            if (now - ts) < DATA_CACHE_TTL_SECONDS:
                return val

        async def _load_one(acc: SealedAccount) -> dict[str, Any]:
            client = entry.service.account(acc.alias)
            is_student_only = bool(
                acc.student_username
                and acc.student_password
                and acc.username == acc.student_username
            )
            has_parent = not is_student_only and bool(acc.username and acc.password)
            has_student = bool(acc.student_username and acc.student_password)
            parent_username = "" if is_student_only else acc.username
            student_username = acc.student_username or ""
            base_num = (
                acc.username[:-1]
                if acc.username and acc.username.lower().endswith("u")
                else acc.username
            )
            suggested_student = f"{base_num}u" if base_num else ""
            suggested_parent = base_num if base_num else ""
            account_mode = (
                "both"
                if (has_parent and has_student)
                else ("student_only" if is_student_only else "parent_only")
            )
            try:
                try:
                    if self._is_bad_credentials(acc.username, acc.password):
                        raise RuntimeError("access_denied (cached circuit breaker)")
                    if getattr(client, "_identity", None) is None and await self.check_librus_captcha_needed(acc.username):
                        raise LibrusCaptchaRequiredError(
                            acc.username,
                            f"Konto „{acc.username}” wymaga odblokowania CAPTCHA (Cloudflare Turnstile) na stronie portal.librus.pl/rodzina/synergia/loguj.",
                        )
                    info = await client.student_information(max_age_seconds=300.0)
                    self._clear_bad_credentials(acc.username, acc.password)
                except Exception as primary_exc:
                    if self._is_auth_failure(primary_exc) and getattr(client, "_identity", None) is None:
                        self._mark_bad_credentials(acc.username, acc.password, primary_exc)
                    student_key = f"{acc.alias}__student"
                    if has_student and student_key in entry.backends:
                        if self._is_bad_credentials(acc.student_username, acc.student_password):
                            raise primary_exc
                        st_client = entry.service.account(student_key)
                        try:
                            if getattr(st_client, "_identity", None) is None and await self.check_librus_captcha_needed(acc.student_username):
                                raise LibrusCaptchaRequiredError(
                                    acc.student_username,
                                    f"Konto „{acc.student_username}” wymaga odblokowania CAPTCHA (Cloudflare Turnstile) na stronie portal.librus.pl/rodzina/synergia/loguj.",
                                )
                            info = await st_client.student_information(
                                max_age_seconds=300.0
                            )
                            self._clear_bad_credentials(acc.student_username, acc.student_password)
                        except Exception as st_exc:
                            if self._is_auth_failure(st_exc) and getattr(st_client, "_identity", None) is None:
                                self._mark_bad_credentials(acc.student_username, acc.student_password, st_exc)
                            raise
                    else:
                        raise
                full_name = str(info.name or acc.alias.capitalize()).strip()
                first_name = full_name.split()[0] if full_name else acc.alias.capitalize()
                school_lines = [
                    line.strip()
                    for line in str(info.school or "").splitlines()
                    if line.strip()
                ]
                lucky_raw = info.lucky_number
                lucky_num = (
                    lucky_raw
                    if isinstance(lucky_raw, int)
                    else getattr(lucky_raw, "number", None)
                )
                return {
                    "alias": acc.alias,
                    "account": acc.alias,
                    "first_name": first_name,
                    "full_name": full_name,
                    "student_name": full_name,
                    "class_name": str(info.class_name or "").strip(),
                    "number": info.register_number,
                    "educator": str(info.tutor or "").strip(),
                    "school": school_lines[0] if school_lines else "",
                    "lucky_number": lucky_num,
                    "parent_username": parent_username,
                    "has_parent_account": has_parent,
                    "has_student_account": has_student,
                    "student_username": student_username,
                    "suggested_student_username": suggested_student,
                    "suggested_parent_username": suggested_parent,
                    "account_mode": account_mode,
                    "auth_error": False,
                    "auth_captcha_required": False,
                    "auth_error_message": "",
                }
            except Exception as exc:
                logger.warning("Failed to load student_information for %s: %s", acc.alias, exc)
                err_text = str(exc)
                is_auth_err = self._is_auth_failure(exc)
                captcha_needed = isinstance(exc, LibrusCaptchaRequiredError)
                if is_auth_err and not captcha_needed:
                    if acc.username and await self.check_librus_captcha_needed(acc.username):
                        captcha_needed = True
                    elif acc.student_username and await self.check_librus_captcha_needed(acc.student_username):
                        captcha_needed = True
                return {
                    "alias": acc.alias,
                    "account": acc.alias,
                    "first_name": acc.alias.capitalize(),
                    "full_name": acc.alias.capitalize(),
                    "student_name": acc.alias.capitalize(),
                    "class_name": "",
                    "number": None,
                    "educator": "",
                    "school": "",
                    "lucky_number": None,
                    "parent_username": parent_username,
                    "has_parent_account": has_parent,
                    "has_student_account": has_student,
                    "student_username": student_username,
                    "suggested_student_username": suggested_student,
                    "suggested_parent_username": suggested_parent,
                    "account_mode": account_mode,
                    "auth_error": is_auth_err,
                    "auth_captcha_required": captcha_needed,
                    "auth_error_message": (
                        "Konto ma aktywną blokadę CAPTCHA w Librus Synergia („Przekroczono limit prób logowania”). Zaloguj się raz przez Cloudflare Turnstile na portal.librus.pl/rodzina/synergia/loguj, aby odblokować konto."
                        if captcha_needed
                        else (
                            "Hasło lub login zostały odrzucone przez Librus Synergia (blokada CAPTCHA nie jest aktywna — sprawdź hasło do Synergii)."
                            if is_auth_err
                            else err_text
                        )
                    ),
                }

        profiles: list[dict[str, Any]] = []
        for acc in vault.accounts:
            profiles.append(await _load_one(acc))
        entry.cache[cache_key] = (now, profiles)
        return profiles

    async def get_timetable(
        self,
        vault: UnsealedVaultPayload,
        alias: str,
        monday: date,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        entry = await self._acquire_entry(vault)
        monday_iso = monday.isoformat()
        cache_key = f"timetable:{alias}:{monday_iso}"
        now = time.monotonic()
        if not force and cache_key in entry.cache:
            ts, val = entry.cache[cache_key]
            if (now - ts) < DATA_CACHE_TTL_SECONDS:
                return val

        client = entry.service.account(alias)
        tt_task = client.timetable(monday, max_age_seconds=0.0 if force else 120.0)
        free_task = client.class_free_days(max_age_seconds=600.0)
        tt_res, free_res = await asyncio.gather(tt_task, free_task, return_exceptions=True)
        if isinstance(tt_res, BaseException):
            if getattr(client, "_identity", None) is not None:
                with contextlib.suppress(Exception):
                    if self._reset_client_session(client, full=True):
                        await client.student_information(max_age_seconds=0.0)
                        tt_res = await client.timetable(monday, max_age_seconds=0.0)
            if isinstance(tt_res, BaseException):
                raise tt_res

        occupied_numbers: set[int] = set()
        enriched_days: list[dict[str, Any]] = []

        for day_obj in tt_res.days:
            day_str = day_obj.day.isoformat()
            enriched_periods: list[dict[str, Any]] = []
            for period in day_obj.periods:
                num = int(period.number)
                starts_at = (
                    period.interval.starts_at.strftime("%H:%M")
                    if period.interval and period.interval.starts_at
                    else DEFAULT_LESSON_INTERVALS.get(num, ("08:00", "08:45"))[0]
                )
                ends_at = (
                    period.interval.ends_at.strftime("%H:%M")
                    if period.interval and period.interval.ends_at
                    else DEFAULT_LESSON_INTERVALS.get(num, ("08:00", "08:45"))[1]
                )

                recess_minutes = 0
                if period.next_recess and period.next_recess.starts_at and period.next_recess.ends_at:
                    sh, sm = period.next_recess.starts_at.hour, period.next_recess.starts_at.minute
                    eh, em = period.next_recess.ends_at.hour, period.next_recess.ends_at.minute
                    recess_minutes = max(0, (eh * 60 + em) - (sh * 60 + sm))

                seen_lessons: set[tuple[str, str]] = set()
                deduped_lessons: list[dict[str, Any]] = []
                for lesson in period.lessons:
                    subj = str(lesson.subject or "").strip()
                    tac = str(lesson.teacher_and_classroom or "").strip()
                    key = (subj, tac)
                    if key in seen_lessons:
                        continue
                    seen_lessons.add(key)
                    parsed = parse_teacher_and_classroom(tac)
                    deduped_lessons.append(
                        {
                            "subject": subj,
                            "teacher_and_classroom": tac or None,
                            "teacher": parsed["teacher"],
                            "group": parsed["group"],
                            "room": parsed["room"],
                        }
                    )

                enriched_changes = [
                    summarize_change(c.label, c.metadata) for c in period.changes
                ]
                if deduped_lessons or enriched_changes:
                    occupied_numbers.add(num)

                enriched_periods.append(
                    {
                        "number": num,
                        "starts_at": starts_at,
                        "ends_at": ends_at,
                        "recess_minutes": recess_minutes,
                        "lessons": deduped_lessons,
                        "changes": enriched_changes,
                    }
                )

            active_slots = [
                p for p in enriched_periods if p["lessons"] or p["changes"]
            ]
            changes_count = sum(len(p["changes"]) for p in enriched_periods)
            enriched_days.append(
                {
                    "day": day_str,
                    "periods": enriched_periods,
                    "lesson_count": len(active_slots),
                    "first_start": active_slots[0]["starts_at"] if active_slots else None,
                    "last_end": active_slots[-1]["ends_at"] if active_slots else None,
                    "changes_count": changes_count,
                }
            )

        min_num = min(occupied_numbers) if occupied_numbers else 1
        max_num = max(occupied_numbers) if occupied_numbers else 8
        min_period = min(min_num, 1) if 0 in occupied_numbers else 1
        max_period = max(max_num, 8)
        for d in enriched_days:
            d["periods"] = [
                p for p in d["periods"] if min_period <= p["number"] <= max_period
            ]

        free_days_list: list[dict[str, Any]] = []
        if not isinstance(free_res, BaseException):
            week_end = monday + timedelta(days=6)
            for fd in free_res.items:
                if fd.date_to >= monday and fd.date_from <= week_end:
                    free_days_list.append(
                        {
                            "id": fd.identifier,
                            "date_from": fd.date_from.isoformat(),
                            "date_to": fd.date_to.isoformat(),
                            "lesson_from": fd.lesson_no_from,
                            "lesson_to": fd.lesson_no_to,
                        }
                    )

        result = {
            "monday": tt_res.monday.isoformat(),
            "timezone": "Europe/Warsaw",
            "min_period": min_period,
            "max_period": max_period,
            "days": enriched_days,
            "class_free_days": free_days_list,
        }
        entry.cache[cache_key] = (now, result)
        return result

    @staticmethod
    def _has_student_account(vault: UnsealedVaultPayload, alias: str) -> bool:
        return any(
            a.alias == alias and bool(a.student_username and a.student_password)
            for a in vault.accounts
        )

    @staticmethod
    def _is_student_only_account(vault: UnsealedVaultPayload, alias: str) -> bool:
        return any(
            a.alias == alias
            and bool(
                a.student_username
                and a.student_password
                and a.username == a.student_username
            )
            for a in vault.accounts
        )

    async def _fetch_merged_messages_with_excerpts(
        self,
        entry: PooledServiceEntry,
        vault: UnsealedVaultPayload,
        alias: str,
        *,
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        """Fetch recent messages from parent account and (if configured) student account, merging & tagging."""
        primary_source = "uczeń" if self._is_student_only_account(vault, alias) else "rodzic"
        tasks = [
            self._fetch_recent_messages_with_excerpts(
                entry, alias, limit=limit, account_source=primary_source
            )
        ]
        student_key = f"{alias}__student"
        if self._has_student_account(vault, alias) and student_key in entry.backends:
            tasks.append(
                self._fetch_recent_messages_with_excerpts(
                    entry, student_key, limit=limit, account_source="uczeń"
                )
            )
        results = await asyncio.gather(*tasks, return_exceptions=True)
        merged: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        for res in results:
            if isinstance(res, BaseException):
                logger.warning("Failed to fetch messages for %s: %s", alias, res)
                continue
            for m in res:
                dedup_k = f"{str(m.get('correspondent') or '').lower()}|{str(m.get('subject') or '').lower()}|{str(m.get('sent_at') or '')[:16]}"
                if dedup_k in seen_keys:
                    continue
                seen_keys.add(dedup_k)
                merged.append(m)
        merged.sort(key=lambda x: str(x.get("sent_at") or ""), reverse=True)
        return merged

    async def get_exams_and_study_cards(
        self,
        vault: UnsealedVaultPayload,
        alias: str,
        *,
        days_ahead: int = 14,
        days_behind: int = 0,
        today: date | None = None,
        use_ai: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        """Aggregate upcoming exams, quizzes, projects, readings and build full StudyCards."""
        entry = await self._acquire_entry(vault)
        ref_today = today or date.today()
        start_date = ref_today - timedelta(days=max(0, days_behind))
        end_date = ref_today + timedelta(days=days_ahead)
        cache_key = f"exams:{alias}:{start_date.isoformat()}:{end_date.isoformat()}:{int(use_ai)}"
        now = time.monotonic()
        if not force and cache_key in entry.cache:
            ts, val = entry.cache[cache_key]
            if (now - ts) < DATA_CACHE_TTL_SECONDS:
                return val

        client = entry.service.account(alias)
        months_to_fetch: list[tuple[int, int]] = []
        for d_ref in (start_date, ref_today, end_date):
            ym = (d_ref.year, d_ref.month)
            if ym not in months_to_fetch:
                months_to_fetch.append(ym)

        lessons_start = start_date - timedelta(days=38)
        agenda_tasks = [
            client.agenda(y, m, max_age_seconds=120.0) for (y, m) in months_to_fetch
        ]
        lessons_task = client.completed_lessons(
            lessons_start, ref_today, limit=128, max_pages=3, max_age_seconds=180.0
        )
        hw_task = client.homework(start_date - timedelta(days=7), end_date, max_age_seconds=180.0)
        ann_task = client.announcements(max_age_seconds=180.0)
        msg_list_task = self._fetch_merged_messages_with_excerpts(entry, vault, alias, limit=30)

        extra_hw_tasks = []
        student_key = f"{alias}__student"
        if self._has_student_account(vault, alias) and student_key in entry.backends:
            student_client = entry.service.account(student_key)
            extra_hw_tasks.append(
                student_client.homework(start_date - timedelta(days=7), end_date, max_age_seconds=180.0)
            )

        gathered = await asyncio.gather(
            *agenda_tasks,
            lessons_task,
            hw_task,
            ann_task,
            msg_list_task,
            *extra_hw_tasks,
            return_exceptions=True,
        )
        n_ag = len(months_to_fetch)
        agenda_results = gathered[:n_ag]
        lessons_res = gathered[n_ag]
        hw_res = gathered[n_ag + 1]
        ann_res = gathered[n_ag + 2]
        recent_messages = (
            gathered[n_ag + 3]
            if not isinstance(gathered[n_ag + 3], BaseException)
            else []
        )
        student_hw_res = gathered[n_ag + 4] if extra_hw_tasks else None

        # Normalize completed lessons
        completed_lessons_list: list[dict[str, Any]] = []
        if not isinstance(lessons_res, BaseException):
            for les in lessons_res.items:
                completed_lessons_list.append(
                    {
                        "day": les.day.isoformat(),
                        "weekday": les.weekday,
                        "lesson_number": les.lesson_number,
                        "subject": les.subject,
                        "teacher": les.teacher,
                        "topic": les.topic,
                    }
                )

        # Normalize homework items (parent + optional student account)
        homework_list: list[dict[str, Any]] = []
        seen_hw: set[str] = set()
        for h_source in (hw_res, student_hw_res):
            if h_source is not None and not isinstance(h_source, BaseException):
                for hw in h_source.items:
                    hk = f"{hw.subject}|{hw.due_on.isoformat()}|{hw.topic}"
                    if hk in seen_hw:
                        continue
                    seen_hw.add(hk)
                    homework_list.append(
                        {
                            "subject": hw.subject,
                            "teacher": hw.teacher,
                            "topic": hw.topic,
                            "category": hw.category,
                            "assigned_on": hw.assigned_on.isoformat(),
                            "due_on": hw.due_on.isoformat(),
                        }
                    )

        announcements_list: list[dict[str, Any]] = []
        if not isinstance(ann_res, BaseException):
            for ann in ann_res.items[:20]:
                announcements_list.append(
                    {
                        "title": ann.title,
                        "author": ann.author,
                        "published_on": ann.published_on.isoformat() if ann.published_on else ann.date_text,
                        "content": ann.content,
                    }
                )

        # Collect agenda events in [start_date, end_date], excluding routine timetable
        # substitutions/cancellations, school holidays, and trips.
        if agenda_results and all(isinstance(ag, BaseException) for ag in agenda_results):
            raise agenda_results[0]

        raw_events = []
        for ag in agenda_results:
            if isinstance(ag, BaseException):
                continue
            for day_obj in getattr(ag, "days", ()):
                for ev in getattr(day_obj, "events", ()):
                    if start_date <= ev.day <= end_date:
                        title_low = (ev.title or "").strip().lower()
                        text_low = (ev.text or "").strip().lower()
                        if title_low.startswith(
                            ("zastępstwo", "odwołane", "nieobecność", "przesunięcie")
                        ) or (
                            "zastępstwo z " in title_low
                            and not any(
                                kw in title_low or kw in text_low
                                for kw in (
                                    "sprawdzian",
                                    "kartkówka",
                                    "klasówka",
                                    "test",
                                    "projekt",
                                    "lektura",
                                )
                            )
                        ):
                            continue
                        raw_events.append(ev)

        raw_events.sort(key=lambda e: (e.day, e.lesson_number or 99, e.title))

        # Fetch agenda_detail concurrently for each event
        sem = asyncio.Semaphore(5)

        async def _enrich_event(ev: Any) -> dict[str, Any] | None:
            detail_map: dict[str, str] = {}
            for k, v in ev.metadata or ():
                detail_map[str(k)] = str(v)

            if ev.reference is not None:
                async with sem:
                    with contextlib.suppress(Exception):
                        det = await client.agenda_detail(ev.reference, max_age_seconds=300.0)
                        for nf in det.normalized_fields or ():
                            detail_map[str(nf.raw_label)] = str(nf.value)

            subject = (
                ev.subject
                or detail_map.get("Przedmiot")
                or (ev.title.split(",", 1)[0].strip() if "," in ev.title else None)
            )
            category_label = (
                detail_map.get("Rodzaj")
                or (ev.title.split(",", 1)[1].strip() if "," in ev.title else ev.title)
            )
            teacher = detail_map.get("Nauczyciel") or ""
            classroom = detail_map.get("Sala") or ""
            added_at = detail_map.get("Data dodania") or ""
            class_label = detail_map.get("Klasa") or ""
            verbatim_desc = (
                detail_map.get("Opis")
                or detail_map.get("Zakres")
                or ev.text
                or ev.title
            ).strip()

            cat_type = classify_agenda_event(ev.title, category_label, verbatim_desc)
            cat_label_low = (category_label or "").strip().lower()
            # Exclude substitutions, school free days/holidays, and class trips from 02 Sprawdziany
            if cat_type in ("wolne", "wycieczka"):
                return None
            if cat_type == "inne" and cat_label_low.startswith(
                ("zastępstwo", "odwołane", "nieobecność", "przesunięcie", "wycieczka", "dzień wolny")
            ):
                return None

            lesson_num = ev.lesson_number
            if lesson_num is None and detail_map.get("Nr lekcji", "").isdigit():
                lesson_num = int(detail_map["Nr lekcji"])

            interval = (
                DEFAULT_LESSON_INTERVALS.get(lesson_num)
                if lesson_num is not None
                else None
            )

            cleaned_desc = clean_verbatim_description(verbatim_desc, subject, category_label)
            verbatim_reqs = extract_verbatim_requirements(verbatim_desc, subject, category_label)
            matched_lessons = match_completed_lessons_for_subject(
                subject,
                ev.day,
                completed_lessons_list,
                event_type=cat_type,
                verbatim_description=cleaned_desc,
            )
            matched_mats = match_messages_and_materials(
                subject,
                teacher,
                recent_messages,
                homework_list,
                announcements=announcements_list,
                exam_day=ev.day,
                event_type=cat_type,
                verbatim_description=cleaned_desc,
                unit_lessons=matched_lessons["unit_lessons"],
            )

            event_id = (
                ev.reference.identifier
                if ev.reference is not None
                else hashlib.sha1(f"{alias}:{ev.day.isoformat()}:{ev.title}".encode("utf-8")).hexdigest()[:12]
            )

            synthesized_reqs = build_synthesized_requirements(
                subject=subject or ev.title,
                category_label=category_label,
                category_type=cat_type,
                verbatim_reqs=verbatim_reqs,
                unit_lessons=matched_lessons["unit_lessons"],
                matched_mats=matched_mats,
            )

            checklist = build_deterministic_checklist(
                event_id,
                verbatim_reqs,
                matched_lessons["unit_lessons"],
                matched_mats,
            )

            days_until = (ev.day - ref_today).days
            card: dict[str, Any] = {
                "id": str(event_id),
                "day": ev.day.isoformat(),
                "weekday_pl": WEEKDAYS_PL[ev.day.weekday()],
                "days_until": days_until,
                "title": ev.title,
                "subject": subject or ev.title,
                "category_label": category_label,
                "category_type": cat_type,
                "lesson_number": lesson_num,
                "starts_at": (
                    interval[0]
                    if interval
                    else (ev.at_time.strftime("%H:%M") if ev.at_time else None)
                ),
                "ends_at": interval[1] if interval else None,
                "teacher": teacher,
                "classroom": classroom,
                "class_label": class_label,
                "added_at": added_at,
                "verbatim_description": cleaned_desc or verbatim_desc,
                "verbatim_requirements": verbatim_reqs,
                "synthesized_requirements": synthesized_reqs,
                "unit_lessons": matched_lessons["unit_lessons"],
                "all_recent_lessons": matched_lessons["all_recent_lessons"],
                "related_messages": matched_mats["messages"],
                "related_homework": matched_mats["homework"],
                "related_announcements": matched_mats["announcements"],
                "extracted_codes": matched_mats["codes"],
                "extracted_links": matched_mats["links"],
                "checklist": checklist,
            }
            if use_ai:
                card["ai_synthesis"] = await self.study_assistant.enrich_study_card(card)
            else:
                card["ai_synthesis"] = self.study_assistant._build_deterministic_summary(card)
            return card

        enriched_cards = await asyncio.gather(*(_enrich_event(ev) for ev in raw_events))
        cards = [c for c in enriched_cards if c is not None]

        # Compute summary counts
        counts = {
            "total": len(cards),
            "sprawdzian": sum(1 for c in cards if c["category_type"] == "sprawdzian"),
            "kartkowka": sum(1 for c in cards if c["category_type"] == "kartkowka"),
            "projekt": sum(1 for c in cards if c["category_type"] == "projekt"),
            "lektura": sum(1 for c in cards if c["category_type"] == "lektura"),
            "inne": sum(
                1
                for c in cards
                if c["category_type"] not in ("sprawdzian", "kartkowka", "projekt", "lektura")
            ),
        }

        result = {
            "alias": alias,
            "has_student_account": self._has_student_account(vault, alias),
            "date_from": start_date.isoformat(),
            "date_to": end_date.isoformat(),
            "counts": counts,
            "items": cards,
            "homework": homework_list,
        }
        entry.cache[cache_key] = (now, result)
        return result

    async def get_materials_and_announcements(
        self,
        vault: UnsealedVaultPayload,
        alias: str,
        *,
        today: date | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Fetch school announcements, homework, trips/events, and teacher messages with synthesized Action Items."""
        entry = await self._acquire_entry(vault)
        ref_today = today or date.today()
        cache_key = f"materials:{alias}:{ref_today.isoformat()}"
        now = time.monotonic()
        if not force and cache_key in entry.cache:
            ts, val = entry.cache[cache_key]
            if (now - ts) < DATA_CACHE_TTL_SECONDS:
                return val

        client = entry.service.account(alias)
        end_date = ref_today + timedelta(days=30)
        months_to_fetch: list[tuple[int, int]] = [(ref_today.year, ref_today.month)]
        if (end_date.year, end_date.month) not in months_to_fetch:
            months_to_fetch.append((end_date.year, end_date.month))

        ann_task = client.announcements(max_age_seconds=180.0)
        hw_task = client.homework(
            ref_today - timedelta(days=14),
            end_date,
            max_age_seconds=180.0,
        )
        free_task = client.class_free_days(max_age_seconds=600.0)
        lessons_task = client.completed_lessons(
            ref_today - timedelta(days=30), ref_today, limit=80, max_pages=2, max_age_seconds=300.0
        )
        msg_task = self._fetch_merged_messages_with_excerpts(entry, vault, alias, limit=30)
        ag_tasks = [client.agenda(y, m, max_age_seconds=180.0) for (y, m) in months_to_fetch]

        student_key = f"{alias}__student"
        extra_hw_tasks = []
        if self._has_student_account(vault, alias) and student_key in entry.backends:
            student_client = entry.service.account(student_key)
            extra_hw_tasks.append(
                student_client.homework(
                    ref_today - timedelta(days=14), end_date, max_age_seconds=180.0
                )
            )

        gathered = await asyncio.gather(
            ann_task,
            hw_task,
            free_task,
            lessons_task,
            msg_task,
            *ag_tasks,
            *extra_hw_tasks,
            return_exceptions=True,
        )
        ann_res, hw_res, free_res, lessons_res, messages = gathered[:5]
        n_ag = len(ag_tasks)
        ag_results = gathered[5 : 5 + n_ag]
        student_hw_res = gathered[5 + n_ag] if extra_hw_tasks else None

        # Build teacher -> subject map from recent completed lessons
        teacher_subject_map: dict[str, str] = {}
        if not isinstance(lessons_res, BaseException):
            for les in lessons_res.items:
                if les.teacher and les.subject:
                    teacher_subject_map[str(les.teacher).strip()] = str(les.subject).strip()

        action_items: list[dict[str, Any]] = []

        # 1. Homework items (parent + optional student account)
        homework_list: list[dict[str, Any]] = []
        seen_hw_keys: set[str] = set()
        for hw_src_tag, h_res in (("rodzic", hw_res), ("uczeń", student_hw_res)):
            if h_res is not None and not isinstance(h_res, BaseException):
                for idx, hw in enumerate(h_res.items):
                    due_iso = hw.due_on.isoformat()
                    assigned_iso = hw.assigned_on.isoformat()
                    dedup_hw = f"{hw.subject}|{due_iso}|{hw.topic}"
                    if dedup_hw in seen_hw_keys:
                        continue
                    seen_hw_keys.add(dedup_hw)
                    synth = synthesize_action_item(
                        kind="homework",
                        title=f"{hw.category or 'Zadanie'}: {hw.subject}",
                        body=hw.topic,
                        sender=hw.teacher,
                        subject=hw.subject,
                        published_on=assigned_iso,
                        due_on=due_iso,
                        account_source=hw_src_tag,
                        teacher_subject_map=teacher_subject_map,
                        ref_today=ref_today,
                    )
                    hw_dict = {
                        "id": f"hw-{hw_src_tag}-{idx}-{due_iso}",
                        "subject": hw.subject,
                        "teacher": hw.teacher,
                        "topic": hw.topic,
                        "category": hw.category,
                        "assigned_on": assigned_iso,
                        "due_on": due_iso,
                        "status": hw.submission_status,
                        **synth,
                    }
                    homework_list.append(hw_dict)
                    action_items.append(hw_dict)

        # 2. Trips / school events from Terminarz (which were excluded from 02 Sprawdziany)
        for ag in ag_results:
            if isinstance(ag, BaseException):
                continue
            for day_obj in getattr(ag, "days", ()):
                for ev in getattr(day_obj, "events", ()):
                    if ref_today <= ev.day <= end_date:
                        cat_t = classify_agenda_event(ev.title or "", "", ev.text or "")
                        if cat_t in ("wycieczka", "zadanie"):
                            ev_desc = clean_verbatim_description(ev.text or ev.title or "", ev.subject, "")
                            synth = synthesize_action_item(
                                kind="trip" if cat_t == "wycieczka" else "homework",
                                title=ev.title or "Wydarzenie klasowe",
                                body=ev_desc or ev.title or "",
                                sender="Terminarz",
                                subject=ev.subject or ("Wycieczka / Klasa" if cat_t == "wycieczka" else "Zadanie"),
                                published_on=ev.day.isoformat(),
                                due_on=ev.day.isoformat(),
                                teacher_subject_map=teacher_subject_map,
                                ref_today=ref_today,
                            )
                            action_items.append(
                                {
                                    "id": f"ag-{ev.day.isoformat()}-{hashlib.sha1((ev.title or '').encode('utf-8')).hexdigest()[:6]}",
                                    **synth,
                                }
                            )

        # 3. Important messages (excluding behavior remarks "Dodano uwagę dla ucznia..." and deleted messages)
        extracted_materials: list[dict[str, Any]] = []
        if not isinstance(messages, BaseException):
            for m in messages:
                subj_str = str(m.get("subject") or "").strip()
                subj_low = subj_str.lower()
                body_str = str(m.get("body") or "").strip()
                body_low = body_str.lower()
                # Skip behavior remarks and sender-deleted messages in 03 Zadania
                if any(
                    pat in subj_low or pat in body_low[:120]
                    for pat in (
                        "dodano uwagę",
                        "uwaga dla ucznia",
                        "uwaga negatywna",
                        "uwaga pozytywna",
                        "wiadomość usunięta przez nadawcę",
                        "wiadomosc usunieta przez nadawce",
                    )
                ):
                    continue

                codes = _CODE_RE.findall(f"{subj_str} {body_str}")
                links = _URL_RE.findall(f"{subj_str} {body_str}")
                synth = synthesize_action_item(
                    kind="message",
                    title=subj_str,
                    body=body_str,
                    sender=str(m.get("correspondent") or ""),
                    published_on=str(m.get("sent_at") or ""),
                    unread=bool(m.get("unread")),
                    has_attachment=bool(m.get("has_attachment")),
                    codes=codes,
                    links=links,
                    account_source=str(m.get("account_source") or "rodzic"),
                    teacher_subject_map=teacher_subject_map,
                    ref_today=ref_today,
                )
                msg_entry = {
                    "id": f"msg-{m.get('id')}",
                    "message_id": m.get("id"),
                    "sent_at": m.get("sent_at"),
                    "sender": m.get("correspondent"),
                    "subject": subj_str,
                    "body": body_str,
                    "has_attachment": m.get("has_attachment"),
                    "attachments": m.get("attachments") or [],
                    "codes": codes,
                    "links": links,
                    "unread": m.get("unread"),
                    "account_source": m.get("account_source") or "rodzic",
                    **synth,
                }
                extracted_materials.append(msg_entry)
                action_items.append(msg_entry)

        # 4. School announcements
        announcements_list: list[dict[str, Any]] = []
        if not isinstance(ann_res, BaseException):
            for idx, ann in enumerate(ann_res.items[:25]):
                pub_str = ann.published_on.isoformat() if ann.published_on else str(ann.date_text or "")
                synth = synthesize_action_item(
                    kind="announcement",
                    title=ann.title,
                    body=ann.content,
                    sender=ann.author,
                    published_on=pub_str,
                    teacher_subject_map=teacher_subject_map,
                    ref_today=ref_today,
                )
                ann_entry = {
                    "id": f"ann-{idx}-{pub_str[:10]}",
                    "title": ann.title,
                    "author": ann.author,
                    "published_on": pub_str,
                    "content": ann.content,
                    **synth,
                }
                announcements_list.append(ann_entry)
                action_items.append(ann_entry)

        free_days_list: list[dict[str, Any]] = []
        if not isinstance(free_res, BaseException):
            for fd in free_res.items:
                if fd.date_to >= ref_today:
                    free_days_list.append(
                        {
                            "id": fd.identifier,
                            "date_from": fd.date_from.isoformat(),
                            "date_to": fd.date_to.isoformat(),
                            "lesson_from": fd.lesson_no_from,
                            "lesson_to": fd.lesson_no_to,
                        }
                    )

        # Deduplicate identical title + summary across parent/student inbox or message/announcement
        deduped_items: list[dict[str, Any]] = []
        seen_action_keys: set[str] = set()
        for item in action_items:
            t_norm = re.sub(r"[\s–—\-:;,\.]+", " ", str(item.get("title") or "").lower()).strip()
            s_norm = re.sub(r"[\s–—\-:;,\.]+", " ", str(item.get("action_summary") or "")[:90].lower()).strip()
            d_key = f"{t_norm}|{s_norm}"
            if d_key in seen_action_keys:
                continue
            seen_action_keys.add(d_key)
            deduped_items.append(item)
        action_items = deduped_items

        # Sort unified action_items: cluster first (1=action_required, 2=materials_and_changes, 3=optional_competition, 4=organizational_info),
        # then priority within cluster, then active deadlines ascending, then newest published date first
        prio_rank = {
            "urgent": 0,
            "important": 1,
            "schedule_change": 2,
            "study_material": 3,
            "confirmation": 4,
            "optional": 5,
            "info": 6,
            "archived": 7,
        }

        def _date_sort_key(x: dict[str, Any]) -> tuple[int, int]:
            raw_d = str(x.get("display_date") or x.get("published_on") or "")[:10]
            ord_val = 0
            if len(raw_d) == 10:
                try:
                    ord_val = date.fromisoformat(raw_d).toordinal()
                except ValueError:
                    ord_val = 0
            if x.get("is_deadline") and not x.get("is_expired"):
                # Active deadlines: earliest deadline first
                return (0, ord_val or 9999999)
            # Non-deadline or expired items: newest publication date first
            return (1, -ord_val)

        action_items.sort(
            key=lambda x: (
                int(x.get("cluster_order") or 4),
                prio_rank.get(str(x.get("priority")), 8),
                _date_sort_key(x),
            )
        )

        result = {
            "alias": alias,
            "action_items": action_items,
            "announcements": announcements_list,
            "homework": homework_list,
            "important_messages": extracted_materials,
            "class_free_days": free_days_list,
        }
        entry.cache[cache_key] = (now, result)
        return result

    async def get_grades_and_attendance(
        self,
        vault: UnsealedVaultPayload,
        alias: str,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        """Fetch recent grades, subject averages, attendance frequency, and absences."""
        entry = await self._acquire_entry(vault)
        cache_key = f"grades_att:{alias}"
        now = time.monotonic()
        if not force and cache_key in entry.cache:
            ts, val = entry.cache[cache_key]
            if (now - ts) < DATA_CACHE_TTL_SECONDS:
                return val

        client = entry.service.account(alias)
        grades_task = client.grades_window(view=GradeView.ALL, max_age_seconds=180.0)
        freq_task = client.attendance_frequency(max_age_seconds=300.0)
        subj_freq_task = client.subject_frequency(max_age_seconds=300.0)
        att_task = client.attendance_window(view=AttendanceView.ALL, max_age_seconds=180.0)

        grades_res, freq_res, subj_freq_res, att_res = await asyncio.gather(
            grades_task, freq_task, subj_freq_task, att_task, return_exceptions=True
        )

        recent_grades: list[dict[str, Any]] = []
        by_subject: dict[str, dict[str, Any]] = {}

        if not isinstance(grades_res, BaseException):
            for g in grades_res.numeric:
                meta = {str(k): str(v) for k, v in (g.metadata or ())}
                val_num = self._parse_grade_value(g.raw)
                weight = g.weight or 1
                item = {
                    "kind": "numeric",
                    "subject": g.subject,
                    "grade": g.raw,
                    "value": val_num,
                    "weight": weight,
                    "category": g.category or meta.get("Kategoria") or "",
                    "day": g.day.isoformat() if g.day else "",
                    "teacher": g.teacher or meta.get("Nauczyciel") or "",
                    "comment": g.comment or meta.get("Komentarz") or "",
                    "counts_toward_average": g.counts_toward_average,
                    "semester": g.semester,
                }
                recent_grades.append(item)
                subj_bucket = by_subject.setdefault(
                    g.subject,
                    {"subject": g.subject, "grades": [], "weighted_sum": 0.0, "weight_total": 0},
                )
                subj_bucket["grades"].append(item)
                if val_num is not None and g.counts_toward_average and weight > 0:
                    subj_bucket["weighted_sum"] += val_num * weight
                    subj_bucket["weight_total"] += weight

            for dg in grades_res.descriptive:
                item = {
                    "kind": "descriptive",
                    "subject": dg.subject,
                    "grade": dg.raw,
                    "value": None,
                    "weight": None,
                    "category": "Opisowa",
                    "day": dg.day.isoformat() if dg.day else "",
                    "teacher": dg.teacher or "",
                    "comment": dg.comment or "",
                    "counts_toward_average": False,
                    "semester": dg.semester,
                }
                recent_grades.append(item)
                by_subject.setdefault(
                    dg.subject,
                    {"subject": dg.subject, "grades": [], "weighted_sum": 0.0, "weight_total": 0},
                )["grades"].append(item)
        else:
            fb_grades, fb_subjects = await self._fetch_grades_html_fallback(client)
            recent_grades.extend(fb_grades)
            by_subject.update(fb_subjects)

        recent_grades.sort(key=lambda x: x["day"], reverse=True)

        # Mark the most recent grades so the main subject table can visually emphasize them
        today_iso = date.today().isoformat()
        cutoff_14d = (date.today() - timedelta(days=14)).isoformat()
        recent_cutoff = cutoff_14d
        if recent_grades and recent_grades[0].get("day"):
            try:
                latest_d = date.fromisoformat(recent_grades[0]["day"][:10])
                recent_cutoff = min(cutoff_14d, (latest_d - timedelta(days=6)).isoformat())
            except ValueError:
                pass
        for idx, g_item in enumerate(recent_grades):
            g_day = str(g_item.get("day") or "")[:10]
            g_item["is_recent"] = bool(
                (g_day and g_day >= recent_cutoff) or idx < 6
            )
            g_item["is_today"] = bool(g_day == today_iso)

        subjects_summary: list[dict[str, Any]] = []
        for s_name, bucket in sorted(by_subject.items()):
            avg = bucket.get("explicit_average")
            if avg is None and bucket["weight_total"] > 0:
                avg = round(bucket["weighted_sum"] / bucket["weight_total"], 2)
            has_recent = any(bool(g.get("is_recent")) for g in bucket["grades"])
            subjects_summary.append(
                {
                    "subject": s_name,
                    "average": avg,
                    "has_recent": has_recent,
                    "grades": bucket["grades"],
                }
            )

        frequency_summary: dict[str, Any] = {}
        if not isinstance(freq_res, BaseException):
            for key, measure in (
                ("overall", freq_res.overall),
                ("first_semester", freq_res.first_semester),
                ("second_semester", freq_res.second_semester),
            ):
                if measure:
                    frequency_summary[key] = {
                        "attended": measure.attended_count,
                        "total": measure.total_count,
                        "percentage": round(measure.ratio * 100, 1) if measure.ratio is not None else None,
                    }
        else:
            frequency_summary = await self._fetch_frequency_fallback(client)

        subject_frequencies: list[dict[str, Any]] = []
        if not isinstance(subj_freq_res, BaseException):
            for sf in subj_freq_res.items:
                if sf.frequency and sf.frequency.total_count:
                    subject_frequencies.append(
                        {
                            "subject": sf.subject,
                            "attended": sf.frequency.attended_count,
                            "total": sf.frequency.total_count,
                            "percentage": (
                                round(sf.frequency.ratio * 100, 1)
                                if sf.frequency.ratio is not None
                                else None
                            ),
                        }
                    )

        attendance_records: list[dict[str, Any]] = []
        if not isinstance(att_res, BaseException):
            for rec in sorted(att_res.items, key=lambda r: (r.day, r.period or 0), reverse=True)[:50]:
                attendance_records.append(
                    {
                        "id": rec.detail_id,
                        "day": rec.day.isoformat(),
                        "period": rec.period,
                        "symbol": rec.symbol,
                        "type": rec.attendance_type,
                        "subject": rec.subject,
                        "teacher": rec.teacher,
                        "topic": rec.topic,
                        "excursion": rec.excursion,
                    }
                )

        result = {
            "alias": alias,
            "recent_grades": recent_grades[:40],
            "subjects": subjects_summary,
            "frequency": frequency_summary,
            "subject_frequencies": subject_frequencies,
            "recent_attendance": attendance_records,
        }
        entry.cache[cache_key] = (now, result)
        return result

    async def _list_messages_for_account_key(
        self,
        entry: _ServicePoolEntry,
        account_key: str,
        msg_folder: MessageFolder,
        *,
        account_source: str = "rodzic",
        unread_only: bool = False,
        limit: int = 40,
        force: bool = False,
    ) -> list[dict[str, Any]]:
        backend = entry.backends.get(account_key, MessagingBackend.MODERN)
        client = entry.service.account(account_key)
        items_out: list[dict[str, Any]] = []
        if backend is MessagingBackend.MODERN:
            try:
                res = await client.modern_messages(
                    msg_folder,
                    limit=limit,
                    max_pages=2,
                    unread_only=(unread_only if msg_folder is MessageFolder.RECEIVED else False),
                    max_age_seconds=0.0 if force else 60.0,
                )
            except Exception:
                if getattr(client, "_identity", None) is None or not self._reset_client_session(client, full=True):
                    raise
                with contextlib.suppress(Exception):
                    await client.student_information(max_age_seconds=0.0)
                res = await client.modern_messages(
                    msg_folder,
                    limit=limit,
                    max_pages=2,
                    unread_only=(unread_only if msg_folder is MessageFolder.RECEIVED else False),
                    max_age_seconds=0.0,
                )
            for item in res.items:
                items_out.append(
                    {
                        "id": item.reference.identifier,
                        "folder": item.reference.folder.value,
                        "archived": item.reference.archived,
                        "backend": "modern",
                        "account_source": account_source,
                        "correspondent": item.correspondent,
                        "subject": item.subject,
                        "sent_at": self._format_sent_at(getattr(item, "raw_sent_at", None), item.sent_at),
                        "unread": bool(item.unread),
                        "has_attachment": bool(item.has_attachment),
                    }
                )
        else:
            res_leg = await client.messages(
                msg_folder,
                limit=limit,
                max_pages=2,
                max_age_seconds=0.0 if force else 60.0,
            )
            for item in res_leg.items:
                items_out.append(
                    {
                        "id": item.reference.identifier,
                        "folder": item.reference.folder.value,
                        "archived": False,
                        "backend": "legacy",
                        "account_source": account_source,
                        "correspondent": item.correspondent,
                        "subject": item.subject,
                        "sent_at": item.timestamp.raw,
                        "unread": bool(item.unread),
                        "has_attachment": bool(item.has_attachment),
                    }
                )
        return items_out

    async def _fetch_behaviour_notes(
        self, entry: _ServicePoolEntry, alias: str, *, force: bool = False
    ) -> list[dict[str, str]]:
        """Fetch student remarks (/uwagi) so 'Dodano uwagę dla ucznia' messages show the actual remark."""
        cache_key = f"behaviour_notes:{alias}"
        now = time.monotonic()
        if not force and cache_key in entry.cache:
            ts, cached_notes = entry.cache[cache_key]
            if (now - ts) < 300.0:
                return cached_notes

        notes: list[dict[str, str]] = []
        try:
            from lxml import html as lhtml
            from librus_python_api.service import RequestBudget

            client = entry.service.account(alias)
            await client.student_information(max_age_seconds=300.0)
            resp = await client._transport.request("behaviour_notes_probe", RequestBudget())
            doc = lhtml.fromstring(resp.body.decode("utf-8", errors="replace"))
            for table in doc.xpath('//table[contains(@class, "decorated")]'):
                for tr in table.xpath("./tbody/tr|./tr"):
                    tds = [re.sub(r"\s+", " ", " ".join(td.itertext())).strip() for td in tr.findall("td")]
                    if len(tds) >= 4 and re.match(r"^\d{4}-\d{2}-\d{2}$", tds[1]):
                        notes.append(
                            {
                                "text": tds[0],
                                "day": tds[1],
                                "teacher": tds[2],
                                "kind": tds[3],
                                "category": tds[4] if len(tds) > 4 else "",
                            }
                        )
        except Exception as exc:
            logger.debug("Could not fetch behaviour notes for %s: %s", alias, exc)

        entry.cache[cache_key] = (now, notes)
        return notes

    @staticmethod
    def _match_behaviour_note(
        notes: list[dict[str, str]],
        subject: str,
        body: str,
        sent_at: str,
    ) -> dict[str, str] | None:
        subj_low = (subject or "").lower()
        if "dodano uwagę" not in subj_low and "uwaga dla ucznia" not in subj_low:
            return None
        if not notes:
            return None

        target_day = str(sent_at or "")[:10]
        teacher_tokens: set[str] = set()
        if body:
            m_date = re.search(r"w dniu\s+(\d{2})/(\d{2})/(\d{4})", body)
            if m_date:
                target_day = f"{m_date.group(3)}-{m_date.group(2)}-{m_date.group(1)}"
            m_teacher = re.search(r"nauczyciel\s+([^\n\r]+?)\s+dodał", body)
            if m_teacher:
                teacher_tokens = normalize_teacher_tokens(m_teacher.group(1))

        candidates = [n for n in notes if n.get("day") == target_day]
        if teacher_tokens and candidates:
            for c in candidates:
                if teacher_tokens & normalize_teacher_tokens(c.get("teacher")):
                    return c
        if candidates:
            return candidates[0]
        return None

    async def get_messages_list(
        self,
        vault: UnsealedVaultPayload,
        alias: str,
        *,
        folder: str = "received",
        unread_only: bool = False,
        limit: int = 40,
        force: bool = False,
    ) -> dict[str, Any]:
        """List messages from received or sent folder (merging child account if configured)."""
        entry = await self._acquire_entry(vault)
        msg_folder = MessageFolder.SENT if folder == "sent" else MessageFolder.RECEIVED
        backend = entry.backends.get(alias, MessagingBackend.MODERN)

        primary_source = "uczeń" if self._is_student_only_account(vault, alias) else "rodzic"
        tasks: list[Any] = [
            self._list_messages_for_account_key(
                entry,
                alias,
                msg_folder,
                account_source=primary_source,
                unread_only=unread_only,
                limit=limit,
                force=force,
            )
        ]
        student_key = f"{alias}__student"
        if self._has_student_account(vault, alias) and student_key in entry.backends:
            tasks.append(
                self._list_messages_for_account_key(
                    entry,
                    student_key,
                    msg_folder,
                    account_source="uczeń",
                    unread_only=unread_only,
                    limit=limit,
                    force=force,
                )
            )
        notes_task = self._fetch_behaviour_notes(entry, alias, force=force)

        gathered = await asyncio.gather(*tasks, notes_task, return_exceptions=True)
        results = gathered[:-1]
        notes_res = gathered[-1]
        behaviour_notes = notes_res if isinstance(notes_res, list) else []

        items_out: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        for res in results:
            if isinstance(res, BaseException):
                logger.warning("Failed to list messages for %s: %s", alias, res)
                continue
            for m in res:
                dedup_k = f"{str(m.get('correspondent') or '').lower()}|{str(m.get('subject') or '').lower()}|{str(m.get('sent_at') or '')[:16]}"
                if dedup_k in seen_keys:
                    continue
                seen_keys.add(dedup_k)
                matched_note = self._match_behaviour_note(
                    behaviour_notes,
                    str(m.get("subject") or ""),
                    "",
                    str(m.get("sent_at") or ""),
                )
                if matched_note:
                    m["behaviour_note"] = matched_note
                items_out.append(m)

        items_out.sort(key=lambda x: str(x.get("sent_at") or ""), reverse=True)
        return {
            "alias": alias,
            "folder": msg_folder.value,
            "backend": backend.value,
            "has_student_account": self._has_student_account(vault, alias),
            "unread_count": sum(1 for m in items_out if m["unread"]),
            "items": items_out[: limit * 2],
        }

    async def _read_message_detail_for_key(
        self,
        entry: _ServicePoolEntry,
        alias: str,
        target_key: str,
        account_source: str,
        message_id: str,
        msg_folder: MessageFolder,
        *,
        archived: bool = False,
        mark_read: bool = False,
    ) -> dict[str, Any]:
        cache_key = f"msg_detail:{target_key}:{msg_folder.value}:{message_id}"
        now = time.monotonic()
        if cache_key in entry.cache:
            ts, cached_val = entry.cache[cache_key]
            if (now - ts) < 600.0 and not cached_val.get("protected_unread"):
                subj_cached = str(cached_val.get("subject") or "").lower()
                if ("dodano uwagę" in subj_cached or "uwaga dla ucznia" in subj_cached) and "behaviour_note" not in cached_val:
                    notes = await self._fetch_behaviour_notes(entry, alias)
                    matched_note = self._match_behaviour_note(
                        notes,
                        str(cached_val.get("subject") or ""),
                        str(cached_val.get("body") or ""),
                        str(cached_val.get("sent_at") or ""),
                    )
                    if matched_note:
                        cached_val["behaviour_note"] = matched_note
                return cached_val

        backend = entry.backends.get(target_key, MessagingBackend.MODERN)
        client = entry.service.account(target_key)

        if backend is MessagingBackend.MODERN:
            if msg_folder is MessageFolder.RECEIVED and not mark_read:
                with contextlib.suppress(Exception):
                    list_res = await client.modern_messages(
                        MessageFolder.RECEIVED,
                        limit=50,
                        max_pages=2,
                        archived=bool(archived),
                        max_age_seconds=60.0,
                    )
                    matched = next(
                        (it for it in list_res.items if str(it.reference.identifier) == str(message_id)),
                        None,
                    )
                    if matched is not None and bool(matched.unread):
                        return {
                            "id": str(message_id),
                            "folder": msg_folder.value,
                            "archived": bool(matched.reference.archived),
                            "backend": "modern",
                            "account_source": account_source,
                            "correspondent": matched.correspondent,
                            "subject": matched.subject,
                            "sent_at": self._format_sent_at(getattr(matched, "raw_sent_at", None), matched.sent_at),
                            "unread": True,
                            "protected_unread": True,
                            "has_attachment": bool(matched.has_attachment),
                            "body": "",
                            "attachments": [],
                            "codes": _CODE_RE.findall(matched.subject or ""),
                            "links": _URL_RE.findall(matched.subject or ""),
                        }

            ref = ModernMessageReference(
                folder=msg_folder,
                identifier=str(message_id),
                account=target_key,
                archived=bool(archived),
            )
            try:
                det = await client.modern_message_content(
                    ref, allow_mark_read=True, max_age_seconds=180.0
                )
            except Exception:
                if getattr(client, "_identity", None) is None or not self._reset_client_session(client, full=True):
                    raise
                with contextlib.suppress(Exception):
                    await client.student_information(max_age_seconds=0.0)
                det = await client.modern_message_content(
                    ref, allow_mark_read=True, max_age_seconds=0.0
                )
            attachments = [
                {
                    "id": att.reference.identifier,
                    "filename": att.filename,
                    "archived": att.reference.archived,
                    "message_id": str(message_id),
                    "folder": msg_folder.value,
                    "account_source": account_source,
                }
                for att in det.attachments
            ]
            body_text = (det.text or "").strip()
            orig_text = (getattr(det, "original_text", None) or "").strip()
            if orig_text and orig_text not in body_text:
                body_text = f"{body_text}\n\n--- Cytowana wiadomość ---\n{orig_text}".strip()
            sent_at_fmt = self._format_sent_at(getattr(det.summary, "raw_sent_at", None), det.summary.sent_at)
            result_mod: dict[str, Any] = {
                "id": str(message_id),
                "folder": msg_folder.value,
                "archived": det.archived,
                "backend": "modern",
                "account_source": account_source,
                "correspondent": det.summary.correspondent,
                "subject": det.summary.subject,
                "sent_at": sent_at_fmt,
                "unread": det.summary.unread,
                "protected_unread": False,
                "body": body_text,
                "attachments": attachments,
                "codes": _CODE_RE.findall(f"{det.summary.subject} {body_text}"),
                "links": _URL_RE.findall(f"{det.summary.subject} {body_text}"),
            }
            if "dodano uwagę" in (det.summary.subject or "").lower() or "uwaga dla ucznia" in (det.summary.subject or "").lower():
                notes = await self._fetch_behaviour_notes(entry, alias)
                matched_note = self._match_behaviour_note(notes, det.summary.subject or "", body_text, sent_at_fmt)
                if matched_note:
                    result_mod["behaviour_note"] = matched_note
            entry.cache[cache_key] = (now, result_mod)
            return result_mod

        if msg_folder is MessageFolder.RECEIVED and not mark_read:
            with contextlib.suppress(Exception):
                list_leg = await client.messages(
                    MessageFolder.RECEIVED, limit=50, max_pages=2, max_age_seconds=60.0
                )
                matched_leg = next(
                    (it for it in list_leg.items if str(it.reference.identifier) == str(message_id)),
                    None,
                )
                if matched_leg is not None and bool(matched_leg.unread):
                    return {
                        "id": str(message_id),
                        "folder": msg_folder.value,
                        "archived": False,
                        "backend": "legacy",
                        "account_source": account_source,
                        "correspondent": matched_leg.correspondent,
                        "subject": matched_leg.subject,
                        "sent_at": matched_leg.timestamp.raw,
                        "unread": True,
                        "protected_unread": True,
                        "has_attachment": bool(matched_leg.has_attachment),
                        "body": "",
                        "attachments": [],
                        "codes": _CODE_RE.findall(matched_leg.subject or ""),
                        "links": _URL_RE.findall(matched_leg.subject or ""),
                    }

        ref_leg = MessageReference(
            folder=msg_folder,
            identifier=str(message_id),
            account=target_key,
        )
        det_leg = await client.message_content(
            ref_leg, allow_mark_read=True, max_age_seconds=180.0
        )
        attachments_leg = [
            {
                "id": att.reference.identifier,
                "filename": att.filename,
                "archived": False,
                "message_id": str(message_id),
                "folder": msg_folder.value,
                "account_source": account_source,
            }
            for att in det_leg.content.attachments
        ]
        body_leg = det_leg.content.text or ""
        result_leg: dict[str, Any] = {
            "id": str(message_id),
            "folder": msg_folder.value,
            "archived": False,
            "backend": "legacy",
            "account_source": account_source,
            "correspondent": det_leg.content.correspondent,
            "subject": det_leg.content.subject,
            "sent_at": det_leg.content.timestamp.raw,
            "unread": False,
            "protected_unread": False,
            "body": body_leg,
            "attachments": attachments_leg,
            "codes": _CODE_RE.findall(f"{det_leg.content.subject} {body_leg}"),
            "links": _URL_RE.findall(f"{det_leg.content.subject} {body_leg}"),
        }
        entry.cache[cache_key] = (now, result_leg)
        return result_leg

    async def get_message_detail(
        self,
        vault: UnsealedVaultPayload,
        alias: str,
        message_id: str,
        *,
        folder: str = "received",
        archived: bool = False,
        mark_read: bool = False,
        account_source: str = "rodzic",
    ) -> dict[str, Any]:
        """Read complete message body and attachment references without touching disk.

        Automatically falls back between parent account (`alias`) and student account (`{alias}__student`)
        if the message ID belongs to the other account.
        """
        entry = await self._acquire_entry(vault)
        msg_folder = MessageFolder.SENT if folder == "sent" else MessageFolder.RECEIVED
        student_key = f"{alias}__student"
        has_student = student_key in entry.backends

        primary_source = "uczeń" if self._is_student_only_account(vault, alias) else "rodzic"
        if account_source == "uczeń" and has_student:
            candidates = [(student_key, "uczeń"), (alias, primary_source)]
        elif has_student:
            candidates = [(alias, primary_source), (student_key, "uczeń")]
        else:
            candidates = [(alias, primary_source)]

        last_exc: Exception | None = None
        for t_key, src_label in candidates:
            try:
                return await self._read_message_detail_for_key(
                    entry,
                    alias,
                    t_key,
                    src_label,
                    message_id,
                    msg_folder,
                    archived=archived,
                    mark_read=mark_read,
                )
            except Exception as exc:
                last_exc = exc
                continue
        if last_exc is not None:
            raise last_exc
        raise ValueError(f"Nie udało się odczytać wiadomości {message_id}.")

    async def _download_modern_attachment_bytes(
        self,
        entry: PooledServiceEntry,
        client: Any,
        msg_ref: ModernMessageReference,
        att_ref: ModernMessageAttachmentReference,
        *,
        max_bytes: int = 15 * 1024 * 1024,
    ) -> tuple[bytes, str | None]:
        """Download a modern attachment supporting both direct GetFile and CloudStorage CSTryToDownload flows."""
        cooldowns = getattr(client, "_cooldowns", None)
        if isinstance(cooldowns, dict):
            cooldowns.pop("modern_attachment_download", None)
            cooldowns.pop("attachment_download", None)

        # Ensure modern mailbox session is authenticated and active for this account
        try:
            await client.modern_message_content(msg_ref, allow_mark_read=True, max_age_seconds=60.0)
        except Exception:
            self._reset_client_session(client, full=True)
            with contextlib.suppress(Exception):
                await client.student_information(max_age_seconds=0.0)
            await client.modern_message_content(msg_ref, allow_mark_read=True, max_age_seconds=0.0)

        budget = entry.service._budget()
        budget._bind_loop()
        resp = await client._transport.resolve_modern_attachment(att_ref, budget)
        if resp.status != 200:
            self._reset_client_session(client, full=False)
            await client.modern_message_content(msg_ref, allow_mark_read=True, max_age_seconds=0.0)
            budget = entry.service._budget()
            budget._bind_loop()
            resp = await client._transport.resolve_modern_attachment(att_ref, budget)

        payload = json.loads(resp.body.decode("utf-8", errors="replace"))
        dl_link = str((payload.get("data") or {}).get("downloadLink") or "").strip()
        if not dl_link:
            raise ValueError("Brak linku pobierania załącznika z serwera Librus.")

        parsed_dl = urlsplit(dl_link)
        qs = parse_qs(parsed_dl.query)
        single_use_keys = qs.get("singleUseKey") or []

        if single_use_keys:
            single_use_key = single_use_keys[0]
            base_origin = f"{parsed_dl.scheme}://{parsed_dl.netloc}"
            timeout = aiohttp.ClientTimeout(total=45.0, connect=10.0)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                # 1. Initiate CloudStorage staging on sandbox.librus.pl
                async with sess.get(dl_link) as init_resp:
                    await init_resp.read()

                # 2. Poll CSCheckKey until status == "ready"
                check_url = f"{base_origin}/index.php?action=CSCheckKey"
                is_ready = False
                for attempt in range(18):
                    async with sess.post(check_url, data={"singleUseKey": single_use_key}) as chk_resp:
                        chk_json = await chk_resp.json(content_type=None)
                        status_val = str((chk_json or {}).get("status") or "")
                        if status_val == "ready":
                            is_ready = True
                            break
                        if status_val == "download_failed":
                            raise ValueError("Serwer Librus zgłosił błąd przygotowania załącznika (download_failed).")
                    await asyncio.sleep(0.4 if attempt < 4 else 0.8)

                if not is_ready:
                    raise TimeoutError("Przekroczono czas oczekiwania na przygotowanie załącznika w Librus CloudStorage.")

                # 3. Stream the ready binary from CSDownload
                final_url = f"{base_origin}/index.php?action=CSDownload&singleUseKey={single_use_key}"
                async with sess.get(final_url) as dl_resp:
                    if dl_resp.status != 200:
                        raise ValueError(f"Błąd pobierania pliku z CloudStorage (HTTP {dl_resp.status}).")
                    ctype = dl_resp.headers.get("Content-Type")
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in dl_resp.content.iter_chunked(65536):
                        total += len(chunk)
                        if total > max_bytes:
                            raise ValueError("Załącznik przekracza maksymalny dozwolony rozmiar w pamięci RAM.")
                        chunks.append(chunk)
                    return b"".join(chunks), ctype

        # Fallback for direct GetFile links without singleUseKey
        chunks_direct: list[bytes] = []
        ctype_direct: str | None = None
        async with client.stream_modern_attachment(att_ref, max_bytes=max_bytes) as stream:
            ctype_direct = stream.metadata.headers.content_type
            async for chunk in stream:
                chunks_direct.append(chunk)
        return b"".join(chunks_direct), ctype_direct

    async def download_attachment_in_ram(
        self,
        vault: UnsealedVaultPayload,
        alias: str,
        message_id: str,
        attachment_id: str,
        *,
        filename: str = "zalacznik",
        folder: str = "received",
        archived: bool = False,
        account_source: str = "rodzic",
        max_bytes: int = 15 * 1024 * 1024,
    ) -> dict[str, Any]:
        """Stream an attachment directly into memory (BytesIO) and optionally extract its text."""
        entry = await self._acquire_entry(vault)
        msg_folder = MessageFolder.SENT if folder == "sent" else MessageFolder.RECEIVED
        student_key = f"{alias}__student"
        has_student = student_key in entry.backends

        if account_source == "uczeń" and has_student:
            candidate_keys = [student_key, alias]
        elif has_student:
            candidate_keys = [alias, student_key]
        else:
            candidate_keys = [alias]

        last_exc: Exception | None = None
        for target_key in candidate_keys:
            try:
                backend = entry.backends.get(target_key, MessagingBackend.MODERN)
                client = entry.service.account(target_key)

                if backend is MessagingBackend.MODERN:
                    msg_ref = ModernMessageReference(
                        folder=msg_folder,
                        identifier=str(message_id),
                        account=target_key,
                        archived=bool(archived),
                    )
                    att_ref = ModernMessageAttachmentReference(
                        message=msg_ref,
                        identifier=str(attachment_id),
                        archived=bool(archived),
                    )
                    raw_bytes, content_type = await self._download_modern_attachment_bytes(
                        entry, client, msg_ref, att_ref, max_bytes=max_bytes
                    )
                else:
                    chunks: list[bytes] = []
                    content_type = None
                    msg_ref_leg = MessageReference(
                        folder=msg_folder,
                        identifier=str(message_id),
                        account=target_key,
                    )
                    att_ref_leg = MessageAttachmentReference(
                        message=msg_ref_leg,
                        identifier=str(attachment_id),
                    )
                    async with client.stream_attachment(att_ref_leg, max_bytes=max_bytes) as stream:
                        content_type = stream.metadata.headers.content_type
                        async for chunk in stream:
                            chunks.append(chunk)
                    raw_bytes = b"".join(chunks)

                extracted_text = extract_text_from_bytes(filename, content_type, raw_bytes)
                return {
                    "filename": filename,
                    "content_type": content_type or "application/octet-stream",
                    "size_bytes": len(raw_bytes),
                    "sha256": hashlib.sha256(raw_bytes).hexdigest(),
                    "bytes": raw_bytes,
                    "extracted_text": extracted_text,
                }
            except Exception as exc:
                last_exc = exc
                continue
        if last_exc is not None:
            raise last_exc
        raise ValueError(f"Nie udało się pobrać załącznika {attachment_id}.")

    async def resolve_reply_recipient(
        self,
        vault: UnsealedVaultPayload,
        alias: str,
        message_id: str,
        *,
        account_source: str = "rodzic",
    ) -> dict[str, Any]:
        """Find the directory recipient matching the sender of `message_id` for a safe reply."""
        detail = await self.get_message_detail(
            vault, alias, message_id, folder="received", account_source=account_source
        )
        correspondent = str(detail.get("correspondent") or "").strip()
        orig_subject = str(detail.get("subject") or "").strip()
        reply_subject = (
            orig_subject
            if orig_subject.lower().startswith("re:")
            else f"Re: {orig_subject}"
        )

        sender_tokens = normalize_teacher_tokens(correspondent)
        entry = await self._acquire_entry(vault)
        backend = entry.backends.get(alias, MessagingBackend.MODERN)
        client = entry.service.account(alias)

        matched_recipient: dict[str, Any] | None = None

        if backend is MessagingBackend.MODERN:
            r_types = await client.modern_recipient_types(max_age_seconds=600.0)
            for rt in r_types.items:
                if not rt.lookup_supported:
                    continue
                with contextlib.suppress(Exception):
                    rec_list = await client.modern_recipients(rt.reference, max_age_seconds=600.0)
                    for r in rec_list.items:
                        r_tokens = normalize_teacher_tokens(r.label)
                        if sender_tokens and sender_tokens.issubset(r_tokens):
                            matched_recipient = {
                                "backend": "modern",
                                "label": r.label,
                                "account_id": r.reference.account_id,
                                "user_id": r.reference.user_id,
                                "recipient_type": r.reference.recipient_type,
                                "class_label": r.reference.class_label,
                                "include_virtual": r.reference.include_virtual,
                            }
                            break
                if matched_recipient:
                    break
        else:
            groups = await client.recipient_groups(max_age_seconds=600.0)
            for grp in groups.groups:
                if not grp.available or not grp.lookup_supported:
                    continue
                with contextlib.suppress(Exception):
                    rec_list_leg = await client.recipients(grp.reference, max_age_seconds=600.0)
                    for r in rec_list_leg.items:
                        r_tokens = normalize_teacher_tokens(r.label or "")
                        if sender_tokens and sender_tokens.issubset(r_tokens):
                            matched_recipient = {
                                "backend": "legacy",
                                "label": r.label or correspondent,
                                "identifier": r.reference.identifier,
                                "group_type": r.reference.group_type,
                                "selection_id": r.reference.selection_id,
                            }
                            break
                if matched_recipient:
                    break

        if not matched_recipient:
            raise ValueError(
                f"Nie udało się automatycznie dopasować odbiorcy '{correspondent}' w książce adresowej szkoły."
            )

        return {
            "alias": alias,
            "message_id": message_id,
            "correspondent": correspondent,
            "reply_subject": reply_subject,
            "recipient": matched_recipient,
        }

    async def execute_confirmed_reply(
        self,
        vault: UnsealedVaultPayload,
        confirmation: dict[str, Any],
    ) -> dict[str, Any]:
        """Execute a previously previewed and confirmed reply message."""
        alias = str(confirmation["alias"])
        subject = str(confirmation["subject"])
        body = str(confirmation["body"])
        rec = dict(confirmation["recipient"])

        entry = await self._acquire_entry(vault)
        client = entry.service.account(alias)

        if rec.get("backend") == "modern":
            mod_ref = ModernRecipientReference(
                account_id=str(rec["account_id"]),
                user_id=str(rec["user_id"]),
                account=alias,
                recipient_type=str(rec["recipient_type"]),
                class_label=str(rec["class_label"]),
                include_virtual=bool(rec.get("include_virtual", False)),
            )
            attempt = client.prepare_modern_send(
                recipients=(mod_ref,), subject=subject, body=body
            )
        else:
            leg_ref = RecipientReference(
                identifier=str(rec["identifier"]),
                account=alias,
                group_type=str(rec["group_type"]),
                selection_id=str(rec.get("selection_id", "0")),
            )
            attempt = client.prepare_send(
                recipients=(leg_ref,), subject=subject, body=body
            )

        res = await attempt.execute()
        return {
            "alias": alias,
            "status": res.status.value,
            "recipient_label": rec.get("label"),
            "subject": subject,
        }

    async def _fetch_recent_messages_with_excerpts(
        self,
        entry: PooledServiceEntry,
        alias: str,
        *,
        limit: int = 30,
        account_source: str = "rodzic",
    ) -> list[dict[str, Any]]:
        """Fetch recent received message summaries and open already-read messages for study materials.

        IMPORTANT: Never auto-opens unread messages (`item.unread == True`) in the background,
        so background synchronization never marks a parent's unread message as read in Librus.
        Opens all already-read messages in the batch (`limit=30`) so no card has an empty body.
        """
        backend = entry.backends.get(alias, MessagingBackend.MODERN)
        client = entry.service.account(alias)
        out: list[dict[str, Any]] = []
        sem = asyncio.Semaphore(5)
        now = time.monotonic()

        if backend is MessagingBackend.MODERN:
            try:
                res = await client.modern_messages(
                    MessageFolder.RECEIVED, limit=limit, max_pages=1, max_age_seconds=180.0
                )
            except Exception:
                if getattr(client, "_identity", None) is None or not self._reset_client_session(client, full=True):
                    raise
                with contextlib.suppress(Exception):
                    await client.student_information(max_age_seconds=0.0)
                res = await client.modern_messages(
                    MessageFolder.RECEIVED, limit=limit, max_pages=1, max_age_seconds=0.0
                )
            for item in res.items:
                should_open = not bool(item.unread)
                out.append(
                    {
                        "id": item.reference.identifier,
                        "correspondent": item.correspondent,
                        "subject": item.subject,
                        "sent_at": self._format_sent_at(getattr(item, "raw_sent_at", None), item.sent_at),
                        "unread": bool(item.unread),
                        "has_attachment": bool(item.has_attachment),
                        "account_source": account_source,
                        "body": "",
                        "attachments": [],
                        "_ref": item.reference if should_open else None,
                    }
                )

            async def _open_mod(m: dict[str, Any]) -> None:
                ref = m.pop("_ref", None)
                if ref is None:
                    return
                cache_key = f"msg_detail:{alias}:received:{m['id']}"
                if cache_key in entry.cache:
                    ts, cached_val = entry.cache[cache_key]
                    cached_atts = cached_val.get("attachments") or []
                    needs_att_extract = any(
                        str(ca.get("filename") or "").lower().endswith((".docx", ".pdf", ".txt", ".md", ".csv"))
                        and "extracted_text" not in ca
                        for ca in cached_atts
                    )
                    if (now - ts) < 600.0 and cached_val.get("body") and not needs_att_extract:
                        m["body"] = cached_val["body"]
                        m["attachments"] = cached_atts
                        return
                async with sem:
                    with contextlib.suppress(Exception):
                        det = await client.modern_message_content(
                            ref, allow_mark_read=True, max_age_seconds=600.0
                        )
                        body_text = (det.text or "").strip()
                        orig_text = (getattr(det, "original_text", None) or "").strip()
                        if orig_text and orig_text not in body_text:
                            body_text = f"{body_text}\n\n--- Cytowana wiadomość ---\n{orig_text}".strip()
                        atts: list[dict[str, Any]] = []
                        for a in det.attachments:
                            att_dict: dict[str, Any] = {
                                "id": a.reference.identifier,
                                "filename": a.filename,
                                "archived": a.reference.archived,
                                "message_id": m["id"],
                                "folder": "received",
                                "extracted_text": "",
                            }
                            fname_low = (a.filename or "").lower()
                            if fname_low.endswith((".docx", ".pdf", ".txt", ".md", ".csv")):
                                with contextlib.suppress(Exception):
                                    raw_b, ctype = await self._download_modern_attachment_bytes(
                                        entry, client, ref, a.reference, max_bytes=5 * 1024 * 1024
                                    )
                                    ext_txt = extract_text_from_bytes(a.filename, ctype, raw_b)
                                    if ext_txt and not ext_txt.startswith("[Brak wbudowanego"):
                                        att_dict["extracted_text"] = ext_txt.strip()
                            atts.append(att_dict)
                        m["body"] = body_text
                        m["attachments"] = atts
                        entry.cache[cache_key] = (
                            now,
                            {
                                "id": str(m["id"]),
                                "folder": "received",
                                "archived": det.archived,
                                "backend": "modern",
                                "correspondent": m["correspondent"],
                                "subject": m["subject"],
                                "sent_at": m["sent_at"],
                                "account_source": account_source,
                                "unread": False,
                                "protected_unread": False,
                                "body": body_text,
                                "attachments": atts,
                                "codes": _CODE_RE.findall(f"{m['subject']} {body_text}"),
                                "links": _URL_RE.findall(f"{m['subject']} {body_text}"),
                            },
                        )

            await asyncio.gather(*(_open_mod(m) for m in out))
        else:
            res_leg = await client.messages(
                MessageFolder.RECEIVED, limit=limit, max_pages=1, max_age_seconds=180.0
            )
            for item in res_leg.items:
                should_open_leg = not bool(item.unread)
                out.append(
                    {
                        "id": item.reference.identifier,
                        "correspondent": item.correspondent,
                        "subject": item.subject,
                        "sent_at": item.timestamp.raw,
                        "unread": bool(item.unread),
                        "has_attachment": bool(item.has_attachment),
                        "body": "",
                        "attachments": [],
                        "_ref": item.reference if should_open_leg else None,
                    }
                )

            async def _open_leg(m: dict[str, Any]) -> None:
                ref_l = m.pop("_ref", None)
                if ref_l is None:
                    return
                async with sem:
                    with contextlib.suppress(Exception):
                        det_l = await client.message_content(
                            ref_l, allow_mark_read=True, max_age_seconds=600.0
                        )
                        m["body"] = (det_l.content.text or "").strip()

            await asyncio.gather(*(_open_leg(m) for m in out))
        return out

    @staticmethod
    def _parse_grade_value(raw: str) -> float | None:
        s = (raw or "").strip()
        mapping = {
            "1": 1.0,
            "1+": 1.5,
            "2-": 1.75,
            "2": 2.0,
            "2+": 2.5,
            "3-": 2.75,
            "3": 3.0,
            "3+": 3.5,
            "4-": 3.75,
            "4": 4.0,
            "4+": 4.5,
            "5-": 4.75,
            "5": 5.0,
            "5+": 5.5,
            "6-": 5.75,
            "6": 6.0,
        }
        if "/" in s:
            parts = s.split("/", 1)
            try:
                num = float(parts[0].replace(",", "."))
                den = float(parts[1].replace(",", "."))
                if den > 0:
                    return round((num / den) * 100.0, 1)
            except ValueError:
                pass
        return mapping.get(s)

    async def _fetch_grades_html_fallback(
        self, client: Any
    ) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
        """Fallback HTML parser for point-based ('Oceny punktowe') and multi-line comment grade tables."""
        recent_grades: list[dict[str, Any]] = []
        by_subject: dict[str, dict[str, Any]] = {}
        try:
            from lxml import html as lhtml
            from librus_python_api.service import RequestBudget, grade_view_form

            await client.student_information(max_age_seconds=300.0)
            resp = await client._transport.request(
                "grades", RequestBudget(), form=grade_view_form(GradeView.ALL)
            )
            doc = lhtml.fromstring(resp.body.decode("utf-8", errors="replace"))
            for table in doc.xpath(
                '//table[contains(@class, "decorated") and contains(@class, "stretch")]'
            ):
                if "display: none" in (table.get("style") or "").lower():
                    continue
                for tr in table.xpath("./tbody/tr|./tr"):
                    if tr.xpath(".//table"):
                        continue
                    tds = tr.findall("td")
                    if len(tds) < 3:
                        continue
                    subj = " ".join(tds[1].itertext()).strip()
                    if not subj or subj.lower().startswith(("przedmiot", "zachowanie", "ocena")):
                        continue

                    explicit_avg: float | None = None
                    if len(tds) > 3:
                        raw_avg = (" ".join(tds[3].itertext())).strip().replace(",", ".")
                        with contextlib.suppress(ValueError):
                            explicit_avg = round(float(raw_avg), 2)

                    subj_bucket = by_subject.setdefault(
                        subj,
                        {
                            "subject": subj,
                            "grades": [],
                            "weighted_sum": 0.0,
                            "weight_total": 0,
                            "explicit_average": explicit_avg,
                        },
                    )
                    if explicit_avg is not None and subj_bucket.get("explicit_average") is None:
                        subj_bucket["explicit_average"] = explicit_avg

                    for sem_idx, col_idx in ((1, 2), (2, 5)):
                        if col_idx >= len(tds):
                            continue
                        for span in tds[col_idx].xpath('.//span[contains(@class, "grade-box")]'):
                            a = span.find("a")
                            raw_val = (
                                " ".join((a if a is not None else span).itertext())
                            ).strip()
                            if not raw_val:
                                continue
                            title = (
                                a.get("title") if a is not None else span.get("title")
                            ) or ""
                            meta: dict[str, str] = {}
                            last_k: str | None = None
                            for line in re.split(r"<br\s*/?>|\n", title, flags=re.I):
                                clean_line = re.sub(r"<[^>]+>", " ", line).strip()
                                if not clean_line:
                                    continue
                                if ":" in clean_line:
                                    k, _, v = clean_line.partition(":")
                                    last_k = k.strip()
                                    meta[last_k] = v.strip()
                                elif last_k:
                                    meta[last_k] += " " + clean_line

                            day_str = (meta.get("Data") or "").split()[0]
                            val_num = self._parse_grade_value(raw_val)
                            weight = (
                                int(meta["Waga"])
                                if meta.get("Waga", "").isdigit()
                                else 1
                            )
                            counts = meta.get("Licz do średniej", "tak").lower().startswith("tak")
                            item = {
                                "kind": "numeric",
                                "subject": subj,
                                "grade": raw_val,
                                "value": val_num,
                                "weight": weight,
                                "category": meta.get("Kategoria", ""),
                                "day": day_str,
                                "teacher": meta.get("Nauczyciel", ""),
                                "comment": meta.get("Komentarz", ""),
                                "counts_toward_average": counts,
                                "semester": sem_idx,
                            }
                            recent_grades.append(item)
                            subj_bucket["grades"].append(item)
                            if val_num is not None and counts and weight > 0:
                                subj_bucket["weighted_sum"] += val_num * weight
                                subj_bucket["weight_total"] += weight
        except Exception as exc:
            logger.warning("Fallback grades HTML parser failed: %s", exc)
        return recent_grades, by_subject

    async def _fetch_frequency_fallback(self, client: Any) -> dict[str, Any]:
        """Fallback gateway_attendance parser when upstream JSON uses integer IDs."""
        freq_out: dict[str, Any] = {}
        try:
            from librus_python_api.config import ATTENDANCE_TYPE_KINDS
            from librus_python_api.models import AttendanceKind, GatewayAttendanceRecord
            from librus_python_api.service import RequestBudget, summarize_frequency

            await client.student_information(max_age_seconds=300.0)
            resp = await client._transport.request("gateway_attendance", RequestBudget())
            data = json.loads(resp.body.decode("utf-8", errors="replace"))
            gw_rows: list[GatewayAttendanceRecord] = []
            for r in data.get("Attendances") or []:
                tid_str = str((r.get("Type") or {}).get("Id", ""))
                kind_str = ATTENDANCE_TYPE_KINDS.get(tid_str, "unknown")
                gw_rows.append(
                    GatewayAttendanceRecord(
                        str(r.get("Id")),
                        date.fromisoformat(str(r["Date"])),
                        int(r.get("Semester") or 1),
                        tid_str,
                        AttendanceKind(
                            kind_str
                            if kind_str in AttendanceKind._value2member_map_
                            else "unknown"
                        ),
                        str(r["Lesson"]["Id"])
                        if isinstance(r.get("Lesson"), dict) and r["Lesson"].get("Id") is not None
                        else None,
                        int(r.get("LessonNo") or 0),
                    )
                )
            rows_tuple = tuple(gw_rows)
            sem1 = tuple(r for r in rows_tuple if r.semester == 1)
            sem2 = tuple(r for r in rows_tuple if r.semester == 2)
            for key, subset in (
                ("overall", rows_tuple),
                ("first_semester", sem1),
                ("second_semester", sem2),
            ):
                measure = summarize_frequency(subset, subject_policy=False)
                freq_out[key] = {
                    "attended": measure.attended_count,
                    "total": measure.total_count,
                    "percentage": (
                        round(measure.ratio * 100, 1)
                        if measure.ratio is not None
                        else None
                    ),
                }
        except Exception as exc:
            logger.warning("Fallback gateway_attendance parser failed: %s", exc)
        return freq_out
