/**
 * Librus // Family Hub — Frontend Controller (pi.dev Minimalist Blueprint Edition)
 * Light mode by default · 1px solid borders · 0px radius · Muted solid pastels
 */
const state = {
  profiles: [],
  activeAccount: null,
  activeModule: "timetable", // 'timetable' | 'exams' | 'materials' | 'grades' | 'messages' | 'vault'
  viewMode: "today", // 'today' | 'week'
  weekStart: null,
  selectedDayIndex: 0,
  timetableData: null,
  examsDaysAhead: 14,
  examsData: null,
  examsSearch: "",
  examsCategory: "all",
  materialsFilter: "all",
  materialsCluster: "active",
  materialsPriority: "all",
  materialsSearch: "",
  materialsData: null,
  gradesData: null,
  gradesSearch: "",
  gradesSort: "subject-asc",
  gradesOnlyRecent: false,
  gradesDateFilter: "all", // 'all' | 'today' | '3d' | '7d' | '14d'
  messagesFolder: "received",
  messagesSearch: "",
  messagesQuickFilter: "all",
  messagesData: null,
  alertsData: null,
  unseenAlerts: [],
  alertIndex: 0,
  icalLinks: {},
};

const WEEKDAYS_PL = [
  "Poniedziałek",
  "Wtorek",
  "Środa",
  "Czwartek",
  "Piątek",
  "Sobota",
  "Niedziela",
];

const els = {
  profileSwitcher: document.getElementById("profileSwitcher"),
  profileSelect: document.getElementById("profileSelect"),
  studentInfoText: document.getElementById("studentInfoText"),
  securityBadge: document.getElementById("securityBadge"),
  moduleNav: document.getElementById("moduleNav"),
  badgeExams: document.getElementById("badgeExams"),
  badgeMaterials: document.getElementById("badgeMaterials"),
  badgeMessages: document.getElementById("badgeMessages"),
  liveClock: document.getElementById("liveClock"),
  themeToggleBtn: document.getElementById("themeToggleBtn"),
  themeIcon: document.getElementById("themeIcon"),
  themeLabel: document.getElementById("themeLabel"),
  refreshBtn: document.getElementById("refreshBtn"),
  openWelcomeModalBtn: document.getElementById("openWelcomeModalBtn"),
  welcomeModal: document.getElementById("welcomeModal"),
  closeWelcomeModalBtn: document.getElementById("closeWelcomeModalBtn"),
  ackWelcomeModalBtn: document.getElementById("ackWelcomeModalBtn"),
  alertBellWrap: document.getElementById("alertBellWrap"),
  alertBellBtn: document.getElementById("alertBellBtn"),
  alertCountBadge: document.getElementById("alertCountBadge"),
  alertPopover: document.getElementById("alertPopover"),
  closeAlertPopoverBtn: document.getElementById("closeAlertPopoverBtn"),
  alertBannerText: document.getElementById("alertBannerText"),
  alertStepper: document.getElementById("alertStepper"),
  prevAlertBtn: document.getElementById("prevAlertBtn"),
  alertCounter: document.getElementById("alertCounter"),
  nextAlertBtn: document.getElementById("nextAlertBtn"),
  openAlertBtn: document.getElementById("openAlertBtn"),
  enableNotificationsBtn: document.getElementById("enableNotificationsBtn"),
  dismissAlertsBtn: document.getElementById("dismissAlertsBtn"),
  daySelectorStrip: document.getElementById("daySelectorStrip"),
  dayPillsGroup: document.getElementById("dayPillsGroup"),
  contentArea: document.getElementById("contentArea"),
  weekRangeLabel: document.getElementById("weekRangeLabel"),
  prevWeekBtn: document.getElementById("prevWeekBtn"),
  thisWeekBtn: document.getElementById("thisWeekBtn"),
  nextWeekBtn: document.getElementById("nextWeekBtn"),
  modeTodayBtn: document.getElementById("modeTodayBtn"),
  modeWeekBtn: document.getElementById("modeWeekBtn"),
  studyModal: document.getElementById("studyModal"),
  studyModalBadge: document.getElementById("studyModalBadge"),
  studyModalTitle: document.getElementById("studyModalTitle"),
  studyModalMeta: document.getElementById("studyModalMeta"),
  studyModalBody: document.getElementById("studyModalBody"),
  closeStudyModalBtn: document.getElementById("closeStudyModalBtn"),
  messageModal: document.getElementById("messageModal"),
  messageModalFolderBadge: document.getElementById("messageModalFolderBadge"),
  messageModalSubject: document.getElementById("messageModalSubject"),
  messageModalMeta: document.getElementById("messageModalMeta"),
  messageModalBody: document.getElementById("messageModalBody"),
  closeMessageModalBtn: document.getElementById("closeMessageModalBtn"),
  toastBox: document.getElementById("toastBox"),
};

function escapeHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function showToast(msg, duration = 3800) {
  els.toastBox.textContent = msg;
  els.toastBox.classList.remove("hidden");
  clearTimeout(showToast._timer);
  showToast._timer = setTimeout(() => {
    els.toastBox.classList.add("hidden");
  }, duration);
}

function getMondayIso(d = new Date()) {
  const date = new Date(d);
  const day = date.getDay();
  const diff = day === 0 ? 1 : day === 6 ? 2 : 1 - day;
  date.setDate(date.getDate() + diff);
  return formatIsoDate(date);
}

function addDaysIso(isoStr, days) {
  const [y, m, d] = isoStr.split("-").map(Number);
  const dt = new Date(y, m - 1, d);
  dt.setDate(dt.getDate() + days);
  return formatIsoDate(dt);
}

function getWeekdayNamePl(isoStr) {
  if (!isoStr) return "";
  const [y, m, d] = isoStr.split("-").map(Number);
  const dt = new Date(y, m - 1, d);
  const jsDay = dt.getDay();
  const idx = jsDay === 0 ? 6 : jsDay - 1;
  return WEEKDAYS_PL[idx] || isoStr;
}

function formatIsoDate(dt) {
  const y = dt.getFullYear();
  const m = String(dt.getMonth() + 1).padStart(2, "0");
  const d = String(dt.getDate()).padStart(2, "0");
  return `${y}-${m}-${d}`;
}

function formatShortDate(isoStr) {
  if (!isoStr) return "";
  const parts = isoStr.split("-");
  if (parts.length !== 3) return isoStr;
  return `${parts[2]}.${parts[1]}`;
}

function updateClock() {
  const now = new Date();
  const hh = String(now.getHours()).padStart(2, "0");
  const mm = String(now.getMinutes()).padStart(2, "0");
  els.liveClock.textContent = `${hh}:${mm}`;
}

function isLessonNow(dayIso, timeFrom, timeTo) {
  const now = new Date();
  const todayIso = formatIsoDate(now);
  if (dayIso !== todayIso || !timeFrom || !timeTo) return false;
  const nowMins = now.getHours() * 60 + now.getMinutes();
  const [fH, fM] = timeFrom.split(":").map(Number);
  const [tH, tM] = timeTo.split(":").map(Number);
  return nowMins >= fH * 60 + fM && nowMins <= tH * 60 + tM;
}

async function apiFetch(path, options = {}) {
  const headers = Object.assign({}, options.headers || {});
  const vaultHeader = window.LibrusVault ? window.LibrusVault.getAccountsHeaderValue() : null;
  if (vaultHeader) {
    headers["X-Librus-Accounts"] = vaultHeader;
  }
  if (options.body && typeof options.body === "object" && !(options.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(options.body);
  }
  options.headers = headers;
  const res = await fetch(path, options);
  const data = await res.json();
  if (!res.ok) {
    throw new Error(data.error || `Błąd HTTP ${res.status}`);
  }
  return data;
}

/* Profile & Navigation Setup */
function renderProfiles() {
  if (els.profileSelect) {
    if (state.profiles.length === 0) {
      els.profileSelect.innerHTML = `<option value="">Brak konta</option>`;
    } else {
      els.profileSelect.innerHTML = state.profiles
        .map((p) => {
          const alias = p.alias || p.account;
          const shortName = p.first_name || (p.full_name || alias).split(" ")[0];
          const classInfo = p.class_name ? ` · kl. ${p.class_name}` : "";
          const selected = alias === state.activeAccount ? "selected" : "";
          return `<option value="${escapeHtml(alias)}" ${selected}>${escapeHtml(shortName.toUpperCase())}${escapeHtml(classInfo)}</option>`;
        })
        .join("");
    }
    els.profileSelect.onchange = (e) => {
      const nextAlias = e.target.value;
      if (nextAlias && state.activeAccount !== nextAlias) {
        state.activeAccount = nextAlias;
        updateStudentSubtitle();
        loadActiveModule(false);
        refreshBackgroundAlerts();
      }
    };
  }
  updateStudentSubtitle();
}

function updateStudentSubtitle() {
  const profile = state.profiles.find((p) => (p.alias || p.account) === state.activeAccount);
  if (profile) {
    const luckyShort = profile.lucky_number ? `NR ${profile.lucky_number}` : `KL. ${profile.class_name || "---"}`;
    els.studentInfoText.textContent = luckyShort;
  } else {
    els.studentInfoText.textContent = "BRAK KONTA";
  }
  const localAccs = window.LibrusVault ? window.LibrusVault.getMemoryAccounts() : [];
  if (localAccs.length > 0) {
    els.securityBadge.textContent = `VAULT (${localAccs.length})`;
  } else {
    els.securityBadge.textContent = "ZERO-DB";
  }
}

function switchModule(moduleName) {
  state.activeModule = moduleName;
  document.querySelectorAll(".module-tab").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.module === moduleName);
  });

  if (moduleName !== "timetable") {
    els.daySelectorStrip.classList.add("hidden");
  } else {
    els.daySelectorStrip.classList.remove("hidden");
  }

  if (els.contentArea) {
    els.contentArea.scrollTop = 0;
  }

  loadActiveModule(false);
}

async function loadActiveModule(forceRefresh = false) {
  if (!state.activeAccount && state.activeModule !== "vault") {
    switchModule("vault");
    return;
  }
  switch (state.activeModule) {
    case "timetable":
      await loadTimetable(forceRefresh);
      break;
    case "exams":
      await loadExams(forceRefresh);
      break;
    case "materials":
      await loadMaterials(forceRefresh);
      break;
    case "grades":
      await loadGrades(forceRefresh);
      break;
    case "messages":
      await loadMessages(forceRefresh);
      break;
    case "vault":
      await renderVaultAndCalendarModule();
      break;
  }
}

/* ==========================================================================
   MODULE 1: TIMETABLE + SUBSTITUTIONS (pi.dev/models Data Table + Week Matrix)
   ========================================================================== */
function extractDayLessonsFromPeriods(dayObj) {
  const periods = dayObj.periods || [];
  const out = [];
  periods.forEach((p) => {
    const lessons = p.lessons || [];
    const changes = p.changes || [];
    if (lessons.length === 0 && changes.length === 0) return;

    const primary = lessons[0] || {};
    const isCancelled = changes.some((c) => {
      const lbl = String(c.label || "").toLowerCase();
      return lbl.includes("odwoł") || lbl.includes("nieobec");
    });
    const isSubstitution = changes.length > 0 && !isCancelled;
    const changeNote = changes.map((c) => `${c.label}: ${c.summary}`).join(" | ");

    out.push({
      lesson_number: p.number,
      time_from: p.starts_at,
      time_to: p.ends_at,
      subject: primary.subject || (changes[0] && changes[0].summary) || "Lekcja",
      teacher: primary.teacher || "",
      room: (primary.room || "").replace(/^Sala\s+/i, ""),
      is_cancelled: isCancelled,
      is_substitution: isSubstitution,
      note: changeNote,
    });
  });
  return out;
}

async function loadTimetable(forceRefresh = false) {
  if (!state.activeAccount) return;
  els.contentArea.innerHTML = `
    <div class="loading-state">
      <div class="spinner"></div>
      <p>POBIERANIE PLANU LEKCJI I ZASTĘPSTW...</p>
    </div>
  `;
  try {
    const url = `/api/timetable?account=${encodeURIComponent(state.activeAccount)}&monday=${encodeURIComponent(state.weekStart)}${forceRefresh ? "&refresh=1" : ""}`;
    const data = await apiFetch(url);
    state.timetableData = data;

    const schoolDays = (data.days || []).slice(0, 5);
    const todayIso = formatIsoDate(new Date());
    const todayIdx = schoolDays.findIndex((d) => (d.day || d.date) === todayIso);
    if (todayIdx >= 0) {
      state.selectedDayIndex = todayIdx;
    } else if (state.selectedDayIndex >= schoolDays.length) {
      state.selectedDayIndex = 0;
    }
    renderTimetableModule();
  } catch (err) {
    els.contentArea.innerHTML = `<div class="empty-day-card"><h3>BŁĄD POBIERANIA PLANU</h3><p>${escapeHtml(err.message)}</p></div>`;
  }
}

function renderTimetableModule() {
  const data = state.timetableData;
  if (!data || !data.days) return;
  const schoolDays = data.days.slice(0, 5).map((d) => {
    const dateIso = d.day || d.date;
    return {
      date: dateIso,
      day_name: d.day_name || getWeekdayNamePl(dateIso),
      lessons: d.lessons || extractDayLessonsFromPeriods(d),
    };
  });

  const mondayIso = data.monday || state.weekStart;
  const fridayIso = schoolDays.length > 0 ? schoolDays[schoolDays.length - 1].date : addDaysIso(mondayIso, 4);
  els.weekRangeLabel.textContent = `${formatShortDate(mondayIso)} – ${formatShortDate(fridayIso)}`;

  const todayIso = formatIsoDate(new Date());
  const pillsContainer = els.dayPillsGroup || els.daySelectorStrip;
  pillsContainer.innerHTML = "";
  schoolDays.forEach((day, idx) => {
    const pill = document.createElement("button");
    pill.type = "button";
    const isToday = day.date === todayIso;
    const isActiveDay = state.viewMode === "today" && idx === state.selectedDayIndex;
    pill.className = `day-pill ${isActiveDay ? "active" : ""} ${isToday ? "is-today" : ""}`;
    pill.innerHTML = `
      <span class="day-name">${escapeHtml(day.day_name)}</span>
      <span class="day-date">${formatShortDate(day.date)} · ${day.lessons.length} LEK.</span>
    `;
    pill.onclick = () => {
      state.selectedDayIndex = idx;
      if (state.viewMode !== "today") {
        state.viewMode = "today";
        els.modeTodayBtn.classList.add("active");
        els.modeWeekBtn.classList.remove("active");
      }
      renderTimetableModule();
    };
    pillsContainer.appendChild(pill);
  });

  els.daySelectorStrip.classList.remove("hidden");
  if (state.viewMode === "today") {
    renderTimetableDayView(schoolDays[state.selectedDayIndex], todayIso);
  } else {
    renderTimetableWeekView(schoolDays, todayIso);
  }
}

function renderTimetableDayView(dayObj, todayIso) {
  if (!dayObj || dayObj.lessons.length === 0) {
    els.contentArea.innerHTML = `
      <div class="empty-day-card">
        <h2>Brak zaplanowanych lekcji w tym dniu</h2>
        <p>${dayObj ? `${escapeHtml(dayObj.day_name)}, ${formatShortDate(dayObj.date)}` : ""}</p>
      </div>
    `;
    return;
  }

  const panel = document.createElement("div");
  panel.className = "surface-panel content-card data-table-card";

  const rowsHtml = dayObj.lessons.map((l) => {
    const nowActive = isLessonNow(dayObj.date, l.time_from, l.time_to);
    const rowClasses = [
      nowActive ? "row-highlight" : "",
      l.is_cancelled ? "row-cancelled" : "",
    ].filter(Boolean).join(" ");

    const badges = [];
    if (nowActive) badges.push(`<span class="badge badge-now">TERAZ</span>`);
    if (l.is_cancelled) badges.push(`<span class="badge badge-cancelled">ODWOŁANA</span>`);
    if (l.is_substitution) badges.push(`<span class="badge badge-sub">ZASTĘPSTWO</span>`);
    if (!nowActive && !l.is_cancelled && !l.is_substitution) {
      badges.push(`<span class="badge badge-info">PLANOWA</span>`);
    }

    const subjStyle = l.is_cancelled ? "text-decoration:line-through;color:var(--accent-rose);" : "";

    return `
      <tr class="${rowClasses}">
        <td style="width:58px;font-weight:700;">#${escapeHtml(l.lesson_number)}</td>
        <td style="width:130px;white-space:nowrap;">${escapeHtml(l.time_from)} – ${escapeHtml(l.time_to)}</td>
        <td>
          <span class="cell-primary" style="${subjStyle}">${escapeHtml(l.subject)}</span>
          ${l.note ? `<span class="cell-secondary" style="color:var(--accent-amber);font-weight:700;">↳ ${escapeHtml(l.note)}</span>` : ""}
        </td>
        <td>${escapeHtml(l.teacher || "---")}</td>
        <td style="width:100px;">${l.room ? `<span class="room-badge">S. ${escapeHtml(l.room)}</span>` : "---"}</td>
        <td style="width:150px;">${badges.join(" ")}</td>
      </tr>
    `;
  }).join("");

  panel.innerHTML = `
    <div class="data-table-scroll">
      <table class="data-table">
        <thead>
          <tr>
            <th>NR</th>
            <th>GODZINY</th>
            <th>PRZEDMIOT / ZMIANY W PLANIE</th>
            <th>NAUCZYCIEL</th>
            <th>SALA</th>
            <th>STATUS</th>
          </tr>
        </thead>
        <tbody>
          ${rowsHtml}
        </tbody>
      </table>
    </div>
  `;

  els.contentArea.innerHTML = "";
  els.contentArea.appendChild(panel);
}

function renderTimetableWeekView(days, todayIso) {
  const grid = document.createElement("div");
  grid.className = "week-grid";

  days.forEach((d, idx) => {
    const col = document.createElement("div");
    col.className = `week-col ${d.date === todayIso ? "today-col" : ""}`;
    const lessonsHtml = d.lessons.length === 0
      ? `<p style="text-align:center;color:var(--text-muted);padding:16px;font-size:12px;">BRAK LEKCJI</p>`
      : d.lessons.map((l) => `
          <div class="mini-lesson ${l.is_cancelled ? "cancelled" : ""} ${l.is_substitution ? "substitution" : ""}">
            <div class="mini-top">
              <span>#${escapeHtml(l.lesson_number)}</span>
              <span>${escapeHtml(l.time_from)}-${escapeHtml(l.time_to)}</span>
            </div>
            <div class="mini-subj">${escapeHtml(l.subject)}</div>
            <div class="mini-room">${l.room ? `s. ${escapeHtml(l.room)}` : ""} ${l.is_cancelled ? "· ODWOŁANA" : l.is_substitution ? "· ZAST." : ""}</div>
          </div>
        `).join("");

    col.innerHTML = `
      <div class="week-col-header" style="cursor:pointer" data-idx="${idx}">
        <h3>${escapeHtml(d.day_name)}</h3>
        <span>${formatShortDate(d.date)}</span>
      </div>
      <div class="week-col-lessons">${lessonsHtml}</div>
    `;
    col.querySelector(".week-col-header").onclick = () => {
      state.selectedDayIndex = idx;
      state.viewMode = "today";
      els.modeTodayBtn.classList.add("active");
      els.modeWeekBtn.classList.remove("active");
      renderTimetableModule();
    };
    grid.appendChild(col);
  });

  els.contentArea.innerHTML = "";
  els.contentArea.appendChild(grid);
}

/* ==========================================================================
   MODULE 2: EXAMS, QUIZZES & STUDY CARDS (pi.dev/changelog Timeline + Filter Bar)
   ========================================================================== */
async function loadExams(forceRefresh = false) {
  if (!state.activeAccount) return;
  els.contentArea.innerHTML = `
    <div class="loading-state">
      <div class="spinner"></div>
      <p>AGREGOWANIE SPRAWDZIANÓW, TEMATÓW LEKCJI I WIADOMOŚCI...</p>
    </div>
  `;
  try {
    const url = `/api/exams?account=${encodeURIComponent(state.activeAccount)}&days=14${forceRefresh ? "&refresh=1" : ""}`;
    const data = await apiFetch(url);
    state.examsData = data;
    const items = data.items || data.exams || [];
    updateExamsBadge(items);
    renderExamsModule();
  } catch (err) {
    els.contentArea.innerHTML = `<div class="empty-day-card"><h3>BŁĄD POBIERANIA TERMINARZA</h3><p>${escapeHtml(err.message)}</p></div>`;
  }
}

function updateExamsBadge(exams) {
  if (exams && exams.length > 0) {
    els.badgeExams.textContent = String(exams.length);
    els.badgeExams.classList.remove("hidden");
  } else {
    els.badgeExams.classList.add("hidden");
  }
}

function renderExamsModule() {
  const allExams = (state.examsData && (state.examsData.items || state.examsData.exams)) || [];
  const hasStudentAccount = Boolean(state.examsData && state.examsData.has_student_account);
  const currentProfile = state.profiles.find((p) => (p.alias || p.account) === state.activeAccount);
  const suggestedStudentLogin = (currentProfile && currentProfile.suggested_student_username) || "";

  // Extract distinct categories for filter select
  const categories = Array.from(
    new Set(allExams.map((e) => e.category_label || e.category || "Sprawdzian"))
  );

  const horizonDays = Number(state.examsDaysAhead || 14);
  const q = state.examsSearch.trim().toLowerCase();
  const filtered = allExams.filter((exam) => {
    const cat = exam.category_label || exam.category || "Sprawdzian";
    if (state.examsCategory !== "all" && cat !== state.examsCategory) return false;
    const daysLeft = exam.days_until ?? exam.days_left ?? 0;
    if (daysLeft > horizonDays) return false;

    if (q) {
      const synth = exam.synthesized_requirements || {};
      const hay = [
        exam.subject,
        exam.title,
        exam.teacher,
        cat,
        exam.verbatim_description,
        exam.raw_description,
        synth.clue_text,
        ...(synth.requirement_points || []),
        ...(exam.verbatim_requirements || []),
      ]
        .filter(Boolean)
        .join(" ")
        .toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });

  const panel = document.createElement("div");
  panel.className = "surface-panel content-card data-table-card";

  const rowsHtml = filtered.map((exam, idx) => {
    const daysLeft = exam.days_until ?? exam.days_left ?? 0;
    const examDate = exam.day || exam.date;
    const weekday = exam.weekday_pl || getWeekdayNamePl(examDate);
    const categoryLabel = exam.category_label || exam.category || "Sprawdzian";
    const room = exam.classroom || exam.room || "";

    const daysLabel = daysLeft === 0
      ? "DZISIAJ"
      : daysLeft === 1
      ? "JUTRO"
      : `ZA ${daysLeft} DNI`;

    const badgeClass = daysLeft <= 3 ? "badge-cancelled" : daysLeft <= 7 ? "badge-sub" : "badge-info";

    const synth = exam.synthesized_requirements || {};
    const reqPoints = synth.requirement_points || exam.verbatim_requirements || [];
    const teacherGuidelines = synth.teacher_guidelines || [];
    const rawDesc = (exam.verbatim_description || exam.raw_description || "").trim();
    const pointsHtml = reqPoints.length > 0
      ? `<ul class="scope-list" style="margin:0;padding-left:16px;font-family:var(--prose);font-size:13px;line-height:1.45;color:var(--text-primary);">${reqPoints.slice(0, 4).map((pt) => {
          const text = typeof pt === "string" ? pt : pt.text || "";
          const src = typeof pt === "object" && pt.source && pt.source !== "Terminarz" ? ` <span class="card-subtitle">[${escapeHtml(pt.source)}]</span>` : "";
          return `<li>${escapeHtml(text)}${src}</li>`;
        }).join("")}</ul>`
      : `<div style="font-family:var(--prose);font-size:13px;line-height:1.45;color:var(--text-primary);white-space:pre-wrap;">${escapeHtml(rawDesc || "Brak opisu w terminarzu — otwórz Kartę nauki, aby zobaczyć powiązane tematy z działu.")}</div>`;

    const guidelinesHtml = renderTeacherGuidelinesBlocks(teacherGuidelines, true);

    const sources = synth.sources || synth.sources_used || ["Terminarz"];
    const sourcesHtml = `<div class="chip-row" style="margin-top:6px;">
      ${sources.map((s) => `<span class="badge badge-info" style="font-size:9px;padding:1px 5px;">ŹRÓDŁO: ${escapeHtml(s)}</span>`).join("")}
      ${(exam.extracted_codes || []).map((c) => `<span class="code-chip">KOD: ${escapeHtml(typeof c === "string" ? c : c.code)}</span>`).join("")}
    </div>`;

    return `
      <tr class="${daysLeft <= 3 ? "row-highlight" : ""}" data-exam-idx="${idx}" style="cursor:pointer;">
        <td style="width:140px;white-space:nowrap;">
          <span class="cell-primary">${escapeHtml(examDate)}</span>
          <span class="cell-secondary">${escapeHtml(weekday)} · <strong style="color:var(--text-primary);">${escapeHtml(daysLabel)}</strong></span>
        </td>
        <td style="width:145px;">
          <span class="badge ${badgeClass}">${escapeHtml(categoryLabel)}</span>
          <span class="cell-secondary" style="margin-top:3px;">
            ${exam.lesson_number ? `Lekcja #${escapeHtml(exam.lesson_number)}` : ""}${room ? ` · s. ${escapeHtml(room)}` : ""}
          </span>
        </td>
        <td style="width:200px;">
          <span class="cell-primary">${escapeHtml(exam.subject || exam.title)}</span>
          <span class="cell-secondary">${escapeHtml(exam.teacher || "---")}</span>
        </td>
        <td>
          ${pointsHtml}
          ${guidelinesHtml}
          ${sourcesHtml}
        </td>
        <td style="width:120px;text-align:right;">
          <button type="button" class="action-button open-study-btn" data-exam-idx="${idx}">Karta nauki</button>
        </td>
      </tr>
    `;
  }).join("");

  const studentAccountStrip = hasStudentAccount
    ? `<span class="badge badge-now" title="Wiadomości z wymaganiami są pobierane z konta rodzica oraz konta ucznia">RODZIC + UCZEŃ</span>`
    : `<button type="button" id="pairStudentAccountQuickBtn" class="btn-outline btn-xs" title="Część nauczycieli wysyła wymagania tylko na konto dziecka">+ Połącz konto ucznia${suggestedStudentLogin ? ` (${escapeHtml(suggestedStudentLogin)})` : ""}</button>`;

  panel.innerHTML = `
    <div class="pi-action-bar" style="border-left:0;border-right:0;border-top:0;">
      <input
        type="search"
        id="examsSearchInput"
        class="pi-search-input"
        placeholder="Szukaj przedmiotu, nauczyciela lub wymagań..."
        value="${escapeHtml(state.examsSearch)}"
      />
      <select id="examsCategorySelect" class="pi-select">
        <option value="all">Typ: Wszystkie (${allExams.length})</option>
        ${categories.map((c) => `<option value="${escapeHtml(c)}" ${state.examsCategory === c ? "selected" : ""}>${escapeHtml(c)}</option>`).join("")}
      </select>
      <div class="pi-filter-pills">
        <button type="button" class="pi-filter-pill ${horizonDays === 3 ? "active" : ""}" data-days="3">3 dni</button>
        <button type="button" class="pi-filter-pill ${horizonDays === 7 ? "active" : ""}" data-days="7">7 dni</button>
        <button type="button" class="pi-filter-pill ${horizonDays === 14 ? "active" : ""}" data-days="14">14 dni</button>
      </div>
      ${studentAccountStrip}
      <span class="pi-counter">${filtered.length} / ${allExams.length}</span>
      <button type="button" id="examsResetBtn" class="action-button">Reset</button>
    </div>
    <div class="data-table-scroll">
      <table class="data-table">
        <thead>
          <tr>
            <th>DATA / TERMIN</th>
            <th>TYP</th>
            <th>PRZEDMIOT / NAUCZYCIEL</th>
            <th>WYMAGANIA I ZAKRES (SYNTEZA: TERMINARZ + WIADOMOŚCI + OGŁOSZENIA)</th>
            <th style="text-align:right;">AKCJA</th>
          </tr>
        </thead>
        <tbody>
          ${rowsHtml || `<tr><td colspan="5">Brak sprawdzianów w wybranym horyzoncie (${horizonDays} dni).</td></tr>`}
        </tbody>
      </table>
    </div>
  `;

  els.contentArea.innerHTML = "";
  els.contentArea.appendChild(panel);

  panel.querySelectorAll("tbody tr[data-exam-idx]").forEach((tr) => {
    tr.onclick = () => {
      const idx = Number(tr.dataset.examIdx);
      if (filtered[idx]) openStudyCardModal(filtered[idx]);
    };
  });

  const pairBtn = document.getElementById("pairStudentAccountQuickBtn");
  if (pairBtn) {
    pairBtn.onclick = () => switchModule("vault");
  }

  // Bind Action Bar controls
  const searchInput = document.getElementById("examsSearchInput");
  if (searchInput) {
    searchInput.oninput = (e) => {
      state.examsSearch = e.target.value;
      renderExamsModule();
      const restored = document.getElementById("examsSearchInput");
      if (restored) {
        restored.focus();
        restored.setSelectionRange(restored.value.length, restored.value.length);
      }
    };
  }
  const catSelect = document.getElementById("examsCategorySelect");
  if (catSelect) {
    catSelect.onchange = (e) => {
      state.examsCategory = e.target.value;
      renderExamsModule();
    };
  }
  panel.querySelectorAll(".pi-filter-pill[data-days]").forEach((pill) => {
    pill.onclick = () => {
      state.examsDaysAhead = Number(pill.dataset.days || 14);
      renderExamsModule();
    };
  });
  const resetBtn = document.getElementById("examsResetBtn");
  if (resetBtn) {
    resetBtn.onclick = () => {
      state.examsSearch = "";
      state.examsCategory = "all";
      state.examsDaysAhead = 14;
      renderExamsModule();
    };
  }
}

function renderStructuredGuidelineItems(items) {
  if (!items || items.length === 0) return "";
  return items
    .map((it) => {
      const lvl = it.level || "paragraph";
      const mrk = it.marker || "";
      const txt = it.text || "";
      if (lvl === "attachment_heading") {
        return `<div style="margin:6px 0 4px 0;font-family:var(--prose);font-size:12.5px;font-weight:700;color:var(--text-primary);border-bottom:1px solid var(--border-color);padding-bottom:3px;">${escapeHtml(txt)}</div>`;
      }
      if (lvl === "footer_tip") {
        return `<div style="margin-top:7px;padding:6px 10px;background:var(--pastel-blue-bg);border-left:3px solid var(--pastel-blue-border);font-family:var(--prose);font-size:12px;font-weight:600;line-height:1.45;color:var(--pastel-blue-fg);">${escapeHtml(txt)}</div>`;
      }
      if (lvl === "numbered") {
        return `<div style="padding-left:2px;margin-top:6px;font-family:var(--prose);font-size:12.5px;line-height:1.45;color:var(--text-primary);display:flex;align-items:baseline;gap:6px;">
          <span style="font-family:var(--accent-mono);font-size:11px;font-weight:700;color:var(--accent-primary);min-width:20px;">${escapeHtml(mrk)}</span>
          <span style="flex:1;font-weight:700;">${escapeHtml(txt)}</span>
        </div>`;
      }
      if (lvl === "subitem_alpha") {
        return `<div style="padding-left:22px;margin-top:2px;font-family:var(--prose);font-size:12px;line-height:1.45;color:var(--text-primary);display:flex;align-items:baseline;gap:6px;">
          <span style="font-family:var(--accent-mono);font-size:11px;font-weight:700;color:var(--text-secondary);min-width:16px;">${escapeHtml(mrk)}</span>
          <span style="flex:1;font-weight:600;">${escapeHtml(txt)}</span>
        </div>`;
      }
      if (lvl === "subitem_dash") {
        return `<div style="padding-left:40px;margin-top:1.5px;font-family:var(--prose);font-size:12px;line-height:1.45;color:var(--text-secondary);display:flex;align-items:baseline;gap:6px;">
          <span style="font-family:var(--accent-mono);font-size:11px;">–</span>
          <span style="flex:1;">${escapeHtml(txt)}</span>
        </div>`;
      }
      return `<div style="margin-bottom:5px;font-family:var(--prose);font-size:12.5px;line-height:1.5;color:var(--text-primary);">${escapeHtml(txt)}</div>`;
    })
    .join("");
}

function renderTeacherGuidelinesBlocks(guidelines, compactMode = false) {
  if (!guidelines || guidelines.length === 0) return "";
  return guidelines
    .map((blk) => {
      const isStudent = blk.account_source === "uczeń";
      const badgeClass = isStudent ? "badge-now" : "badge-info";
      const accLabel = blk.account_label || (isStudent ? "KONTO UCZNIA" : "KONTO RODZICA");
      const attNames = blk.attachment_filenames || [];
      const bodyItems = blk.body_items || [];
      const attItems = blk.attachment_items || [];

      const bodyHtml = renderStructuredGuidelineItems(bodyItems);
      let attSectionHtml = "";
      if (attItems.length > 0) {
        const itemsHtml = renderStructuredGuidelineItems(attItems);
        const attLabel = attNames.length > 0 ? attNames.join(", ") : "Załącznik z zagadnieniami";
        attSectionHtml = `
          <div style="margin-top:8px;padding-top:7px;border-top:1px dashed var(--border-color);">
            <div style="display:flex;align-items:center;gap:6px;margin-bottom:4px;flex-wrap:wrap;">
              <span class="badge badge-purple" style="font-size:9px;padding:1px 5px;">ZAŁĄCZNIK</span>
              <span style="font-family:var(--accent-mono);font-size:10.5px;font-weight:700;color:var(--text-secondary);">${escapeHtml(attLabel)}</span>
            </div>
            ${itemsHtml}
          </div>
        `;
      }

      return `
        <div style="margin-top:8px;padding:9px 12px;background:var(--bg-subtle);border:1px solid var(--border-color);border-left:3px solid ${isStudent ? "var(--pastel-sage-text)" : "var(--accent-primary)"};">
          <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;margin-bottom:6px;padding-bottom:4px;border-bottom:1px solid var(--border-color);">
            <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;">
              <span class="badge ${badgeClass}" style="font-size:9px;padding:1px 6px;">WYTYCZNE NAUCZYCIELA · ${escapeHtml(accLabel)}</span>
              ${blk.subject ? `<span style="font-family:var(--prose);font-size:12px;font-weight:700;color:var(--text-primary);">${escapeHtml(blk.subject)}</span>` : ""}
            </div>
            <span class="card-subtitle" style="font-size:10.5px;">${escapeHtml(blk.sender || "")}${blk.sent_at ? ` · ${escapeHtml(String(blk.sent_at).slice(0, 10))}` : ""}</span>
          </div>
          ${bodyHtml}
          ${attSectionHtml}
        </div>
      `;
    })
    .join("");
}

function openStudyCardModal(exam) {
  const examDate = exam.day || exam.date;
  const examKey = `${state.activeAccount}::${exam.id || examDate}`;
  const categoryLabel = exam.category_label || exam.category || "Sprawdzian";
  const daysLeft = exam.days_until ?? exam.days_left ?? 0;
  const room = exam.classroom || exam.room || "";

  els.studyModalBadge.textContent = `${categoryLabel} · ${examDate} (${exam.weekday_pl || getWeekdayNamePl(examDate)})`;
  els.studyModalBadge.className = `badge ${daysLeft <= 3 ? "badge-cancelled" : "badge-info"}`;
  els.studyModalTitle.textContent = `${exam.subject || exam.title}`;
  els.studyModalMeta.textContent = `Nauczyciel: ${exam.teacher || "---"} ${exam.lesson_number ? `· Lekcja #${exam.lesson_number}` : ""} ${room ? `· Sala ${room}` : ""}`;

  const synth = exam.synthesized_requirements || {};
  const reqPoints = synth.requirement_points || exam.verbatim_requirements || [];
  const teacherGuidelines = synth.teacher_guidelines || [];
  const sources = synth.sources || synth.sources_used || ["Terminarz"];

  const checklistItems = exam.checklist || exam.study_checklist || [];
  let doneCount = 0;
  const checklistHtml = checklistItems.map((item, i) => {
    const text = typeof item === "string" ? item : item.text;
    const subitems = (typeof item === "object" && Array.isArray(item.subitems)) ? item.subitems : [];
    const badge = typeof item === "object" && item.badge ? `<span class="badge ${item.badge === "Konto ucznia" ? "badge-now" : "badge-info"}" style="margin-left:auto;flex-shrink:0;">${escapeHtml(item.badge)}</span>` : "";
    const done = window.LibrusVault && window.LibrusVault.isChecklistItemDone(examKey, i);
    if (done) doneCount++;
    const subitemsHtml = subitems.length > 0
      ? `<div class="checklist-subitems">${subitems.map((sub) => {
          const isAlpha = sub.level === "subitem_alpha";
          const padLeft = isAlpha ? "6px" : "20px";
          const marker = sub.marker || "–";
          return `<div class="checklist-subitem" style="padding-left:${padLeft};">
            <span style="font-family:var(--accent-mono);font-size:11px;font-weight:${isAlpha ? "700" : "500"};color:var(--text-secondary);min-width:16px;">${escapeHtml(marker)}</span>
            <span style="flex:1;font-weight:${isAlpha ? "600" : "400"};">${escapeHtml(sub.text || "")}</span>
          </div>`;
        }).join("")}</div>`
      : "";
    return `
      <div class="checklist-item ${done ? "done" : ""}" data-idx="${i}">
        <input type="checkbox" ${done ? "checked" : ""} style="margin-top:3px;pointer-events:none;flex-shrink:0;" />
        <div style="flex:1;min-width:0;">
          <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:8px;">
            <span class="checklist-main-text" style="font-weight:${subitems.length > 0 ? "700" : "500"};line-height:1.4;">${escapeHtml(text)}</span>
            ${badge}
          </div>
          ${subitemsHtml}
        </div>
      </div>
    `;
  }).join("");

  const unitLessons = exam.unit_lessons || exam.recent_lessons || [];
  const lessonsHtml = unitLessons.length === 0
    ? `<p class="card-subtitle">Brak powiązanych tematów z bieżącego działu.</p>`
    : `<ul class="scope-list">${unitLessons.map((l) => `<li><strong>${escapeHtml(l.day || l.date)}</strong>: ${escapeHtml(l.topic)}</li>`).join("")}</ul>`;

  const codes = exam.extracted_codes || [];
  const codesHtml = codes.length > 0
    ? `<div class="chip-row" style="margin-top:6px;">${codes.map((c) => `<span class="code-chip">KOD: ${escapeHtml(typeof c === "string" ? c : c.code)}</span>`).join("")}</div>`
    : "";

  // When structured teacherGuidelines are present, only show Terminarz points in synthPointsHtml so we never duplicate the attachment/message twice
  const nonGuidelinePoints = teacherGuidelines.length > 0
    ? reqPoints.filter((pt) => typeof pt === "object" && (!pt.source || pt.source === "Terminarz"))
    : reqPoints;

  const synthPointsHtml = nonGuidelinePoints.length > 0
    ? `<ul class="scope-list" style="margin-top:6px;">${nonGuidelinePoints.map((pt) => {
        const text = typeof pt === "string" ? pt : pt.text || "";
        const src = typeof pt === "object" && pt.source && pt.source !== "Terminarz" ? ` <span class="card-subtitle">[${escapeHtml(pt.source)}]</span>` : "";
        return `<li>${escapeHtml(text)}${src}</li>`;
      }).join("")}</ul>`
    : (teacherGuidelines.length > 0 ? "" : `<div style="white-space:pre-wrap;">${escapeHtml(exam.verbatim_description || exam.raw_description || "Brak opisu")}</div>`);

  const guidelinesModalHtml = renderTeacherGuidelinesBlocks(teacherGuidelines, false);

  els.studyModalBody.innerHTML = `
    <div class="scope-box" style="border-left:3px solid var(--accent-primary);">
      <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:6px;margin-bottom:6px;">
        <span style="font-family:var(--accent-mono);font-size:11px;text-transform:uppercase;color:var(--accent-primary);font-weight:700;">
          WYMAGANIA DO OPANOWANIA (SYNTEZA ZE ŹRÓDEŁ: ${escapeHtml(sources.join(" + "))})
        </span>
      </div>
      ${synthPointsHtml}
      ${guidelinesModalHtml}
      ${codesHtml}
      <div style="margin-top:8px;padding-top:6px;border-top:1px dashed var(--line);font-size:11px;color:var(--text-secondary);">
        <strong>Wpis w terminarzu (1:1):</strong> ${escapeHtml(exam.verbatim_description || exam.raw_description || "---")}
      </div>
    </div>

    <div>
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;">
        <h3 style="font-family:var(--accent-mono);font-size:12px;text-transform:uppercase;margin:0;">
          CHECKLISTA NAUKI OSADZONA W WYMAGANIACH
        </h3>
        <span id="modalChecklistCounterBadge" class="badge ${doneCount === checklistItems.length && checklistItems.length > 0 ? "badge-now" : "badge-info"}">
          UKOŃCZONO: ${doneCount} / ${checklistItems.length}
        </span>
      </div>
      <div style="display:flex;flex-direction:column;gap:6px;" id="modalChecklistContainer">
        ${checklistHtml}
      </div>
    </div>

    <div>
      <h3 style="font-family:var(--accent-mono);font-size:12px;text-transform:uppercase;margin-bottom:6px;">
        POWIĄZANE TEMATY LEKCJI Z BIEŻĄCEGO DZIAŁU (${escapeHtml(exam.subject)})
      </h3>
      <div class="scope-box">${lessonsHtml}</div>
    </div>
  `;

  els.studyModalBody.querySelectorAll(".checklist-item").forEach((row) => {
    row.onclick = () => {
      const i = Number(row.dataset.idx);
      const isDone = window.LibrusVault.toggleChecklistItem(examKey, i);
      row.classList.toggle("done", isDone);
      const cb = row.querySelector("input[type='checkbox']");
      if (cb) cb.checked = isDone;
      const total = checklistItems.length;
      let currentDone = 0;
      for (let k = 0; k < total; k++) {
        if (window.LibrusVault.isChecklistItemDone(examKey, k)) currentDone++;
      }
      const badgeEl = document.getElementById("modalChecklistCounterBadge");
      if (badgeEl) {
        badgeEl.textContent = `UKOŃCZONO: ${currentDone} / ${total}`;
        badgeEl.className = `badge ${currentDone === total && total > 0 ? "badge-now" : "badge-info"}`;
      }
    };
  });

  els.studyModal.classList.remove("hidden");
}

/* ==========================================================================
   MODULE 3: ACTION ITEMS — HOMEWORK, ANNOUNCEMENTS & MESSAGES (Compact Table)
   ========================================================================== */
async function loadMaterials(forceRefresh = false) {
  if (!state.activeAccount) return;
  els.contentArea.innerHTML = `
    <div class="loading-state">
      <div class="spinner"></div>
      <p>SYNTEZA ZADAŃ, TERMINÓW I WAŻNYCH WIADOMOŚCI (ACTION ITEMS)...</p>
    </div>
  `;
  try {
    const url = `/api/materials?account=${encodeURIComponent(state.activeAccount)}${forceRefresh ? "&refresh=1" : ""}`;
    const data = await apiFetch(url);
    state.materialsData = data;
    const allItems = data.action_items || [];
    const activeCount = allItems.filter((i) => (i.cluster || "") !== "organizational_info").length;
    if (activeCount > 0) {
      els.badgeMaterials.textContent = String(activeCount);
      els.badgeMaterials.classList.remove("hidden");
    } else {
      els.badgeMaterials.classList.add("hidden");
    }
    renderMaterialsModule();
  } catch (err) {
    els.contentArea.innerHTML = `<div class="empty-day-card"><h3>BŁĄD POBIERANIA ZADAŃ</h3><p>${escapeHtml(err.message)}</p></div>`;
  }
}

function renderMaterialsModule() {
  const data = state.materialsData;
  if (!data) return;
  const filter = state.materialsFilter;
  const clusterFilter = state.materialsCluster || "active";

  const rawActionItems = data.action_items || [];
  const sourceFiltered = rawActionItems.filter((item) => {
    const srcType = item.kind || item.source_type || "";
    if (filter === "homework" && srcType !== "homework" && srcType !== "schedule" && srcType !== "agenda_event" && srcType !== "calendar") return false;
    if (filter === "messages" && srcType !== "message") return false;
    if (filter === "announcements" && srcType !== "announcement") return false;
    return true;
  });

  const q = state.materialsSearch.trim().toLowerCase();
  const searchFiltered = sourceFiltered.filter((item) => {
    if (!q) return true;
    const hay = [
      item.subject_label || item.subject,
      item.sender,
      item.title,
      item.action_summary,
      item.deadline_date || item.deadline,
      item.display_date || item.date_short,
      item.category_label,
      item.priority_label,
      item.action_target,
      ...(item.codes || []),
    ]
      .filter(Boolean)
      .join(" ")
      .toLowerCase();
    return hay.includes(q);
  });

  const getItemCluster = (item) => {
    if (item.cluster) return item.cluster;
    const textLow = `${item.title || ""} ${item.action_summary || ""}`.toLowerCase();
    if (
      item.kind !== "homework" &&
      (textLow.includes("zapraszamy") ||
        textLow.includes("konkurs") ||
        textLow.includes("olimpiad") ||
        textLow.includes("turniej") ||
        textLow.includes("zawody") ||
        textLow.includes("chętnych"))
    ) {
      return "optional_competition";
    }
    if (item.priority === "urgent" || item.priority === "important") {
      return "action_required";
    }
    return "organizational_info";
  };

  const todayMid = new Date();
  todayMid.setHours(0, 0, 0, 0);

  const isUrgentUnder3Days = (item) => {
    if (typeof item.days_to_deadline === "number") {
      return item.days_to_deadline >= 0 && item.days_to_deadline <= 3;
    }
    const dlRaw = (item.deadline_date || item.deadline || "").slice(0, 10);
    if (!/^\d{4}-\d{2}-\d{2}$/.test(dlRaw)) return false;
    const [y, m, d] = dlRaw.split("-").map(Number);
    const dlDate = new Date(y, m - 1, d);
    dlDate.setHours(0, 0, 0, 0);
    const diffDays = Math.round((dlDate.getTime() - todayMid.getTime()) / 86400000);
    return diffDays >= 0 && diffDays <= 3;
  };

  const countsByCluster = {
    active: searchFiltered.filter((i) => getItemCluster(i) !== "organizational_info").length,
    all: searchFiltered.length,
    urgent_3d: searchFiltered.filter((i) => isUrgentUnder3Days(i)).length,
    action_required: searchFiltered.filter((i) => getItemCluster(i) === "action_required").length,
    materials_and_changes: searchFiltered.filter((i) => getItemCluster(i) === "materials_and_changes").length,
    optional_competition: searchFiltered.filter((i) => getItemCluster(i) === "optional_competition").length,
    organizational_info: searchFiltered.filter((i) => getItemCluster(i) === "organizational_info").length,
  };

  const filtered = searchFiltered.filter((item) => {
    if (clusterFilter === "active") return getItemCluster(item) !== "organizational_info";
    if (clusterFilter === "all") return true;
    if (clusterFilter === "urgent_3d") return isUrgentUnder3Days(item);
    return getItemCluster(item) === clusterFilter;
  });

  const clusterDefs = [
    {
      key: "action_required",
      title: "Wymaga działania — do dopilnowania lub przekazania dziecku",
      headerBg: "var(--pastel-sky-bg)",
      headerColor: "var(--pastel-sky-text)",
    },
    {
      key: "materials_and_changes",
      title: "Informacje — materiały do nauki i zmiany w planie",
      headerBg: "var(--bg-subtle)",
      headerColor: "var(--text-primary)",
    },
    {
      key: "optional_competition",
      title: "Opcjonalne — konkursy i wydarzenia dla chętnych",
      headerBg: "var(--bg-subtle)",
      headerColor: "var(--text-secondary)",
    },
    {
      key: "organizational_info",
      title: "Pozostałe komunikaty i archiwum (bez wymaganej akcji)",
      headerBg: "var(--bg-subtle)",
      headerColor: "var(--text-secondary)",
    },
  ];

  // Closed 6-item dictionary for 03 Zadania (each has unique color + shape + symbol)
  const TASK_TAG_DICTIONARY = {
    action_required: {
      code: "action_required",
      label: "WYMAGA DZIAŁANIA",
      symbol: "▲",
      cssClass: "tag-action_required",
      desc: "Zadanie domowe, wpłata, zgoda lub sprawa do dopilnowania w terminie.",
    },
    schedule_change: {
      code: "schedule_change",
      label: "ZMIANA W PLANIE",
      symbol: "◆",
      cssClass: "tag-schedule_change",
      desc: "Odwołane zajęcia, zastępstwo, zmiana sali lub przesunięcie lekcji.",
    },
    study_material: {
      code: "study_material",
      label: "MATERIAŁY",
      symbol: "■",
      cssClass: "tag-study_material",
      desc: "Zagadnienia, notatki, załączniki DOCX/PDF lub kody dostępu od nauczyciela.",
    },
    confirmation: {
      code: "confirmation",
      label: "POTWIERDZENIE",
      symbol: "✔",
      cssClass: "tag-confirmation",
      desc: "Potwierdzenie wpłaty, odbioru lub zapisu — sprawa załatwiona, nie wymaga reakcji.",
    },
    optional: {
      code: "optional",
      label: "DLA CHĘTNYCH",
      symbol: "★",
      cssClass: "tag-optional",
      desc: "Konkurs, olimpiada, zawody lub wydarzenie dodatkowe dla chętnych uczniów.",
    },
    info: {
      code: "info",
      label: "KOMUNIKAT",
      symbol: "●",
      cssClass: "tag-info",
      desc: "Ogólne ogłoszenie szkolne lub informacja organizacyjna niewymagająca reakcji.",
    },
  };

  const resolveTaskDictTag = (item, clusterKey) => {
    if (item.tag_code && TASK_TAG_DICTIONARY[item.tag_code]) {
      return TASK_TAG_DICTIONARY[item.tag_code];
    }
    const textLow = `${item.title || ""} ${item.action_summary || ""} ${item.category_label || ""}`.toLowerCase();
    if (
      textLow.includes("potwierdzen") ||
      textLow.includes("zaksięgowan") ||
      textLow.includes("otrzymaliśmy wpłat") ||
      textLow.includes("wpłata została") ||
      textLow.includes("opłacono") ||
      textLow.includes("dziękujemy za wpłat")
    ) {
      return TASK_TAG_DICTIONARY.confirmation;
    }
    if (clusterKey === "optional_competition") {
      return TASK_TAG_DICTIONARY.optional;
    }
    if (clusterKey === "action_required") {
      return TASK_TAG_DICTIONARY.action_required;
    }
    if (
      item.kind === "schedule" ||
      textLow.includes("odwołan") ||
      textLow.includes("zastępstw") ||
      textLow.includes("przeniesion") ||
      textLow.includes("zmiana w planie") ||
      textLow.includes("zmiana planu")
    ) {
      return TASK_TAG_DICTIONARY.schedule_change;
    }
    if (clusterKey === "materials_and_changes") {
      return TASK_TAG_DICTIONARY.study_material;
    }
    return TASK_TAG_DICTIONARY.info;
  };

  const renderItemRow = (item, idx) => {
    const clusterKey = getItemCluster(item);
    const dictTag = resolveTaskDictTag(item, clusterKey);

    const targetLabel = item.action_target || "";
    const showRecipientSubtag =
      dictTag.code === "action_required" &&
      (targetLabel === "DLA RODZICA" || targetLabel === "DLA UCZNIA");
    const targetBadgeHtml = showRecipientSubtag
      ? `<div><span class="task-recipient-subtag">→ ${escapeHtml(targetLabel)}</span></div>`
      : "";

    // Explicit account / source flag so it is never lost
    const isStudentAcc = item.account_source === "uczeń";
    let accountFlagBadge = "";
    if (isStudentAcc) {
      accountFlagBadge = `<span class="badge badge-now" style="font-size:9px;padding:1px 5px;">KONTO UCZNIA</span>`;
    } else if (item.kind === "announcement") {
      accountFlagBadge = `<span class="badge badge-info" style="font-size:9px;padding:1px 5px;">OGŁOSZENIE</span>`;
    } else if (item.kind === "schedule") {
      accountFlagBadge = `<span class="badge badge-info" style="font-size:9px;padding:1px 5px;">TERMINARZ</span>`;
    } else {
      accountFlagBadge = `<span class="badge badge-info" style="font-size:9px;padding:1px 5px;">KONTO RODZICA</span>`;
    }

    const codes = item.codes || [];
    const codesHtml = codes.length > 0
      ? `<span style="margin-left:6px;">${codes.map((c) => `<span class="code-chip">KOD: ${escapeHtml(c)}</span>`).join(" ")}</span>`
      : "";

    const deadlineStr = item.deadline_date || item.deadline || "";
    const dateShortStr = (item.display_date || item.date_short || "").replace("T", " ").slice(0, 16);
    const subjectStr = item.subject_label || item.subject || "Sprawy szkolne";

    const deadlineDisplay = deadlineStr
      ? `<span class="cell-primary" style="color:${clusterKey === "optional_competition" || clusterKey === "organizational_info" ? "var(--text-primary)" : "var(--accent-primary)"};font-weight:700;">Termin: ${escapeHtml(deadlineStr)}</span>`
      : `<span class="cell-primary">${escapeHtml(dateShortStr || "---")}</span>`;

    const dateSubDisplay = deadlineStr && dateShortStr && dateShortStr.slice(0, 10) !== deadlineStr
      ? `<span class="cell-secondary">Wysłano: ${escapeHtml(dateShortStr.slice(0, 10))}</span>`
      : `<span class="cell-secondary">${escapeHtml(item.category_label || (item.kind === "announcement" ? "Ogłoszenie" : "Wiadomość"))}</span>`;

    return `
      <tr class="${item.unread ? "row-highlight" : ""}" data-action-idx="${idx}" style="cursor:pointer;">
        <td style="width:168px;white-space:nowrap;">
          <span class="task-dict-badge ${dictTag.cssClass}" title="${escapeHtml(dictTag.desc)}">
            <span class="dict-symbol" aria-hidden="true">${dictTag.symbol}</span>
            <span>${escapeHtml(dictTag.label)}</span>
          </span>
          ${targetBadgeHtml}
        </td>
        <td style="width:135px;white-space:nowrap;">
          ${deadlineDisplay}
          ${dateSubDisplay}
        </td>
        <td style="width:195px;">
          <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-bottom:2px;">
            <span class="cell-primary">${escapeHtml(subjectStr)}</span>
            ${accountFlagBadge}
          </div>
          <span class="cell-secondary">${escapeHtml(item.sender || item.category_label || "")}</span>
        </td>
        <td>
          <div style="font-family:var(--prose);font-size:13px;font-weight:600;line-height:1.4;color:var(--text-primary);">
            ${escapeHtml(item.action_summary || item.title)}
            ${codesHtml}
          </div>
          ${item.title && item.action_summary && item.title !== item.action_summary
            ? `<div class="cell-secondary" style="margin-top:2px;">Temat: ${escapeHtml(item.title)}</div>`
            : ""
          }
        </td>
        <td style="width:95px;text-align:right;white-space:nowrap;">
          <button class="action-button open-action-item-btn" data-action-idx="${idx}" type="button">Otwórz</button>
        </td>
      </tr>
    `;
  };

  const tbodyChunks = [];
  clusterDefs.forEach((cDef) => {
    const clusterItems = [];
    filtered.forEach((item, idx) => {
      if (getItemCluster(item) === cDef.key) {
        clusterItems.push({ item, idx });
      }
    });
    if (clusterItems.length === 0) return;
    tbodyChunks.push(`
      <tr class="cluster-section-row">
        <td colspan="5" style="background:${cDef.headerBg};color:${cDef.headerColor};padding:6px 12px;border-top:1px solid var(--border-strong);border-bottom:1px solid var(--border-color);">
          <span style="font-family:var(--prose);font-size:12px;font-weight:700;letter-spacing:0.01em;">
            ${escapeHtml(cDef.title)} <span style="font-family:var(--accent-mono);font-size:11px;font-weight:700;opacity:0.85;">(${clusterItems.length})</span>
          </span>
        </td>
      </tr>
    `);
    clusterItems.forEach(({ item, idx }) => {
      tbodyChunks.push(renderItemRow(item, idx));
    });
  });

  if (clusterFilter === "active" && countsByCluster.organizational_info > 0) {
    tbodyChunks.push(`
      <tr class="cluster-section-row">
        <td colspan="5" style="background:var(--surface-muted);color:var(--text-muted);padding:8px 12px;border-top:1px solid var(--border-color);text-align:center;font-family:var(--prose);font-size:11px;">
          Odfiltrowano <strong>${countsByCluster.organizational_info}</strong> wiadomości konwersacyjnych (np. odpowiedzi „Re:”) oraz komunikatów archiwalnych niewymagających żadnej akcji.
          <button type="button" id="showArchivedTasksInlineBtn" class="action-button" style="height:22px;padding:0 8px;margin-left:8px;font-size:10px;">Pokaż bez akcji / archiwum (${countsByCluster.organizational_info})</button>
        </td>
      </tr>
    `);
  }

  const panel = document.createElement("div");
  panel.className = "surface-panel content-card data-table-card";

  panel.innerHTML = `
    <div class="pi-action-bar" style="border-left:0;border-right:0;border-top:0;">
      <input
        type="search"
        id="materialsSearchInput"
        class="pi-search-input"
        placeholder="Szukaj przedmiotu, zadania, terminu lub kodu..."
        value="${escapeHtml(state.materialsSearch)}"
      />
      <select id="materialsTypeSelect" class="pi-select">
        <option value="all" ${filter === "all" ? "selected" : ""}>Typ: Wszystko</option>
        <option value="homework" ${filter === "homework" ? "selected" : ""}>Typ: Zadania i wyjścia</option>
        <option value="messages" ${filter === "messages" ? "selected" : ""}>Typ: Wiadomości</option>
        <option value="announcements" ${filter === "announcements" ? "selected" : ""}>Typ: Ogłoszenia</option>
      </select>
      <div class="pi-filter-pills">
        <button type="button" class="pi-filter-pill ${clusterFilter === "active" ? "active" : ""}" data-cluster="active" title="Zadania wymagające działania, informacje i opcjonalne (bez szumu konwersacyjnego)">Aktywne (${countsByCluster.active})</button>
        <button type="button" class="pi-filter-pill ${clusterFilter === "urgent_3d" ? "active" : ""}" data-cluster="urgent_3d" title="Punkty z wszystkich sekcji z terminem ważności do 3 dni">Pilne &lt; 3 dni (${countsByCluster.urgent_3d})</button>
        <button type="button" class="pi-filter-pill ${clusterFilter === "action_required" ? "active" : ""}" data-cluster="action_required">Wymaga działania (${countsByCluster.action_required})</button>
        <button type="button" class="pi-filter-pill ${clusterFilter === "materials_and_changes" ? "active" : ""}" data-cluster="materials_and_changes">Informacje (${countsByCluster.materials_and_changes})</button>
        <button type="button" class="pi-filter-pill ${clusterFilter === "optional_competition" ? "active" : ""}" data-cluster="optional_competition">Opcjonalne (${countsByCluster.optional_competition})</button>
        <button type="button" class="pi-filter-pill ${clusterFilter === "organizational_info" ? "active" : ""}" data-cluster="organizational_info" title="Wiadomości konwersacyjne, podziękowania i komunikaty archiwalne bez wymaganej akcji">Bez akcji / Archiwum (${countsByCluster.organizational_info})</button>
      </div>
      <span class="pi-counter">${filtered.length} / ${rawActionItems.length}</span>
      <button type="button" id="materialsResetBtn" class="action-button">Reset</button>
      <div class="legend-popover-wrap" id="materialsLegendWrap">
        <button type="button" id="materialsLegendBtn" class="legend-help-btn ${state.materialsLegendOpen ? "active" : ""}" title="Legenda etykiet (kolory i kształty)" aria-expanded="${state.materialsLegendOpen ? "true" : "false"}">?</button>
        <div id="materialsLegendPopover" class="legend-popover ${state.materialsLegendOpen ? "" : "hidden"}" role="dialog" aria-label="Legenda etykiet zadań">
          <div class="legend-popover-header">
            <span class="legend-popover-title">Słownik etykiet (kolor + kształt)</span>
            <button type="button" id="closeMaterialsLegendBtn" class="alert-step-btn" title="Zamknij legendę" style="border:1px solid var(--line-strong);background:var(--control-bg);">✕</button>
          </div>
          <div class="legend-list">
            ${Object.values(TASK_TAG_DICTIONARY).map((dt) => `
              <div class="legend-row">
                <div>
                  <span class="task-dict-badge ${dt.cssClass}">
                    <span class="dict-symbol" aria-hidden="true">${dt.symbol}</span>
                    <span>${escapeHtml(dt.label)}</span>
                  </span>
                </div>
                <div>${escapeHtml(dt.desc)}</div>
              </div>
            `).join("")}
          </div>
        </div>
      </div>
    </div>
    <div class="data-table-scroll">
      <table class="data-table">
        <thead>
          <tr>
            <th>TYP / ETYKIETA</th>
            <th>TERMIN / DATA</th>
            <th>PRZEDMIOT / ŹRÓDŁO</th>
            <th>ACTION ITEM (SYNTEZA ZADANIA LUB KOMUNIKATU)</th>
            <th style="text-align:right;">SZCZEGÓŁY</th>
          </tr>
        </thead>
        <tbody>
          ${tbodyChunks.join("") || `<tr><td colspan="5">Brak zadań spełniających kryteria filtra.</td></tr>`}
        </tbody>
      </table>
    </div>
  `;

  els.contentArea.innerHTML = "";
  els.contentArea.appendChild(panel);

  const openItemHandler = (idx) => {
    const item = filtered[idx];
    if (!item) return;
    const srcType = item.kind || item.source_type || "";
    if (srcType === "message" && item.message_id) {
      openMessageModal(item.message_id, item.folder || "received", false, item.account_source || "rodzic");
    } else {
      openActionItemModal(item);
    }
  };

  panel.querySelectorAll("tbody tr[data-action-idx]").forEach((tr) => {
    tr.onclick = () => openItemHandler(Number(tr.dataset.actionIdx));
  });

  const typeSelect = document.getElementById("materialsTypeSelect");
  if (typeSelect) {
    typeSelect.onchange = (e) => {
      state.materialsFilter = e.target.value;
      renderMaterialsModule();
    };
  }

  panel.querySelectorAll(".pi-filter-pill[data-cluster]").forEach((pill) => {
    pill.onclick = () => {
      state.materialsCluster = pill.dataset.cluster;
      renderMaterialsModule();
    };
  });

  const searchInput = document.getElementById("materialsSearchInput");
  if (searchInput) {
    searchInput.oninput = (e) => {
      state.materialsSearch = e.target.value;
      renderMaterialsModule();
      const restored = document.getElementById("materialsSearchInput");
      if (restored) {
        restored.focus();
        restored.setSelectionRange(restored.value.length, restored.value.length);
      }
    };
  }
  const resetBtn = document.getElementById("materialsResetBtn");
  if (resetBtn) {
    resetBtn.onclick = () => {
      state.materialsSearch = "";
      state.materialsFilter = "all";
      state.materialsCluster = "active";
      state.materialsPriority = "all";
      renderMaterialsModule();
    };
  }
  const showArchivedInlineBtn = document.getElementById("showArchivedTasksInlineBtn");
  if (showArchivedInlineBtn) {
    showArchivedInlineBtn.onclick = () => {
      state.materialsCluster = "organizational_info";
      renderMaterialsModule();
    };
  }
  const legendBtn = document.getElementById("materialsLegendBtn");
  const legendPopover = document.getElementById("materialsLegendPopover");
  const closeLegendBtn = document.getElementById("closeMaterialsLegendBtn");
  if (legendBtn && legendPopover) {
    legendBtn.onclick = (e) => {
      e.stopPropagation();
      state.materialsLegendOpen = !state.materialsLegendOpen;
      legendPopover.classList.toggle("hidden", !state.materialsLegendOpen);
      legendBtn.classList.toggle("active", Boolean(state.materialsLegendOpen));
      legendBtn.setAttribute("aria-expanded", state.materialsLegendOpen ? "true" : "false");
    };
    legendPopover.onclick = (e) => e.stopPropagation();
  }
  if (closeLegendBtn && legendPopover && legendBtn) {
    closeLegendBtn.onclick = (e) => {
      e.stopPropagation();
      state.materialsLegendOpen = false;
      legendPopover.classList.add("hidden");
      legendBtn.classList.remove("active");
      legendBtn.setAttribute("aria-expanded", "false");
    };
  }
}

function openActionItemModal(item) {
  els.messageModal.classList.remove("hidden");
  const prioLabel = item.priority_label || item.priority || "INFO";
  const subjectStr = item.subject_label || item.subject || "---";
  const deadlineStr = item.deadline_date || item.deadline || "";
  const dateShortStr = item.display_date || item.date_short || "---";
  els.messageModalFolderBadge.textContent = `${item.category_label || "ZADANIE"} · ${prioLabel}`;
  els.messageModalSubject.textContent = item.title || item.action_summary || "Szczegóły zadania";
  els.messageModalMeta.textContent = `Przedmiot: ${subjectStr} · Autor: ${item.sender || "---"} ${deadlineStr ? `· Termin: ${deadlineStr}` : `· Data: ${dateShortStr}`}`;

  const codes = item.codes || [];
  const codesHtml = codes.length > 0
    ? `<div class="chip-row" style="margin-bottom:8px;">${codes.map((c) => `<span class="code-chip">KOD: ${escapeHtml(c)}</span>`).join("")}</div>`
    : "";

  const links = item.links || [];
  const linksHtml = links.length > 0
    ? `<div class="chip-row" style="margin-bottom:8px;">${links.map((l) => `<a href="${escapeHtml(l)}" target="_blank" rel="noopener" class="btn-outline btn-xs">OTWÓRZ LINK ↗</a>`).join("")}</div>`
    : "";

  els.messageModalBody.innerHTML = `
    <div class="scope-box" style="border-left:3px solid var(--accent-primary);">
      <div style="font-family:var(--accent-mono);font-size:11px;text-transform:uppercase;color:var(--accent-primary);margin-bottom:4px;">
        ACTION ITEM (SYNTEZA):
      </div>
      <div style="font-weight:700;font-size:13px;">${escapeHtml(item.action_summary || item.title)}</div>
    </div>
    ${codesHtml}
    ${linksHtml}
    <div class="scope-box" style="white-space:pre-wrap;line-height:1.6;">
      <div style="font-family:var(--accent-mono);font-size:11px;text-transform:uppercase;color:var(--text-secondary);margin-bottom:6px;">
        PEŁNA ORYGINALNA TREŚĆ WPISU:
      </div>
      ${escapeHtml(item.full_body || item.action_summary || "Brak dodatkowego opisu.")}
    </div>
  `;
}

/* ==========================================================================
   MODULE 4: GRADES & ATTENDANCE (pi.dev/models Sticky Data Tables)
   ========================================================================== */
async function loadGrades(forceRefresh = false) {
  if (!state.activeAccount) return;
  els.contentArea.innerHTML = `
    <div class="loading-state">
      <div class="spinner"></div>
      <p>POBIERANIE OCEN, ŚREDNICH WAŻONYCH I FREKWENCJI...</p>
    </div>
  `;
  try {
    const url = `/api/grades-attendance?account=${encodeURIComponent(state.activeAccount)}${forceRefresh ? "&refresh=1" : ""}`;
    const data = await apiFetch(url);
    state.gradesData = data;
    renderGradesModule();
  } catch (err) {
    els.contentArea.innerHTML = `<div class="empty-day-card"><h3>BŁĄD POBIERANIA OCEN</h3><p>${escapeHtml(err.message)}</p></div>`;
  }
}

function getGradeClass(valStr) {
  const v = String(valStr || "").trim();
  if (v.includes("/")) {
    const parts = v.split("/");
    const num = parseFloat(parts[0].replace(",", "."));
    const den = parseFloat((parts[1] || "").replace(",", "."));
    if (!isNaN(num) && !isNaN(den) && den > 0) {
      const pct = (num / den) * 100;
      if (pct >= 85) return "grade-high";
      if (pct >= 65) return "grade-mid";
      if (pct >= 45) return "grade-warn";
      return "grade-low";
    }
    return "";
  }
  if (v.startsWith("6") || v.startsWith("5")) return "grade-high";
  if (v.startsWith("4") || v.startsWith("3")) return "grade-mid";
  if (v.startsWith("2")) return "grade-warn";
  if (v === "1" || v === "1+" || v === "1-" || v === "np" || v === "bz") return "grade-low";
  return "";
}

function renderGradesModule() {
  const data = state.gradesData;
  if (!data) return;
  const overallFreq = (data.frequency && data.frequency.overall) || {};
  const sem1Freq = (data.frequency && data.frequency.first_semester) || {};
  const allSubjects = data.subjects || [];
  const recentGrades = data.recent_grades || [];
  const recentAtt = data.recent_attendance || [];

  const dateFilter = state.gradesDateFilter || "all";
  const todayStr = formatIsoDate(new Date());
  const parseIsoDay = (s) => {
    const clean = String(s || "").slice(0, 10);
    if (!/^\d{4}-\d{2}-\d{2}$/.test(clean)) return null;
    const [y, m, d] = clean.split("-").map(Number);
    return new Date(y, m - 1, d);
  };
  const todayObj = parseIsoDay(todayStr) || new Date();

  const getDaysOld = (gradeObj) => {
    const dt = parseIsoDay(gradeObj.day || gradeObj.date);
    if (!dt) return 999;
    return Math.round((todayObj.getTime() - dt.getTime()) / (1000 * 60 * 60 * 24));
  };

  // Determine which grades count as "NOWA" or match the active date filter
  const maxDaysWindow =
    dateFilter === "today"
      ? 0
      : dateFilter === "3d"
      ? 3
      : dateFilter === "7d"
      ? 7
      : dateFilter === "14d"
      ? 14
      : 7; // when 'all' is selected, highlight last 7 days as NOWA

  const matchesGradeWindow = (g) => {
    const age = getDaysOld(g);
    return age >= 0 && age <= maxDaysWindow;
  };

  const countGradesInDays = (days) =>
    recentGrades.filter((g) => {
      const age = getDaysOld(g);
      return age >= 0 && age <= days;
    }).length;

  const q = state.gradesSearch.trim().toLowerCase();
  const filteredSubjects = allSubjects
    .filter((s) => {
      if (q && !String(s.subject || "").toLowerCase().includes(q)) return false;
      if (dateFilter !== "all") {
        const hasInWindow = (s.grades || []).some((g) => matchesGradeWindow(g));
        if (!hasInWindow) return false;
      }
      return true;
    })
    .slice()
    .sort((a, b) => {
      if (state.gradesSort === "avg-desc") {
        return (Number(b.average ?? b.weighted_average ?? -1)) - (Number(a.average ?? a.weighted_average ?? -1));
      }
      if (state.gradesSort === "count-desc") {
        return ((b.grades || []).length) - ((a.grades || []).length);
      }
      return String(a.subject || "").localeCompare(String(b.subject || ""), "pl");
    });

  const wrapper = document.createElement("div");
  wrapper.style.display = "flex";
  wrapper.style.flexDirection = "column";
  wrapper.style.gap = "12px";
  wrapper.style.paddingBottom = "24px";

  const subjectRowsHtml = filteredSubjects.map((s) => {
    const avg = s.average ?? s.weighted_average;
    const gradesList = s.grades || [];
    const visibleGrades = dateFilter !== "all"
      ? gradesList.filter((g) => matchesGradeWindow(g))
      : gradesList;
    const recentCountInSubject = gradesList.filter((g) => matchesGradeWindow(g)).length;

    const pillsHtml = visibleGrades.length === 0
      ? `<span class="card-subtitle">Brak ocen</span>`
      : visibleGrades.map((g) => {
          const rawG = g.grade || g.value;
          const isRecent = matchesGradeWindow(g);
          return `<span class="grade-pill ${getGradeClass(rawG)} ${isRecent ? "is-recent" : ""}" title="${isRecent ? "[NOWA OCENA] " : ""}${escapeHtml(g.category || "")} | Waga: ${g.weight ?? "-"} | Data: ${escapeHtml(g.day || "")} | ${escapeHtml(g.comment || "")}">${escapeHtml(rawG)}</span>`;
        }).join(" ");

    return `
      <tr class="${recentCountInSubject > 0 ? "row-highlight" : ""}">
        <td style="width:240px;">
          <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;">
            <span class="cell-primary">${escapeHtml(s.subject)}</span>
            ${recentCountInSubject > 0 ? `<span class="badge badge-now" style="font-size:9px;padding:1px 5px;">+${recentCountInSubject} NOWA</span>` : ""}
          </div>
        </td>
        <td style="width:110px;font-weight:700;">${avg != null ? Number(avg).toFixed(2) : "---"}</td>
        <td style="width:90px;">${visibleGrades.length}${dateFilter !== "all" ? ` / ${gradesList.length}` : ""}</td>
        <td><div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center;">${pillsHtml}</div></td>
      </tr>
    `;
  }).join("");

  const filteredRecentGrades = dateFilter !== "all"
    ? recentGrades.filter((g) => matchesGradeWindow(g))
    : recentGrades;

  const recentGradeRowsHtml = filteredRecentGrades.slice(0, 16).map((g) => {
    const rawGrade = g.grade || g.value;
    const isNew = matchesGradeWindow(g);
    return `
      <tr>
        <td style="width:95px;">${escapeHtml(g.day || g.date || "---")}</td>
        <td><span class="cell-primary">${escapeHtml(g.subject)}</span></td>
        <td>
          <span class="cell-secondary">${escapeHtml(g.category || "Ocena")}${g.weight ? ` · waga ${escapeHtml(g.weight)}` : ""}</span>
        </td>
        <td style="width:70px;text-align:right;">
          <span class="grade-pill ${getGradeClass(rawGrade)} ${isNew ? "is-recent" : ""}" title="${escapeHtml(g.comment || "")}">${escapeHtml(rawGrade)}</span>
        </td>
      </tr>
    `;
  }).join("");

  const recentAttRowsHtml = recentAtt.slice(0, 14).map((a) => `
    <tr>
      <td style="width:95px;">${escapeHtml(a.day || a.date || "---")}</td>
      <td style="width:70px;">#${escapeHtml(a.period ?? "-")}</td>
      <td><span class="cell-primary">${escapeHtml(a.subject || `Lekcja #${a.period}`)}</span></td>
      <td style="width:90px;text-align:right;">
        <span class="badge ${a.symbol === "nb" ? "badge-cancelled" : "badge-sub"}">${escapeHtml(a.symbol || a.type)}</span>
      </td>
    </tr>
  `).join("");

  const activeWindowLabel =
    dateFilter === "today"
      ? "z dzisiaj"
      : dateFilter === "3d"
      ? "z ostatnich 3 dni"
      : dateFilter === "7d"
      ? "z ostatnich 7 dni"
      : dateFilter === "14d"
      ? "z ostatnich 14 dni"
      : "z ostatnich 7 dni";

  wrapper.innerHTML = `
    <div class="stats-banner">
      <div class="stat-tile">
        <span class="stat-value" style="color:var(--accent-emerald)">${overallFreq.percentage != null ? `${overallFreq.percentage}%` : "---"}</span>
        <span class="stat-label">Frekwencja ogólna (${overallFreq.attended ?? 0}/${overallFreq.total ?? 0})</span>
      </div>
      <div class="stat-tile">
        <span class="stat-value">${sem1Freq.percentage != null ? `${sem1Freq.percentage}%` : "---"}</span>
        <span class="stat-label">I Semestr (${sem1Freq.attended ?? 0}/${sem1Freq.total ?? 0})</span>
      </div>
      <div class="stat-tile">
        <span class="stat-value" style="color:var(--accent-primary)">${recentGrades.filter((g) => matchesGradeWindow(g)).length}</span>
        <span class="stat-label">Nowe oceny (${activeWindowLabel})</span>
      </div>
      <div class="stat-tile">
        <span class="stat-value" style="color:var(--accent-amber)">${recentAtt.length}</span>
        <span class="stat-label">Wpisy nieobecności / spóźnień</span>
      </div>
    </div>

    <!-- Subjects & Weighted Averages Table (pi.dev/models style) -->
    <div class="surface-panel content-card data-table-card">
      <div class="pi-action-bar" style="border-left:0;border-right:0;border-top:0;">
        <input
          type="search"
          id="gradesSubjectSearch"
          class="pi-search-input"
          placeholder="Filtruj przedmioty..."
          value="${escapeHtml(state.gradesSearch)}"
        />
        <div class="pi-filter-pills">
          <button type="button" class="pi-filter-pill ${dateFilter === "all" ? "active" : ""}" data-gdate="all">
            Wszystkie
          </button>
          <button type="button" class="pi-filter-pill ${dateFilter === "today" ? "active" : ""}" data-gdate="today">
            Dziś (${countGradesInDays(0)})
          </button>
          <button type="button" class="pi-filter-pill ${dateFilter === "3d" ? "active" : ""}" data-gdate="3d">
            Ostatnie 3 dni (${countGradesInDays(3)})
          </button>
          <button type="button" class="pi-filter-pill ${dateFilter === "7d" ? "active" : ""}" data-gdate="7d">
            Ostatnie 7 dni (${countGradesInDays(7)})
          </button>
          <button type="button" class="pi-filter-pill ${dateFilter === "14d" ? "active" : ""}" data-gdate="14d">
            Ostatnie 14 dni (${countGradesInDays(14)})
          </button>
        </div>
        <select id="gradesSortSelect" class="pi-select">
          <option value="subject-asc" ${state.gradesSort === "subject-asc" ? "selected" : ""}>Sortuj: Przedmiot (A–Z)</option>
          <option value="avg-desc" ${state.gradesSort === "avg-desc" ? "selected" : ""}>Sortuj: Średnia ważona (malejąco)</option>
          <option value="count-desc" ${state.gradesSort === "count-desc" ? "selected" : ""}>Sortuj: Liczba ocen (malejąco)</option>
        </select>
        <span class="pi-counter">${filteredSubjects.length} / ${allSubjects.length}</span>
        <button type="button" id="gradesResetBtn" class="action-button">Reset</button>
      </div>
      <div class="data-table-scroll">
        <table class="data-table">
          <thead>
            <tr>
              <th>PRZEDMIOT</th>
              <th>ŚR. WAŻONA</th>
              <th>OCEN</th>
              <th>OCENY CZĄSTKOWE (NOWE OCENY ${activeWindowLabel.toUpperCase()} OZNACZONE „NOWA”)</th>
            </tr>
          </thead>
          <tbody>
            ${subjectRowsHtml || `<tr><td colspan="4">Brak ocen w wybranym przedziale dat (${activeWindowLabel}). Kliknij „Wszystkie” lub wybierz szerszy zakres.</td></tr>`}
          </tbody>
        </table>
      </div>
    </div>

    <!-- Side-by-side Recent Grades & Recent Attendance Tables -->
    <div class="cards-grid">
      <div class="surface-panel content-card data-table-card">
        <div class="pi-action-bar" style="border-left:0;border-right:0;border-top:0;">
          <span class="toolbar-title" style="font-size:11px;">OSTATNIE OCENY W DZIENNIKU (${dateFilter === "all" ? "WSZYSTKIE OSTATNIE" : activeWindowLabel.toUpperCase()})</span>
          <span class="pi-counter">${Math.min(filteredRecentGrades.length, 16)} / ${filteredRecentGrades.length}</span>
        </div>
        <div class="data-table-scroll">
          <table class="data-table">
            <thead>
              <tr>
                <th>DATA</th>
                <th>PRZEDMIOT</th>
                <th>KATEGORIA / WAGA</th>
                <th style="text-align:right;">OCENA</th>
              </tr>
            </thead>
            <tbody>
              ${recentGradeRowsHtml || `<tr><td colspan="4">Brak ocen w wybranym przedziale czasowym.</td></tr>`}
            </tbody>
          </table>
        </div>
      </div>

      <div class="surface-panel content-card data-table-card">
        <div class="pi-action-bar" style="border-left:0;border-right:0;border-top:0;">
          <span class="toolbar-title" style="font-size:11px;">OSTATNIE NIEOBECNOŚCI I SPÓŹNIENIA</span>
          <span class="pi-counter">${Math.min(recentAtt.length, 14)} / ${recentAtt.length}</span>
        </div>
        <div class="data-table-scroll">
          <table class="data-table">
            <thead>
              <tr>
                <th>DATA</th>
                <th>LEKCJA</th>
                <th>PRZEDMIOT</th>
                <th style="text-align:right;">TYP</th>
              </tr>
            </thead>
            <tbody>
              ${recentAttRowsHtml || `<tr><td colspan="4">Brak wpisów nieobecności.</td></tr>`}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  `;

  els.contentArea.innerHTML = "";
  els.contentArea.appendChild(wrapper);

  wrapper.querySelectorAll(".pi-filter-pill[data-gdate]").forEach((pill) => {
    pill.onclick = () => {
      state.gradesDateFilter = pill.dataset.gdate;
      state.gradesOnlyRecent = state.gradesDateFilter !== "all";
      renderGradesModule();
    };
  });

  const searchInput = document.getElementById("gradesSubjectSearch");
  if (searchInput) {
    searchInput.oninput = (e) => {
      state.gradesSearch = e.target.value;
      renderGradesModule();
      const restored = document.getElementById("gradesSubjectSearch");
      if (restored) {
        restored.focus();
        restored.setSelectionRange(restored.value.length, restored.value.length);
      }
    };
  }
  const sortSelect = document.getElementById("gradesSortSelect");
  if (sortSelect) {
    sortSelect.onchange = (e) => {
      state.gradesSort = e.target.value;
      renderGradesModule();
    };
  }
  const resetBtn = document.getElementById("gradesResetBtn");
  if (resetBtn) {
    resetBtn.onclick = () => {
      state.gradesSearch = "";
      state.gradesSort = "subject-asc";
      state.gradesOnlyRecent = false;
      state.gradesDateFilter = "all";
      renderGradesModule();
    };
  }
}

/* ==========================================================================
   MODULE 5: MESSAGES, IN-RAM ATTACHMENT READER & 2-STEP REPLY (pi.dev Data Table)
   ========================================================================== */
async function loadMessages(forceRefresh = false) {
  if (!state.activeAccount) return;
  els.contentArea.innerHTML = `
    <div class="loading-state">
      <div class="spinner"></div>
      <p>POBIERANIE SKRZYNKI WIADOMOŚCI...</p>
    </div>
  `;
  try {
    const url = `/api/messages?account=${encodeURIComponent(state.activeAccount)}&folder=${encodeURIComponent(state.messagesFolder)}${forceRefresh ? "&refresh=1" : ""}`;
    const data = await apiFetch(url);
    state.messagesData = data;
    const msgs = data.items || data.messages || [];
    const unread = msgs.filter((m) => m.unread).length;
    if (unread > 0) {
      els.badgeMessages.textContent = String(unread);
      els.badgeMessages.classList.remove("hidden");
    } else {
      els.badgeMessages.classList.add("hidden");
    }
    renderMessagesModule();
  } catch (err) {
    els.contentArea.innerHTML = `<div class="empty-day-card"><h3>BŁĄD POBIERANIA WIADOMOŚCI</h3><p>${escapeHtml(err.message)}</p></div>`;
  }
}

function renderMessagesModule() {
  const allMsgs = (state.messagesData && (state.messagesData.items || state.messagesData.messages)) || [];

  const q = state.messagesSearch.trim().toLowerCase();
  const filtered = allMsgs.filter((m) => {
    const hasAtt = m.has_attachment || m.has_attachments;
    if (state.messagesQuickFilter === "unread" && !m.unread) return false;
    if (state.messagesQuickFilter === "attachments" && !hasAtt) return false;
    if (q) {
      const hay = `${m.correspondent || m.sender || ""} ${m.subject || ""}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });

  const panel = document.createElement("div");
  panel.className = "surface-panel content-card data-table-card";

  const rowsHtml = filtered.map((m) => {
    const sender = m.correspondent || m.sender || "---";
    const hasAtt = m.has_attachment || m.has_attachments;
    const accSrc = m.account_source || "rodzic";
    const accBadge = accSrc === "uczeń"
      ? `<span class="badge badge-now" style="font-size:9px;padding:1px 5px;margin-top:2px;">KONTO UCZNIA</span>`
      : `<span class="badge badge-info" style="font-size:9px;padding:1px 5px;margin-top:2px;">KONTO RODZICA</span>`;
    const bNote = m.behaviour_note;
    const isPosNote = bNote && String(bNote.kind || "").toLowerCase().includes("pozytywn");
    const isNegNote = Boolean(bNote && bNote.text && !isPosNote);
    const bNoteInline = bNote && bNote.text
      ? `<div style="margin-top:5px;padding:5px 9px;background:${isPosNote ? "var(--pastel-sage-bg)" : "var(--pastel-rose-bg)"};border:1px solid ${isPosNote ? "var(--pastel-sage-border)" : "var(--pastel-rose-border)"};border-left:4px solid ${isPosNote ? "var(--pastel-sage-border)" : "var(--pastel-rose-border)"};font-size:11.5px;color:${isPosNote ? "var(--pastel-sage-fg)" : "var(--pastel-rose-fg)"};line-height:1.4;">
           <span style="font-family:var(--accent-mono);font-size:10px;font-weight:800;color:${isPosNote ? "var(--pastel-sage-text)" : "var(--pastel-rose-text)"};text-transform:uppercase;margin-right:4px;">${isPosNote ? "✔ UWAGA POZYTYWNA:" : "▲ UWAGA NEGATYWNA:"}</span>
           <strong style="font-weight:700;">${escapeHtml(bNote.text)}</strong>${bNote.category ? ` <span style="opacity:0.85;">(${escapeHtml(bNote.category)})</span>` : ""}
         </div>`
      : "";
    return `
      <tr class="${m.unread ? "row-highlight" : ""} ${isNegNote ? "row-negative-note" : ""}" data-msg-id="${escapeHtml(m.id)}" data-account-source="${escapeHtml(accSrc)}" style="cursor:pointer;">
        <td style="width:135px;">
          ${m.unread
            ? `<span class="badge badge-sub" title="Chroniona przed automatycznym odczytaniem">NIEPRZECZYTANA</span>`
            : `<span class="badge badge-info">PRZECZYTANA</span>`
          }
        </td>
        <td style="width:240px;">
          <span class="cell-primary" style="${m.unread ? "font-weight:700;" : ""}">${escapeHtml(sender)}</span>
          <div>${accBadge}</div>
        </td>
        <td>
          <span class="cell-primary" style="${m.unread ? "font-weight:700;" : ""}">${escapeHtml(m.subject)}</span>
          ${bNoteInline}
        </td>
        <td style="width:110px;">
          ${hasAtt ? `<span class="badge badge-purple">ZAŁĄCZNIK</span>` : "---"}
        </td>
        <td style="width:135px;white-space:nowrap;">${escapeHtml(String(m.sent_at || "").replace("T", " ").slice(0, 16))}</td>
        <td style="width:100px;text-align:right;">
          <button type="button" class="action-button open-row-msg-btn" data-msg-id="${escapeHtml(m.id)}" data-account-source="${escapeHtml(accSrc)}">Otwórz</button>
        </td>
      </tr>
    `;
  }).join("");

  panel.innerHTML = `
    <div class="pi-action-bar" style="border-left:0;border-right:0;border-top:0;">
      <input
        type="search"
        id="messagesSearchInput"
        class="pi-search-input"
        placeholder="Szukaj nadawcy lub tematu wiadomości..."
        value="${escapeHtml(state.messagesSearch)}"
      />
      <div class="pi-filter-pills">
        <button type="button" class="pi-filter-pill ${state.messagesFolder === "received" ? "active" : ""}" data-folder="received">Odebrane</button>
        <button type="button" class="pi-filter-pill ${state.messagesFolder === "sent" ? "active" : ""}" data-folder="sent">Wysłane</button>
      </div>
      <div class="pi-filter-pills">
        <button type="button" class="pi-filter-pill ${state.messagesQuickFilter === "all" ? "active" : ""}" data-qfilter="all">Wszystkie</button>
        <button type="button" class="pi-filter-pill ${state.messagesQuickFilter === "unread" ? "active" : ""}" data-qfilter="unread">Nieprzeczytane</button>
        <button type="button" class="pi-filter-pill ${state.messagesQuickFilter === "attachments" ? "active" : ""}" data-qfilter="attachments">Z załącznikiem</button>
      </div>
      <span class="pi-counter">${filtered.length} / ${allMsgs.length}</span>
      <button type="button" id="messagesResetBtn" class="action-button">Reset</button>
    </div>
    <div class="data-table-scroll">
      <table class="data-table">
        <thead>
          <tr>
            <th>STATUS</th>
            <th>NADAWCA / KONTO</th>
            <th>TEMAT WIADOMOŚCI</th>
            <th>ZAŁĄCZNIK</th>
            <th>DATA</th>
            <th style="text-align:right;">AKCJA</th>
          </tr>
        </thead>
        <tbody>
          ${rowsHtml || `<tr><td colspan="6">Brak wiadomości spełniających kryteria filtra.</td></tr>`}
        </tbody>
      </table>
    </div>
  `;

  els.contentArea.innerHTML = "";
  els.contentArea.appendChild(panel);

  panel.querySelectorAll("tbody tr[data-msg-id]").forEach((tr) => {
    tr.onclick = () => openMessageModal(tr.dataset.msgId, state.messagesFolder, false, tr.dataset.accountSource || "");
  });

  const searchInput = document.getElementById("messagesSearchInput");
  if (searchInput) {
    searchInput.oninput = (e) => {
      state.messagesSearch = e.target.value;
      renderMessagesModule();
      const restored = document.getElementById("messagesSearchInput");
      if (restored) {
        restored.focus();
        restored.setSelectionRange(restored.value.length, restored.value.length);
      }
    };
  }
  panel.querySelectorAll(".pi-filter-pill[data-folder]").forEach((pill) => {
    pill.onclick = () => {
      const nextFolder = pill.dataset.folder;
      if (nextFolder && nextFolder !== state.messagesFolder) {
        state.messagesFolder = nextFolder;
        loadMessages(false);
      }
    };
  });
  panel.querySelectorAll(".pi-filter-pill[data-qfilter]").forEach((pill) => {
    pill.onclick = () => {
      state.messagesQuickFilter = pill.dataset.qfilter;
      renderMessagesModule();
    };
  });
  const resetBtn = document.getElementById("messagesResetBtn");
  if (resetBtn) {
    resetBtn.onclick = () => {
      state.messagesSearch = "";
      state.messagesQuickFilter = "all";
      renderMessagesModule();
    };
  }
}

async function openMessageModal(messageId, folder = "received", forceMarkRead = false, accountSource = "") {
  els.messageModal.classList.remove("hidden");
  const srcLabel = accountSource === "uczeń" ? " · KONTO UCZNIA" : (accountSource === "rodzic" ? " · KONTO RODZICA" : "");
  els.messageModalFolderBadge.textContent = `${folder === "sent" ? "WYSŁANA" : "ODEBRANA"}${srcLabel}`;
  els.messageModalSubject.textContent = "Ładowanie wiadomości...";
  els.messageModalMeta.textContent = "";
  els.messageModalBody.innerHTML = `<div class="loading-state" style="min-height:160px"><div class="spinner"></div></div>`;

  try {
    const detail = await apiFetch(
      `/api/messages/detail?account=${encodeURIComponent(state.activeAccount)}&folder=${encodeURIComponent(folder)}&id=${encodeURIComponent(messageId)}${accountSource ? `&account_source=${encodeURIComponent(accountSource)}` : ""}${forceMarkRead ? "&mark_read=1" : ""}`
    );
    const resolvedAccSrc = detail.account_source || accountSource || "rodzic";
    const resolvedBadgeSuffix = resolvedAccSrc === "uczeń" ? " · KONTO UCZNIA" : " · KONTO RODZICA";
    els.messageModalFolderBadge.textContent = `${folder === "sent" ? "WYSŁANA" : "ODEBRANA"}${resolvedBadgeSuffix}`;
    const sender = detail.correspondent || detail.sender || "---";
    els.messageModalSubject.textContent = detail.subject || "(Bez tematu)";
    els.messageModalMeta.textContent = `Od: ${sender} · Data: ${String(detail.sent_at || "---").replace("T", " ").slice(0, 16)}`;

    if (detail.protected_unread) {
      els.messageModalFolderBadge.textContent = `CHRONIONA (NIEPRZECZYTANA)${resolvedBadgeSuffix}`;
      els.messageModalBody.innerHTML = `
        <div class="scope-box" style="border-left:4px solid var(--accent-amber);">
          <div style="font-family:var(--accent-mono);font-size:13px;font-weight:700;margin-bottom:8px;text-transform:uppercase;">
            Ochrona statusu „Nieprzeczytana” w Librus Synergia
          </div>
          <p style="font-size:12px;line-height:1.6;margin-bottom:10px;">
            Ta wiadomość ma obecnie w Librusie status <strong>Nieprzeczytana</strong>. Serwery Librus Synergia (<code>wiadomosci.librus.pl</code>) wysyłają na początku wyłącznie nagłówki wiadomości (nadawcę, temat, datę i informację o załączniku). Pobranie pełnej treści wiadomości przez API wymaga wywołania <code>GET /api/inbox/messages/${escapeHtml(messageId)}</code>, co po stronie serwera Librusa <strong>automatycznie ustawia datę odczytania i informuje nauczyciela</strong>, że wiadomość została przeczytana.
          </p>
          <p style="font-size:12px;line-height:1.6;margin-bottom:12px;">
            Dzięki blokadzie w <strong>Librus // Hub</strong> żadna nieprzeczytana wiadomość nie jest otwierana w tle ani po przypadkowym kliknięciu.
          </p>
          <div style="display:flex;flex-wrap:wrap;gap:10px;align-items:center;">
            <button id="unlockUnreadMessageBtn" class="btn-primary" type="button">
              Pobierz treść z Librusa (oznacza jako przeczytaną)
            </button>
            <button id="keepUnreadCloseBtn" class="btn-outline" type="button">
              Pozostaw jako nieprzeczytaną
            </button>
          </div>
        </div>
      `;
      document.getElementById("unlockUnreadMessageBtn").onclick = async () => {
        await openMessageModal(messageId, folder, true, resolvedAccSrc);
        if (state.activeModule === "messages") {
          loadMessages(true);
        } else if (state.activeModule === "materials") {
          loadMaterials(true);
        }
      };
      document.getElementById("keepUnreadCloseBtn").onclick = () => {
        els.messageModal.classList.add("hidden");
      };
      return;
    }

    const bNote = detail.behaviour_note;
    const isPosDetailNote = bNote && String(bNote.kind || "").toLowerCase().includes("pozytywn");
    const bNoteBoxHtml = bNote && bNote.text
      ? `
        <div class="scope-box" style="background:${isPosDetailNote ? "var(--pastel-sage-bg)" : "var(--pastel-rose-bg)"};border:1px solid ${isPosDetailNote ? "var(--pastel-sage-border)" : "var(--pastel-rose-border)"};border-left:4px solid ${isPosDetailNote ? "var(--pastel-sage-text)" : "var(--pastel-rose-text)"};margin-bottom:12px;">
          <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;margin-bottom:6px;">
            <span style="font-family:var(--accent-mono);font-size:11px;font-weight:700;color:${isPosDetailNote ? "var(--pastel-sage-text)" : "var(--pastel-rose-text)"};text-transform:uppercase;">
              PEŁNA TREŚĆ UWAGI POBRANA AUTOMATYCZNIE Z MODUŁU „UWAGI”
            </span>
            <span class="badge ${isPosDetailNote ? "badge-now" : "badge-cancelled"}">UWAGA ${escapeHtml((bNote.kind || "").toUpperCase())}${bNote.category ? ` · ${escapeHtml(bNote.category)}` : ""}</span>
          </div>
          <div style="font-family:var(--prose);font-size:14px;font-weight:700;color:var(--text-primary);line-height:1.5;margin-bottom:4px;">
            ${escapeHtml(bNote.text)}
          </div>
          <div style="font-family:var(--accent-mono);font-size:11px;color:var(--text-secondary);">
            Nauczyciel: ${escapeHtml(bNote.teacher || sender)} · Data wpisu: ${escapeHtml(bNote.day || "---")}
          </div>
        </div>
      `
      : "";

    const attachments = detail.attachments || [];
    const attachmentsHtml = attachments.length === 0
      ? ""
      : `
        <div class="scope-box">
          <div style="font-family:var(--accent-mono);font-size:11px;text-transform:uppercase;margin-bottom:8px;">
            ZAŁĄCZNIKI (${attachments.length}):
          </div>
          <div style="display:flex;flex-direction:column;gap:8px;">
            ${attachments.map((att) => `
              <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;padding:8px 10px;background:var(--bg-card);border:1px solid var(--line);">
                <span style="font-size:12px;font-weight:700;">${escapeHtml(att.filename)}</span>
                <div style="display:flex;gap:8px;">
                  <a class="btn-primary btn-xs" href="/api/messages/attachment?account=${encodeURIComponent(state.activeAccount)}&account_source=${encodeURIComponent(resolvedAccSrc)}&message_id=${encodeURIComponent(messageId)}&attachment_id=${encodeURIComponent(att.id)}&filename=${encodeURIComponent(att.filename)}&mode=download${state.sealedToken ? `&token=${encodeURIComponent(state.sealedToken)}` : ""}" target="_blank" rel="noopener">
                    Pobierz plik
                  </a>
                </div>
              </div>
            `).join("")}
          </div>
        </div>
      `;

    const codes = detail.codes || detail.extracted_codes || [];
    const codesHtml = codes.length > 0
      ? `<div class="chip-row">${codes.map((c) => `<span class="code-chip">KOD: ${escapeHtml(c)}</span>`).join("")}</div>`
      : "";

    els.messageModalBody.innerHTML = `
      ${bNoteBoxHtml}
      ${attachmentsHtml}
      ${codesHtml}
      <div class="scope-box" style="white-space:pre-wrap;line-height:1.6;">${escapeHtml(detail.body || "")}</div>

      <!-- 2-Step Safe Teacher Reply Section -->
      <div class="scope-box">
        <div style="font-family:var(--accent-mono);font-size:11px;text-transform:uppercase;margin-bottom:8px;">
          ODPOWIEDZ NAUCZYCIELOWI (BEZPIECZNY TRYB 2-KROKOWY)
        </div>
        <textarea id="replyBodyInput" class="textarea-field" rows="4" placeholder="Wpisz treść odpowiedzi do nauczyciela..."></textarea>
        <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:8px;">
          <button id="previewReplyBtn" class="action-button" type="button">1. Przygotuj podgląd odpowiedzi</button>
        </div>
        <div id="replyConfirmationBox" class="hidden" style="margin-top:10px;padding:10px;background:var(--pastel-sage-bg);border:1px solid var(--pastel-sage-border);"></div>
      </div>
    `;

    // Bind 2-step reply
    const previewBtn = document.getElementById("previewReplyBtn");
    const confirmBox = document.getElementById("replyConfirmationBox");
    if (previewBtn) {
      previewBtn.onclick = async () => {
        const bodyText = document.getElementById("replyBodyInput").value.trim();
        if (!bodyText) {
          showToast("Wpisz treść wiadomości przed wygenerowaniem podglądu.");
          return;
        }
        previewBtn.disabled = true;
        try {
          const preview = await apiFetch("/api/messages/reply-preview", {
            method: "POST",
            body: {
              account: state.activeAccount,
              account_source: resolvedAccSrc,
              message_id: String(messageId),
              subject: detail.subject ? `Re: ${detail.subject}` : "Odpowiedź",
              body: bodyText,
            },
          });
          confirmBox.classList.remove("hidden");
          confirmBox.innerHTML = `
            <div style="font-size:12px;font-weight:700;margin-bottom:4px;">Potwierdź wysyłkę wiadomości w Librus Synergia:</div>
            <div class="card-subtitle">Odbiorca: <strong>${escapeHtml(preview.recipient_label || preview.recipient)}</strong> | Temat: <strong>${escapeHtml(preview.subject)}</strong></div>
            <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:8px;">
              <button id="confirmSendReplyBtn" class="btn-emerald btn-xs" type="button">2. Potwierdzam — Wyślij wiadomość teraz</button>
            </div>
          `;
          document.getElementById("confirmSendReplyBtn").onclick = async () => {
            try {
              await apiFetch("/api/messages/reply-send", {
                method: "POST",
                body: {
                  account: state.activeAccount,
                  confirmation_token: preview.confirmation_token,
                },
              });
              showToast("Odpowiedź została wysłana!");
              confirmBox.innerHTML = `<div style="color:var(--accent-emerald);font-weight:700;">Wiadomość wysłana pomyślnie!</div>`;
            } catch (sendErr) {
              showToast(`Błąd wysyłki: ${sendErr.message}`);
            }
          };
        } catch (err) {
          showToast(`${err.message}`);
        } finally {
          previewBtn.disabled = false;
        }
      };
    }
  } catch (err) {
    els.messageModalBody.innerHTML = `<p style="color:var(--accent-rose)">${escapeHtml(err.message)}</p>`;
  }
}

/* ==========================================================================
   MODULE 6: ZERO-DATABASE VAULT, GOOGLE CALENDAR (.ICS & OAUTH) & ALERTS
   ========================================================================== */
async function renderVaultAndCalendarModule() {
  const localAccounts = window.LibrusVault ? await window.LibrusVault.loadAccounts() : [];
  const cachedToken = window.LibrusVault ? window.LibrusVault.getCachedSealedToken() : null;
  const activeLinks =
    (state.icalLinks && Object.keys(state.icalLinks).length > 0 ? state.icalLinks : null) ||
    (cachedToken && (cachedToken.ical_links || cachedToken.feeds)) ||
    {};

  const wrapper = document.createElement("div");
  wrapper.style.display = "flex";
  wrapper.style.flexDirection = "column";
  wrapper.style.gap = "12px";
  wrapper.style.paddingBottom = "24px";

  const activeProfileObj =
    state.profiles.find((p) => (p.alias || p.account) === state.activeAccount) ||
    state.profiles[0] ||
    null;
  const defaultSuggestedStudent = (activeProfileObj && activeProfileObj.suggested_student_username) || "";

  const accountRowsHtml = localAccounts.length === 0
    ? state.profiles.map((p) => {
        const alias = p.alias || p.account;
        const hasChild = Boolean(p.has_student_account);
        const childBadge = hasChild
          ? `<span class="badge badge-now">RODZIC + UCZEŃ (${escapeHtml(p.student_username || "AKTYWNE")})</span>`
          : `<span class="badge badge-sub">TYLKO RODZIC${p.suggested_student_username ? ` (sugerowany login ucznia: ${escapeHtml(p.suggested_student_username)})` : ""}</span>`;
        return `
          <tr>
            <td style="width:140px;"><span class="badge badge-now">SERWER DOMOWY</span></td>
            <td style="width:200px;"><span class="cell-primary">${escapeHtml(p.full_name || p.first_name)}</span></td>
            <td style="width:110px;"><code>${escapeHtml(alias)}</code></td>
            <td>
              <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
                <span>KLASA ${escapeHtml(p.class_name || "---")} · Rodzic: <code>${escapeHtml(p.parent_username || "---")}</code></span>
                ${childBadge}
              </div>
            </td>
            <td style="width:140px;text-align:right;">
              ${hasChild
                ? `<button class="btn-outline btn-xs unpair-child-btn" data-alias="${escapeHtml(alias)}" type="button">Odłącz ucznia</button>`
                : `<span class="cell-secondary">AKTYWNE</span>`
              }
            </td>
          </tr>
        `;
      }).join("")
    : localAccounts.map((acc) => {
        const hasChild = Boolean(acc.student_username);
        const childBadge = hasChild
          ? `<span class="badge badge-now">RODZIC + UCZEŃ (${escapeHtml(acc.student_username)})</span>`
          : `<span class="badge badge-sub">TYLKO RODZIC</span>`;
        return `
          <tr>
            <td style="width:140px;"><span class="badge badge-info">INDEXEDDB VAULT</span></td>
            <td style="width:200px;"><span class="cell-primary">${escapeHtml(acc.label)}</span></td>
            <td style="width:110px;"><code>${escapeHtml(acc.alias)}</code></td>
            <td>
              <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
                <span>Rodzic: <code>${escapeHtml(acc.login)}</code> · AES-GCM 256-bit</span>
                ${childBadge}
              </div>
            </td>
            <td style="width:140px;text-align:right;">
              <button class="btn-outline btn-xs remove-acc-btn" data-alias="${escapeHtml(acc.alias)}" type="button">Usuń</button>
            </td>
          </tr>
        `;
      }).join("");

  const icsRowsHtml = [];
  Object.entries(activeLinks).forEach(([alias, f]) => {
    const ttDownload = f.timetable_download || `/api/ical/download?account=${encodeURIComponent(alias)}&feed=timetable&download=1`;
    const exDownload = f.exams_download || `/api/ical/download?account=${encodeURIComponent(alias)}&feed=exams&download=1`;
    const gcalImportUrl = f.gcal_import_page || "https://calendar.google.com/calendar/u/0/r/settings/export";
    icsRowsHtml.push(`
      <tr>
        <td style="width:105px;"><span class="cell-primary">${escapeHtml(alias.toUpperCase())}</span></td>
        <td style="width:170px;"><span class="badge badge-info">PLAN LEKCJI (.ICS)</span></td>
        <td>
          <input class="input-field" readonly value="${escapeHtml(f.timetable_ics)}" style="height:28px;font-size:11px;width:100%;max-width:none;" />
        </td>
        <td style="width:390px;text-align:right;white-space:nowrap;">
          <a class="btn-primary btn-xs" href="${escapeHtml(ttDownload)}" download="plan-lekcji-${escapeHtml(alias)}.ics">1. Pobierz plik .ics</a>
          <a class="btn-outline btn-xs" href="${escapeHtml(gcalImportUrl)}" target="_blank" rel="noopener" style="margin-left:4px;">2. Importuj w Google Cal ↗</a>
          <button class="action-button copy-url-btn" data-url="${escapeHtml(f.timetable_ics)}" type="button" style="margin-left:4px;">Kopiuj URL</button>
        </td>
      </tr>
      <tr>
        <td style="width:105px;"><span class="cell-primary">${escapeHtml(alias.toUpperCase())}</span></td>
        <td style="width:170px;"><span class="badge badge-sub">SPRAWDZIANY (.ICS)</span></td>
        <td>
          <input class="input-field" readonly value="${escapeHtml(f.exams_ics)}" style="height:28px;font-size:11px;width:100%;max-width:none;" />
        </td>
        <td style="width:390px;text-align:right;white-space:nowrap;">
          <a class="btn-primary btn-xs" href="${escapeHtml(exDownload)}" download="sprawdziany-${escapeHtml(alias)}.ics">1. Pobierz plik .ics</a>
          <a class="btn-outline btn-xs" href="${escapeHtml(gcalImportUrl)}" target="_blank" rel="noopener" style="margin-left:4px;">2. Importuj w Google Cal ↗</a>
          <button class="action-button copy-url-btn" data-url="${escapeHtml(f.exams_ics)}" type="button" style="margin-left:4px;">Kopiuj URL</button>
        </td>
      </tr>
    `);
  });

  const profileSelectOptions = state.profiles.map((p) => {
    const alias = p.alias || p.account;
    const selected = alias === state.activeAccount ? "selected" : "";
    return `<option value="${escapeHtml(alias)}" data-suggested="${escapeHtml(p.suggested_student_username || "")}" ${selected}>Profil: ${escapeHtml(p.first_name || alias)} (${escapeHtml(alias)})</option>`;
  }).join("");

  wrapper.innerHTML = `
    <!-- Section 1: Accounts Table + Child Account Pairing + Vault Add Row -->
    <div class="surface-panel content-card data-table-card">
      <div class="pi-action-bar" style="border-left:0;border-right:0;border-top:0;">
        <span class="toolbar-title" style="font-size:12px;">KONTA LIBRUS SYNERGIA (RODZIC + UCZEŃ)</span>
        <span class="pi-counter">${localAccounts.length || state.profiles.length} PROFILI</span>
      </div>
      <div class="data-table-scroll">
        <table class="data-table">
          <thead>
            <tr>
              <th>ŹRÓDŁO</th>
              <th>UCZEŃ / ETYKIETA</th>
              <th>ALIAS</th>
              <th>STATUS POŁĄCZENIA (KONTO RODZICA + KONTO DZIECKA)</th>
              <th style="text-align:right;">AKCJA</th>
            </tr>
          </thead>
          <tbody>
            ${accountRowsHtml || `<tr><td colspan="5">Brak skonfigurowanych kont. Dodaj konto poniżej.</td></tr>`}
          </tbody>
        </table>
      </div>

      <!-- Dedicated Child Account Pairing Bar -->
      <div class="pi-action-bar" style="border-left:0;border-right:0;background:var(--bg-subtle);flex-wrap:wrap;gap:8px;">
        <span class="toolbar-title" style="font-size:11px;color:var(--accent-primary);">POŁĄCZ KONTO DZIECKA (UCZNIA):</span>
        <select id="studentPairProfileSelect" class="pi-select" style="min-width:170px;">
          ${profileSelectOptions}
        </select>
        <input id="studentPairLoginInput" class="input-field" style="max-width:190px;" placeholder="Login ucznia (np. 8090546)" value="${escapeHtml(defaultSuggestedStudent)}" />
        <input id="studentPairPasswordInput" type="password" class="input-field" style="max-width:190px;" placeholder="Hasło ucznia do Librusa" />
        <button id="saveStudentPairBtn" class="btn-primary btn-xs" type="button">+ Połącz konto ucznia (pobieraj wymagania dla dzieci)</button>
        <span id="studentPairStatusMsg" class="card-subtitle" style="margin-left:auto;">Login ucznia to zazwyczaj numer bez litery „u” na końcu (rodzic ma „u”).</span>
      </div>

      <!-- Add New Full Account Row -->
      <div class="pi-action-bar" style="border-left:0;border-right:0;border-bottom:0;">
        <span class="card-subtitle" style="white-space:nowrap;">NOWY PROFIL (VAULT):</span>
        <input id="vaultLabelInput" class="input-field" style="max-width:130px;" placeholder="Imię (np. Ola)" />
        <input id="vaultAliasInput" class="input-field" style="max-width:110px;" placeholder="Alias (np. ola)" />
        <input id="vaultLoginInput" class="input-field" style="max-width:155px;" placeholder="Login rodzica (np. 8090546)" />
        <input id="vaultPasswordInput" type="password" class="input-field" style="max-width:145px;" placeholder="Hasło rodzica" />
        <input id="vaultStudentLoginInput" class="input-field" style="max-width:155px;" placeholder="Login ucznia (np. 8090546u)" />
        <input id="vaultStudentPasswordInput" type="password" class="input-field" style="max-width:145px;" placeholder="Hasło ucznia (opcj.)" />
        <button id="saveVaultAccountBtn" class="action-button" type="button" style="margin-left:auto;">+ Zapisz profil</button>
      </div>
    </div>

    <!-- Section 2: iCal Feeds Table -->
    <div class="surface-panel content-card data-table-card">
      <div class="data-table-scroll">
        <table class="data-table">
          <thead>
            <tr>
              <th>PROFIL</th>
              <th>TYP KANAŁU</th>
              <th>ADRES KANAŁU (.ICS)</th>
              <th style="text-align:right;">
                <span style="display:inline-flex;align-items:center;gap:6px;justify-content:flex-end;">
                  <span>POBIERZ DO IMPORTU / SUBSKRYBUJ</span>
                  <button id="generateIcsLinksBtn" class="action-btn refresh-btn" title="Odśwież linki .ics" type="button" style="padding:1px 6px;font-size:11px;line-height:1.2;">↻</button>
                </span>
              </th>
            </tr>
          </thead>
          <tbody>
            ${icsRowsHtml.join("") || `<tr><td colspan="4">Kliknij ikonę ↻ w nagłówku tabeli, aby utworzyć kanały kalendarza .ics.</td></tr>`}
          </tbody>
        </table>
      </div>
    </div>
  `;

  els.contentArea.innerHTML = "";
  els.contentArea.appendChild(wrapper);

  // Update suggested student login when profile select changes
  const pairSelect = document.getElementById("studentPairProfileSelect");
  const pairLoginInput = document.getElementById("studentPairLoginInput");
  if (pairSelect && pairLoginInput) {
    pairSelect.onchange = () => {
      const opt = pairSelect.options[pairSelect.selectedIndex];
      const suggested = opt ? opt.getAttribute("data-suggested") : "";
      if (suggested) pairLoginInput.value = suggested;
    };
  }

  // Bind Student Account Pairing button
  const saveStudentPairBtn = document.getElementById("saveStudentPairBtn");
  const pairStatusMsg = document.getElementById("studentPairStatusMsg");
  if (saveStudentPairBtn) {
    saveStudentPairBtn.onclick = async () => {
      const targetAlias = (pairSelect && pairSelect.value) || state.activeAccount || (state.profiles[0] && state.profiles[0].alias) || "";
      const studentUsername = pairLoginInput ? pairLoginInput.value.trim() : "";
      const studentPassword = document.getElementById("studentPairPasswordInput").value;
      if (!studentUsername || !studentPassword) {
        const msg = "Podaj login ucznia (zwykle bez litery 'u' na końcu) oraz hasło ucznia.";
        if (pairStatusMsg) {
          pairStatusMsg.style.color = "var(--accent-rose)";
          pairStatusMsg.textContent = msg;
        }
        showToast(msg);
        return;
      }
      saveStudentPairBtn.disabled = true;
      saveStudentPairBtn.textContent = "Weryfikacja logowania ucznia...";
      if (pairStatusMsg) {
        pairStatusMsg.style.color = "var(--accent-primary)";
        pairStatusMsg.textContent = `Łączenie i weryfikacja konta ucznia (${studentUsername})...`;
      }
      try {
        await apiFetch("/api/vault/student-account", {
          method: "POST",
          body: {
            alias: targetAlias,
            account: targetAlias,
            student_username: studentUsername,
            student_password: studentPassword,
            verify: true,
          },
        });
        // Also update IndexedDB Vault if this alias exists there
        if (window.LibrusVault) {
          const memAccs = window.LibrusVault.getMemoryAccounts();
          const matching = memAccs.find((a) => a.alias === targetAlias);
          if (matching) {
            await window.LibrusVault.addOrUpdateAccount({
              ...matching,
              student_username: studentUsername,
              student_password: studentPassword,
            });
          }
        }
        showToast(`Połączono konto ucznia (${studentUsername}) z profilem ${targetAlias}!`);
        await bootstrapProfiles();
        await renderVaultAndCalendarModule();
      } catch (err) {
        if (pairStatusMsg) {
          pairStatusMsg.style.color = "var(--accent-rose)";
          pairStatusMsg.textContent = `Błąd: ${err.message}`;
        }
        showToast(`Błąd łączenia konta ucznia: ${err.message}`, 5500);
      } finally {
        saveStudentPairBtn.disabled = false;
        saveStudentPairBtn.textContent = "+ Połącz konto ucznia (pobieraj wymagania dla dzieci)";
      }
    };
  }

  // Bind unpair child account button
  wrapper.querySelectorAll(".unpair-child-btn").forEach((btn) => {
    btn.onclick = async () => {
      const alias = btn.dataset.alias;
      try {
        await apiFetch("/api/vault/student-account", {
          method: "POST",
          body: { alias, account: alias, student_username: "", student_password: "", verify: false },
        });
        showToast(`Odłączono konto ucznia od profilu ${alias}.`);
        await bootstrapProfiles();
        await renderVaultAndCalendarModule();
      } catch (err) {
        showToast(`${err.message}`);
      }
    };
  });

  // Bind Vault events
  document.getElementById("saveVaultAccountBtn").onclick = async () => {
    const label = document.getElementById("vaultLabelInput").value.trim();
    const alias = document.getElementById("vaultAliasInput").value.trim() || label;
    const login = document.getElementById("vaultLoginInput").value.trim();
    const password = document.getElementById("vaultPasswordInput").value;
    const student_username = document.getElementById("vaultStudentLoginInput").value.trim();
    const student_password = document.getElementById("vaultStudentPasswordInput").value;
    if (!login || !password) {
      showToast("Podaj login i hasło do konta Librus Synergia.");
      return;
    }
    await window.LibrusVault.addOrUpdateAccount({
      alias,
      label,
      login,
      password,
      student_username,
      student_password,
    });
    showToast("Konto zostało zaszyfrowane i zapisane w przeglądarce!");
    await bootstrapProfiles();
    await renderVaultAndCalendarModule();
  };

  wrapper.querySelectorAll(".remove-acc-btn").forEach((btn) => {
    btn.onclick = async () => {
      await window.LibrusVault.removeAccount(btn.dataset.alias);
      showToast("Konto usunięte z pamięci przeglądarki.");
      await bootstrapProfiles();
      await renderVaultAndCalendarModule();
    };
  });

  // Bind .ics Token generator
  document.getElementById("generateIcsLinksBtn").onclick = async () => {
    try {
      const accs = window.LibrusVault.getMemoryAccounts();
      if (accs.length > 0) {
        const res = await apiFetch("/api/vault/seal", {
          method: "POST",
          body: { accounts: accs, verify: false },
        });
        window.LibrusVault.setCachedSealedToken(res);
        state.icalLinks = res.ical_links || res.feeds || {};
      } else {
        await bootstrapProfiles();
      }
      showToast("Odświeżono kanały i pliki .ics!");
      await renderVaultAndCalendarModule();
    } catch (err) {
      showToast(`${err.message}`);
    }
  };

  wrapper.querySelectorAll(".copy-url-btn").forEach((btn) => {
    btn.onclick = () => {
      navigator.clipboard.writeText(btn.dataset.url);
      showToast("Skopiowano adres .ics do schowka!");
    };
  });
}

/* ==========================================================================
   BACKGROUND ALERTS ("CO NOWEGO") — TOPBAR BELL POPOVER & STEPPER
   ========================================================================== */
function renderAlertBanner() {
  const unseen = state.unseenAlerts || [];
  if (unseen.length === 0) {
    if (els.alertCountBadge) els.alertCountBadge.classList.add("hidden");
    if (els.alertBellBtn) els.alertBellBtn.classList.remove("has-alerts");
    if (els.alertBannerText) els.alertBannerText.textContent = "Brak nowych powiadomień.";
    if (els.alertCounter) els.alertCounter.textContent = "0 / 0";
    if (els.alertStepper) els.alertStepper.classList.add("hidden");
    return;
  }
  if (state.alertIndex >= unseen.length) state.alertIndex = 0;
  if (state.alertIndex < 0) state.alertIndex = unseen.length - 1;

  if (els.alertCountBadge) {
    els.alertCountBadge.textContent = String(unseen.length);
    els.alertCountBadge.classList.remove("hidden");
  }
  if (els.alertBellBtn) {
    els.alertBellBtn.classList.add("has-alerts");
  }

  const current = unseen[state.alertIndex];
  if (els.alertBannerText) {
    els.alertBannerText.textContent = `${current.title}${current.subtitle ? ` — ${current.subtitle}` : ""}`;
  }
  if (els.alertCounter) {
    els.alertCounter.textContent = `${state.alertIndex + 1} / ${unseen.length}`;
  }
  if (els.alertStepper) {
    els.alertStepper.classList.remove("hidden");
  }
}

async function refreshBackgroundAlerts() {
  if (!state.activeAccount) return;
  try {
    const data = await apiFetch(`/api/alerts?account=${encodeURIComponent(state.activeAccount)}`);
    state.alertsData = data;
    const seen = window.LibrusVault ? window.LibrusVault.getSeenAlerts() : {};
    state.unseenAlerts = (data.alerts || []).filter((a) => !seen[a.id]);
    state.alertIndex = 0;
    renderAlertBanner();
  } catch {
    // ignore background alert errors
  }
}

/* ==========================================================================
   INITIALIZATION & EVENT LISTENERS (Default Theme: LIGHT)
   ========================================================================== */
async function bootstrapProfiles() {
  if (window.LibrusVault) {
    await window.LibrusVault.loadAccounts();
  }
  try {
    const data = await apiFetch("/api/profiles");
    state.profiles = data.profiles || [];
    state.icalLinks = data.ical_links || {};
    if (state.profiles.length > 0) {
      if (!state.activeAccount || !state.profiles.some((p) => (p.alias || p.account) === state.activeAccount)) {
        state.activeAccount = state.profiles[0].alias || state.profiles[0].account;
      }
    } else {
      state.activeAccount = null;
    }
    renderProfiles();
  } catch (err) {
    console.warn("Could not load profiles:", err);
    state.profiles = [];
    state.activeAccount = null;
    renderProfiles();
  }
}

function applyTheme(theme) {
  const normalized = theme === "dark" ? "dark" : "light";
  document.documentElement.setAttribute("data-theme", normalized);
  localStorage.setItem("librus_pi_theme", normalized);
  if (els.themeIcon) {
    els.themeIcon.textContent = normalized === "light" ? "◐" : "◑";
  }
  if (els.themeLabel) {
    els.themeLabel.textContent = normalized === "light" ? "Ciemny" : "Jasny";
  }
}

function toggleTheme() {
  const current = document.documentElement.getAttribute("data-theme") || "light";
  applyTheme(current === "light" ? "dark" : "light");
}

function detectDeviceContext() {
  const ua = navigator.userAgent || "";
  const isCastDisplay =
    window.location.search.includes("nesthub=1") ||
    ua.includes("CrKey") ||
    ua.includes("Fuchsia") ||
    (window.innerWidth <= 1150 && window.innerHeight <= 660);
  const isMobile = !isCastDisplay && window.innerWidth <= 768;
  const device = isCastDisplay ? "nesthub" : isMobile ? "mobile" : "desktop";
  document.documentElement.setAttribute("data-device", device);
  updateStudentSubtitle();
}

async function initApp() {
  detectDeviceContext();
  window.addEventListener("resize", detectDeviceContext);

  // Default to Light Mode ("light") per user preference
  const savedTheme = localStorage.getItem("librus_pi_theme") || "light";
  applyTheme(savedTheme);
  if (els.themeToggleBtn) {
    els.themeToggleBtn.onclick = toggleTheme;
  }

  state.weekStart = getMondayIso(new Date());
  updateClock();
  setInterval(updateClock, 15000);

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/static/sw.js").catch(() => {});
  }

  // First-run Welcome / Open Source / Security Modal
  const closeWelcome = () => {
    if (els.welcomeModal) els.welcomeModal.classList.add("hidden");
    localStorage.setItem("bursli_welcome_seen_v1", "1");
  };
  if (els.closeWelcomeModalBtn) els.closeWelcomeModalBtn.onclick = closeWelcome;
  if (els.ackWelcomeModalBtn) els.ackWelcomeModalBtn.onclick = closeWelcome;
  if (els.welcomeModal) {
    els.welcomeModal.onclick = (e) => {
      if (e.target === els.welcomeModal) closeWelcome();
    };
  }
  if (els.openWelcomeModalBtn) {
    els.openWelcomeModalBtn.onclick = () => {
      if (els.welcomeModal) els.welcomeModal.classList.remove("hidden");
    };
  }
  if (!localStorage.getItem("bursli_welcome_seen_v1") && els.welcomeModal) {
    els.welcomeModal.classList.remove("hidden");
  }

  // Module tab navigation
  document.querySelectorAll(".module-tab").forEach((btn) => {
    btn.onclick = () => switchModule(btn.dataset.module);
  });

  // Brand logo click -> navigate home to Module 01 (Plan)
  const brandLogoHomeBtn = document.getElementById("brandLogoHomeBtn");
  if (brandLogoHomeBtn) {
    brandLogoHomeBtn.onclick = () => switchModule("timetable");
    brandLogoHomeBtn.onkeydown = (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        switchModule("timetable");
      }
    };
  }

  // Timetable week navigation
  els.prevWeekBtn.onclick = () => {
    state.weekStart = addDaysIso(state.weekStart, -7);
    state.selectedDayIndex = 0;
    loadTimetable(false);
  };
  els.thisWeekBtn.onclick = () => {
    state.weekStart = getMondayIso(new Date());
    loadTimetable(false);
  };
  els.nextWeekBtn.onclick = () => {
    state.weekStart = addDaysIso(state.weekStart, 7);
    state.selectedDayIndex = 0;
    loadTimetable(false);
  };
  els.modeTodayBtn.onclick = () => {
    state.viewMode = "today";
    els.modeTodayBtn.classList.add("active");
    els.modeWeekBtn.classList.remove("active");
    renderTimetableModule();
  };
  els.modeWeekBtn.onclick = () => {
    state.viewMode = "week";
    els.modeWeekBtn.classList.add("active");
    els.modeTodayBtn.classList.remove("active");
    renderTimetableModule();
  };

  // Modals close
  els.closeStudyModalBtn.onclick = () => els.studyModal.classList.add("hidden");
  els.studyModal.onclick = (e) => {
    if (e.target === els.studyModal) els.studyModal.classList.add("hidden");
  };
  els.closeMessageModalBtn.onclick = () => els.messageModal.classList.add("hidden");
  els.messageModal.onclick = (e) => {
    if (e.target === els.messageModal) els.messageModal.classList.add("hidden");
  };

  // Bell Alert Popover & Stepper
  if (els.alertBellBtn && els.alertPopover) {
    els.alertBellBtn.onclick = (e) => {
      e.stopPropagation();
      els.alertPopover.classList.toggle("hidden");
    };
  }
  if (els.closeAlertPopoverBtn && els.alertPopover) {
    els.closeAlertPopoverBtn.onclick = () => {
      els.alertPopover.classList.add("hidden");
    };
  }
  document.addEventListener("click", (e) => {
    if (
      els.alertPopover &&
      !els.alertPopover.classList.contains("hidden") &&
      els.alertBellWrap &&
      !els.alertBellWrap.contains(e.target)
    ) {
      els.alertPopover.classList.add("hidden");
    }
    const legendPopover = document.getElementById("materialsLegendPopover");
    const legendWrap = document.getElementById("materialsLegendWrap");
    const legendBtn = document.getElementById("materialsLegendBtn");
    if (
      legendPopover &&
      !legendPopover.classList.contains("hidden") &&
      legendWrap &&
      !legendWrap.contains(e.target)
    ) {
      state.materialsLegendOpen = false;
      legendPopover.classList.add("hidden");
      if (legendBtn) {
        legendBtn.classList.remove("active");
        legendBtn.setAttribute("aria-expanded", "false");
      }
    }
  });

  if (els.prevAlertBtn) {
    els.prevAlertBtn.onclick = () => {
      if (state.unseenAlerts.length > 0) {
        state.alertIndex = (state.alertIndex - 1 + state.unseenAlerts.length) % state.unseenAlerts.length;
        renderAlertBanner();
      }
    };
  }
  if (els.nextAlertBtn) {
    els.nextAlertBtn.onclick = () => {
      if (state.unseenAlerts.length > 0) {
        state.alertIndex = (state.alertIndex + 1) % state.unseenAlerts.length;
        renderAlertBanner();
      }
    };
  }
  const handleOpenCurrentAlert = () => {
    const current = state.unseenAlerts[state.alertIndex];
    if (els.alertPopover) els.alertPopover.classList.add("hidden");
    if (!current) return;
    const t = String(current.type || "").toLowerCase();
    if (t.includes("exam")) switchModule("exams");
    else if (t.includes("grade") || t.includes("attendance")) switchModule("grades");
    else if (t.includes("message")) switchModule("messages");
    else if (t.includes("substitution") || t.includes("timetable")) switchModule("timetable");
    else switchModule("exams");
  };
  if (els.openAlertBtn) {
    els.openAlertBtn.onclick = handleOpenCurrentAlert;
  }
  if (els.alertBannerText) {
    els.alertBannerText.onclick = handleOpenCurrentAlert;
  }
  if (els.dismissAlertsBtn) {
    els.dismissAlertsBtn.onclick = () => {
      const current = state.unseenAlerts[state.alertIndex];
      if (current && current.id) {
        if (window.LibrusVault) window.LibrusVault.markAlertsSeen([current.id]);
        state.unseenAlerts = state.unseenAlerts.filter((a) => a.id !== current.id);
      } else {
        const ids = ((state.alertsData && state.alertsData.alerts) || []).map((a) => a.id);
        if (window.LibrusVault) window.LibrusVault.markAlertsSeen(ids);
        state.unseenAlerts = [];
      }
      renderAlertBanner();
      if (state.unseenAlerts.length === 0 && els.alertPopover) {
        els.alertPopover.classList.add("hidden");
      }
    };
  }

  // Refresh button
  if (els.refreshBtn) {
    els.refreshBtn.onclick = () => loadActiveModule(true);
  }

  await bootstrapProfiles();
  if (state.activeAccount) {
    await loadTimetable(false);
    refreshBackgroundAlerts();
  } else {
    switchModule("vault");
  }
}

initApp();
