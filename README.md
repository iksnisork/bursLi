# bursLi - Open Source Librus Synergia Family Hub

**bursLi** (anagram of *Librus*) is a privacy-first, **Zero-Database** web application for parents and students using the Polish **Librus Synergia** school journal. It synthesizes timetables, exam study scopes (including teacher guidelines from `.docx`/`.pdf` attachments), action items, grades, messages, and RFC 5545 `.ics` calendar feeds into a fast, high-density dashboard for desktop, mobile, and Google Nest Hub displays.

---

## 🔒 Security & Privacy Architecture (Zero-Database)

1. **Client-Side Encrypted Vault (`IndexedDB` + `AES-GCM 256-bit`)** - All Librus Synergia credentials (both parent and student accounts) are encrypted using the browser's native `Web Crypto API (AES-GCM 256-bit)` and stored **exclusively locally in the user's browser (`IndexedDB`)**.
2. **Stateless In-RAM Server Processing** - The backend stores zero user accounts, zero passwords, and zero student data on disk. All exam scope synthesis, `.docx`/`.pdf`/`.txt` attachment parsing, and task clustering run 100% deterministically in RAM during an HTTPS request and are immediately discarded.
3. **Stateless `.ics` Calendar Feeds & Smart Display / Kiosk Links (`lfh1...`)** - iCalendar subscriptions (`timetable.ics` and `exams.ics` for Google Calendar and Apple Calendar) and optional Kiosk / Google Nest Hub display URLs (`/?token=lfh1...&nesthub=1`) use the same cryptographically sealed `AES-256-GCM` envelope token decrypted exclusively in RAM for the duration of a single refresh request.

---

## ✨ Core Modules

1. **`Plan` (Timetable & Substitutions)** - Daily and weekly timetable matrix with live lesson highlighting, substitutions, cancelled classes, and school-free days.
2. **`Upcoming Exams` (Exams & Interactive Study Cards)** - Combines *Agenda*, *Messages (Parent + Student)*, *DOCX/PDF Teacher Attachments*, and *Completed Lesson Topics* into structured Study Cards with interactive checklists (`< 3 days`, `< 7 days`, and `14 days` horizon).
3. **`Action Items` (Action Items & Information)** - Deterministic classification of school tasks, trips, schedule changes, study materials, and optional competitions using a color + geometric shape badge dictionary, `< 3 days` urgency filter, and automatic filtering of conversational noise.
4. **`Grades` (Grades & Attendance)** - Subject gradebook, weighted averages, recent grade badges, and attendance statistics.
5. **`Messages` (Dual-Inbox Messages)** - Merged parent + student inbox with Librus *Unread* status protection, color-coded positive (green) and negative (red) behavioral remarks, direct attachment downloads (`sandbox.librus.pl`), and a safe **2-step teacher reply preview & confirmation**.
6. **`Accounts & .ics` (Zero-DB Vault, Calendar Subscriptions & Kiosk / Nest Hub Link)** - Multi-child profile switcher, optional student account pairing, 1-click live `.ics` calendar subscription links, and an optional `AES-256-GCM` sealed URL for keyboardless Kiosk / Google Nest Hub displays.

---

## 🚀 Local Execution & Proof of Code

This repository serves as the complete, auditable **Proof of Code** for the public instance running at [**bursLi.com**](https://bursli.com), and allows anyone to run an independent instance locally on their own machine or home network:

```bash
uv sync --python 3.14
uv run --python 3.14 python server.py
```

The application starts locally at `http://127.0.0.1:8088` (or via Docker using the included [`Dockerfile`](Dockerfile)).

To run the automated unit and integration test suite:

```bash
uv run --python 3.14 pytest
```

---

## ☁️ Public Cloud Instance (`bursLi.com`) & Google Cloud Architecture

The project is publicly available at [**https://bursli.com**](https://bursli.com) and is hosted on **Google Cloud Platform (GCP)** , built upon the following GCP components:

- **Google Cloud Run** - Stateless, serverless container execution environment (`europe-west1`). Containers scale dynamically down to zero (`min-instances=0`) and operate without any persistent disk volumes or databases; all Librus session handling and document parsing occur strictly in ephemeral RAM.
- **Google Secret Manager & Cloud KMS** - Secure management of the server-side `AES-256-GCM` master key used solely to unseal stateless `.ics` calendar subscription and Kiosk / Nest Hub display tokens in memory during requests.
- **Google Cloud Build & Artifact Registry** - Container build pipeline and private image registry compiling the runtime image directly from the repository's [`Dockerfile`](Dockerfile).
- **Google Cloud Managed Certificates & Domain Mapping** - Managed `TLS 1.3` HTTPS termination and custom domain routing for [**bursLi.com**](https://bursli.com).

---

## 📜 Credits & License

- **Author**: **Krzysztof Rosiński** ([bursli.com](https://bursli.com))
- **Powered by**: [**`librus-mcp`**](https://github.com/krzysztofbury/librus-mcp) by **Krzysztof Bury**
- **License**: Open Source under the **MIT License** (strictly for non-commercial / educational use). See [`LICENSE`](LICENSE) for details.
