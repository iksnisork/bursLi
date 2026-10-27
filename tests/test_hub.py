"""Comprehensive Unit & Integration Tests for Librus Family Hub (Serverless + Local)."""

from __future__ import annotations

import io
import zipfile
from datetime import date
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer
import pytest

from app.calendar_alerts import (
    _validate_public_https_webhook,
    build_proactive_alerts,
    format_email_digest_html,
    format_study_card_description,
    generate_exams_ics,
    generate_timetable_ics,
)
from app.crypto_tokens import (
    SealedAccount,
    TokenValidationError,
    TokenVault,
    UnsealedVaultPayload,
)
from app.librus_client import (
    StatelessLibrusHub,
    extract_text_from_bytes,
    parse_teacher_and_classroom,
    summarize_change,
)
from app.study_assistant import (
    StudyAssistant,
    build_deterministic_checklist,
    classify_agenda_event,
    extract_verbatim_requirements,
    match_completed_lessons_for_subject,
    match_messages_and_materials,
    synthesize_action_item,
)
from server import NestHubCaster, create_app


# ==============================================================================
# 1. Zero-Database Crypto Vault Tests
# ==============================================================================


def test_token_vault_seal_and_unseal_roundtrip() -> None:
    vault = TokenVault(master_key=b"0123456789abcdef0123456789abcdef")
    accounts = [
        {"alias": "ola", "username": "1101135u", "password": "secret-password-1", "messaging_backend": "modern"},
        {"alias": "lena", "username": "1105579u", "password": "secret-password-2", "messaging_backend": "modern"},
    ]
    token = vault.seal_vault(
        accounts,
        alert_email="rodzic@example.com",
        whatsapp_webhook="https://hooks.example.com/wa",
    )
    assert token.startswith("lfh1.")
    assert "secret-password-1" not in token

    unsealed = vault.unseal_vault(token)
    assert len(unsealed.accounts) == 2
    assert unsealed.accounts[0] == SealedAccount(
        alias="ola",
        username="1101135u",
        password="secret-password-1",
        messaging_backend="modern",
    )
    assert unsealed.alert_email == "rodzic@example.com"
    assert unsealed.whatsapp_webhook == "https://hooks.example.com/wa"
    assert len(unsealed.fingerprint()) == 64


def test_token_vault_rejects_tampering_and_expiry() -> None:
    vault = TokenVault(master_key=b"abcdef0123456789abcdef0123456789")
    token = vault.seal_vault(
        [{"alias": "ola", "username": "u1", "password": "p1"}],
        ttl_seconds=3600,
    )
    # Flip a character in the ciphertext
    tampered = token[:-4] + ("AAAA" if token[-4:] != "AAAA" else "BBBB")
    with pytest.raises(TokenValidationError):
        vault.unseal_vault(tampered)

    # Expired token
    expired_token = vault.seal_vault(
        [{"alias": "ola", "username": "u1", "password": "p1"}],
        ttl_seconds=-10,
    )
    with pytest.raises(TokenValidationError, match="wygasł"):
        vault.unseal_vault(expired_token)


def test_reply_confirmation_token_roundtrip() -> None:
    vault = TokenVault(master_key=b"fedcba9876543210fedcba9876543210")
    payload = {
        "account": "ola",
        "message_id": "2086399",
        "subject": "Re: Konsultacje",
        "body": "Dzień dobry, dziękuję za informację.",
    }
    reply_token = vault.seal_reply_confirmation(payload)
    assert reply_token.startswith("rpl1.")

    unsealed = vault.unseal_reply_confirmation(reply_token)
    assert unsealed["account"] == "ola"
    assert unsealed["message_id"] == "2086399"
    assert unsealed["body"] == "Dzień dobry, dziękuję za informację."


# ==============================================================================
# 2. Deterministic Study Scope Aggregator (Zero-Omission) Tests
# ==============================================================================


def test_classify_agenda_event() -> None:
    assert classify_agenda_event("Sprawdzian - Zdrowie", "Sprawdzian") == "sprawdzian"
    assert classify_agenda_event("Kartkówka - stolice Europy") == "kartkowka"
    assert classify_agenda_event("Lektura", "", "Omawiamy lekturę Chłopcy z Placu Broni") == "lektura"
    assert classify_agenda_event("Projekt z geografii") == "projekt"
    assert classify_agenda_event("Wyjście do teatru") == "wycieczka"


def test_zero_omission_verbatim_requirements_and_lessons() -> None:
    teacher_description = (
        "Dział Zdrowie i choroba:\n"
        "- pojęcie zdrowia i choroby\n"
        "- czynniki wpływające na zdrowie\n"
        "- odporność swoista i nieswoista\n"
        "- szczepienia ochronne i antybiotyki"
    )
    reqs = extract_verbatim_requirements(teacher_description)
    assert len(reqs) == 5
    assert "odporność swoista i nieswoista" in reqs
    assert "szczepienia ochronne i antybiotyki" in reqs

    completed_lessons = [
        {"day": "2026-09-20", "lesson_number": 3, "subject": "Przyroda", "topic": "Sprawdzian wiadomości - Komórka"},
        {"day": "2026-10-02", "lesson_number": 3, "subject": "Przyroda", "topic": "Czym jest zdrowie i choroba?"},
        {"day": "2026-10-09", "lesson_number": 3, "subject": "Przyroda", "topic": "Układ odpornościowy człowieka"},
        {"day": "2026-10-16", "lesson_number": 3, "subject": "Przyroda", "topic": "Układ odpornościowy człowieka"},
        {"day": "2026-10-23", "lesson_number": 3, "subject": "Przyroda", "topic": "Powtórzenie działu Zdrowie"},
    ]
    matched_lessons = match_completed_lessons_for_subject(
        "Przyroda",
        date(2026, 10, 30),
        completed_lessons,
    )
    unit_lessons = matched_lessons["unit_lessons"]
    # Should slice after the previous 'Sprawdzian wiadomości - Komórka' and deduplicate consecutive identical topics
    assert len(unit_lessons) == 3
    assert unit_lessons[0]["topic"] == "Czym jest zdrowie i choroba?"
    assert unit_lessons[1]["topic"] == "Układ odpornościowy człowieka"
    assert unit_lessons[1]["count"] == 2
    assert unit_lessons[2]["topic"] == "Powtórzenie działu Zdrowie"

    messages = [
        {
            "id": "101",
            "correspondent": "Kowalska Anna",
            "subject": "Sprawdzian z przyrody - wymagania i materiały",
            "body": "Kod dostępu do podręcznika z przyrody: MULT-ABCD-1234 oraz ćwiczenia https://epodreczniki.pl/przyroda",
            "has_attachment": False,
            "sent_at": "2026-10-20 10:00",
        }
    ]
    materials = match_messages_and_materials("Przyroda", "Anna Kowalska", messages, [], exam_day=date(2026, 10, 30))
    assert len(materials["codes"]) == 1
    assert materials["codes"][0]["code"] == "MULT-ABCD-1234"
    assert "https://epodreczniki.pl/przyroda" in materials["links"]

    checklist = build_deterministic_checklist(
        exam_id="ex-1",
        verbatim_requirements=reqs,
        unit_lessons=unit_lessons,
        matched_materials=materials,
    )
    # 5 verbatim requirements + 1 teacher message guideline + 3 unit lesson topics + 1 textbook code = 10 deterministic checklist items
    assert len(checklist) == 10
    checklist_texts = "\n".join(item["text"] for item in checklist)
    for req in reqs:
        assert req in checklist_texts
    assert "Układ odpornościowy człowieka (2 godz.)" in checklist_texts
    assert "MULT-ABCD-1234" in checklist_texts


@pytest.mark.asyncio
async def test_study_assistant_deterministic_fallback() -> None:
    assistant = StudyAssistant()
    card = {
        "id": "card-1",
        "subject": "Historia",
        "category_label": "Sprawdzian",
        "verbatim_description": "Polska w XIV i XV wieku: Kazimierz Wielki, Unia w Krewie, Bitwa pod Grunwaldem.",
        "verbatim_requirements": [
            "Kazimierz Wielki",
            "Unia w Krewie",
            "Bitwa pod Grunwaldem",
        ],
        "unit_lessons": [
            {"day": "2026-10-12", "topic": "Ostatni Piastowie na tronie Polski", "count": 1},
            {"day": "2026-10-19", "topic": "Wielka wojna z zakonem krzyżackim", "count": 1},
        ],
    }
    enrichment = await assistant.enrich_study_card(card)
    assert "summary_headline" in enrichment
    assert len(enrichment["key_concepts_to_review"]) >= 2
    assert "study_strategy_tip" in enrichment


def test_conversational_messages_filtered_to_organizational_info() -> None:
    # 1. Conversational reply referencing a past lesson date ("15 września") inside a message sent 2026-09-29
    item = synthesize_action_item(
        kind="message",
        title="Re: Niezrozumienie",
        body=(
            "Dzień dobry,\n"
            "bardzo się cieszę, że zapytała Pani o tę sytuację z lekcji 15 września.\n"
            "Wszystko zostało już wyjaśnione i nie ma powodu do zmartwień.\n"
            "Pozdrawiam serdecznie"
        ),
        sender="Karwowska-Gastrau Natalia",
        published_on="2026-09-29 14:20",
        ref_today=date(2026, 10, 26),
    )
    assert item["cluster"] == "organizational_info"
    assert item["priority_label"] == "KOMUNIKAT"
    assert item["deadline_date"] == ""  # Past lesson date (2026-09-15 < 2026-09-29) must not become a deadline

    # 2. Historical date ("16 września 1877") in announcement must not become a 2026 deadline
    hist_item = synthesize_action_item(
        kind="announcement",
        title="Feliks Dzierżyński",
        body="Urodził się 16 września 1877 r. w majątku Dzierżynowo.",
        sender="Biblioteka",
        published_on="2026-09-11",
        ref_today=date(2026, 10, 26),
    )
    assert hist_item["deadline_date"] == ""
    assert hist_item["cluster"] == "organizational_info"


# ==============================================================================
# 3. In-Memory Attachment Text Extractor & Helpers Tests
# ==============================================================================


def test_parse_teacher_and_classroom_and_changes() -> None:
    parsed = parse_teacher_and_classroom("mgr Jan Nowak (gr. 1) s. 204")
    assert parsed == {"teacher": "mgr Jan Nowak", "group": "gr. 1", "room": "Sala 204"}

    change = summarize_change(
        "zastępstwo",
        [
            ["Przedmiot", "Matematyka -> Fizyka"],
            ["Nauczyciel", "Kowalski -> Wiśniewski"],
            ["Sala", "101 -> 202"],
        ],
    )
    assert "Za: Matematyka" in change["summary"]
    assert "Kowalski → Wiśniewski" in change["summary"]
    assert "Sala: 202" in change["summary"]


def test_extract_text_from_docx_and_txt_in_memory() -> None:
    # Build an in-memory .docx (ZIP with word/document.xml)
    buf = io.BytesIO()
    xml_content = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>"
        "<w:p><w:r><w:t>Konkurs recytatorski Poezja Dziecięca</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>Termin zgłoszeń: 30 października.</w:t></w:r></w:p>"
        "</w:body></w:document>"
    )
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", xml_content.encode("utf-8"))

    extracted_docx = extract_text_from_bytes(
        "Regulamin_konkursu.docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        buf.getvalue(),
    )
    assert extracted_docx is not None
    assert "Konkurs recytatorski Poezja Dziecięca" in extracted_docx
    assert "Termin zgłoszeń: 30 października." in extracted_docx

    extracted_txt = extract_text_from_bytes(
        "notatka.txt",
        "text/plain",
        "Zażółć gęślą jaźń".encode("cp1250"),
    )
    assert extracted_txt == "Zażółć gęślą jaźń"


# ==============================================================================
# 4. Dynamic RFC 5545 iCalendar (.ics) & Alert Engine Tests
# ==============================================================================


def test_generate_timetable_and_exams_ics_valid_rfc5545() -> None:
    timetables = [
        {
            "days": [
                {
                    "day": "2026-10-27",
                    "periods": [
                        {
                            "number": 1,
                            "starts_at": "08:00",
                            "ends_at": "08:45",
                            "lessons": [{"subject": "Język polski", "teacher": "Anna Nowak", "room": "Sala 12"}],
                            "changes": [{"label": "odwołane", "summary": "Lekcja odwołana"}],
                        },
                        {
                            "number": 2,
                            "starts_at": "08:55",
                            "ends_at": "09:40",
                            "lessons": [{"subject": "Matematyka", "teacher": "Piotr Kowalski", "room": "Sala 15"}],
                            "changes": [],
                        },
                    ],
                }
            ]
        }
    ]
    tt_ics = generate_timetable_ics("Aleksandra", "ola", timetables)
    assert tt_ics.startswith("BEGIN:VCALENDAR\r\n")
    assert tt_ics.endswith("END:VCALENDAR\r\n")
    assert "TZID:Europe/Warsaw" in tt_ics
    assert "ODWOŁANE" in tt_ics
    assert "BEGIN:VALARM" in tt_ics  # 1st period cancelled triggers evening alert

    for raw_line in tt_ics.split("\r\n"):
        if raw_line:
            assert len(raw_line.encode("utf-8")) <= 75

    exams_data = {
        "items": [
            {
                "id": "ex-100",
                "day": "2026-10-29",
                "weekday_pl": "Czwartek",
                "days_until": 3,
                "lesson_number": 3,
                "starts_at": "09:50",
                "ends_at": "10:35",
                "category_type": "sprawdzian",
                "category_label": "Sprawdzian",
                "subject": "Historia",
                "teacher": "Marek Zając",
                "classroom": "21",
                "verbatim_description": "Polska Jagiellonów - pełny dział 3.",
                "unit_lessons": [{"day": "2026-10-22", "topic": "Unia w Krewie", "count": 1}],
                "extracted_codes": [{"code": "MULT-1111-2222", "source": "Wiadomość"}],
                "ai_synthesis": {"study_strategy_tip": "Powtórz daty unii polsko-litewskich."},
            }
        ]
    }
    ex_ics = generate_exams_ics("Aleksandra", "ola", exams_data)
    ex_ics_unfolded = ex_ics.replace("\r\n ", "")
    assert "BEGIN:VCALENDAR" in ex_ics
    assert "TRIGGER:-PT72H" in ex_ics
    assert "TRIGGER:-PT24H" in ex_ics
    assert "Polska Jagiellonów" in ex_ics_unfolded
    assert "Unia w Krewie" in format_study_card_description(exams_data["items"][0])

    alerts = build_proactive_alerts(
        profile={"first_name": "Aleksandra", "alias": "ola"},
        timetable=timetables[0],
        exams_data=exams_data,
        materials_data={"important_messages": []},
        today=date(2026, 10, 26),
    )
    assert len(alerts) >= 2
    subj, plain, html_body = format_email_digest_html(
        [
            {
                "profile": {"full_name": "Aleksandra Rosińska", "class_name": "4b"},
                "exams": exams_data,
                "alerts": alerts,
            }
        ],
        generated_on=date(2026, 10, 26),
    )
    assert "Librus Family Hub" in subj
    assert "Polska Jagiellonów" in plain
    assert "Unia w Krewie" in html_body


# ==============================================================================
# 5. HTTP Server Integration Tests (aiohttp)
# ==============================================================================


@pytest.mark.asyncio
async def test_http_server_status_and_vault_seal_endpoints(tmp_path: Path) -> None:
    static_dir = Path(__file__).resolve().parent.parent / "static"
    token_vault = TokenVault(master_key=b"99999999999999999999999999999999")
    hub = StatelessLibrusHub(local_config_path=tmp_path / "nonexistent.json")
    caster = NestHubCaster(hub_ip="127.0.0.1", dashboard_url="http://127.0.0.1:8088/", keep_alive=False)
    app = create_app(hub, caster, static_dir, token_vault=token_vault)

    async with TestClient(TestServer(app)) as client:
        # 1. Index HTML
        resp_idx = await client.get("/")
        assert resp_idx.status == 200
        html_text = await client.get("/")
        html_str = await html_text.text()
        assert "bursLi" in html_str
        assert "krzysztofbury/librus-mcp" in html_str
        assert "salestube.tech" in html_str

        # 2. Status endpoint
        resp_status = await client.get("/api/status")
        assert resp_status.status == 200
        status_json = await client.get("/api/status")
        status_data = await status_json.json()
        assert status_data["mode"] == "empty"
        assert "nest_hub" in status_data

        # 3. Seal credentials into JWE token + get .ics subscription links
        resp_seal = await client.post(
            "/api/vault/seal",
            json={
                "accounts": [
                    {"alias": "ola", "username": "1101135u", "password": "pw1"},
                    {"alias": "lena", "username": "1105579u", "password": "pw2"},
                ],
                "alert_email": "parent@example.com",
            },
        )
        assert resp_seal.status == 200
        seal_json = await resp_seal.json()
        assert seal_json["ok"] is True
        assert seal_json["token"].startswith("lfh1.")
        assert "ola" in seal_json["ical_links"]
        assert "lena" in seal_json["ical_links"]
        assert seal_json["ical_links"]["ola"]["timetable_ics"].endswith("/ola/timetable.ics")
        assert seal_json["ical_links"]["ola"]["exams_ics"].endswith("/ola/exams.ics")
        assert seal_json["ical_links"]["ola"]["exams_gcal_add"].startswith(
            "https://calendar.google.com/calendar/r?cid="
        )

        # 4. Security headers and token scope enforcement
        assert resp_idx.headers.get("Referrer-Policy") == "no-referrer"
        assert resp_idx.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp_idx.headers.get("X-Frame-Options") == "SAMEORIGIN"
        assert "googletagmanager.com" in (resp_idx.headers.get("Content-Security-Policy") or "")

        # Extract the ical-scoped token from the generated .ics URL and verify it cannot be used for replies
        tt_url = seal_json["ical_links"]["ola"]["timetable_ics"]
        ical_tok = tt_url.split("/api/ical/")[1].split("/")[0]
        unsealed_ical = token_vault.unseal_vault(ical_tok, allowed_scopes=("ical",))
        assert unsealed_ical.scope == "ical"

        resp_reply_forbidden = await client.post(
            "/api/messages/reply-preview",
            headers={"X-Librus-Token": ical_tok},
            json={"account": "ola", "message_id": "123", "body": "Test"},
        )
        assert resp_reply_forbidden.status == 401


def test_scoped_tokens_and_reply_fingerprint_binding() -> None:
    vault = TokenVault(master_key=b"fedcba9876543210fedcba9876543210")
    accounts = [{"alias": "ola", "username": "1101135u", "password": "pw1"}]

    ical_token = vault.seal_vault(accounts, scope="ical")
    kiosk_token = vault.seal_vault(accounts, scope="kiosk")
    full_token = vault.seal_vault(accounts, scope="full")

    # ical token rejected when only full or kiosk scopes are allowed
    with pytest.raises(TokenValidationError, match="wyłącznie do subskrypcji lub odczytu"):
        vault.unseal_vault(ical_token, allowed_scopes=("full", "kiosk"))

    # kiosk token allowed for read-only dashboard ("full", "kiosk"), rejected for ("full",)
    assert vault.unseal_vault(kiosk_token, allowed_scopes=("full", "kiosk")).scope == "kiosk"
    with pytest.raises(TokenValidationError, match="wyłącznie do subskrypcji lub odczytu"):
        vault.unseal_vault(kiosk_token, allowed_scopes=("full",))

    # Reply confirmation bound to vault fingerprint
    fp1 = vault.unseal_vault(full_token).fingerprint()
    reply_tok = vault.seal_reply_confirmation(
        {"alias": "ola", "message_id": "99", "body": "OK"},
        vault_fingerprint=fp1,
    )
    assert vault.unseal_reply_confirmation(reply_tok, expected_fingerprint=fp1)["message_id"] == "99"
    with pytest.raises(TokenValidationError, match="innego konta"):
        vault.unseal_reply_confirmation(reply_tok, expected_fingerprint="different-fingerprint")


def test_ssrf_webhook_validation_and_docx_xxe_rejection() -> None:
    # 1. SSRF webhook validation blocks non-HTTPS, localhost, metadata IP, and private IPs
    for unsafe_url in (
        "http://example.com/webhook",
        "https://localhost/webhook",
        "https://127.0.0.1/webhook",
        "https://169.254.169.254/computeMetadata/v1/",
        "https://192.168.1.10/hook",
        "https://10.0.0.1/hook",
        "https://metadata.google.internal/computeMetadata/v1/",
    ):
        with pytest.raises(ValueError):
            _validate_public_https_webhook(unsafe_url)

    # 2. DOCX parser rejects XML with <!DOCTYPE / <!ENTITY (XXE defense)
    xxe_buf = io.BytesIO()
    xxe_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<!DOCTYPE foo [ <!ENTITY xxe SYSTEM "file:///etc/passwd"> ]>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>&xxe;</w:t></w:r></w:p></w:body></w:document>"
    )
    with zipfile.ZipFile(xxe_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", xxe_xml.encode("utf-8"))

    assert (
        extract_text_from_bytes(
            "evil.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            xxe_buf.getvalue(),
        )
        is None
    )


def test_three_account_modes_parent_student_and_both(tmp_path: Path) -> None:
    hub = StatelessLibrusHub(local_config_path=tmp_path / "nonexistent.json")
    payload = UnsealedVaultPayload(
        accounts=(
            SealedAccount(
                alias="both_acc",
                username="8090546",
                password="p1",
                student_username="8090546u",
                student_password="s1",
            ),
            SealedAccount(
                alias="parent_only",
                username="8090547",
                password="p2",
            ),
            SealedAccount(
                alias="student_only",
                username="8090548u",
                password="s3",
                student_username="8090548u",
                student_password="s3",
            ),
        )
    )
    assert hub._is_student_only_account(payload, "both_acc") is False
    assert hub._is_student_only_account(payload, "parent_only") is False
    assert hub._is_student_only_account(payload, "student_only") is True


@pytest.mark.asyncio
async def test_auth_error_surfaced_in_profiles_and_exams(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.librus_client import MessagingBackend, PooledServiceEntry

    class AccessDeniedError(Exception):
        pass

    class DummyDeniedClient:
        async def student_information(self, **_kwargs: object) -> object:
            raise AccessDeniedError("access_denied")

        async def agenda(self, *_args: object, **_kwargs: object) -> object:
            raise AccessDeniedError("access_denied")

        async def completed_lessons(self, *_args: object, **_kwargs: object) -> object:
            raise AccessDeniedError("access_denied")

        async def homework(self, *_args: object, **_kwargs: object) -> object:
            raise AccessDeniedError("access_denied")

        async def announcements(self, *_args: object, **_kwargs: object) -> object:
            raise AccessDeniedError("access_denied")

        async def messages(self, *_args: object, **_kwargs: object) -> object:
            raise AccessDeniedError("access_denied")

    class DummyDeniedService:
        def account(self, _key: str) -> DummyDeniedClient:
            return DummyDeniedClient()

    hub = StatelessLibrusHub(local_config_path=tmp_path / "nonexistent.json")
    payload = UnsealedVaultPayload(
        accounts=(
            SealedAccount(
                alias="mila",
                username="8090999",
                password="bad-parent-password",
                student_username="8090999u",
                student_password="bad-student-password",
            ),
        )
    )

    async def _fake_acquire_entry(_vault: UnsealedVaultPayload) -> PooledServiceEntry:
        return PooledServiceEntry(
            service=DummyDeniedService(),  # type: ignore[arg-type]
            backends={"mila": MessagingBackend.MODERN, "mila__student": MessagingBackend.MODERN},
            created_at=0.0,
            last_used_at=0.0,
        )

    monkeypatch.setattr(hub, "_acquire_entry", _fake_acquire_entry)

    profiles = await hub.get_profiles(payload)
    assert len(profiles) == 1
    assert profiles[0]["alias"] == "mila"
    assert profiles[0]["auth_error"] is True
    assert "odrzucone przez Librus Synergia" in profiles[0]["auth_error_message"]

    with pytest.raises(AccessDeniedError):
        await hub.get_exams_and_study_cards(payload, "mila", days_ahead=14)


