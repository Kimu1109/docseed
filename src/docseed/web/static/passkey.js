// パスキー（WebAuthn）のログイン・登録・本人確認。docs/design/extensions.md 5.2。
// サーバーの options（JSON）を navigator.credentials に渡せる形に変え、結果を JSON にして送り返す。
(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const supported = !!(window.PublicKeyCredential && navigator.credentials);

  // ---- base64url -----------------------------------------------------------
  function b64ToBuf(s) {
    s = s.replace(/-/g, "+").replace(/_/g, "/");
    s += "=".repeat((4 - (s.length % 4)) % 4);
    const bin = atob(s);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out.buffer;
  }
  function bufToB64(buf) {
    const bytes = new Uint8Array(buf);
    let bin = "";
    for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
    return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  function creationOptions(o) {
    return {
      ...o,
      challenge: b64ToBuf(o.challenge),
      user: { ...o.user, id: b64ToBuf(o.user.id) },
      excludeCredentials: (o.excludeCredentials || []).map((c) => ({ ...c, id: b64ToBuf(c.id) })),
    };
  }
  function requestOptions(o) {
    return {
      ...o,
      challenge: b64ToBuf(o.challenge),
      allowCredentials: (o.allowCredentials || []).map((c) => ({ ...c, id: b64ToBuf(c.id) })),
    };
  }
  function credentialJSON(cred) {
    const r = cred.response;
    const out = { id: cred.id, rawId: bufToB64(cred.rawId), type: cred.type, clientExtensionResults: {},
                  authenticatorAttachment: cred.authenticatorAttachment || undefined, response: {} };
    out.response.clientDataJSON = bufToB64(r.clientDataJSON);
    if (r.attestationObject) {
      out.response.attestationObject = bufToB64(r.attestationObject);
      if (r.getTransports) out.response.transports = r.getTransports();
    } else {
      out.response.authenticatorData = bufToB64(r.authenticatorData);
      out.response.signature = bufToB64(r.signature);
      if (r.userHandle) out.response.userHandle = bufToB64(r.userHandle);
    }
    return out;
  }

  // ---- 通信 ------------------------------------------------------------------
  function csrfToken() {
    try { return JSON.parse(document.body.getAttribute("hx-headers") || "{}")["X-CSRF-Token"] || ""; }
    catch { return ""; }
  }
  async function post(url, body) {
    const r = await fetch(url, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
      body: JSON.stringify(body || {}),
    });
    if (r.status === 204) return {};
    let data = {};
    try { data = await r.json(); } catch { /* 本文なし */ }
    if (!r.ok) throw new Error(data.detail || "サーバーでエラーが起きました（" + r.status + "）");
    return data;
  }

  function showError(msg) {
    const el = $("[data-passkey-error]");
    if (el) { el.textContent = msg; el.hidden = false; } else { alert(msg); }
  }
  function cancelled(e) {
    return e && (e.name === "NotAllowedError" || e.name === "AbortError");
  }

  // ---- ログイン ---------------------------------------------------------------
  let conditionalAbort = null;

  async function login(form, conditional) {
    const opts = await post("/auth/passkeys/login/options");
    const req = { publicKey: requestOptions(opts.publicKey) };
    if (conditional) {
      conditionalAbort = new AbortController();
      req.mediation = "conditional";
      req.signal = conditionalAbort.signal;
    }
    let cred;
    try {
      cred = await navigator.credentials.get(req);
    } catch (e) {
      // 自動入力（conditional）は裏での試みなので、ブラウザ側の失敗（パスキーに対応していない・
      // 取り消された など）は画面に出さない。サーバーの確認の失敗だけを出す
      if (conditional) return;
      throw e;
    }
    if (!cred) return;
    const remember = !!$("input[name=remember]", form)?.checked;
    const res = await post("/auth/passkeys/login/verify", {
      challenge_id: opts.challenge_id, credential: credentialJSON(cred), next: form.dataset.next || null, remember,
    });
    location.assign(res.redirect || "/");
  }

  document.addEventListener("DOMContentLoaded", async () => {
    const form = $("[data-passkey-login]");
    if (!form || form.dataset.passkeyAvailable !== "1") return;
    if (!supported) {
      $("[data-passkey-button]")?.setAttribute("hidden", "");
      return;
    }
    $("[data-passkey-button]")?.addEventListener("click", async () => {
      conditionalAbort?.abort();
      try { await login(form, false); }
      catch (e) { if (!cancelled(e)) showError(e.message || String(e)); }
    });
    // ユーザー名の欄の自動入力の候補にパスキーを出す
    try {
      if (await PublicKeyCredential.isConditionalMediationAvailable?.()) {
        login(form, true).catch((e) => { if (!cancelled(e)) showError(e.message || String(e)); });
      }
    } catch { /* 対応していないブラウザ */ }
  });

  // ---- 登録（設定 → セキュリティ）---------------------------------------------
  document.addEventListener("submit", async (e) => {
    const form = e.target.closest("[data-passkey-register]");
    if (!form) return;
    e.preventDefault();
    if (!supported) { showError("このブラウザはパスキーに対応していません"); return; }
    try {
      const opts = await post("/api/v1/me/passkeys/register/options");
      const cred = await navigator.credentials.create({ publicKey: creationOptions(opts.publicKey) });
      await post("/api/v1/me/passkeys/register/verify", {
        challenge_id: opts.challenge_id, credential: credentialJSON(cred),
        name: $("input[name=name]", form)?.value || null,
      });
      location.assign("/settings/security?done=passkey_added");
    } catch (err) {
      if (err && err.name === "InvalidStateError") showError("この端末のパスキーは既に登録されています");
      else if (!cancelled(err)) showError(err.message || String(err));
    }
  });

  // ---- 本人確認 -----------------------------------------------------------------
  document.addEventListener("click", async (e) => {
    if (!e.target.closest("[data-passkey-reauth]")) return;
    if (!supported) { showError("このブラウザはパスキーに対応していません"); return; }
    try {
      const opts = await post("/auth/passkeys/login/options");
      const cred = await navigator.credentials.get({ publicKey: requestOptions(opts.publicKey) });
      await post("/auth/reauth", { challenge_id: opts.challenge_id, credential: credentialJSON(cred) });
      location.assign("/settings/security?done=reauthed");
    } catch (err) {
      if (!cancelled(err)) showError(err.message || String(err));
    }
  });
})();
