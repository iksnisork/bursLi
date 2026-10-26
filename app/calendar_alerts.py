"""Dynamic iCal (.ics) Feed Generator (with VALARM), Google Calendar OAuth Sync & Alert Digest Engine."""

from __future__ import annotations

import datetime as dt
import hashlib
import html
import ipaddress
import logging
import os
import smtplib
import socket
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

logger = logging.getLogger("librus-hub.calendar")

CATEGORY_EMOJI = {
    "sprawdzian": "🔴 [SPRAWDZIAN]",
    "kartkowka": "🟠 [KARTKÓWKA]",
    "projekt": "🟣 [PROJEKT]",
    "lektura": "📘 [LEKTURA]",
    "zadanie": "📝 [ZADANIE]",
    "wycieczka": "🚌 [WYJŚCIE]",
    "inne": "📌 [TERMINARZ]",
}

# Google Calendar event colorId mapping
GCAL_COLOR_IDS = {
    "sprawdzian": "11",  # Tomato Red
    "kartkowka": "6",    # Tangerine Orange
    "projekt": "3",      # Grape Purple
    "lektura": "9",      # Blueberry
    "cancelled": "8",    # Graphite Gray
    "substitution": "5", # Banana Yellow
}


def _ics_escape(text: str | None) -> str:
    """Escape text for RFC 5545 iCalendar properties."""
    if not text:
        return ""
    s = str(text).replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
    s = s.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")
    return s


def _fold_ics_line(line: str) -> str:
    """Fold RFC 5545 lines at <=73 octets without ever splitting an escape sequence (e.g. \\n or \\,)."""
    if len(line.encode("utf-8")) <= 73:
        return line
    out: list[str] = []
    current = ""
    for ch in line:
        limit = 70 if ch == "\\" else 72
        if len((current + ch).encode("utf-8")) > limit and not current.endswith("\\"):
            out.append(current)
            current = " " + ch
        else:
            current += ch
    if current:
        out.append(current)
    return "\r\n".join(out)


def _vtimezone_warsaw() -> list[str]:
    return [
        "BEGIN:VTIMEZONE",
        "TZID:Europe/Warsaw",
        "X-LIC-LOCATION:Europe/Warsaw",
        "BEGIN:DAYLIGHT",
        "TZOFFSETFROM:+0100",
        "TZOFFSETTO:+0200",
        "TZNAME:CEST",
        "DTSTART:19700329T020000",
        "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU",
        "END:DAYLIGHT",
        "BEGIN:STANDARD",
        "TZOFFSETFROM:+0200",
        "TZOFFSETTO:+0100",
        "TZNAME:CET",
        "DTSTART:19701025T030000",
        "RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU",
        "END:STANDARD",
        "END:VTIMEZONE",
    ]


def format_study_card_description(card: dict[str, Any]) -> str:
    """Format a complete zero-omission StudyCard into plain text for Google Calendar DESCRIPTION."""
    lines: list[str] = []
    cat_label = card.get("category_label") or card.get("title") or "Wydarzenie"
    subj = card.get("subject") or ""
    teacher = card.get("teacher") or ""
    room = card.get("classroom") or ""
    lesson_no = card.get("lesson_number")

    header_parts = [f"Przedmiot: {subj}", f"Rodzaj: {cat_label}"]
    if lesson_no is not None:
        header_parts.append(f"Lekcja: {lesson_no}")
    if teacher:
        header_parts.append(f"Nauczyciel: {teacher}")
    if room:
        header_parts.append(f"Sala: {room}")
    lines.append(" | ".join(header_parts))
    lines.append("")

    syn_req = card.get("synthesized_requirements") or {}
    if syn_req.get("clue_text"):
        lines.append("=== WYMAGANIA I ZAKRES MATERIAŁU ===")
        lines.append(str(syn_req["clue_text"]))
        for pt in (syn_req.get("requirement_points") or [])[:10]:
            if pt.get("text") and pt.get("text") != syn_req.get("clue_text"):
                lines.append(f"• [{pt.get('source', 'Źródło')}] {pt['text']}")
        lines.append("")
    else:
        lines.append("=== ZAKRES Z TERMINARZA (1:1) ===")
        lines.append(str(card.get("verbatim_description") or "Brak dodatkowego opisu."))
        lines.append("")

    unit_lessons = card.get("unit_lessons") or []
    if unit_lessons:
        lines.append("=== ZREALIZOWANE TEMATY LEKCJI W TYM DZIALE ===")
        for les in unit_lessons:
            cnt = f" ({les['count']} godz.)" if les.get("count", 1) > 1 else ""
            lines.append(f"• [{les['day']}] {les['topic']}{cnt}")
        lines.append("")

    codes = card.get("extracted_codes") or []
    if codes:
        lines.append("=== KODY I MATERIAŁY Z WIADOMOŚCI ===")
        for c in codes:
            lines.append(f"• {c['code']} ({c['source']})")
        lines.append("")

    ai_syn = card.get("ai_synthesis") or {}
    if ai_syn.get("study_strategy_tip"):
        lines.append(f"💡 Wskazówka: {ai_syn['study_strategy_tip']}")

    return "\n".join(lines).strip()


def generate_timetable_ics(
    profile_name: str,
    alias: str,
    timetables: list[dict[str, Any]],
) -> str:
    """Generate RFC 5545 iCalendar feed for student's weekly timetables (with changes & cancellations)."""
    now_utc = datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    cal_name = f"Plan lekcji – {profile_name}"

    lines: list[str] = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Librus Family Hub//Serverless GCP//PL",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_ics_escape(cal_name)}",
        "X-WR-TIMEZONE:Europe/Warsaw",
        f"X-WR-CALDESC:{_ics_escape(f'Automatycznie aktualizowany plan lekcji i zastępstw dla: {profile_name}')}",
        *_vtimezone_warsaw(),
    ]

    for tt in timetables:
        for day_entry in tt.get("days") or []:
            day_str = str(day_entry.get("day") or "")
            if not day_str:
                continue
            ymd = day_str.replace("-", "")
            for period in day_entry.get("periods") or []:
                lessons = period.get("lessons") or []
                changes = period.get("changes") or []
                if not lessons and not changes:
                    continue

                num = int(period.get("number") or 0)
                starts_at = str(period.get("starts_at") or "08:00").replace(":", "")
                ends_at = str(period.get("ends_at") or "08:45").replace(":", "")
                if len(starts_at) == 4:
                    starts_at += "00"
                if len(ends_at) == 4:
                    ends_at += "00"

                primary = lessons[0] if lessons else {}
                subject = str(primary.get("subject") or "Lekcja").strip()
                teacher = str(primary.get("teacher") or "").strip()
                room = str(primary.get("room") or "").strip()

                is_cancelled = any(
                    "odwoł" in str(c.get("label") or "").lower()
                    or "nieobec" in str(c.get("label") or "").lower()
                    for c in changes
                )
                is_substitution = bool(changes) and not is_cancelled

                if is_cancelled:
                    summary = f"❌ [ODWOŁANE] {subject} (lekcja {num})"
                elif is_substitution:
                    summary = f"🔄 [ZASTĘPSTWO] {subject} (lekcja {num})"
                else:
                    summary = f"{num}. {subject}"

                desc_lines = [f"Lekcja {num} ({period.get('starts_at')}–{period.get('ends_at')})"]
                if teacher:
                    desc_lines.append(f"Nauczyciel: {teacher}")
                if room:
                    desc_lines.append(f"Sala: {room}")
                if changes:
                    desc_lines.append("")
                    desc_lines.append("Zmiany w planie:")
                    for c in changes:
                        desc_lines.append(f"• {c.get('label')}: {c.get('summary')}")

                uid_seed = f"tt:{alias}:{day_str}:{num}:{subject}"
                uid = hashlib.sha1(uid_seed.encode("utf-8")).hexdigest()[:20] + "@librus-family-hub"

                lines.extend(
                    [
                        "BEGIN:VEVENT",
                        f"UID:{uid}",
                        f"DTSTAMP:{now_utc}",
                        "SEQUENCE:0",
                        "STATUS:CONFIRMED",
                        "TRANSP:OPAQUE",
                        f"DTSTART;TZID=Europe/Warsaw:{ymd}T{starts_at}",
                        f"DTEND;TZID=Europe/Warsaw:{ymd}T{ends_at}",
                        f"SUMMARY:{_ics_escape(summary)}",
                        f"DESCRIPTION:{_ics_escape(chr(10).join(desc_lines))}",
                    ]
                )
                if room:
                    lines.append(f"LOCATION:{_ics_escape(room)}")

                # If 1st period is cancelled, add a reminder the evening before
                if is_cancelled and num <= 1:
                    lines.extend(
                        [
                            "BEGIN:VALARM",
                            "ACTION:DISPLAY",
                            f"DESCRIPTION:{_ics_escape(f'Uwaga: Jutro odwołana {num}. lekcja ({subject}) u {profile_name}!')}",
                            "TRIGGER:-PT13H",
                            "END:VALARM",
                        ]
                    )

                lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold_ics_line(l) for l in lines) + "\r\n"


def generate_exams_ics(
    profile_name: str,
    alias: str,
    exams_data: dict[str, Any],
) -> str:
    """Generate RFC 5545 iCalendar feed for exams, quizzes, projects, readings & study scopes."""
    now_utc = datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    cal_name = f"Sprawdziany i Zakres – {profile_name}"

    lines: list[str] = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Librus Family Hub//Exams & Study Cards//PL",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_ics_escape(cal_name)}",
        "X-WR-TIMEZONE:Europe/Warsaw",
        f"X-WR-CALDESC:{_ics_escape(f'Sprawdziany, kartkówki, projekty i lektury wraz z pełnym zakresem materiału dla: {profile_name}')}",
        *_vtimezone_warsaw(),
    ]

    for card in exams_data.get("items") or []:
        day_str = str(card.get("day") or "")
        if not day_str:
            continue
        ymd = day_str.replace("-", "")
        cat_type = str(card.get("category_type") or "inne")
        prefix = CATEGORY_EMOJI.get(cat_type, "📌 [TERMINARZ]")
        subj = str(card.get("subject") or card.get("title") or "").strip()
        lesson_num = card.get("lesson_number")
        lesson_suffix = f" (lekcja {lesson_num})" if lesson_num is not None else ""
        summary = f"{prefix} {subj} – {profile_name}{lesson_suffix}"

        full_desc = format_study_card_description(card)
        uid_seed = f"exam:{alias}:{card.get('id')}:{day_str}:{subj}"
        uid = "exam-" + hashlib.sha1(uid_seed.encode("utf-8")).hexdigest()[:20] + "@librus-family-hub"

        lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:{uid}",
                f"DTSTAMP:{now_utc}",
                "SEQUENCE:0",
                "STATUS:CONFIRMED",
                "TRANSP:OPAQUE",
            ]
        )

        starts_at = card.get("starts_at")
        ends_at = card.get("ends_at")
        if starts_at and ends_at and ":" in str(starts_at) and ":" in str(ends_at):
            st = str(starts_at)[:5].replace(":", "") + "00"
            et = str(ends_at)[:5].replace(":", "") + "00"
            lines.append(f"DTSTART;TZID=Europe/Warsaw:{ymd}T{st}")
            lines.append(f"DTEND;TZID=Europe/Warsaw:{ymd}T{et}")
        else:
            next_day = (date.fromisoformat(day_str) + timedelta(days=1)).strftime("%Y%m%d")
            lines.append(f"DTSTART;VALUE=DATE:{ymd}")
            lines.append(f"DTEND;VALUE=DATE:{next_day}")

        lines.append(f"SUMMARY:{_ics_escape(summary)}")
        lines.append(f"DESCRIPTION:{_ics_escape(full_desc)}")
        if card.get("classroom"):
            classroom_label = f"Sala {card['classroom']}"
            lines.append(f"LOCATION:{_ics_escape(classroom_label)}")

        # Native VALARM reminders
        if cat_type == "sprawdzian":
            lines.extend(
                [
                    "BEGIN:VALARM",
                    "ACTION:DISPLAY",
                    f"DESCRIPTION:{_ics_escape(f'Za 3 dni sprawdzian z: {subj} ({profile_name})')}",
                    "TRIGGER:-PT72H",
                    "END:VALARM",
                    "BEGIN:VALARM",
                    "ACTION:DISPLAY",
                    f"DESCRIPTION:{_ics_escape(f'Jutro sprawdzian z: {subj} ({profile_name})!')}",
                    "TRIGGER:-PT24H",
                    "END:VALARM",
                ]
            )
        else:
            cat_label = card.get("category_label", "termin")
            lines.extend(
                [
                    "BEGIN:VALARM",
                    "ACTION:DISPLAY",
                    f"DESCRIPTION:{_ics_escape(f'Jutro {cat_label}: {subj} ({profile_name})')}",
                    "TRIGGER:-PT24H",
                    "END:VALARM",
                ]
            )

        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold_ics_line(l) for l in lines) + "\r\n"


async def sync_events_to_google_calendar_oauth(
    access_token: str,
    calendar_id: str,
    profile_name: str,
    alias: str,
    exams_data: dict[str, Any],
) -> dict[str, Any]:
    """Push upcoming exams & study scopes directly into user's Google Calendar via REST API."""
    if not access_token:
        raise ValueError("Brak tokenu OAuth2 dla Google Calendar.")
    cal_id = (calendar_id or "primary").strip()
    safe_cal_id = quote(cal_id, safe="")
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    synced_count = 0
    errors: list[str] = []

    async with httpx.AsyncClient(timeout=20.0) as client:
        for card in exams_data.get("items") or []:
            day_str = str(card.get("day") or "")
            if not day_str:
                continue
            cat_type = str(card.get("category_type") or "inne")
            prefix = CATEGORY_EMOJI.get(cat_type, "📌 [TERMINARZ]")
            subj = str(card.get("subject") or card.get("title") or "").strip()
            summary = f"{prefix} {subj} – {profile_name}"
            description = format_study_card_description(card)
            color_id = GCAL_COLOR_IDS.get(cat_type, "9")

            # Deterministic Google Calendar event ID (lowercase hex/base32 characters a-v and 0-9)
            deterministic_id = "lfh" + hashlib.sha1(
                f"{alias}:{card.get('id')}:{day_str}".encode("utf-8")
            ).hexdigest()[:24]

            starts_at = card.get("starts_at")
            ends_at = card.get("ends_at")
            if starts_at and ends_at and ":" in str(starts_at) and ":" in str(ends_at):
                start_obj = {
                    "dateTime": f"{day_str}T{str(starts_at)[:5]}:00",
                    "timeZone": "Europe/Warsaw",
                }
                end_obj = {
                    "dateTime": f"{day_str}T{str(ends_at)[:5]}:00",
                    "timeZone": "Europe/Warsaw",
                }
            else:
                next_day = (date.fromisoformat(day_str) + timedelta(days=1)).isoformat()
                start_obj = {"date": day_str}
                end_obj = {"date": next_day}

            body = {
                "id": deterministic_id,
                "summary": summary,
                "description": description,
                "colorId": color_id,
                "start": start_obj,
                "end": end_obj,
                "reminders": {
                    "useDefault": False,
                    "overrides": [
                        {"method": "popup", "minutes": 24 * 60},
                        {"method": "popup", "minutes": 3 * 24 * 60},
                    ],
                },
            }

            put_url = f"https://www.googleapis.com/calendar/v3/calendars/{safe_cal_id}/events/{deterministic_id}"
            resp = await client.put(put_url, headers=headers, json=body)
            if resp.status_code == 404:
                post_url = f"https://www.googleapis.com/calendar/v3/calendars/{safe_cal_id}/events"
                resp = await client.post(post_url, headers=headers, json=body)

            if resp.status_code in (200, 201):
                synced_count += 1
            else:
                errors.append(f"{subj} ({day_str}): HTTP {resp.status_code}")

    return {
        "synced_count": synced_count,
        "calendar_id": cal_id,
        "errors": errors,
    }


def build_proactive_alerts(
    profile: dict[str, Any],
    timetable: dict[str, Any],
    exams_data: dict[str, Any],
    materials_data: dict[str, Any],
    today: date | None = None,
) -> list[dict[str, Any]]:
    """Build actionable alerts (upcoming exams within 4 days, tomorrow's timetable changes, new messages)."""
    ref_today = today or date.today()
    tomorrow_iso = (ref_today + timedelta(days=1)).isoformat()
    today_iso = ref_today.isoformat()
    student_name = profile.get("first_name") or profile.get("alias") or "Uczeń"
    alerts: list[dict[str, Any]] = []

    # 1. Urgent exams (within 0..4 days)
    for card in exams_data.get("items") or []:
        days_until = int(card.get("days_until", 99))
        if 0 <= days_until <= 4:
            when_label = (
                "DZIŚ"
                if days_until == 0
                else ("JUTRO" if days_until == 1 else f"za {days_until} dni ({card['weekday_pl']})")
            )
            severity = "critical" if days_until <= 1 else "warning"
            alerts.append(
                {
                    "id": f"alert-exam-{card['id']}",
                    "severity": severity,
                    "category": card.get("category_type", "sprawdzian"),
                    "student": student_name,
                    "title": f"{student_name}: {card['category_label']} z przedmiotu {card['subject']} – {when_label}",
                    "subtitle": card.get("verbatim_description", "")[:180],
                    "day": card.get("day"),
                    "exam_id": card.get("id"),
                }
            )

    # 2. Timetable changes for today or tomorrow
    for day_entry in timetable.get("days") or []:
        d_str = day_entry.get("day")
        if d_str not in (today_iso, tomorrow_iso):
            continue
        when_str = "Dziś" if d_str == today_iso else "Jutro"
        for p in day_entry.get("periods") or []:
            for ch in p.get("changes") or []:
                subj_name = (
                    p["lessons"][0]["subject"]
                    if p.get("lessons")
                    else ch.get("summary")
                )
                alerts.append(
                    {
                        "id": f"alert-tt-{student_name}-{d_str}-{p['number']}",
                        "severity": "info",
                        "category": "zmiana_planu",
                        "student": student_name,
                        "title": f"{student_name} ({when_str}, lekcja {p['number']}): {ch.get('label', 'Zmiana').upper()} – {subj_name}",
                        "subtitle": ch.get("summary", ""),
                        "day": d_str,
                    }
                )

    # 3. Unread important messages or messages with attachments
    for msg in (materials_data.get("important_messages") or [])[:5]:
        if msg.get("unread") or msg.get("has_attachment"):
            alerts.append(
                {
                    "id": f"alert-msg-{student_name}-{msg.get('message_id')}",
                    "severity": "info",
                    "category": "wiadomosc",
                    "student": student_name,
                    "title": f"{student_name} – Wiadomość: {msg.get('subject')}",
                    "subtitle": f"Od: {msg.get('sender')}"
                    + (" (zawiera załącznik 📎)" if msg.get("has_attachment") else ""),
                    "day": str(msg.get("sent_at") or "")[:10],
                    "message_id": msg.get("message_id"),
                }
            )

    return alerts


def format_email_digest_html(
    family_bundles: list[dict[str, Any]],
    generated_on: date | None = None,
) -> tuple[str, str, str]:
    """Generate (subject, plain_text, html_body) for the family daily/weekly exam & timetable digest."""
    ref_day = generated_on or date.today()
    subject = f"🎒 Librus Family Hub – Podsumowanie planu i sprawdzianów ({ref_day.isoformat()})"

    plain_lines = [f"Podsumowanie Librus Family Hub na dzień {ref_day.isoformat()}", ""]
    html_parts = [
        "<div style='font-family:Inter,system-ui,sans-serif;max-width:680px;margin:0 auto;color:#0f172a;'>",
        f"<h2 style='color:#1e293b;'>🎒 Podsumowanie Librus Family Hub ({ref_day.isoformat()})</h2>",
    ]

    for bundle in family_bundles:
        prof = bundle["profile"]
        exams = bundle["exams"].get("items") or []
        alerts = bundle.get("alerts") or []
        name = html.escape(prof.get("full_name") or prof.get("first_name") or prof.get("alias"))
        cls_name = html.escape(prof.get("class_name") or "")

        plain_lines.append(f"=== {name} ({cls_name}) ===")
        html_parts.append(
            f"<div style='border:1px solid #e2e8f0;border-radius:12px;padding:16px;margin-bottom:20px;background:#f8fafc;'>"
            f"<h3 style='margin-top:0;color:#0f172a;'>👩‍🎓 {name} <span style='font-weight:normal;color:#64748b;'>({cls_name})</span></h3>"
        )

        if alerts:
            html_parts.append("<h4>🔔 Najważniejsze alerty:</h4><ul>")
            for al in alerts[:6]:
                plain_lines.append(f" ! {al['title']} — {al.get('subtitle', '')}")
                html_parts.append(
                    f"<li><strong>{html.escape(al['title'])}</strong><br/>"
                    f"<span style='color:#475569;font-size:13px;'>{html.escape(al.get('subtitle', ''))}</span></li>"
                )
            html_parts.append("</ul>")

        if exams:
            html_parts.append("<h4>🎯 Nadchodzące sprawdziany, kartkówki i projekty:</h4>")
            for ex in exams[:8]:
                plain_lines.append(
                    f" * {ex['day']} ({ex['weekday_pl']}), lekcja {ex.get('lesson_number') or '-'}: "
                    f"{ex['subject']} — {ex['category_label']}"
                )
                plain_lines.append(f"   Zakres: {ex['verbatim_description']}")
                lessons_html = "".join(
                    f"<li>{html.escape(l['topic'])} <span style='color:#64748b;'>({l['day']})</span></li>"
                    for l in (ex.get("unit_lessons") or [])[:6]
                )
                html_parts.append(
                    f"<div style='background:#fff;border-left:4px solid #3b82f6;padding:10px 14px;margin-bottom:10px;border-radius:6px;'>"
                    f"<strong>{html.escape(ex['day'])} ({html.escape(ex['weekday_pl'])}) · "
                    f"{html.escape(ex['subject'])} – {html.escape(ex['category_label'])}</strong>"
                    f"<p style='margin:6px 0;font-size:13px;white-space:pre-line;'>{html.escape(ex['verbatim_description'])}</p>"
                    + (
                        f"<div style='font-size:12px;color:#334155;'><em>Zrealizowane lekcje z działu:</em><ul style='margin:4px 0;'>{lessons_html}</ul></div>"
                        if lessons_html
                        else ""
                    )
                    + "</div>"
                )
        else:
            plain_lines.append(" Brak zapowiedzianych sprawdzianów w najbliższym oknie.")
            html_parts.append("<p>Brak zapowiedzianych sprawdzianów w najbliższym okresie.</p>")

        plain_lines.append("")
        html_parts.append("</div>")

    html_parts.append("</div>")
    return subject, "\n".join(plain_lines), "\n".join(html_parts)


def _validate_public_https_webhook(url: str) -> str:
    """Validate that a webhook URL uses HTTPS and resolves exclusively to public global IP addresses (anti-SSRF)."""
    clean = (url or "").strip()
    parsed = urlsplit(clean)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ValueError("Webhook musi używać protokołu HTTPS oraz prawidłowej nazwy hosta.")
    host = parsed.hostname.lower().strip(".")
    if (
        host in ("localhost", "metadata.google.internal", "metadata", "169.254.169.254")
        or host.endswith((".internal", ".local", ".localhost", ".lan"))
    ):
        raise ValueError("Niedozwolony host wewnętrzny w adresie webhooka.")

    try:
        addr_infos = socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError(f"Nie można rozwiązać nazwy hosta webhooka ({host}).") from exc

    if not addr_infos:
        raise ValueError(f"Brak adresów IP dla hosta webhooka ({host}).")

    for info in addr_infos:
        ip_str = info[4][0]
        ip_obj = ipaddress.ip_address(ip_str)
        if (
            not ip_obj.is_global
            or ip_obj.is_private
            or ip_obj.is_loopback
            or ip_obj.is_link_local
            or ip_obj.is_multicast
            or ip_obj.is_reserved
        ):
            raise ValueError("Adres webhooka wskazuje na prywatną lub wewnętrzną sieć (zablokowano SSRF).")
    return clean


async def dispatch_digest_notifications(
    recipient_email: str | None,
    whatsapp_webhook: str | None,
    subject: str,
    plain_text: str,
    html_body: str,
) -> dict[str, Any]:
    """Send the digest via configured Email (Resend API or SMTP) and/or WhatsApp webhook."""
    delivery_status: dict[str, Any] = {"email": "skipped", "whatsapp": "skipped"}

    if recipient_email:
        resend_key = os.environ.get("RESEND_API_KEY", "").strip()
        smtp_host = os.environ.get("SMTP_HOST", "").strip()
        if resend_key:
            from_addr = os.environ.get("ALERT_FROM_EMAIL", "Librus Hub <onboarding@resend.dev>")
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    "https://api.resend.com/emails",
                    headers={"Authorization": f"Bearer {resend_key}"},
                    json={
                        "from": from_addr,
                        "to": [recipient_email],
                        "subject": subject,
                        "html": html_body,
                        "text": plain_text,
                    },
                )
                delivery_status["email"] = (
                    "sent" if resp.status_code in (200, 201) else f"error_http_{resp.status_code}"
                )
        elif smtp_host:
            smtp_port = int(os.environ.get("SMTP_PORT", "587"))
            smtp_user = os.environ.get("SMTP_USER", "")
            smtp_pass = os.environ.get("SMTP_PASSWORD", "")
            from_addr = os.environ.get("ALERT_FROM_EMAIL", smtp_user or "noreply@localhost")

            msg = EmailMessage()
            msg["Subject"] = subject
            msg["From"] = from_addr
            msg["To"] = recipient_email
            msg.set_content(plain_text)
            msg.add_alternative(html_body, subtype="html")

            with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
                if smtp_user and smtp_pass:
                    server.starttls()
                    server.login(smtp_user, smtp_pass)
                server.send_message(msg)
            delivery_status["email"] = "sent"
        else:
            delivery_status["email"] = "preview_only (ustaw RESEND_API_KEY lub SMTP_HOST na Cloud Run)"

    if whatsapp_webhook:
        validated_webhook = _validate_public_https_webhook(whatsapp_webhook)
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
            resp = await client.post(
                validated_webhook,
                json={"text": f"*{subject}*\n\n{plain_text[:3500]}"},
            )
            delivery_status["whatsapp"] = (
                "sent" if resp.status_code < 300 else f"error_http_{resp.status_code}"
            )

    return delivery_status
