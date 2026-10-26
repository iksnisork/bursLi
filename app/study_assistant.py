"""Deterministic Exam & Study Scope Aggregator and Zero-Omission Synthesizer.

Combines data from 4 Librus modules:
1. Terminarz (`agenda` + `agenda_detail`) — 100% verbatim teacher description & requirements
2. Zrealizowane lekcje (`completed_lessons`) — exact lesson topics covered in class for that subject
3. Zadania domowe (`homework`) — subject homework items
4. Wiadomości i Ogłoszenia (`messages` + `announcements`) — teacher messages (from parent & student accounts), textbook codes, attachments

Focuses strictly on the clue: WHEN the exam/quiz/project is, WHAT subject, and WHAT the synthesized
requirements are based on Terminarz, relevant messages (parent + child accounts), announcements,
and relevant lesson topics.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger("librus-hub.study")

WEEKDAYS_PL = [
    "Poniedziałek",
    "Wtorek",
    "Środa",
    "Czwartek",
    "Piątek",
    "Sobota",
    "Niedziela",
]

# Standard Polish school bell intervals fallback when timetable period is not loaded
DEFAULT_LESSON_INTERVALS: dict[int, tuple[str, str]] = {
    0: ("07:10", "07:55"),
    1: ("08:00", "08:45"),
    2: ("08:55", "09:40"),
    3: ("09:50", "10:35"),
    4: ("10:45", "11:30"),
    5: ("11:45", "12:30"),
    6: ("12:45", "13:30"),
    7: ("13:40", "14:25"),
    8: ("14:30", "15:15"),
    9: ("15:20", "16:05"),
}

_URL_RE = re.compile(r"https?://[^\s<>()\"']+")
_CODE_RE = re.compile(r"\b(?:MULT-[A-Z0-9]{4}-[A-Z0-9]{4}|[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4})\b")
_BULLET_SPLIT_RE = re.compile(r"(?:\r?\n)+|(?:\s*;\s*)(?=\d+\.|\-|\•)")

# Hard blocklist of message subjects that are NEVER exam requirements
_IRRELEVANT_MSG_SUBJECT_PATTERNS = (
    "dodano uwagę",
    "uwaga dla ucznia",
    "uwaga pozytywna",
    "uwaga negatywna",
    "zachowanie",
    "nieobecność",
    "usprawiedliwieni",
    "zwolnieni",
    "zielona szkoła",
    "dzień otwarty",
    "dzien otwarty",
    "multikino",
    "wycieczka",
    "ubezpieczeni",
    "obiad",
    "świetlic",
    "swietlic",
    "składk",
    "rada rodziców",
    "zebranie",
    "konsultacje",
    "zdjęcia",
    "fotograf",
    "legitymacj",
    "stypendi",
)

# Keywords indicating a message actually discusses an exam, quiz, project, or study scope
_EXAM_SCOPE_KEYWORDS = (
    "sprawdzian",
    "kartkówk",
    "kartkowk",
    "klasówk",
    "klasowk",
    "test",
    "zagadnieni",
    "wymagani",
    "zakres",
    "powtórzeni",
    "powtork",
    "powtórk",
    "obowiązuj",
    "obowiazuj",
    "kryteri",
    "projekt",
    "prezentacj",
    "lektur",
    "dyktand",
    "odpowiedź ustna",
    "praca klasowa",
    "dział",
    "rozdział",
)

_STOPWORDS_PL = {
    "sprawdzian",
    "kartkówka",
    "kartkowka",
    "zapowiedziana",
    "zapowiedziany",
    "klasa",
    "klasy",
    "lekcja",
    "lekcji",
    "temat",
    "tematy",
    "rozdział",
    "rozdzial",
    "dział",
    "dzial",
    "wiadomości",
    "wiadomosci",
    "powtórzenie",
    "powtorzenie",
    "podsumowanie",
    "zadanie",
    "zadania",
    "praca",
    "uczeń",
    "ucznia",
    "uczniów",
    "szkoła",
    "szkole",
    "nauczyciel",
    "przedmiot",
    "grupa",
    "część",
    "czesc",
    "dzień",
    "dzien",
    "godzina",
    "godz",
    "proszę",
    "prosze",
    "bardzo",
    "pozdrawiam",
    "poważaniem",
    "szanowni",
    "państwo",
    "drodzy",
    "rodzice",
    "dzieci",
    "strona",
    "strony",
    "ćwiczenia",
    "zeszyt",
    "podręcznik",
    "rodzaj",
    "opis",
    "zakres",
    "data",
    "dodania",
    "historia",
    "matematyka",
    "polski",
    "język",
    "jezyk",
    "angielski",
    "niemiecki",
    "biologia",
    "geografia",
    "przyroda",
    "fizyka",
    "chemia",
    "muzyka",
    "plastyka",
    "technika",
    "informatyka",
}


class ChecklistItem(BaseModel):
    id: str
    text: str
    source: str = Field(description="Źródło: 'Terminarz', 'Lekcja' lub 'Wiadomość'")
    mandatory: bool = True


class StudyCardEnrichment(BaseModel):
    """Structured schema for deterministic study card enrichment."""

    summary_headline: str = Field(
        description="Zwięzła synteza wymagań (1-2 zdania): dokładnie co obowiązuje na sprawdzian/kartkówkę/projekt."
    )
    key_concepts_to_review: list[str] = Field(
        description="Konkretne wymagania, zagadnienia i pojęcia do opanowania (na bazie terminarza, wiadomości i tematów lekcji)."
    )
    study_strategy_tip: str = Field(
        description="Krótka, praktyczna wskazówka jak się przygotować pod kątem konkretnych wymagań."
    )


def classify_agenda_event(title: str, category_meta: str = "", text: str = "") -> str:
    """Classify an agenda event into a canonical type, separating holidays and trips from exams."""
    combined = f"{title} {category_meta} {text}".lower()

    # 1. Free days / holidays -> exclude from 02 Sprawdziany
    if any(
        kw in combined
        for kw in (
            "dzień wolny",
            "dzien wolny",
            "niepodległości",
            "niepodleglosci",
            "święto",
            "swieto",
            "ferie",
            "wakacje",
            "przerwa świąteczna",
            "rekolekcje",
            "rozpoczęcie roku",
            "zakończenie roku",
        )
    ):
        return "wolne"

    # 2. Trips / school events -> move to 03 Zadania / Wydarzenia, exclude from 02 Sprawdziany
    if any(
        kw in combined
        for kw in (
            "wycieczk",
            "wyjście",
            "wyjscie",
            "tepfactory",
            "kino",
            "multikino",
            "teatr",
            "muzeum",
            "zielona szkoła",
            "dyskoteka",
            "bal ",
            "apel",
            "zebranie",
            "konsultacje",
            "dzień otwarty",
        )
    ):
        return "wycieczka"

    # 3. Actual exams, quizzes, readings, projects
    if (
        "sprawdzian" in combined
        or "klasówk" in combined
        or "klasowk" in combined
        or "praca klasowa" in combined
        or "test" in combined
        or "diagnoz" in combined
    ):
        return "sprawdzian"
    if (
        "kartkówk" in combined
        or "kartkowk" in combined
        or "dyktand" in combined
        or "odpowiedź ustna" in combined
    ):
        return "kartkowka"
    if "lektur" in combined:
        return "lektura"
    if (
        "projekt" in combined
        or "prezentacj" in combined
        or "referat" in combined
        or "plakat" in combined
        or "makiet" in combined
    ):
        return "projekt"
    if "zadanie" in combined or "praca domowa" in combined:
        return "zadanie"
    return "inne"


def normalize_subject_name(subject: str | None) -> str:
    if not subject:
        return ""
    return re.sub(r"\s+", " ", subject.strip().lower())


def normalize_teacher_tokens(teacher_raw: str | None) -> set[str]:
    """Extract lowercase surname/firstname tokens (>=3 chars) for teacher matching."""
    if not teacher_raw:
        return set()
    cleaned = re.sub(r"\[.*?\]|\(.*?\)", " ", teacher_raw)
    tokens = re.findall(r"[a-ząćęłńóśźż]{3,}", cleaned.lower())
    return set(tokens)


def _extract_topic_stems(text: str) -> set[str]:
    """Extract 6-char Polish word stems from text for topical similarity matching."""
    if not text:
        return set()
    extra_stops = {
        "domu",
        "pracy",
        "prace",
        "pracę",
        "czasu",
        "czasie",
        "życia",
        "dzieci",
        "ucznia",
        "uczniów",
        "klasy",
        "klasie",
        "szkoły",
        "szkole",
        "części",
        "część",
        "stopniowo",
        "lektura",
        "lektury",
        "lekturę",
        "lekturą",
        "lekturze",
        "przygotowanie",
        "przygotować",
        "wiadomości",
        "rozdział",
        "rozdziału",
        "rozdziale",
        "dział",
        "działu",
        "dziale",
        "sprawdzian",
        "kartkówka",
        "projekt",
        "zadanie",
        "zadania",
        "dlatego",
        "wszystkich",
        "wszystkie",
        "wszystko",
        "wszyscy",
        "kolejnej",
        "kolejny",
        "kolejne",
        "dodatkowo",
        "rodzicom",
        "rodzice",
        "rodziców",
        "osobie",
        "osoby",
        "formie",
        "jutro",
        "dzisiaj",
        "uwaga",
        "prośbą",
        "proszę",
        "dostępne",
    }
    words = re.findall(r"[a-ząćęłńóśźż]{5,}", text.lower())
    stems: set[str] = set()
    for w in words:
        if w in _STOPWORDS_PL or w in extra_stops:
            continue
        stems.add(w[:6] if len(w) >= 6 else w)
    return stems


def clean_verbatim_description(
    description: str,
    subject: str | None = None,
    category_label: str | None = None,
) -> str:
    """Strip redundant Librus metadata headers from description so only the true scope remains."""
    if not description:
        return ""
    subj_low = (subject or "").strip().lower()
    cat_low = (category_label or "").strip().lower()

    cleaned_lines: list[str] = []
    for raw_line in description.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        low = line.lower()
        # Skip boilerplate metadata lines often repeated in ev.text
        if low.startswith(
            (
                "nr lekcji:",
                "nauczyciel:",
                "rodzaj:",
                "przedmiot:",
                "klasa:",
                "sala:",
                "data dodania:",
                "data:",
                "godzina:",
            )
        ):
            continue
        if low.startswith(("opis:", "zakres:")):
            line = line.split(":", 1)[1].strip()
            low = line.lower()
            if not line:
                continue
        # Skip lines that just repeat "Przedmiot, Kategoria" or "Kategoria"
        if low in (subj_low, cat_low, f"{subj_low}, {cat_low}", f"{cat_low}, {subj_low}"):
            continue
        if low in ("sprawdzian", "kartkówka", "zapowiedziana kartkówka", "projekt", "lektura", "praca klasowa"):
            continue
        cleaned_lines.append(line)

    return "\n".join(cleaned_lines).strip()


def extract_verbatim_requirements(
    description: str,
    subject: str | None = None,
    category_label: str | None = None,
) -> list[str]:
    """Split a teacher's verbatim description into clean requirement items without boilerplate."""
    cleaned_desc = clean_verbatim_description(description, subject, category_label)
    if not cleaned_desc:
        return []
    lines: list[str] = []
    for raw_line in _BULLET_SPLIT_RE.split(cleaned_desc):
        cleaned = raw_line.strip()
        if not cleaned:
            continue
        cleaned = re.sub(r"^[\-\•\*]\s+", "", cleaned).strip()
        if cleaned and cleaned.lower() not in (
            "sprawdzian",
            "kartkówka",
            "zapowiedziana kartkówka",
            "projekt",
            "lektura",
        ):
            lines.append(cleaned)
    return lines


def match_completed_lessons_for_subject(
    subject: str | None,
    exam_day: date,
    completed_lessons: list[dict[str, Any]],
    max_lessons: int = 10,
    *,
    event_type: str = "sprawdzian",
    verbatim_description: str = "",
) -> dict[str, Any]:
    """Select completed lessons that are genuinely part of the tested scope."""
    norm_subj = normalize_subject_name(subject)
    if not norm_subj:
        return {"unit_lessons": [], "all_recent_lessons": []}

    subject_lessons: list[dict[str, Any]] = []
    for item in completed_lessons:
        item_subj = normalize_subject_name(item.get("subject"))
        if item_subj != norm_subj:
            continue
        lesson_date_str = str(item.get("day") or "")
        try:
            lesson_date = date.fromisoformat(lesson_date_str)
        except ValueError:
            continue
        if lesson_date <= exam_day:
            subject_lessons.append(
                {
                    "day": lesson_date_str,
                    "lesson_number": item.get("lesson_number"),
                    "topic": str(item.get("topic") or "").strip(),
                    "teacher": str(item.get("teacher") or "").strip(),
                }
            )

    # Sort chronologically (oldest -> newest) and deduplicate identical topics across the unit
    subject_lessons.sort(key=lambda x: (x["day"], x.get("lesson_number") or 0))

    deduped: list[dict[str, Any]] = []
    topic_index_map: dict[str, int] = {}
    for les in subject_lessons:
        t_key = les["topic"].lower().strip()
        if t_key in topic_index_map:
            existing_idx = topic_index_map[t_key]
            deduped[existing_idx]["count"] = deduped[existing_idx].get("count", 1) + 1
            deduped[existing_idx]["last_day"] = les["day"]
        else:
            entry = dict(les)
            entry["count"] = 1
            topic_index_map[t_key] = len(deduped)
            deduped.append(entry)

    desc_stems = _extract_topic_stems(verbatim_description)

    # For narrow projects or book readings (lektura/projekt), do NOT dump unrelated grammar lessons.
    # Only include lessons that share topical stems with the project/reading description.
    if event_type in ("lektura", "projekt"):
        if desc_stems:
            matching_project_lessons = [
                les
                for les in deduped
                if len(_extract_topic_stems(les["topic"]) & desc_stems) >= 1
            ]
            return {
                "unit_lessons": matching_project_lessons[-max_lessons:],
                "all_recent_lessons": deduped[-max_lessons:],
            }
        return {
            "unit_lessons": [],
            "all_recent_lessons": deduped[-max_lessons:],
        }

    # For a short quiz (kartkówka) with explicit topic stems in description (e.g. "stopniowanie przymiotnika, liczebnik"),
    # select lessons that directly match those topic stems if any exist.
    if event_type == "kartkowka" and desc_stems:
        matching_quiz_lessons = [
            les
            for les in deduped
            if len(_extract_topic_stems(les["topic"]) & desc_stems) >= 1
        ]
        if matching_quiz_lessons:
            return {
                "unit_lessons": matching_quiz_lessons[-max_lessons:],
                "all_recent_lessons": deduped[-max_lessons:],
            }

    # For sprawdzian / kartkówka: find unit start after previous chapter exam
    unit_start_idx = 0
    for idx in range(len(deduped) - 2, -1, -1):
        t_low = deduped[idx]["topic"].lower()
        if (
            "sprawdzian" in t_low
            or "klasówka" in t_low
            or "praca klasowa" in t_low
            or "test " in t_low
        ) and "lektur" not in t_low:
            unit_start_idx = idx + 1
            break

    unit_lessons = deduped[unit_start_idx:]

    # If the teacher already conducted a chapter repetition/summary lesson ("powtórzenie wiadomości", "podsumowanie działu")
    # and then started a NEW chapter before the exam date (e.g. "Ustrój starożytnego Rzymu" after
    # "Powtórzenie wiadomości z rozdziału II" when the exam is on "starożytna Grecja"),
    # cut off any lessons after the repetition lesson unconditionally.
    review_idx = -1
    for idx in range(len(unit_lessons) - 1, -1, -1):
        t_low = unit_lessons[idx]["topic"].lower()
        if (
            "powtórzeni" in t_low
            or "powtorzeni" in t_low
            or "podsumowanie działu" in t_low
            or "podsumowanie rozdziału" in t_low
        ):
            review_idx = idx
            break

    if review_idx != -1 and review_idx < len(unit_lessons) - 1:
        unit_lessons = list(unit_lessons[: review_idx + 1])

    # For a short quiz (kartkówka), keep only the most recent 4 lessons
    limit = 4 if event_type == "kartkowka" else max_lessons
    unit_lessons = unit_lessons[-limit:]

    return {
        "unit_lessons": unit_lessons,
        "all_recent_lessons": deduped[-max_lessons:],
    }


def match_messages_and_materials(
    subject: str | None,
    teacher: str | None,
    messages: list[dict[str, Any]],
    homework_items: list[dict[str, Any]],
    *,
    announcements: list[dict[str, Any]] | None = None,
    exam_day: date | None = None,
    event_type: str = "sprawdzian",
    verbatim_description: str = "",
    unit_lessons: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Find ONLY messages, homework, and announcements that genuinely concern this exam's requirements."""
    teacher_tokens = normalize_teacher_tokens(teacher)
    norm_subj = normalize_subject_name(subject)

    # Build topical stem set from the primary topic line of exam description and unit lessons
    first_desc_line = verbatim_description.splitlines()[0] if verbatim_description else ""
    exam_stems = _extract_topic_stems(first_desc_line) or _extract_topic_stems(verbatim_description)
    lesson_stems: set[str] = set()
    for les in unit_lessons or []:
        lesson_stems |= _extract_topic_stems(str(les.get("topic") or ""))
    all_scope_stems = exam_stems | lesson_stems

    matched_messages: list[dict[str, Any]] = []
    seen_msg_keys: set[str] = set()
    extracted_codes: list[dict[str, str]] = []
    extracted_links: list[str] = []

    for msg in messages:
        sender = str(msg.get("correspondent") or msg.get("sender") or "").strip()
        sender_tokens = normalize_teacher_tokens(sender)
        subj_line = str(msg.get("subject") or "").strip()
        body_text = str(msg.get("body") or msg.get("preview") or "").strip()
        atts = msg.get("attachments") or []
        att_texts = [
            str(a.get("extracted_text") or "").strip()
            for a in atts
            if str(a.get("extracted_text") or "").strip()
        ]
        full_msg_text = "\n\n".join([t for t in [body_text] + att_texts if t]).strip()
        subj_low = subj_line.lower()
        combined_low = f"{subj_line} {full_msg_text}".lower()

        # 1. Hard filter out behavior remarks, trips, attendance notices, open days, etc.
        if any(pat in subj_low for pat in _IRRELEVANT_MSG_SUBJECT_PATTERNS):
            continue
        if any(
            pat in combined_low[:160]
            for pat in ("dodano uwagę dla ucznia", "uwaga dla ucznia", "informacja o uwadze")
        ):
            continue

        # 1b. Do not attach an older short-quiz ("kartkówka") message to a chapter exam ("sprawdzian") or vice versa
        if event_type == "sprawdzian" and ("kartkówk" in subj_low or "kartkowk" in subj_low) and "sprawdzian" not in subj_low:
            continue
        if event_type == "kartkowka" and "sprawdzian" in subj_low and not ("kartkówk" in subj_low or "kartkowk" in subj_low):
            continue

        # 2. Check date proximity if exam_day and sent_at are parseable
        sent_at_raw = str(msg.get("sent_at") or "").strip()
        if exam_day and len(sent_at_raw) >= 10:
            try:
                msg_date = date.fromisoformat(sent_at_raw[:10])
                days_diff = (exam_day - msg_date).days
                # Message sent after exam or more than 30 days before exam is not about this exam
                if days_diff < -1 or days_diff > 30:
                    continue
            except ValueError:
                pass

        teacher_match = bool(teacher_tokens and len(teacher_tokens & sender_tokens) >= 2)
        subject_match = bool(norm_subj and len(norm_subj) >= 4 and norm_subj in combined_low)

        if not (teacher_match or subject_match):
            continue

        # 3. Even if sent by the same teacher, the message MUST actually be about exam/quiz/project requirements!
        # Exclude contest/olympiad/tournament invitations unless explicitly about a classroom exam/quiz
        contest_kws = ("konkurs", "olimpiad", "turniej", "zaproszeni", "zapraszamy", "zawody", "wolontariat", "bieg")
        msg_stems = _extract_topic_stems(f"{subj_line} {full_msg_text}")
        subj_stems = _extract_topic_stems(subj_line)
        strict_assessment_kws = (
            "sprawdzian",
            "kartkówk",
            "kartkowk",
            "klasówk",
            "klasowk",
            "praca klasowa",
            "zagadnienia",
            "wymagania",
            "zakres materiału",
            "zakres materialu",
            "do powtórzenia",
            "do powtorzenia",
            "kryteria",
        )
        has_strict_exam_kw = any(kw in combined_low for kw in strict_assessment_kws)
        has_subj_exam_kw = any(kw in subj_low for kw in strict_assessment_kws)
        if any(ck in subj_low for ck in contest_kws) and not has_subj_exam_kw:
            continue
        stem_overlap = msg_stems & all_scope_stems

        # If exam has specific description stems (e.g. "Tajemnica domu w Bielinach", "stopniowanie przymiotnika"),
        # require topical overlap in message subject, >=2 matching stems in body, or an assessment keyword + stem overlap.
        if exam_stems:
            exam_overlap = msg_stems & exam_stems
            is_relevant = (
                bool(subj_stems & exam_stems)
                or len(exam_overlap) >= 2
                or (has_strict_exam_kw and len(exam_overlap) >= 1)
                or (has_subj_exam_kw and len(stem_overlap) >= 1)
            )
        else:
            is_relevant = has_strict_exam_kw and (len(stem_overlap) >= 1 or has_subj_exam_kw)

        if not is_relevant:
            continue

        # Deduplicate identical messages (e.g. sent twice or present in both parent & student inbox)
        dedup_key = f"{sender.lower()}|{subj_low}|{body_text[:100].lower()}"
        if dedup_key in seen_msg_keys:
            continue
        seen_msg_keys.add(dedup_key)

        codes = _CODE_RE.findall(f"{subj_line} {full_msg_text}")
        links = _URL_RE.findall(f"{subj_line} {full_msg_text}")
        for c in codes:
            if not any(ec["code"] == c for ec in extracted_codes):
                extracted_codes.append(
                    {"code": c, "source": subj_line or sender, "teacher": sender}
                )
        for lnk in links:
            if lnk not in extracted_links:
                extracted_links.append(lnk)

        matched_messages.append(
            {
                "id": msg.get("id"),
                "sent_at": msg.get("sent_at"),
                "sender": sender,
                "subject": subj_line,
                "excerpt": (full_msg_text[:420] + "…") if len(full_msg_text) > 420 else full_msg_text,
                "body": body_text,
                "full_text": full_msg_text,
                "account_source": msg.get("account_source") or "rodzic",
                "has_attachment": bool(msg.get("has_attachment")),
                "attachments": atts,
                "codes": codes,
                "links": links,
            }
        )

    matched_homework: list[dict[str, Any]] = []
    for hw in homework_items:
        hw_subj = normalize_subject_name(hw.get("subject"))
        hw_teacher_tokens = normalize_teacher_tokens(hw.get("teacher"))
        if (norm_subj and hw_subj == norm_subj) or (
            teacher_tokens and len(teacher_tokens & hw_teacher_tokens) >= 2
        ):
            hw_text = f"{hw.get('topic', '')} {hw.get('category', '')}".lower()
            hw_stems = _extract_topic_stems(hw_text)
            if any(kw in hw_text for kw in _EXAM_SCOPE_KEYWORDS) or (hw_stems & all_scope_stems):
                matched_homework.append(hw)

    matched_announcements: list[dict[str, Any]] = []
    contest_ignore_kws = ("konkurs", "olimpiad", "turniej", "zaproszeni", "zapraszamy", "zawody", "wolontariat", "bieg")
    strict_ann_exam_kws = (
        "sprawdzian",
        "kartkówk",
        "kartkowk",
        "klasówk",
        "klasowk",
        "praca klasowa",
        "zagadnienia",
        "wymagania",
        "zakres materiału",
        "zakres materialu",
    )
    for ann in announcements or []:
        ann_blob = f"{ann.get('title', '')} {ann.get('content', '')}".lower()
        if any(ck in ann_blob for ck in contest_ignore_kws):
            continue
        if norm_subj and len(norm_subj) >= 4 and norm_subj in ann_blob:
            if any(kw in ann_blob for kw in strict_ann_exam_kws):
                matched_announcements.append(ann)

    return {
        "messages": matched_messages[:5],
        "homework": matched_homework[:5],
        "announcements": matched_announcements[:3],
        "codes": extracted_codes,
        "links": extracted_links[:6],
    }


_GREETING_LINE_PREFIXES = (
    "dzień dobry",
    "dzien dobry",
    "szanowni państwo",
    "szanowni panstwo",
    "drodzy rodzice",
    "drodzy uczniowie",
    "moi drodzy",
    "kochane dzieci",
    "witam",
    "cześć",
    "czesc",
    "pozdrawiam",
    "z poważaniem",
    "z powazaniem",
    "serdecznie pozdrawiam",
)


def _is_greeting_or_signature_line(line: str, sender: str = "") -> bool:
    """Return True if a line is a pure greeting or teacher signature."""
    s = line.strip(" ,.!:-–•*\t")
    if not s:
        return True
    low = s.lower()
    if low in _GREETING_LINE_PREFIXES or low.startswith(_GREETING_LINE_PREFIXES):
        if len(s) <= 42:
            return True
    # Check teacher signature (e.g. "A. Klimczak", "Agnieszka Klimczak", "Klimczak Agnieszka")
    words = s.split()
    if 1 <= len(words) <= 3 and len(s) <= 36:
        sender_toks = normalize_teacher_tokens(sender)
        line_toks = normalize_teacher_tokens(s)
        if sender_toks and line_toks and (line_toks <= sender_toks or len(line_toks & sender_toks) >= 1):
            return True
    return False


def format_teacher_guideline_blocks(rel_msgs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Format matched teacher messages and extracted attachment text into cohesive, indented guideline blocks."""
    blocks: list[dict[str, Any]] = []
    num_re = re.compile(r"^(\d+[\.\)])\s+(.*)$")
    alpha_re = re.compile(r"^([a-zA-Z][\)\.])\s+(.*)$")
    dash_re = re.compile(r"^([\-\•\*–])\s*(.*)$")

    for m in rel_msgs:
        sender = str(m.get("sender") or m.get("correspondent") or "").strip()
        subj = str(m.get("subject") or "").strip()
        sent_at = str(m.get("sent_at") or "").strip()
        acc_src = str(m.get("account_source") or "rodzic").strip()
        body = str(m.get("body") or "").strip()
        atts = m.get("attachments") or []

        att_filenames: list[str] = []
        att_texts: list[str] = []
        for a in atts:
            fn = str(a.get("filename") or "").strip()
            if fn:
                att_filenames.append(fn)
            et = str(a.get("extracted_text") or "").strip()
            if et:
                att_texts.append(et)

        def _parse_lines_to_structured(raw_text: str) -> list[dict[str, str]]:
            items: list[dict[str, str]] = []
            for raw_l in raw_text.splitlines():
                s = raw_l.strip()
                if not s:
                    continue
                if s.lower().startswith("--- cytowana wiadomość ---"):
                    break
                if _is_greeting_or_signature_line(s, sender):
                    continue
                m_num = num_re.match(s)
                if m_num:
                    items.append(
                        {"level": "numbered", "marker": m_num.group(1), "text": m_num.group(2).strip()}
                    )
                    continue
                m_alpha = alpha_re.match(s)
                if m_alpha:
                    items.append(
                        {"level": "subitem_alpha", "marker": m_alpha.group(1), "text": m_alpha.group(2).strip()}
                    )
                    continue
                m_dash = dash_re.match(s)
                if m_dash and m_dash.group(2).strip():
                    items.append(
                        {"level": "subitem_dash", "marker": "–", "text": m_dash.group(2).strip()}
                    )
                    continue
                items.append({"level": "paragraph", "marker": "", "text": s})
            return items

        body_items = _parse_lines_to_structured(body) if body else []
        raw_att_items: list[dict[str, str]] = []
        for et in att_texts:
            raw_att_items.extend(_parse_lines_to_structured(et))

        # Separate motivational/header/footer lines (e.g. "Pamiętaj!", "Powodzenia!", "Zagadnienia do sprawdzianu: ...")
        attachment_items: list[dict[str, str]] = []
        footer_tips: list[str] = []
        attachment_heading = ""
        for it in raw_att_items:
            lvl = it.get("level", "paragraph")
            txt = it.get("text", "").strip()
            low = txt.lower().strip(" !.:;-")
            if lvl == "paragraph":
                if low in ("pamiętaj", "pamietaj", "powodzenia", "miłej nauki", "milej nauki", "uwaga"):
                    continue
                if low.startswith(("przeczytaj cały rozdział", "przeczytaj caly rozdzial", "zrób notatki", "zrob notatki", "pamiętaj, aby", "pamietaj, aby")):
                    footer_tips.append(txt)
                    continue
                if not attachment_items and low.startswith(("zagadnienia do sprawdzianu", "wymagania na sprawdzian", "zakres materiału", "zakres materialu")):
                    attachment_heading = txt.rstrip(":")
                    continue
            attachment_items.append(it)

        if not body_items and not attachment_items and not subj:
            continue

        blocks.append(
            {
                "message_id": m.get("id"),
                "sender": sender,
                "subject": subj,
                "sent_at": sent_at,
                "account_source": acc_src,
                "account_label": "KONTO UCZNIA" if acc_src == "uczeń" else "KONTO RODZICA",
                "attachment_filenames": att_filenames,
                "attachment_heading": attachment_heading,
                "footer_tip": " ".join(footer_tips),
                "body_items": body_items,
                "attachment_items": attachment_items,
            }
        )
    return blocks


def build_synthesized_requirements(
    *,
    subject: str,
    category_label: str,
    category_type: str,
    verbatim_reqs: list[str],
    unit_lessons: list[dict[str, Any]],
    matched_mats: dict[str, Any],
) -> dict[str, Any]:
    """Synthesize a clean, minimal 'Clue' of exam requirements from Terminarz, Messages, Announcements & Lessons."""
    sources: list[str] = []
    terminarz_points: list[dict[str, str]] = []
    seen_low: set[str] = set()

    # 1. Verbatim scope from Terminarz
    if verbatim_reqs:
        sources.append("Terminarz")
        for r in verbatim_reqs:
            low = r.lower().strip()
            if low and low not in seen_low:
                seen_low.add(low)
                terminarz_points.append({"text": r, "source": "Terminarz"})

    # 2. Cohesive teacher guideline blocks from matched teacher/student messages + attachments
    rel_msgs = matched_mats.get("messages") or []
    teacher_guidelines = format_teacher_guideline_blocks(rel_msgs)
    if teacher_guidelines:
        has_student_msg = any(b.get("account_source") == "uczeń" for b in teacher_guidelines)
        has_att = any(bool(b.get("attachment_items")) for b in teacher_guidelines)
        if has_student_msg and has_att:
            sources.append("Wytyczne + załącznik (konto ucznia)")
        elif has_student_msg:
            sources.append("Wiadomość (konto ucznia)")
        else:
            sources.append("Wiadomość nauczyciela")

    # 3. Matched homework / announcements
    for hw in matched_mats.get("homework") or []:
        topic = str(hw.get("topic") or "").strip()
        if topic and topic.lower() not in seen_low:
            seen_low.add(topic.lower())
            if "Zadania" not in sources:
                sources.append("Zadania")
            terminarz_points.append({"text": topic, "source": "Zadanie"})

    for ann in matched_mats.get("announcements") or []:
        title = str(ann.get("title") or "").strip()
        if title and title.lower() not in seen_low:
            seen_low.add(title.lower())
            if "Ogłoszenia" not in sources:
                sources.append("Ogłoszenia")
            terminarz_points.append({"text": title, "source": "Ogłoszenie"})

    # 4. Relevant completed lesson topics from that unit
    lesson_topics: list[str] = []
    for les in unit_lessons:
        t = str(les.get("topic") or "").strip()
        if t and t.lower() not in seen_low:
            lesson_topics.append(t)

    if lesson_topics:
        sources.append(f"Zrealizowane lekcje ({len(unit_lessons)})")
        # Only add lesson topics into terminarz_points if neither Terminarz nor teacher_guidelines gave detailed points
        if len(terminarz_points) <= 2 and not teacher_guidelines:
            for les in unit_lessons[-6:]:
                t = str(les.get("topic") or "").strip()
                if t and t.lower() not in seen_low:
                    seen_low.add(t.lower())
                    terminarz_points.append(
                        {"text": f"{t} (lekcja {les['day']})", "source": "Zrealizowane lekcje"}
                    )

    # Build concise 1-2 line clue for the main table
    if verbatim_reqs or teacher_guidelines:
        clue_parts = [bp["text"] for bp in terminarz_points[:3]]
        for blk in teacher_guidelines:
            att_count = sum(1 for it in blk.get("attachment_items", []) if it.get("level") == "numbered")
            if att_count > 0:
                clue_parts.append(f"{att_count} zagadnień z załącznika ({blk['account_label'].lower()})")
            elif blk.get("subject"):
                clue_parts.append(blk["subject"])
        clue_text = " · ".join(clue_parts[:4])
        has_explicit_scope = True
    elif lesson_topics:
        recent_str = " · ".join(lesson_topics[-3:])
        clue_text = f"Brak opisu w Terminarzu — zakres z ostatnich lekcji: {recent_str}"
        has_explicit_scope = False
    else:
        clue_text = "Nauczyciel nie wpisał szczegółowego zakresu w Terminarzu ani w wiadomościach."
        has_explicit_scope = False

    return {
        "clue_text": clue_text,
        "has_explicit_scope": has_explicit_scope,
        "sources": sources,
        "requirement_points": terminarz_points[:12],
        "teacher_guidelines": teacher_guidelines,
        "lesson_topics": lesson_topics,
    }


def build_deterministic_checklist(
    exam_id: str,
    verbatim_requirements: list[str],
    unit_lessons: list[dict[str, Any]],
    matched_materials: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build a study checklist for the Karta Nauki modal grounded strictly in actual requirements."""
    checklist: list[dict[str, Any]] = []
    seen_norm: set[str] = set()

    # 1. Every verbatim requirement from Terminarz comes first
    for idx, req in enumerate(verbatim_requirements):
        norm = req.lower().strip()
        if not norm or norm in seen_norm:
            continue
        seen_norm.add(norm)
        item_hash = hashlib.sha1(f"{exam_id}:req:{idx}:{norm}".encode("utf-8")).hexdigest()[:10]
        checklist.append(
            {
                "id": f"req-{item_hash}",
                "text": req,
                "source": "Terminarz",
                "badge": "Terminarz",
                "mandatory": True,
            }
        )

    # 2. Structured requirements from teacher guidelines & extracted attachments (without greetings/signatures/filler)
    guideline_blocks = format_teacher_guideline_blocks(matched_materials.get("messages") or [])
    for b_idx, blk in enumerate(guideline_blocks):
        src_label = (
            "Załącznik / Wiadomość (konto ucznia)"
            if blk.get("account_source") == "uczeń"
            else "Wiadomość nauczyciela"
        )
        badge_label = "Konto ucznia" if blk.get("account_source") == "uczeń" else "Wytyczne"

        # If the block has attachment items (e.g. 1..10 study topics), keep sub-items as structured sublists
        att_items = blk.get("attachment_items") or []
        if att_items:
            grouped_topics: list[dict[str, Any]] = []
            for it in att_items:
                lvl = it.get("level")
                txt = it.get("text", "").strip()
                mrk = it.get("marker", "").strip()
                if not txt:
                    continue
                if lvl == "numbered":
                    grouped_topics.append(
                        {"marker": mrk, "text": f"{mrk} {txt}".strip(), "subitems": []}
                    )
                elif lvl in ("subitem_alpha", "subitem_dash") and grouped_topics:
                    grouped_topics[-1]["subitems"].append(
                        {"level": lvl, "marker": mrk or "–", "text": txt}
                    )
                else:
                    grouped_topics.append(
                        {"marker": "", "text": txt, "subitems": []}
                    )

            for t_idx, topic_obj in enumerate(grouped_topics):
                topic_str = topic_obj["text"]
                low = topic_str.lower().strip()
                if low in seen_norm:
                    continue
                seen_norm.add(low)
                item_hash = hashlib.sha1(f"{exam_id}:att:{b_idx}:{t_idx}:{low}".encode("utf-8")).hexdigest()[:10]
                checklist.append(
                    {
                        "id": f"att-{item_hash}",
                        "text": topic_str,
                        "subitems": topic_obj["subitems"],
                        "source": src_label,
                        "badge": badge_label,
                        "mandatory": True,
                    }
                )
        else:
            # Otherwise extract substantive sentences/items from body_items (skipping conversational intro)
            for l_idx, it in enumerate(blk.get("body_items") or []):
                txt = it.get("text", "").strip()
                mrk = it.get("marker", "").strip()
                full_line = f"{mrk} {txt}".strip() if mrk else txt
                if len(full_line) < 8:
                    continue
                low = full_line.lower()
                if low in seen_norm:
                    continue
                seen_norm.add(low)
                item_hash = hashlib.sha1(f"{exam_id}:msg:{b_idx}:{l_idx}:{low}".encode("utf-8")).hexdigest()[:10]
                checklist.append(
                    {
                        "id": f"msg-{item_hash}",
                        "text": full_line,
                        "source": src_label,
                        "badge": badge_label,
                        "mandatory": True,
                    }
                )

    # 3. Completed lesson topics from the tested unit/chapter
    for idx, les in enumerate(unit_lessons):
        topic = str(les.get("topic") or "").strip()
        if not topic:
            continue
        norm = topic.lower()
        if norm in seen_norm:
            continue
        seen_norm.add(norm)
        count_suffix = f" ({les['count']} godz.)" if les.get("count", 1) > 1 else ""
        item_hash = hashlib.sha1(f"{exam_id}:les:{idx}:{norm}".encode("utf-8")).hexdigest()[:10]
        checklist.append(
            {
                "id": f"les-{item_hash}",
                "text": f"{topic}{count_suffix} (lekcja {les['day']})",
                "source": "Zrealizowane lekcje",
                "badge": "Lekcja",
                "mandatory": True,
            }
        )

    # 4. Any textbook codes from relevant messages
    for idx, code_item in enumerate(matched_materials.get("codes") or []):
        c_val = code_item["code"]
        item_hash = hashlib.sha1(f"{exam_id}:code:{idx}:{c_val}".encode("utf-8")).hexdigest()[:10]
        checklist.append(
            {
                "id": f"mat-{item_hash}",
                "text": f"Kod dostępu ({code_item['source']}): {c_val}",
                "source": "Materiały nauczyciela",
                "badge": "Kod",
                "mandatory": False,
            }
        )

    return checklist


_SUBJECT_KEYWORDS_MAP: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Matematyka", ("matematyk", "arytmetyk", "geometri", "ułamk", "liczby")),
    ("Język polski", ("język polski", "j. polski", "polski", "lektur", "dyktand", "ortograf", "gramatyk")),
    ("Język angielski", ("angielsk", "english")),
    ("Język niemiecki", ("niemieck", "deutsch", "austriac")),
    ("Język hiszpański", ("hiszpańsk", "espanol")),
    ("Historia", ("histori", "starożytn", "średniowiecz", "król", "wojna", "rzym", "grecj", "wiedeńsk", "powstani")),
    ("Biologia", ("biologi", "przyrod", "komórk", "organizm", "ekologi")),
    ("Geografia", ("geografi", "mapa", "klimat", "kontynent", "wszechświe", "ziemia")),
    ("Fizyka", ("fizyk", "prędkoś", "siła", "energi")),
    ("Chemia", ("chemi", "pierwiastk", "reakcj")),
    ("Informatyka", ("informatyk", "komputer", "programowani", "scratch", "python")),
    ("Muzyka", ("muzyk", "piosenk", "nuty", "śpiew")),
    ("Plastyka", ("plastyk", "rysunek", "malowani", "farby", "blok rysunkowy")),
    ("Technika", ("technik", "konstrukcj", "narzędzi")),
    ("Wychowanie fizyczne", ("wychowanie fizyczne", "wf", "basen", "pływalni", "zawody", "turniej", "koszykówk", "piłka")),
    ("Religia / Etyka", ("religi", "etyk", "rekolekcj", "kościół")),
    ("Wycieczka / Wydarzenie", ("wycieczk", "kino", "multikino", "teatr", "zielona szkoła", "tepfactory", "muzeum", "dyskoteka")),
    ("Świetlica / Obiady", ("świetlic", "swietlic", "obiad", "stołówk")),
    ("Biblioteka / Konkurs", ("bibliotek", "konkurs", "olimpiad")),
)

_DATE_DM_RE = re.compile(r"(?<!\d)(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?(?!\d)")
_MONTHS_GEN_PL = {
    "stycznia": 1,
    "lutego": 2,
    "marca": 3,
    "kwietnia": 4,
    "maja": 5,
    "czerwca": 6,
    "lipca": 7,
    "sierpnia": 8,
    "września": 9,
    "października": 10,
    "listopada": 11,
    "grudnia": 12,
}


def synthesize_action_item(
    *,
    kind: str,
    title: str,
    body: str,
    sender: str = "",
    subject: str = "",
    published_on: str = "",
    due_on: str = "",
    unread: bool = False,
    has_attachment: bool = False,
    codes: list[str] | None = None,
    links: list[str] | None = None,
    account_source: str = "rodzic",
    teacher_subject_map: dict[str, str] | None = None,
    ref_today: date | None = None,
) -> dict[str, Any]:
    """Synthesize a raw message, announcement, homework, or event into a compact Action Item row."""
    today_d = ref_today or date.today()
    clean_title = (title or "").strip()
    raw_body = (body or "").strip()

    # 1. Resolve Subject / Category label
    resolved_subject = (subject or "").strip()
    if not resolved_subject and sender and teacher_subject_map:
        sender_toks = normalize_teacher_tokens(sender)
        for t_name, t_subj in teacher_subject_map.items():
            if sender_toks and len(sender_toks & normalize_teacher_tokens(t_name)) >= 2:
                resolved_subject = t_subj
                break

    combined_low = f"{clean_title} {raw_body}".lower()
    if not resolved_subject:
        for label, kw_tuple in _SUBJECT_KEYWORDS_MAP:
            if any(kw in combined_low for kw in kw_tuple):
                resolved_subject = label
                break

    if not resolved_subject:
        if kind == "announcement":
            resolved_subject = "Ogłoszenie szkolne"
        elif kind == "homework":
            resolved_subject = "Zadanie domowe"
        else:
            resolved_subject = "Sprawy klasowe"

    # 2. Compute publication date & strip quoted blocks before extracting deadlines
    pub_d: date | None = None
    published_age_days: int = 999
    if published_on and len(published_on) >= 10:
        try:
            pub_d = date.fromisoformat(published_on[:10])
            published_age_days = (today_d - pub_d).days
        except ValueError:
            pass

    unquoted_body = re.split(
        r"(?:---\s*Cytowana wiadomość\s*---|Użytkownik:[^\n]+napisał:|-{5,}\s*Przekazywana wiadomość)",
        raw_body,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0].strip()

    cleaned_lines: list[str] = []
    for line in unquoted_body.splitlines():
        s = line.strip()
        if not s:
            continue
        if _is_greeting_or_signature_line(s, sender):
            continue
        cleaned_lines.append(s)

    condensed_body = " ".join(cleaned_lines)
    condensed_body = re.sub(r"\s+", " ", condensed_body).strip()
    fresh_low = f"{clean_title} {condensed_body}".lower()
    title_low = clean_title.lower()

    # Extract target deadline / event date if mentioned in text.
    # Critical safeguard: never extract a date from message/announcement text that is earlier
    # than the message's own publication date (e.g. a teacher writing on Sept 29 about a grade from Sept 15,
    # or a historical year like "16 września 1877 roku").
    extracted_deadline = (due_on or "").strip()
    is_explicit_deadline = bool(extracted_deadline)
    if not extracted_deadline:
        candidate_dates: list[date] = []
        scan_text = f"{clean_title} {condensed_body[:450]}"
        for m in _DATE_DM_RE.finditer(scan_text):
            d_num, m_num, y_raw = int(m.group(1)), int(m.group(2)), m.group(3)
            if 1 <= d_num <= 31 and 1 <= m_num <= 12:
                year = int(y_raw) if y_raw else today_d.year
                if year < 100:
                    year += 2000
                try:
                    cand = date(year, m_num, d_num)
                except ValueError:
                    continue
                if pub_d is not None and cand < pub_d:
                    continue
                if -90 <= (cand - today_d).days <= 180:
                    candidate_dates.append(cand)

        for m_name, m_idx in _MONTHS_GEN_PL.items():
            for m_word in re.finditer(
                rf"(?<!\d)(\d{{1,2}})\s+{m_name}(?:\s+(\d{{4}}))?(?!\w)",
                scan_text.lower(),
            ):
                d_num = int(m_word.group(1))
                y_explicit = int(m_word.group(2)) if m_word.group(2) else today_d.year
                try:
                    cand = date(y_explicit, m_idx, d_num)
                except ValueError:
                    continue
                if pub_d is not None and cand < pub_d:
                    continue
                if -90 <= (cand - today_d).days <= 180:
                    candidate_dates.append(cand)

        if candidate_dates:
            # Prefer the earliest upcoming date (>= today_d); if all are in the past, take the latest one
            upcoming_cands = [c for c in candidate_dates if c >= today_d]
            chosen_date = min(upcoming_cands) if upcoming_cands else max(candidate_dates)
            extracted_deadline = chosen_date.isoformat()
            is_explicit_deadline = True

    display_date = extracted_deadline or (published_on[:10] if published_on else "")

    # 3. Build a crisp 1-2 sentence action item summary from cleaned lines

    sentences = re.split(r"(?<=[.!?])\s+", condensed_body) if condensed_body else []
    action_sentences: list[str] = []
    for sent in sentences:
        s_low = sent.lower()
        if any(
            kw in s_low
            for kw in (
                "proszę",
                "prosze",
                "należy",
                "nalezy",
                "przypominam",
                "obowiązuje",
                "obowiazuje",
                "obowiązują",
                "termin",
                "przynieść",
                "przygotować",
                "wpłat",
                "zgod",
                "zapisy",
                "konkurs",
                "wycieczk",
                "sprawdzian",
                "kartkówk",
                "kod",
                "odwołan",
            )
        ):
            action_sentences.append(sent)
            if len(" ".join(action_sentences)) >= 145:
                break

    if action_sentences:
        summary_text = " ".join(action_sentences)
    elif condensed_body:
        summary_text = condensed_body
    else:
        summary_text = clean_title

    if len(summary_text) > 165:
        summary_text = summary_text[:162].rstrip(" ,;:-") + "…"

    # 4. Classify into 4 clear semantic clusters & determine priority
    # Cluster 1: action_required (Wymaga działania — zadania, zapisy, wpłaty, zgody)
    # Cluster 2: materials_and_changes (Informacyjne — materiały do nauki, kody dostępu i zmiany w planie)
    # Cluster 3: optional_competition (Opcjonalne — konkursy, zawody, wolontariat, zaproszenia)
    # Cluster 4: organizational_info (Komunikaty szkolne i archiwalne)
    days_to_deadline: int | None = None
    if is_explicit_deadline and len(extracted_deadline) == 10:
        try:
            d_obj = date.fromisoformat(extracted_deadline)
            days_to_deadline = (d_obj - today_d).days
        except ValueError:
            pass

    # Check if an event/deadline is already in the past
    is_expired = (
        (days_to_deadline is not None and days_to_deadline < 0)
        or (
            not is_explicit_deadline
            and published_age_days > 3
            and any(
                kw in fresh_low
                for kw in (
                    "dzisiaj",
                    "w dniu dzisiejszym",
                    "jutro",
                    "jutrzejsz",
                    "na najbliższej lekcji",
                    "najbliższej lekcji",
                )
            )
        )
        or (
            not is_explicit_deadline
            and published_age_days > 7
            and any(
                kw in fresh_low
                for kw in (
                    "w tym tygodniu",
                    "w przyszłym tygodniu",
                    "do poniedziałku",
                    "do wtorku",
                    "do środy",
                    "do czwartku",
                    "do piątku",
                )
            )
        )
        or (
            not is_explicit_deadline
            and published_age_days >= 14
            and not bool(codes)
            and any(
                kw in title_low
                for kw in (
                    "kartkówk",
                    "kartkowk",
                    "sprawdzian",
                    "zagadnienia do sprawdzianu",
                    "wymagania do sprawdzianu",
                    "wymagania do kartkówki",
                )
            )
        )
    )

    # General school-wide safety/principal communiqué, catering, or photo notice
    is_general_communique = (
        any(
            kw in title_low
            for kw in (
                "stan bezpieczeństwa",
                "aktualna informacja dotycząca bezpieczeństwa",
                "z życia szkoły",
                "komunikat miasta",
                "komunikat mazowieckiej policji",
                "podsumowanie dnia",
                "małe podsumowanie",
                "podsumowanie wydarzenia",
                "bieżące działania i kolejne kroki",
                "organizacji pracy szkoły i bezpieczeństwa",
                "radykalizacja młodzieży",
                "szkolenie z bezpieczeństwa",
                "informacje dotyczące zebrań",
                "codzienne doświadczenia",
                "automat",
                "obiady",
                "zdjęcia",
                "uwagi",
            )
        )
        or ("świetlic" in title_low and "konkurs" not in title_low)
    )

    # Informational schedule changes / consultation cancellations / pool cancellations
    is_schedule_change = any(
        kw in title_low for kw in ("basen", "konsultacje", "nieobecność", "odwołan", "zastępstw", "zmiana terminu konsultacji")
    ) or any(
        kw in fresh_low
        for kw in (
            "odwołan",
            "odwołuję",
            "nie odbędą się",
            "nie odbędzie się",
            "nie jadą na basen",
            "nie ma basenu",
            "zmiana planu",
            "zmiana godzin konsultacji",
        )
    )

    # Optional competitions, olympiads, tournaments, volunteering, extra lectures
    is_optional_invitation = (
        kind != "homework"
        and not is_general_communique
        and not is_schedule_change
        and "obowiązkow" not in fresh_low
        and (
            any(
                kw in title_low
                for kw in (
                    "konkurs",
                    "olimpiad",
                    "instalogik",
                    "wolontariat",
                    "turniej",
                    "zaproszenie",
                    "zawody",
                    "dla zainteresowanych",
                    "dla chętnych",
                )
            )
            or any(
                kw in fresh_low
                for kw in (
                    "zapraszamy ucz",
                    "zapraszamy do udziału",
                    "zapraszam do udziału",
                    "zapraszam uczniów",
                    "dla chętnych",
                    "chętnych uczniów",
                    "konkursie",
                    "olimpiadzie",
                    "bieg z pazurem",
                )
            )
        )
    )

    # Study materials, exam guidelines, or textbook codes sent by teachers
    is_study_material_or_code = (
        not is_optional_invitation
        and not is_general_communique
        and (
            bool(codes)
            or "e-podręczniki" in title_low
            or "kod gwo" in title_low
            or "kody do podręcznika" in title_low
            or any(
                kw in title_low
                for kw in (
                    "zagadnienia do sprawdzianu",
                    "wymagania do sprawdzianu",
                    "wymagania do kartkówki",
                    "kartkówka",
                    "kartkowka",
                    "sprawdzian",
                    "słynni austriac",
                    "ćwiczenia interaktywne",
                    "link do ćwiczeń",
                    "materiały",
                    "notatka",
                )
            )
            or (
                bool(links)
                and account_source == "uczeń"
                and not any(kw in title_low for kw in ("konkurs", "olimpiad", "instalogik", "zaproszenie"))
            )
        )
    )

    # Conversational Re: thread vs active appointment/payment confirmation
    is_conversational_reply = title_low.startswith("re:")
    is_active_confirmation = (
        not is_expired
        and published_age_days <= 10
        and len(condensed_body) >= 6
        and not any(
            kw in fresh_low
            for kw in (
                "wstawię ci ocen",
                "nieprzygotowani",
                "v-ce przewodnicz",
                "przewodniczącym klasy",
                "usunęłam uwag",
            )
        )
        and (
            (
                is_conversational_reply
                and any(
                    kw in title_low
                    for kw in ("dzień otwarty", "dzien otwarty", "spotkanie", "konsultacje", "zebranie")
                )
            )
            or any(
                kw in fresh_low
                for kw in (
                    "potwierdzam",
                    "potwierdzen",
                    "zaksięgowan",
                    "otrzymaliśmy wpłat",
                    "wpłata została",
                    "opłacono",
                    "dziękujemy za wpłat",
                )
            )
        )
    )

    # True actionable obligation requiring execution, signup, payment, or consent (and NOT expired!)
    has_active_obligation = (
        not is_expired
        and not is_general_communique
        and not is_optional_invitation
        and not is_schedule_change
        and not is_study_material_or_code
        and not is_conversational_reply
        and (
            kind in ("homework", "event", "trip")
            or any(
                kw in title_low
                for kw in (
                    "dzień otwarty",
                    "dzien otwarty",
                    "zebranie",
                    "wycieczk",
                    "multikino",
                    "kino",
                    "teatr",
                    "zielona szkoła",
                    "praca na lekcji na ocenę",
                )
            )
            or (
                published_age_days <= 21
                and any(
                    kw in fresh_low
                    for kw in (
                        "zapisy na spotkania",
                        "proszę o przyniesienie",
                        "proszę przynieść",
                        "proszę o wpłat",
                        "proszę o podpis",
                        "proszę o wypełnienie",
                        "należy przynieść",
                        "będą wykonywać na ocenę",
                        "wpłat",
                        "opłat",
                        "składk",
                        "zgoda rodzic",
                        "zgodę rodzic",
                        "zgody rodzic",
                        "deklaracj",
                        "usprawiedliw",
                        "odebrać",
                    )
                )
            )
        )
    )

    if has_active_obligation:
        cluster = "action_required"
        cluster_label = "Wymaga działania — do dopilnowania lub przekazania dziecku"
        cluster_order = 1
        tag_code = "action_required"
        priority = "urgent"
        priority_label = "WYMAGA DZIAŁANIA"

        if any(
            kw in fresh_low
            for kw in (
                "wpłat",
                "opłat",
                "składk",
                "zgoda rodzic",
                "zgodę rodzic",
                "podpis",
                "zebranie",
                "dzień otwarty",
                "dzien otwarty",
                "zapisy na spotkania",
                "usprawiedliw",
                "odebrać",
                "deklaracj",
                "wycieczk",
                "zielona szkoła",
                "kapci",
            )
        ):
            action_target = "DLA RODZICA"
        else:
            action_target = "DLA UCZNIA"

    elif (
        (is_study_material_or_code and not is_expired)
        or (is_schedule_change and not is_expired and published_age_days <= 14)
        or is_active_confirmation
    ):
        cluster = "materials_and_changes"
        cluster_label = "Informacje — materiały do nauki i zmiany w planie"
        cluster_order = 2
        action_target = ""
        if is_active_confirmation:
            tag_code = "confirmation"
            priority = "confirmation"
            priority_label = "POTWIERDZENIE"
        elif is_schedule_change:
            tag_code = "schedule_change"
            priority = "schedule_change"
            priority_label = "ZMIANA W PLANIE"
        else:
            tag_code = "study_material"
            priority = "study_material"
            priority_label = "MATERIAŁY"

    elif is_optional_invitation and not is_expired and published_age_days <= 25:
        cluster = "optional_competition"
        cluster_label = "Opcjonalne — konkursy i wydarzenia dla chętnych"
        cluster_order = 3
        tag_code = "optional"
        action_target = ""
        priority = "optional"
        priority_label = "DLA CHĘTNYCH"

    else:
        cluster = "organizational_info"
        cluster_label = "Pozostałe komunikaty i archiwum (bez wymaganej akcji)"
        cluster_order = 4
        tag_code = "info"
        action_target = ""
        priority = "info"
        priority_label = "KOMUNIKAT"

    return {
        "kind": kind,
        "cluster": cluster,
        "cluster_label": cluster_label,
        "cluster_order": cluster_order,
        "tag_code": tag_code,
        "action_target": action_target,
        "priority": priority,
        "priority_label": priority_label,
        "subject_label": resolved_subject,
        "title": clean_title,
        "action_summary": summary_text,
        "full_body": raw_body or clean_title,
        "sender": sender,
        "published_on": published_on,
        "deadline_date": extracted_deadline if is_explicit_deadline else "",
        "display_date": display_date,
        "is_deadline": is_explicit_deadline,
        "days_to_deadline": days_to_deadline,
        "is_expired": is_expired,
        "unread": unread,
        "has_attachment": has_attachment,
        "codes": codes or [],
        "links": links or [],
        "account_source": account_source,
    }


class StudyAssistant:
    """Deterministic in-RAM synthesizer that combines Terminarz, Messages, Announcements & Lessons."""

    def __init__(self) -> None:
        self.model_name = "deterministic-synthesis"
        self._cache: dict[str, dict[str, Any]] = {}

    def is_configured(self) -> bool:
        return False

    async def enrich_study_card(self, card: dict[str, Any]) -> dict[str, Any]:
        """Synthesize exam requirements from Terminarz, parent/student messages, announcements & lessons."""
        synth = card.get("synthesized_requirements") or {}
        cache_key = hashlib.sha256(
            f"{card.get('id')}|{synth.get('clue_text')}|{len(card.get('unit_lessons', []))}|{len(card.get('related_messages', []))}".encode(
                "utf-8"
            )
        ).hexdigest()

        if cache_key in self._cache:
            return self._cache[cache_key]

        fallback = self._build_deterministic_summary(card)
        self._cache[cache_key] = fallback
        return fallback

    @staticmethod
    def _build_deterministic_summary(card: dict[str, Any]) -> dict[str, Any]:
        synth = card.get("synthesized_requirements") or {}
        req_points = synth.get("requirement_points") or []
        lessons = card.get("unit_lessons") or []
        subj = card.get("subject") or card.get("title") or "Przedmiot"
        cat = card.get("category_label") or "Sprawdzian"

        headline = synth.get("clue_text") or f"{cat} — {subj}"

        concepts: list[str] = [bp["text"] for bp in req_points]
        for les in lessons:
            t = str(les.get("topic") or "").strip()
            label = f"Temat lekcji ({les['day']}): {t}"
            if t and not any(t.lower() in c.lower() for c in concepts):
                concepts.append(label)

        if synth.get("has_explicit_scope"):
            tip = (
                f"Skup się na wymaganiach podanych przez nauczyciela ({', '.join(synth.get('sources') or ['Terminarz'])}) "
                f"oraz powtórz notatki z {len(lessons)} powiązanych lekcji."
                if lessons
                else "Przygotuj się dokładnie według punktów wymagań podanych przez nauczyciela."
            )
        elif lessons:
            tip = (
                f"Nauczyciel nie wpisał szczegółowych zagadnień w Terminarzu — powtórz materiał "
                f"z ostatnich {len(lessons)} zrealizowanych lekcji z przedmiotu {subj}."
            )
        else:
            tip = "Sprawdź zeszyt przedmiotowy lub dopytaj nauczyciela o szczegółowy zakres zagadnień."

        return {
            "model": "deterministic-synthesis",
            "ai_active": False,
            "summary_headline": headline,
            "key_concepts_to_review": concepts[:14],
            "study_strategy_tip": tip,
        }
