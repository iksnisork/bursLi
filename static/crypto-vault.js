/**
 * Librus Family Hub — Zero-Database Browser Vault
 * Stores Librus Synergia credentials locally in IndexedDB encrypted via Web Crypto API (AES-GCM 256-bit).
 * No plaintext credentials are stored on the server disk or database.
 */
(function (global) {
  const DB_NAME = "librus_family_hub_vault_v1";
  const DB_VERSION = 1;
  const STORE_KEYS = "crypto_keys";
  const STORE_PAYLOAD = "encrypted_vault";
  const KEY_ID = "master_aes_gcm_key";
  const PAYLOAD_ID = "accounts_bundle";
  const SEALED_TOKEN_STORAGE_KEY = "lfh_sealed_cal_token_v1";
  const CHECKLIST_STORAGE_KEY = "lfh_study_checklists_v1";
  const SEEN_ALERTS_STORAGE_KEY = "lfh_seen_alerts_v1";

  let memoryAccounts = [];

  function openVaultDB() {
    return new Promise((resolve, reject) => {
      if (!global.indexedDB) {
        reject(new Error("IndexedDB nie jest wspierane w tej przeglądarce."));
        return;
      }
      const req = global.indexedDB.open(DB_NAME, DB_VERSION);
      req.onupgradeneeded = (event) => {
        const db = event.target.result;
        if (!db.objectStoreNames.contains(STORE_KEYS)) {
          db.createObjectStore(STORE_KEYS, { keyPath: "id" });
        }
        if (!db.objectStoreNames.contains(STORE_PAYLOAD)) {
          db.createObjectStore(STORE_PAYLOAD, { keyPath: "id" });
        }
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  }

  async function getOrCreateMasterKey(db) {
    return new Promise((resolve, reject) => {
      const tx = db.transaction([STORE_KEYS], "readonly");
      const store = tx.objectStore(STORE_KEYS);
      const getReq = store.get(KEY_ID);
      getReq.onsuccess = async () => {
        if (getReq.result && getReq.result.key) {
          resolve(getReq.result.key);
          return;
        }
        try {
          const cryptoKey = await global.crypto.subtle.generateKey(
            { name: "AES-GCM", length: 256 },
            false, // non-exportable CryptoKey stored in IndexedDB
            ["encrypt", "decrypt"]
          );
          const writeTx = db.transaction([STORE_KEYS], "readwrite");
          writeTx.objectStore(STORE_KEYS).put({ id: KEY_ID, key: cryptoKey });
          writeTx.oncomplete = () => resolve(cryptoKey);
          writeTx.onerror = () => reject(writeTx.error);
        } catch (err) {
          reject(err);
        }
      };
      getReq.onerror = () => reject(getReq.error);
    });
  }

  async function loadAccounts() {
    try {
      const db = await openVaultDB();
      const key = await getOrCreateMasterKey(db);
      return await new Promise((resolve, reject) => {
        const tx = db.transaction([STORE_PAYLOAD], "readonly");
        const req = tx.objectStore(STORE_PAYLOAD).get(PAYLOAD_ID);
        req.onsuccess = async () => {
          if (!req.result || !req.result.ciphertext || !req.result.iv) {
            memoryAccounts = [];
            resolve([]);
            return;
          }
          try {
            const decryptedBuffer = await global.crypto.subtle.decrypt(
              { name: "AES-GCM", iv: req.result.iv },
              key,
              req.result.ciphertext
            );
            const jsonText = new TextDecoder().decode(decryptedBuffer);
            const parsed = JSON.parse(jsonText);
            memoryAccounts = Array.isArray(parsed) ? parsed : [];
            resolve(memoryAccounts);
          } catch (decErr) {
            console.warn("Vault decrypt error, resetting vault:", decErr);
            memoryAccounts = [];
            resolve([]);
          }
        };
        req.onerror = () => reject(req.error);
      });
    } catch (err) {
      console.warn("IndexedDB fallback to in-memory vault:", err);
      return memoryAccounts;
    }
  }

  async function saveAccounts(accounts) {
    const clean = (accounts || [])
      .filter((a) => a && a.alias && a.login && a.password)
      .map((a) => ({
        alias: String(a.alias).trim().toLowerCase().replace(/[^a-z0-9_-]/g, ""),
        label: String(a.label || a.alias).trim(),
        login: String(a.login).trim(),
        password: String(a.password),
        student_username: String(a.student_username || "").trim(),
        student_password: String(a.student_password || ""),
      }));
    memoryAccounts = clean;
    try {
      localStorage.removeItem(SEALED_TOKEN_STORAGE_KEY);
    } catch {
      // ignore storage errors
    }

    try {
      const db = await openVaultDB();
      const key = await getOrCreateMasterKey(db);
      const iv = global.crypto.getRandomValues(new Uint8Array(12));
      const encoded = new TextEncoder().encode(JSON.stringify(clean));
      const ciphertext = await global.crypto.subtle.encrypt(
        { name: "AES-GCM", iv },
        key,
        encoded
      );
      await new Promise((resolve, reject) => {
        const tx = db.transaction([STORE_PAYLOAD], "readwrite");
        tx.objectStore(STORE_PAYLOAD).put({
          id: PAYLOAD_ID,
          iv,
          ciphertext,
          updatedAt: new Date().toISOString(),
        });
        tx.oncomplete = () => resolve();
        tx.onerror = () => reject(tx.error);
      });
    } catch (err) {
      console.warn("Could not persist to IndexedDB:", err);
    }
    return clean;
  }

  async function addOrUpdateAccount({ alias, label, login, password, student_username, student_password }) {
    const accounts = await loadAccounts();
    const explicitParentLogin = login !== undefined ? String(login || "").trim() : "";
    const explicitParentPass = password !== undefined ? String(password || "") : "";
    const resolvedStudentUser =
      student_username !== undefined
        ? String(student_username || "").trim()
        : undefined;
    const resolvedStudentPass =
      student_password !== undefined
        ? String(student_password || "")
        : undefined;
    const seedLogin = explicitParentLogin || resolvedStudentUser || "";
    const normAlias = String(alias || label || seedLogin)
      .trim()
      .toLowerCase()
      .replace(/[^a-z0-9_-]/g, "");
    const existingIdx = accounts.findIndex((a) => a.alias === normAlias);
    const prev = existingIdx >= 0 ? accounts[existingIdx] : {};
    const prevHadParent = Boolean(prev.login && prev.login !== prev.student_username);

    let finalLogin = "";
    let finalPassword = "";
    if (explicitParentLogin && explicitParentPass) {
      finalLogin = explicitParentLogin;
      finalPassword = explicitParentPass;
    } else if (prevHadParent) {
      finalLogin = String(prev.login || "").trim();
      finalPassword = String(prev.password || "");
    } else {
      finalLogin = String(resolvedStudentUser || prev.login || "").trim();
      finalPassword = String(resolvedStudentPass || prev.password || "");
    }

    const finalStudentUser =
      resolvedStudentUser !== undefined
        ? resolvedStudentUser
        : String(prev.student_username || "").trim();
    const finalStudentPass =
      resolvedStudentPass !== undefined
        ? resolvedStudentPass
        : String(prev.student_password || "");

    const entry = {
      alias: normAlias,
      label: String(label || prev.label || alias || normAlias).trim(),
      login: finalLogin,
      password: finalPassword,
      student_username: finalStudentUser,
      student_password: finalStudentPass,
    };
    if (existingIdx >= 0) {
      accounts[existingIdx] = entry;
    } else {
      accounts.push(entry);
    }
    return await saveAccounts(accounts);
  }

  async function removeAccount(alias) {
    const accounts = await loadAccounts();
    const filtered = accounts.filter((a) => a.alias !== alias);
    localStorage.removeItem(SEALED_TOKEN_STORAGE_KEY);
    return await saveAccounts(filtered);
  }

  async function clearVault() {
    memoryAccounts = [];
    localStorage.removeItem(SEALED_TOKEN_STORAGE_KEY);
    try {
      const db = await openVaultDB();
      await new Promise((resolve, reject) => {
        const tx = db.transaction([STORE_PAYLOAD], "readwrite");
        tx.objectStore(STORE_PAYLOAD).delete(PAYLOAD_ID);
        tx.oncomplete = () => resolve();
        tx.onerror = () => reject(tx.error);
      });
    } catch (e) {
      console.warn("Error clearing IndexedDB vault:", e);
    }
  }

  function getAccountsHeaderValue() {
    if (!memoryAccounts || memoryAccounts.length === 0) {
      return null;
    }
    const jsonBytes = new TextEncoder().encode(JSON.stringify(memoryAccounts));
    let binary = "";
    for (let i = 0; i < jsonBytes.length; i++) {
      binary += String.fromCharCode(jsonBytes[i]);
    }
    return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  function getCachedSealedToken() {
    try {
      const raw = localStorage.getItem(SEALED_TOKEN_STORAGE_KEY);
      if (!raw) return null;
      return JSON.parse(raw);
    } catch {
      return null;
    }
  }

  function setCachedSealedToken(tokenBundle) {
    try {
      localStorage.setItem(SEALED_TOKEN_STORAGE_KEY, JSON.stringify(tokenBundle));
    } catch {
      // ignore storage errors
    }
  }

  function getChecklistState() {
    try {
      const raw = localStorage.getItem(CHECKLIST_STORAGE_KEY);
      return raw ? JSON.parse(raw) : {};
    } catch {
      return {};
    }
  }

  function toggleChecklistItem(examKey, itemIndex) {
    const state = getChecklistState();
    const key = `${examKey}::${itemIndex}`;
    state[key] = !state[key];
    try {
      localStorage.setItem(CHECKLIST_STORAGE_KEY, JSON.stringify(state));
    } catch {
      // ignore
    }
    return !!state[key];
  }

  function isChecklistItemDone(examKey, itemIndex) {
    const state = getChecklistState();
    return !!state[`${examKey}::${itemIndex}`];
  }

  function getSeenAlerts() {
    try {
      const raw = localStorage.getItem(SEEN_ALERTS_STORAGE_KEY);
      return raw ? JSON.parse(raw) : {};
    } catch {
      return {};
    }
  }

  function markAlertsSeen(alertIds) {
    const seen = getSeenAlerts();
    const now = Date.now();
    (alertIds || []).forEach((id) => {
      seen[id] = now;
    });
    try {
      localStorage.setItem(SEEN_ALERTS_STORAGE_KEY, JSON.stringify(seen));
    } catch {
      // ignore
    }
  }

  global.LibrusVault = {
    loadAccounts,
    saveAccounts,
    addOrUpdateAccount,
    removeAccount,
    clearVault,
    getMemoryAccounts: () => memoryAccounts.slice(),
    getAccountsHeaderValue,
    getCachedSealedToken,
    setCachedSealedToken,
    toggleChecklistItem,
    isChecklistItemDone,
    getSeenAlerts,
    markAlertsSeen,
  };
})(window);
