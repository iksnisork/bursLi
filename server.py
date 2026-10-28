"""Serverless Multi-Tenant Librus Family Hub + Google Calendar Sync + Nest Hub Caster.

Designed to run both:
1. Locally on macOS/Linux (with automatic `~/.config/librus-mcp/config.json` fallback and Google Nest Hub casting)
2. On Google Cloud Run as a multi-tenant Zero-Database Serverless web application where external users
   keep their credentials encrypted in their browser's IndexedDB (Web Crypto API) and use KMS/AES-GCM
   sealed JWE tokens (`lfh1...`) for stateless background `.ics` feeds and email alerts.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import re
import socket
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

from aiohttp import web
import pychromecast
from pychromecast.controllers.dashcast import APP_DASHCAST, DashCastController

from app.calendar_alerts import (
    build_proactive_alerts,
    dispatch_digest_notifications,
    format_email_digest_html,
    generate_exams_ics,
    generate_timetable_ics,
    sync_events_to_google_calendar_oauth,
)
from app.crypto_tokens import TokenValidationError, TokenVault, UnsealedVaultPayload
from app.librus_client import (
    StatelessLibrusHub,
    load_local_config_vault,
    save_local_student_account,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("librus-family-hub")

DEFAULT_NEST_HUB_IP = os.environ.get("NEST_HUB_IP", "192.168.86.40")
DEFAULT_PORT = int(os.environ.get("PORT", "8088"))
DEFAULT_CONFIG_PATH = Path(
    os.environ.get(
        "LIBRUS_CONFIG",
        str(Path.home() / ".config" / "librus-mcp" / "config.json"),
    )
)
BACKDROP_APP_IDS = {None, "", "E8C28D3C"}
_SAFE_HOST_RE = re.compile(r"^[a-zA-Z0-9.-]+(?::\d{1,5})?$")
_CSP_HEADER = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://www.googletagmanager.com https://www.google-analytics.com; "
    "style-src 'self' 'unsafe-inline'; "
    "font-src 'self' data:; "
    "img-src 'self' data: https://www.google-analytics.com https://www.googletagmanager.com; "
    "connect-src 'self' https://www.google-analytics.com https://*.google-analytics.com "
    "https://*.analytics.google.com https://www.googletagmanager.com; "
    "frame-ancestors 'self'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


def _mask_token_in_url(url: str) -> str:
    return re.sub(r"([?&]token=)[^&]+", r"\1***", url)


def detect_lan_ip(target_ip: str = DEFAULT_NEST_HUB_IP) -> str:
    """Determine the local LAN IP routable to the Nest Hub."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((target_ip, 8009))
        return str(sock.getsockname()[0])
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def current_monday(today: date | None = None) -> date:
    """Return the Monday of the week for the given date (or next Monday on weekends)."""
    d = today or date.today()
    if d.weekday() >= 5:
        return d + timedelta(days=(7 - d.weekday()))
    return d - timedelta(days=d.weekday())


class NestHubCaster:
    """Controls casting the dashboard URL onto a Google Nest Hub using DashCast."""

    def __init__(self, hub_ip: str, dashboard_url: str, keep_alive: bool = False) -> None:
        self.hub_ip = hub_ip
        self.dashboard_url = dashboard_url
        self.keep_alive = keep_alive
        self._lock = asyncio.Lock()
        self.last_status: dict[str, Any] = {
            "hub_ip": hub_ip,
            "dashboard_url": dashboard_url,
            "connected": False,
            "app_id": None,
            "app_name": None,
            "last_cast_at": None,
            "error": None,
        }
        self._monitor_task: asyncio.Task[None] | None = None

    def _cast_blocking(self, force: bool = True) -> dict[str, Any]:
        chromecast = None
        try:
            chromecast = pychromecast.get_chromecast_from_host(
                (self.hub_ip, 8009, None, None, "Nest Hub"),
                tries=2,
                timeout=6.0,
            )
            chromecast.wait(timeout=6.0)
            status = chromecast.status
            current_app_id = getattr(status, "app_id", None)
            if force and current_app_id == "84912283":
                with contextlib.suppress(Exception):
                    chromecast.quit_app()
                    time.sleep(1.2)

            dashcast = DashCastController()
            chromecast.register_handler(dashcast)
            sep = "&" if "?" in self.dashboard_url else "?"
            target_url = f"{self.dashboard_url}{sep}nesthub=1&ts={int(time.time())}"
            dashcast.load_url(target_url, force=True, reload_seconds=0)
            time.sleep(2.5)

            status = chromecast.status
            return {
                "hub_ip": self.hub_ip,
                "dashboard_url": self.dashboard_url,
                "cast_url": target_url,
                "connected": True,
                "app_id": getattr(status, "app_id", None),
                "app_name": getattr(status, "display_name", None),
                "volume_level": round(getattr(status, "volume_level", 0.0) * 100),
                "last_cast_at": datetime.now().isoformat(timespec="seconds"),
                "error": None,
            }
        except Exception as exc:
            logger.warning("Failed to cast to Nest Hub (%s): %s", self.hub_ip, exc)
            return {
                "hub_ip": self.hub_ip,
                "dashboard_url": self.dashboard_url,
                "connected": False,
                "app_id": None,
                "app_name": None,
                "last_cast_at": self.last_status.get("last_cast_at"),
                "error": str(exc),
            }
        finally:
            if chromecast is not None:
                with contextlib.suppress(Exception):
                    chromecast.disconnect(timeout=2.0)

    def _status_blocking(self) -> dict[str, Any]:
        chromecast = None
        try:
            chromecast = pychromecast.get_chromecast_from_host(
                (self.hub_ip, 8009, None, None, "Nest Hub"),
                tries=1,
                timeout=3.0,
            )
            chromecast.wait(timeout=3.0)
            status = chromecast.status
            return {
                "hub_ip": self.hub_ip,
                "dashboard_url": self.dashboard_url,
                "connected": True,
                "app_id": getattr(status, "app_id", None),
                "app_name": getattr(status, "display_name", None),
                "volume_level": round(getattr(status, "volume_level", 0.0) * 100),
                "last_cast_at": self.last_status.get("last_cast_at"),
                "error": None,
            }
        except Exception as exc:
            return {
                "hub_ip": self.hub_ip,
                "dashboard_url": self.dashboard_url,
                "connected": False,
                "app_id": None,
                "app_name": None,
                "last_cast_at": self.last_status.get("last_cast_at"),
                "error": str(exc),
            }
        finally:
            if chromecast is not None:
                with contextlib.suppress(Exception):
                    chromecast.disconnect(timeout=1.5)

    def _stop_blocking(self) -> dict[str, Any]:
        chromecast = None
        try:
            chromecast = pychromecast.get_chromecast_from_host(
                (self.hub_ip, 8009, None, None, "Nest Hub"),
                tries=1,
                timeout=4.0,
            )
            chromecast.wait(timeout=4.0)
            chromecast.quit_app()
            time.sleep(0.8)
            return self._status_blocking()
        except Exception as exc:
            return {
                "hub_ip": self.hub_ip,
                "dashboard_url": self.dashboard_url,
                "connected": False,
                "app_id": None,
                "app_name": None,
                "last_cast_at": self.last_status.get("last_cast_at"),
                "error": str(exc),
            }
        finally:
            if chromecast is not None:
                with contextlib.suppress(Exception):
                    chromecast.disconnect(timeout=1.5)

    async def cast_now(self, force: bool = True) -> dict[str, Any]:
        async with self._lock:
            logger.info("Casting %s to Nest Hub at %s...", _mask_token_in_url(self.dashboard_url), self.hub_ip)
            self.last_status = await asyncio.to_thread(self._cast_blocking, force)
            return self.last_status

    async def check_status(self) -> dict[str, Any]:
        async with self._lock:
            self.last_status = await asyncio.to_thread(self._status_blocking)
            return self.last_status

    async def stop_cast(self) -> dict[str, Any]:
        async with self._lock:
            self.last_status = await asyncio.to_thread(self._stop_blocking)
            return self.last_status

    async def start_keepalive_loop(self) -> None:
        if not self.keep_alive:
            return
        self._monitor_task = asyncio.create_task(self._keepalive_worker())

    async def stop_keepalive_loop(self) -> None:
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._monitor_task
            self._monitor_task = None

    async def _keepalive_worker(self) -> None:
        while True:
            await asyncio.sleep(45)
            try:
                status = await self.check_status()
                if status.get("connected") and status.get("app_id") in BACKDROP_APP_IDS:
                    logger.info("Nest Hub returned to Backdrop; re-casting dashboard...")
                    await self.cast_now(force=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("Keepalive check error: %s", exc)


class DashboardServer:
    """HTTP server for the Librus Family Hub PWA, Calendar Feeds, and Nest Hub Caster."""

    def __init__(
        self,
        hub: StatelessLibrusHub,
        caster: NestHubCaster,
        static_dir: Path,
        token_vault: TokenVault | None = None,
    ) -> None:
        self.hub = hub
        self.caster = caster
        self.static_dir = static_dir
        self.token_vault = token_vault or TokenVault()

    def _base_url(self, request: web.Request) -> str:
        raw_proto = (request.headers.get("X-Forwarded-Proto") or request.scheme or "http").split(",")[0].strip().lower()
        proto = "https" if raw_proto == "https" else "http"
        raw_host = (request.headers.get("X-Forwarded-Host") or request.host or "localhost").split(",")[0].strip()
        host = raw_host if _SAFE_HOST_RE.match(raw_host) else (request.host or "localhost")
        return f"{proto}://{host}"

    def _build_ical_links(
        self,
        base_url: str,
        sealed_token: str,
        aliases: list[str],
        vault: UnsealedVaultPayload | None = None,
    ) -> dict[str, dict[str, Any]]:
        feed_token = (
            self.token_vault.seal_vault(vault.accounts, scope="ical")
            if vault is not None and vault.accounts
            else sealed_token
        )
        links: dict[str, dict[str, Any]] = {}
        is_lan = any(
            h in base_url
            for h in ("127.0.0.1", "localhost", "192.168.", "10.", "172.16.", "172.17.")
        )
        for alias in aliases:
            safe_alias = quote(alias, safe="")
            safe_token = quote(feed_token, safe="")
            tt_http = f"{base_url}/api/ical/{feed_token}/{safe_alias}/timetable.ics"
            ex_http = f"{base_url}/api/ical/{feed_token}/{safe_alias}/exams.ics"
            tt_direct_dl = f"{base_url}/api/ical/download?account={safe_alias}&feed=timetable&token={safe_token}"
            ex_direct_dl = f"{base_url}/api/ical/download?account={safe_alias}&feed=exams&token={safe_token}"
            webcal_tt = tt_http.replace("https://", "webcal://").replace("http://", "webcal://")
            webcal_ex = ex_http.replace("https://", "webcal://").replace("http://", "webcal://")
            links[alias] = {
                "is_lan": is_lan,
                "timetable_ics": tt_http,
                "timetable_download": tt_direct_dl,
                "timetable_webcal": webcal_tt,
                "timetable_gcal_add": f"https://calendar.google.com/calendar/r?cid={quote(webcal_tt, safe='')}",
                "exams_ics": ex_http,
                "exams_download": ex_direct_dl,
                "exams_webcal": webcal_ex,
                "exams_gcal_add": f"https://calendar.google.com/calendar/r?cid={quote(webcal_ex, safe='')}",
                "gcal_import_page": "https://calendar.google.com/calendar/u/0/r/settings/export",
                "gcal_add_by_url_page": "https://calendar.google.com/calendar/u/0/r/settings/addbyurl",
            }
        return links

    def _overlay_local_student_accounts(
        self, payload: UnsealedVaultPayload
    ) -> UnsealedVaultPayload:
        """Overlay any saved child credentials from student_accounts.json onto the resolved vault."""
        if os.environ.get("K_SERVICE"):
            return payload
        sidecar_path = self.hub.local_config_path.parent / "student_accounts.json"
        if not sidecar_path.exists():
            return payload
        try:
            sidecar = json.loads(sidecar_path.read_text(encoding="utf-8")) or {}
        except Exception:
            return payload
        if not sidecar:
            return payload

        from app.crypto_tokens import SealedAccount

        updated: list[SealedAccount] = []
        changed = False
        for acc in payload.accounts:
            side = sidecar.get(acc.alias) or {}
            s_user = str(side.get("student_username") or acc.student_username or "").strip()
            s_pass = str(side.get("student_password") or acc.student_password or "")
            if s_user != acc.student_username or s_pass != acc.student_password:
                changed = True
                updated.append(
                    SealedAccount(
                        alias=acc.alias,
                        username=acc.username,
                        password=acc.password,
                        messaging_backend=acc.messaging_backend,
                        student_username=s_user,
                        student_password=s_pass,
                    )
                )
            else:
                updated.append(acc)
        if not changed:
            return payload
        return UnsealedVaultPayload(
            accounts=tuple(updated),
            alert_email=payload.alert_email,
            whatsapp_webhook=payload.whatsapp_webhook,
            issued_at=payload.issued_at,
            expires_at=payload.expires_at,
            scope=payload.scope,
        )

    def _resolve_vault(
        self,
        request: web.Request,
        explicit_token: str | None = None,
        *,
        allowed_scopes: tuple[str, ...] = ("full", "kiosk"),
    ) -> tuple[UnsealedVaultPayload, str, str]:
        """Resolve (UnsealedVaultPayload, source_mode, active_token) from request or local home config."""
        token = (
            explicit_token
            or request.headers.get("X-Librus-Token", "").strip()
            or request.query.get("token", "").strip()
        )
        if token:
            try:
                payload = self._overlay_local_student_accounts(
                    self.token_vault.unseal_vault(token, allowed_scopes=allowed_scopes)
                )
                return payload, "token", token
            except TokenValidationError:
                # If a stale token was sent on a local home server, fall through to local_home config
                if load_local_config_vault(self.hub.local_config_path) is None:
                    raise

        accounts_header = request.headers.get("X-Librus-Accounts", "").strip()
        if accounts_header:
            try:
                import base64
                from app.crypto_tokens import SealedAccount

                pad = "=" * ((4 - len(accounts_header) % 4) % 4)
                raw_json = base64.urlsafe_b64decode((accounts_header + pad).encode("ascii")).decode("utf-8")
                parsed_list = json.loads(raw_json)
                if isinstance(parsed_list, list) and parsed_list:
                    sealed_accs = tuple(
                        SealedAccount(
                            alias=str(a.get("alias") or "").strip(),
                            username=str(a.get("username") or a.get("login") or "").strip(),
                            password=str(a.get("password") or ""),
                            messaging_backend=str(a.get("messaging_backend") or "modern"),
                            student_username=str(a.get("student_username") or "").strip(),
                            student_password=str(a.get("student_password") or ""),
                        )
                        for a in parsed_list
                        if isinstance(a, dict)
                        and (a.get("alias"))
                        and (a.get("username") or a.get("login"))
                        and a.get("password")
                    )
                    if sealed_accs:
                        vault_payload = self._overlay_local_student_accounts(
                            UnsealedVaultPayload(accounts=sealed_accs)
                        )
                        sealed_tok = self.token_vault.seal_vault(vault_payload.accounts)
                        return vault_payload, "browser_vault", sealed_tok
            except Exception as exc:
                logger.warning("Invalid X-Librus-Accounts header: %s", exc)

        local_vault = load_local_config_vault(self.hub.local_config_path)
        if local_vault is not None:
            local_vault = self._overlay_local_student_accounts(local_vault)
            sealed = self.token_vault.seal_vault(local_vault.accounts)
            return local_vault, "local_home", sealed

        raise TokenValidationError(
            "Brak skonfigurowanych kont Librus. Dodaj konto w zakładce 'Kalendarz i Konta'."
        )

    async def handle_index(self, _request: web.Request) -> web.FileResponse:
        return web.FileResponse(
            self.static_dir / "index.html",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    async def handle_favicon(self, _request: web.Request) -> web.FileResponse:
        return web.FileResponse(self.static_dir / "favicon.ico")

    async def handle_apple_touch_icon(self, _request: web.Request) -> web.FileResponse:
        return web.FileResponse(self.static_dir / "apple-touch-icon.png")

    async def handle_status(self, request: web.Request) -> web.Response:
        local_vault = load_local_config_vault(self.hub.local_config_path)
        base_url = self._base_url(request)

        # Check if request has a valid custom token or local home config
        active_mode = "empty"
        active_aliases: list[str] = []
        ical_links: dict[str, dict[str, str]] = {}
        sealed_token: str | None = None

        try:
            vault, active_mode, sealed_token = self._resolve_vault(request)
            active_aliases = [a.alias for a in vault.accounts]
            ical_links = self._build_ical_links(base_url, sealed_token, active_aliases, vault=vault)
        except TokenValidationError:
            pass

        return web.json_response(
            {
                "server_time": datetime.now().isoformat(timespec="seconds"),
                "today": date.today().isoformat(),
                "default_monday": current_monday().isoformat(),
                "mode": active_mode,
                "local_home_available": local_vault is not None,
                "active_aliases": active_aliases,
                "sealed_token": sealed_token if active_mode == "local_home" else None,
                "ical_links": ical_links,
                "ai": {
                    "configured": self.hub.study_assistant.is_configured(),
                    "model": self.hub.study_assistant.model_name,
                },
                "nest_hub": self.caster.last_status,
            }
        )

    async def handle_vault_seal(self, request: web.Request) -> web.Response:
        """Seal user-supplied Librus accounts into a stateless JWE token and return .ics links."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "Nieprawidłowy format JSON."}, status=400)

        raw_accounts = body.get("accounts") or []
        alert_email = body.get("alert_email") or body.get("email")
        whatsapp_webhook = body.get("whatsapp_webhook")
        verify = bool(body.get("verify", False))

        try:
            if raw_accounts:
                normalized_raw = [
                    {
                        "alias": a.get("alias"),
                        "username": a.get("username") or a.get("login"),
                        "password": a.get("password"),
                        "messaging_backend": a.get("messaging_backend") or "modern",
                        "student_username": a.get("student_username") or "",
                        "student_password": a.get("student_password") or "",
                    }
                    for a in raw_accounts
                    if isinstance(a, dict)
                ]
                token = self.token_vault.seal_vault(
                    normalized_raw,
                    alert_email=alert_email,
                    whatsapp_webhook=whatsapp_webhook,
                )
                vault = self.token_vault.unseal_vault(token)
            else:
                vault, _, token = self._resolve_vault(request)
                if alert_email or whatsapp_webhook:
                    token = self.token_vault.seal_vault(
                        vault.accounts,
                        alert_email=alert_email,
                        whatsapp_webhook=whatsapp_webhook,
                    )
                    vault = self.token_vault.unseal_vault(token)

            profiles = await self.hub.get_profiles(vault, force=True) if verify else []
            aliases = [a.alias for a in vault.accounts]
            base_url = self._base_url(request)
            ical_links = self._build_ical_links(base_url, token, aliases, vault=vault)
            kiosk_token = self.token_vault.seal_vault(
                vault.accounts,
                alert_email=vault.alert_email,
                whatsapp_webhook=vault.whatsapp_webhook,
                scope="kiosk",
            )
            kiosk_url = f"{base_url}/?token={quote(kiosk_token, safe='')}&nesthub=1"
            return web.json_response(
                {
                    "ok": True,
                    "token": token,
                    "sealed_token": token,
                    "kiosk_url": kiosk_url,
                    "profiles": profiles,
                    "ical_links": ical_links,
                    "feeds": ical_links,
                    "expires_at": vault.expires_at,
                }
            )
        except Exception as exc:
            logger.warning("Vault seal error: %s", exc)
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def handle_student_account(self, request: web.Request) -> web.Response:
        """Pair or remove a child's Librus account (student_username/student_password) for a profile."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "Nieprawidłowy JSON."}, status=400)

        alias = str(body.get("alias") or body.get("account") or "").strip()
        student_username = str(body.get("student_username") or "").strip()
        student_password = str(body.get("student_password") or "")
        parent_username = str(body.get("parent_username") or "").strip()
        parent_password = str(body.get("parent_password") or "")
        verify_now = bool(body.get("verify", True))
        verify_only = bool(body.get("verify_only", False))

        if not alias and not verify_only:
            with contextlib.suppress(Exception):
                vault_fallback, _, _ = self._resolve_vault(request)
                if vault_fallback.accounts:
                    alias = vault_fallback.accounts[0].alias

        if not alias and not verify_only:
            return web.json_response({"ok": False, "error": "Brak aliasu profilu."}, status=400)

        try:
            student_info: dict[str, Any] = {}
            if parent_username and parent_password and verify_now:
                student_info = await self.hub.verify_student_account(
                    parent_username, parent_password
                )
            if student_username and student_password and verify_now:
                child_info = await self.hub.verify_student_account(
                    student_username, student_password
                )
                if not student_info:
                    student_info = child_info

            if verify_only:
                return web.json_response(
                    {
                        "ok": True,
                        "verified": True,
                        "alias": alias,
                        "student_info": student_info,
                    }
                )

            if (
                not os.environ.get("K_SERVICE")
                and self.hub.local_config_path.exists()
                and not (parent_username and parent_password)
            ):
                save_local_student_account(
                    self.hub.local_config_path,
                    alias,
                    student_username,
                    student_password,
                )
            vault, _, token = self._resolve_vault(request)
            profiles = await self.hub.get_profiles(vault, force=True)
            base_url = self._base_url(request)
            ical_links = self._build_ical_links(
                base_url, token, [a.alias for a in vault.accounts], vault=vault
            )
            return web.json_response(
                {
                    "ok": True,
                    "alias": alias,
                    "has_student_account": bool(student_username and student_password),
                    "student_username": student_username,
                    "student_info": student_info,
                    "profiles": profiles,
                    "ical_links": ical_links,
                    "token": token,
                }
            )
        except Exception as exc:
            logger.warning("Failed to save student account for %s: %s", alias, exc)
            captcha_req = bool(getattr(exc, "captcha_required", False))
            return web.json_response(
                {
                    "ok": False,
                    "captcha_required": captcha_req,
                    "username": getattr(exc, "username", ""),
                    "error": str(exc),
                },
                status=400,
            )

    async def handle_profiles(self, request: web.Request) -> web.Response:
        force = request.query.get("refresh") == "1"
        try:
            vault, mode, token = self._resolve_vault(request)
            profiles = await self.hub.get_profiles(vault, force=force)
            base_url = self._base_url(request)
            ical_links = self._build_ical_links(
                base_url, token, [a.alias for a in vault.accounts], vault=vault
            )
            kiosk_token = self.token_vault.seal_vault(
                vault.accounts,
                alert_email=vault.alert_email,
                whatsapp_webhook=vault.whatsapp_webhook,
                scope="kiosk",
            )
            kiosk_url = f"{base_url}/?token={quote(kiosk_token, safe='')}&nesthub=1"
            return web.json_response(
                {
                    "profiles": profiles,
                    "mode": mode,
                    "sealed_token": token,
                    "kiosk_url": kiosk_url,
                    "ical_links": ical_links,
                    "default_monday": current_monday().isoformat(),
                    "today": date.today().isoformat(),
                }
            )
        except TokenValidationError as exc:
            return web.json_response(
                {"error": str(exc), "code": "VAULT_EMPTY", "profiles": []}, status=401
            )
        except Exception as exc:
            logger.exception("Error fetching profiles")
            return web.json_response({"error": str(exc)}, status=502)

    async def handle_bootstrap(self, request: web.Request) -> web.Response:
        """Backwards-compatible bootstrap endpoint combining profiles and timetable for Nest Hub."""
        try:
            vault, mode, _ = self._resolve_vault(request)
            profiles = await self.hub.get_profiles(vault)
            alias = request.query.get("account", "").strip() or vault.accounts[0].alias
            monday_str = request.query.get("monday", "").strip()
            if monday_str:
                d = date.fromisoformat(monday_str)
                monday = d - timedelta(days=d.weekday())
            else:
                monday = current_monday()
            timetable = await self.hub.get_timetable(vault, alias, monday)
            return web.json_response(
                {
                    "profiles": profiles,
                    "mode": mode,
                    "account": alias,
                    "today": date.today().isoformat(),
                    "FetchedAt": datetime.now().isoformat(timespec="seconds"),
                    **timetable,
                }
            )
        except Exception as exc:
            logger.exception("Error in /api/bootstrap")
            return web.json_response({"error": str(exc)}, status=502)

    async def handle_timetable(self, request: web.Request) -> web.Response:
        try:
            vault, _, _ = self._resolve_vault(request)
        except TokenValidationError as exc:
            return web.json_response({"error": str(exc), "code": "VAULT_EMPTY"}, status=401)

        alias = request.query.get("account", "").strip() or vault.accounts[0].alias
        monday_str = (
            request.query.get("monday", "").strip()
            or request.query.get("week_start", "").strip()
        )
        force = request.query.get("refresh") == "1"

        if monday_str:
            try:
                d = date.fromisoformat(monday_str)
                monday = d - timedelta(days=d.weekday())
            except ValueError:
                return web.json_response(
                    {"error": f"Invalid monday date: {monday_str}"}, status=400
                )
        else:
            monday = current_monday()

        try:
            timetable = await self.hub.get_timetable(vault, alias, monday, force=force)
            return web.json_response(
                {
                    "account": alias,
                    "today": date.today().isoformat(),
                    "FetchedAt": datetime.now().isoformat(timespec="seconds"),
                    **timetable,
                }
            )
        except Exception as exc:
            logger.exception("Error fetching timetable for %s (%s)", alias, monday)
            return web.json_response({"error": str(exc)}, status=502)

    async def handle_exams(self, request: web.Request) -> web.Response:
        try:
            vault, _, _ = self._resolve_vault(request)
        except TokenValidationError as exc:
            return web.json_response({"error": str(exc), "code": "VAULT_EMPTY"}, status=401)

        alias = request.query.get("account", "").strip() or vault.accounts[0].alias
        raw_days = request.query.get("days") or request.query.get("days_ahead") or "14"
        days_ahead = min(max(int(raw_days), 1), 60)
        use_ai = (
            request.query.get("ai", "0") == "1"
            and os.environ.get("ALLOW_SERVER_AI", "0") == "1"
        )
        force = request.query.get("refresh") == "1"

        try:
            data = await self.hub.get_exams_and_study_cards(
                vault,
                alias,
                days_ahead=days_ahead,
                use_ai=use_ai,
                force=force,
            )
            return web.json_response(data)
        except Exception as exc:
            logger.exception("Error fetching exams for %s", alias)
            return web.json_response({"error": str(exc)}, status=502)

    async def handle_materials(self, request: web.Request) -> web.Response:
        try:
            vault, _, _ = self._resolve_vault(request)
        except TokenValidationError as exc:
            return web.json_response({"error": str(exc), "code": "VAULT_EMPTY"}, status=401)

        alias = request.query.get("account", "").strip() or vault.accounts[0].alias
        force = request.query.get("refresh") == "1"
        try:
            data = await self.hub.get_materials_and_announcements(
                vault, alias, force=force
            )
            return web.json_response(data)
        except Exception as exc:
            logger.exception("Error fetching materials for %s", alias)
            return web.json_response({"error": str(exc)}, status=502)

    async def handle_grades_attendance(self, request: web.Request) -> web.Response:
        try:
            vault, _, _ = self._resolve_vault(request)
        except TokenValidationError as exc:
            return web.json_response({"error": str(exc), "code": "VAULT_EMPTY"}, status=401)

        alias = request.query.get("account", "").strip() or vault.accounts[0].alias
        force = request.query.get("refresh") == "1"
        try:
            data = await self.hub.get_grades_and_attendance(vault, alias, force=force)
            return web.json_response(data)
        except Exception as exc:
            logger.exception("Error fetching grades/attendance for %s", alias)
            return web.json_response({"error": str(exc)}, status=502)

    async def handle_messages(self, request: web.Request) -> web.Response:
        try:
            vault, _, _ = self._resolve_vault(request)
        except TokenValidationError as exc:
            return web.json_response({"error": str(exc), "code": "VAULT_EMPTY"}, status=401)

        alias = request.query.get("account", "").strip() or vault.accounts[0].alias
        folder = request.query.get("folder", "received").strip()
        unread_only = request.query.get("unread_only") == "1"
        force = request.query.get("refresh") == "1"
        try:
            data = await self.hub.get_messages_list(
                vault,
                alias,
                folder=folder,
                unread_only=unread_only,
                force=force,
            )
            return web.json_response(data)
        except Exception as exc:
            logger.exception("Error listing messages for %s", alias)
            return web.json_response({"error": str(exc)}, status=502)

    async def handle_message_detail(self, request: web.Request) -> web.Response:
        try:
            vault, _, _ = self._resolve_vault(request)
        except TokenValidationError as exc:
            return web.json_response({"error": str(exc), "code": "VAULT_EMPTY"}, status=401)

        alias = request.query.get("account", "").strip() or vault.accounts[0].alias
        msg_id = (
            request.query.get("id", "").strip()
            or request.query.get("message_id", "").strip()
        )
        folder = request.query.get("folder", "received").strip()
        archived = request.query.get("archived") == "1"
        mark_read = request.query.get("mark_read") == "1"
        account_source = request.query.get("account_source", "rodzic").strip()
        if not msg_id:
            return web.json_response({"error": "Brak parametru id wiadomości."}, status=400)

        try:
            detail = await self.hub.get_message_detail(
                vault,
                alias,
                msg_id,
                folder=folder,
                archived=archived,
                mark_read=mark_read,
                account_source=account_source,
            )
            return web.json_response(detail)
        except Exception as exc:
            logger.exception("Error reading message %s for %s", msg_id, alias)
            return web.json_response({"error": str(exc)}, status=502)

    async def handle_message_attachment(self, request: web.Request) -> web.Response:
        try:
            vault, _, _ = self._resolve_vault(request)
        except TokenValidationError as exc:
            return web.json_response({"error": str(exc), "code": "VAULT_EMPTY"}, status=401)

        alias = request.query.get("account", "").strip() or vault.accounts[0].alias
        msg_id = request.query.get("message_id", "").strip()
        att_id = request.query.get("attachment_id", "").strip()
        filename = request.query.get("filename", "zalacznik").strip()
        folder = request.query.get("folder", "received").strip()
        archived = request.query.get("archived") == "1"
        mode = request.query.get("mode", "text").strip()
        account_source = request.query.get("account_source", "rodzic").strip() or "rodzic"

        if not msg_id or not att_id:
            return web.json_response(
                {"error": "Wymagane parametry message_id oraz attachment_id."}, status=400
            )

        try:
            att_data = await self.hub.download_attachment_in_ram(
                vault,
                alias,
                msg_id,
                att_id,
                filename=filename,
                folder=folder,
                archived=archived,
                account_source=account_source,
            )
            if mode == "download":
                ascii_fname = (
                    filename.encode("ascii", "ignore").decode("ascii").replace('"', "").strip()
                    or "zalacznik"
                )
                utf8_fname = quote(filename, safe="")
                return web.Response(
                    body=att_data["bytes"],
                    content_type=att_data["content_type"],
                    headers={
                        "Content-Disposition": (
                            f'attachment; filename="{ascii_fname}"; filename*=UTF-8\'\'{utf8_fname}'
                        ),
                    },
                )

            return web.json_response(
                {
                    "filename": att_data["filename"],
                    "content_type": att_data["content_type"],
                    "size_bytes": att_data["size_bytes"],
                    "sha256": att_data["sha256"],
                    "extracted_text": att_data["extracted_text"],
                }
            )
        except Exception as exc:
            logger.exception("Error streaming attachment %s/%s for %s", msg_id, att_id, alias)
            return web.json_response({"error": str(exc)}, status=502)

    async def handle_reply_preview(self, request: web.Request) -> web.Response:
        """Step 1 of 2: Resolve recipient and generate a 5-minute sealed confirmation token."""
        try:
            vault, _, _ = self._resolve_vault(request, allowed_scopes=("full",))
            body = await request.json()
        except TokenValidationError as exc:
            return web.json_response({"error": str(exc), "code": "VAULT_EMPTY"}, status=401)
        except Exception:
            return web.json_response({"error": "Nieprawidłowy JSON."}, status=400)

        alias = str(body.get("account") or vault.accounts[0].alias).strip()
        message_id = str(body.get("message_id") or "").strip()
        reply_body = str(body.get("body") or "").strip()
        custom_subject = str(body.get("subject") or "").strip()
        account_source = str(body.get("account_source") or "rodzic").strip()

        if not message_id or not reply_body:
            return web.json_response(
                {"error": "Podaj treść odpowiedzi oraz identyfikator wiadomości."}, status=400
            )

        try:
            resolved = await self.hub.resolve_reply_recipient(
                vault, alias, message_id, account_source=account_source
            )
            subject = custom_subject or resolved["reply_subject"]
            confirm_payload = {
                "alias": alias,
                "message_id": message_id,
                "subject": subject,
                "body": reply_body,
                "recipient": resolved["recipient"],
            }
            confirmation_token = self.token_vault.seal_reply_confirmation(
                confirm_payload, vault_fingerprint=vault.fingerprint()
            )
            return web.json_response(
                {
                    "ok": True,
                    "account": alias,
                    "recipient_label": resolved["recipient"]["label"],
                    "subject": subject,
                    "body": reply_body,
                    "confirmation_token": confirmation_token,
                    "expires_in_seconds": 300,
                }
            )
        except Exception as exc:
            logger.warning("Reply preview failed for %s: %s", alias, exc)
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def handle_reply_send(self, request: web.Request) -> web.Response:
        """Step 2 of 2: Redeem a 5-minute confirmation token and dispatch the reply."""
        try:
            vault, _, _ = self._resolve_vault(request, allowed_scopes=("full",))
            body = await request.json()
        except TokenValidationError as exc:
            return web.json_response({"error": str(exc), "code": "VAULT_EMPTY"}, status=401)
        except Exception:
            return web.json_response({"error": "Nieprawidłowy JSON."}, status=400)

        confirmation_token = str(body.get("confirmation_token") or "").strip()
        if not confirmation_token:
            return web.json_response({"error": "Brak tokenu potwierdzenia wysyłki."}, status=400)

        try:
            confirm_data = self.token_vault.unseal_reply_confirmation(
                confirmation_token, expected_fingerprint=vault.fingerprint()
            )
            outcome = await self.hub.execute_confirmed_reply(vault, confirm_data)
            return web.json_response({"ok": True, **outcome})
        except Exception as exc:
            logger.warning("Reply send failed: %s", exc)
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    @staticmethod
    def _project_timetable_week(base_tt: dict[str, Any], base_monday: date, target_monday: date) -> dict[str, Any]:
        """Project the current week's recurring schedule onto a future week not yet published in Librus dropdown."""
        delta = timedelta(days=(target_monday - base_monday).days)
        projected_days: list[dict[str, Any]] = []
        for day_obj in base_tt.get("days", []):
            try:
                orig_d = date.fromisoformat(day_obj["day"])
            except Exception:
                continue
            new_d_str = (orig_d + delta).isoformat()
            new_periods: list[dict[str, Any]] = []
            for p in day_obj.get("periods", []):
                new_entries: list[dict[str, Any]] = []
                raw_entries = p.get("entries", [])
                for e in raw_entries:
                    # If there are multiple entries in the slot (e.g. cancelled + substitution),
                    # keep the original lesson rather than the one-off substitution.
                    if len(raw_entries) > 1 and e.get("status") == "substitution":
                        continue
                    cloned = dict(e)
                    cloned["status"] = "normal"
                    cloned["changes"] = []
                    new_entries.append(cloned)
                new_p = dict(p)
                new_p["entries"] = new_entries
                new_periods.append(new_p)
            projected_days.append(
                {
                    "day": new_d_str,
                    "is_class_free_day": False,
                    "class_free_day_title": None,
                    "periods": new_periods,
                }
            )
        return {
            "alias": base_tt.get("alias"),
            "week_start": target_monday.isoformat(),
            "days": projected_days,
        }

    async def _generate_ical_response(
        self, vault: UnsealedVaultPayload, alias: str, feed: str, force_download: bool = False
    ) -> web.Response:
        if not any(a.alias == alias for a in vault.accounts):
            if vault.accounts:
                alias = vault.accounts[0].alias
            else:
                return web.Response(text=f"Unknown account alias: {alias}", status=404)

        try:
            profiles = await self.hub.get_profiles(vault)
            prof = next((p for p in profiles if p["alias"] == alias), {"first_name": alias})
            if prof.get("auth_error"):
                logger.warning(
                    "Skipping .ics feed %s for %s due to active Librus auth_error (circuit breaker)",
                    feed,
                    alias,
                )
                return web.Response(
                    text="Librus authentication failed (invalid credentials or account locked). Calendar sync paused to prevent account lockout.",
                    status=403,
                )
            prof_name = prof.get("first_name") or alias.capitalize()

            if feed == "timetable":
                m1 = current_monday()
                m_prev = m1 - timedelta(days=7)
                m2 = m1 + timedelta(days=7)
                m3 = m1 + timedelta(days=14)
                tt1 = await self.hub.get_timetable(vault, alias, m1)
                weeks: list[dict[str, Any]] = []
                try:
                    tt_prev = await self.hub.get_timetable(vault, alias, m_prev)
                    weeks.append(tt_prev)
                except Exception:
                    pass
                weeks.append(tt1)
                for m_future in (m2, m3):
                    try:
                        tt_fut = await self.hub.get_timetable(vault, alias, m_future)
                        weeks.append(tt_fut)
                    except Exception:
                        weeks.append(self._project_timetable_week(tt1, m1, m_future))
                ics_text = generate_timetable_ics(prof_name, alias, weeks)
                dl_filename = f"plan-lekcji-{alias}.ics"
            elif feed == "exams":
                exams_data = await self.hub.get_exams_and_study_cards(
                    vault, alias, days_ahead=45, days_behind=14, use_ai=False
                )
                ics_text = generate_exams_ics(prof_name, alias, exams_data)
                dl_filename = f"sprawdziany-{alias}.ics"
            else:
                return web.Response(text="Unknown feed type", status=404)

            disposition = "attachment" if force_download else "inline"
            return web.Response(
                text=ics_text,
                content_type="text/calendar",
                charset="utf-8",
                headers={
                    "Content-Disposition": f'{disposition}; filename="{dl_filename}"',
                    "Cache-Control": "no-cache, max-age=0",
                    "Access-Control-Allow-Origin": "*",
                },
            )
        except Exception as exc:
            logger.exception("Error generating .ics feed %s for %s", feed, alias)
            return web.Response(text=f"Error generating calendar feed: {exc}", status=502)

    async def handle_ical_feed(self, request: web.Request) -> web.Response:
        """Stateless RFC 5545 iCalendar (.ics) endpoint decrypted on the fly via JWE token."""
        token = request.match_info.get("token", "").strip()
        alias = request.match_info.get("alias", "").strip()
        feed = request.match_info.get("feed", "").strip()

        # Revoked stale .ics tokens with invalid credentials that Google Calendar polls every hour
        if token.startswith(("lfh1.ht3pwAZ6E93siVDjNaI7", "lfh1.aAY3n8He6w6aLAw_zSI_")):
            return web.Response(
                text="This calendar subscription token has been revoked due to invalid Librus credentials. Please remove this calendar and generate a new .ics link.",
                status=410,
            )

        try:
            vault = self._overlay_local_student_accounts(
                self.token_vault.unseal_vault(token, allowed_scopes=("full", "kiosk", "ical"))
            )
        except TokenValidationError as exc:
            with contextlib.suppress(Exception):
                vault, _, _ = self._resolve_vault(
                    request, allowed_scopes=("full", "kiosk", "ical")
                )
            if not locals().get("vault"):
                return web.Response(text=f"Invalid token: {exc}", status=401)

        force_download = request.query.get("download") == "1"
        return await self._generate_ical_response(vault, alias, feed, force_download=force_download)

    async def handle_ical_download(self, request: web.Request) -> web.Response:
        """Direct iCalendar (.ics) download/feed using active session or local config (no JWE token required in URL)."""
        try:
            vault, _, _ = self._resolve_vault(
                request, allowed_scopes=("full", "kiosk", "ical")
            )
        except TokenValidationError as exc:
            return web.Response(text=f"Vault error: {exc}", status=401)

        alias = (
            request.query.get("account")
            or request.query.get("alias")
            or (vault.accounts[0].alias if vault.accounts else "")
        ).strip()
        feed = (request.query.get("feed") or "exams").strip()
        force_download = request.query.get("download", "1") == "1"
        return await self._generate_ical_response(vault, alias, feed, force_download=force_download)

    async def handle_google_calendar_sync(self, request: web.Request) -> web.Response:
        """Push upcoming exams directly to Google Calendar using an OAuth2 access token."""
        try:
            vault, _, _ = self._resolve_vault(request)
            body = await request.json()
        except TokenValidationError as exc:
            return web.json_response({"error": str(exc), "code": "VAULT_EMPTY"}, status=401)
        except Exception:
            return web.json_response({"error": "Nieprawidłowy JSON."}, status=400)

        access_token = str(body.get("access_token") or "").strip()
        calendar_id = str(body.get("calendar_id") or "primary").strip()
        alias = str(body.get("account") or vault.accounts[0].alias).strip()

        try:
            profiles = await self.hub.get_profiles(vault)
            prof = next((p for p in profiles if p["alias"] == alias), {"first_name": alias})
            exams_data = await self.hub.get_exams_and_study_cards(
                vault, alias, days_ahead=30, use_ai=True
            )
            res = await sync_events_to_google_calendar_oauth(
                access_token,
                calendar_id,
                prof.get("first_name") or alias.capitalize(),
                alias,
                exams_data,
            )
            return web.json_response({"ok": True, **res})
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def handle_alerts_and_digest(self, request: web.Request) -> web.Response:
        """Build proactive family alerts and optionally send an Email / WhatsApp digest."""
        try:
            vault, _, _ = self._resolve_vault(request)
        except TokenValidationError as exc:
            return web.json_response({"error": str(exc), "code": "VAULT_EMPTY"}, status=401)

        send_now = False
        recipient_email = vault.alert_email
        whatsapp_webhook = vault.whatsapp_webhook

        if request.method == "POST":
            with contextlib.suppress(Exception):
                body = await request.json()
                send_now = bool(body.get("send", False))
                if body.get("email"):
                    recipient_email = str(body["email"]).strip()
                if body.get("whatsapp_webhook"):
                    whatsapp_webhook = str(body["whatsapp_webhook"]).strip()

        try:
            profiles = await self.hub.get_profiles(vault)
            monday = current_monday()
            bundles: list[dict[str, Any]] = []
            all_alerts: list[dict[str, Any]] = []

            for prof in profiles:
                alias = prof["alias"]
                tt, exams, mats = await asyncio.gather(
                    self.hub.get_timetable(vault, alias, monday),
                    self.hub.get_exams_and_study_cards(vault, alias, days_ahead=14),
                    self.hub.get_materials_and_announcements(vault, alias),
                )
                prof_alerts = build_proactive_alerts(prof, tt, exams, mats)
                all_alerts.extend(prof_alerts)
                bundles.append(
                    {
                        "profile": prof,
                        "timetable": tt,
                        "exams": exams,
                        "materials": mats,
                        "alerts": prof_alerts,
                    }
                )

            subj, plain_text, html_body = format_email_digest_html(bundles)
            delivery = {"email": "not_requested", "whatsapp": "not_requested"}
            if send_now:
                delivery = await dispatch_digest_notifications(
                    recipient_email, whatsapp_webhook, subj, plain_text, html_body
                )

            return web.json_response(
                {
                    "ok": True,
                    "alerts": all_alerts,
                    "digest_subject": subj,
                    "digest_html": html_body,
                    "digest_text": plain_text,
                    "delivery": delivery,
                }
            )
        except Exception as exc:
            logger.exception("Error generating family alerts/digest")
            return web.json_response({"ok": False, "error": str(exc)}, status=502)

    async def handle_cast(self, request: web.Request) -> web.Response:
        if os.environ.get("K_SERVICE"):
            return web.json_response(
                {
                    **self.caster.last_status,
                    "connected": False,
                    "error": "Funkcja bezpośredniego castowania LAN (pychromecast) jest dostępna tylko na serwerze domowym.",
                },
                status=403,
            )
        action = "cast"
        if request.can_read_body:
            with contextlib.suppress(Exception):
                payload = await request.json()
                action = str(payload.get("action") or "cast")

        if action == "stop":
            status = await self.caster.stop_cast()
        elif action == "status":
            status = await self.caster.check_status()
        else:
            status = await self.caster.cast_now(force=True)
        return web.json_response(status)


def create_app(
    hub: StatelessLibrusHub,
    caster: NestHubCaster,
    static_dir: Path,
    token_vault: TokenVault | None = None,
) -> web.Application:
    server = DashboardServer(hub, caster, static_dir, token_vault=token_vault)

    @web.middleware
    async def canonical_host_middleware(
        request: web.Request,
        handler: Any,
    ) -> web.StreamResponse:
        raw_host = (request.headers.get("X-Forwarded-Host") or request.host or "").split(",")[0].strip()
        host = raw_host if _SAFE_HOST_RE.match(raw_host) else (request.host or "")
        if host.lower().startswith("www."):
            bare_host = host[4:]
            if _SAFE_HOST_RE.match(bare_host):
                raw_proto = (request.headers.get("X-Forwarded-Proto") or request.scheme or "https").split(",")[0].strip().lower()
                proto = "https" if raw_proto == "https" else "http"
                target_url = f"{proto}://{bare_host}{request.rel_url}"
                raise web.HTTPMovedPermanently(location=target_url)
        response = await handler(request)
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Content-Security-Policy", _CSP_HEADER)
        return response

    app = web.Application(middlewares=[canonical_host_middleware])

    app.router.add_get("/", server.handle_index)
    app.router.add_get("/favicon.ico", server.handle_favicon)
    app.router.add_get("/apple-touch-icon.png", server.handle_apple_touch_icon)
    app.router.add_get("/api/status", server.handle_status)
    app.router.add_post("/api/vault/seal", server.handle_vault_seal)
    app.router.add_post("/api/vault/student-account", server.handle_student_account)
    app.router.add_get("/api/profiles", server.handle_profiles)
    app.router.add_get("/api/bootstrap", server.handle_bootstrap)
    app.router.add_get("/api/timetable", server.handle_timetable)
    app.router.add_get("/api/exams", server.handle_exams)
    app.router.add_get("/api/materials", server.handle_materials)
    app.router.add_get("/api/grades-attendance", server.handle_grades_attendance)
    app.router.add_get("/api/messages", server.handle_messages)
    app.router.add_get("/api/messages/detail", server.handle_message_detail)
    app.router.add_get("/api/messages/attachment", server.handle_message_attachment)
    app.router.add_post("/api/messages/reply-preview", server.handle_reply_preview)
    app.router.add_post("/api/messages/reply-send", server.handle_reply_send)
    app.router.add_get("/api/ical/{token}/{alias}/{feed}.ics", server.handle_ical_feed)
    app.router.add_get("/api/ical/download", server.handle_ical_download)
    app.router.add_post("/api/calendar/google-sync", server.handle_google_calendar_sync)
    app.router.add_get("/api/alerts", server.handle_alerts_and_digest)
    app.router.add_post("/api/alerts/digest", server.handle_alerts_and_digest)
    app.router.add_post("/api/cast", server.handle_cast)
    app.router.add_static("/static", static_dir, show_index=False)

    async def on_startup(_app: web.Application) -> None:
        await caster.start_keepalive_loop()

    async def on_cleanup(_app: web.Application) -> None:
        await caster.stop_keepalive_loop()
        await hub.close_all()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


async def async_main(args: argparse.Namespace) -> None:
    lan_ip = detect_lan_ip(args.nest_hub_ip)
    dashboard_url = args.public_url or f"http://{lan_ip}:{args.port}/"
    static_dir = Path(__file__).resolve().parent / "static"

    is_cloud_run = os.environ.get("K_SERVICE") is not None
    hub = StatelessLibrusHub(local_config_path=Path(args.config))
    caster = NestHubCaster(
        hub_ip=args.nest_hub_ip,
        dashboard_url=dashboard_url,
        keep_alive=args.keep_alive and not args.no_cast and not is_cloud_run,
    )
    app = create_app(hub, caster, static_dir)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host=args.host, port=args.port)
    await site.start()

    logger.info("==========================================================")
    logger.info("Librus Family Hub server listening on:")
    logger.info("  Local:     http://127.0.0.1:%d/", args.port)
    logger.info("  LAN / Hub: %s", dashboard_url)
    logger.info("  Nest Hub:  %s (auto_cast=%s)", args.nest_hub_ip, not args.no_cast)
    logger.info("==========================================================")

    # Warm local profiles in the background if local config exists
    local_vault = load_local_config_vault(Path(args.config))
    if local_vault is not None:
        try:
            profiles = await hub.get_profiles(local_vault)
            logger.info(
                "Loaded %d local Librus profiles: %s",
                len(profiles),
                ", ".join(f"{p['first_name']} ({p['class_name']})" for p in profiles),
            )
        except Exception as exc:
            logger.warning("Initial local profile warm-up failed: %s", exc)

    if not args.no_cast and os.environ.get("K_SERVICE") is None:
        await caster.cast_now(force=True)

    stop_event = asyncio.Event()
    try:
        await stop_event.wait()
    finally:
        await runner.cleanup()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Librus Family Hub & Google Calendar Serverless Server"
    )
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help="Bind port (default: 8088)"
    )
    parser.add_argument(
        "--nest-hub-ip",
        default=DEFAULT_NEST_HUB_IP,
        help="IP address of Google Nest Hub (default: 192.168.86.40)",
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help="Path to local librus-mcp config.json fallback",
    )
    parser.add_argument(
        "--public-url",
        default=None,
        help="Override LAN URL sent to Nest Hub",
    )
    parser.add_argument(
        "--no-cast",
        action="store_true",
        help="Start the web server without automatically casting to Nest Hub on boot",
    )
    parser.add_argument(
        "--keep-alive",
        action="store_true",
        default=True,
        help="Automatically re-cast if Nest Hub drops back to ambient Backdrop",
    )
    return parser.parse_args()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(async_main(parse_args()))
