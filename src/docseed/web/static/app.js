// docseed 画面の小さなスクリプト（ダイアログ・アップロード・お知らせ）。
// CSP で inline script を禁止しているため、動きはすべてこのファイルでデータ属性に結びつける。
(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  function csrfToken() {
    try { return JSON.parse(document.body.getAttribute("hx-headers") || "{}")["X-CSRF-Token"] || ""; }
    catch { return ""; }
  }

  // ---- お知らせ ----------------------------------------------------------
  let toastTimer = null;
  function showToast(message, kind = "ok") {
    const t = $("#toast");
    if (!t) return;
    if (message !== undefined) {
      t.textContent = message;
      t.className = "toast toast-" + kind;
    }
    if (!t.textContent.trim()) return;
    t.classList.remove("hide");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.add("hide"), 4000);
  }
  document.addEventListener("DOMContentLoaded", () => {
    showToast();
    // ?done=... を URL から消す（再読み込みで同じお知らせが出ないように）
    const url = new URL(location.href);
    if (url.searchParams.has("done")) {
      url.searchParams.delete("done");
      history.replaceState(null, "", url.pathname + (url.search || "") + url.hash);
    }
  });
  document.addEventListener("htmx:oobAfterSwap", (e) => { if (e.detail.target?.id === "toast") showToast(); });
  document.addEventListener("htmx:responseError", () => showToast("サーバーでエラーが起きました。時間をおいて再度お試しください", "error"));
  document.addEventListener("htmx:sendError", () => showToast("サーバーに接続できません", "error"));

  // ---- メニュー・ダイアログ ---------------------------------------------
  document.addEventListener("click", (e) => {
    // 開いているメニューは外側のクリックで閉じる
    $$("details.rowmenu[open], details.usermenu[open]").forEach((d) => { if (!d.contains(e.target)) d.open = false; });

    const opener = e.target.closest("[data-open-dialog]");
    if (opener) {
      const dlg = document.getElementById(opener.dataset.openDialog);
      dlg?.showModal();
      $("input:not([type=hidden])", dlg)?.focus();
    }
    if (e.target.closest("[data-close-dialog]")) e.target.closest("dialog")?.close();

    const ren = e.target.closest("[data-rename]");
    if (ren) openItemDialog("dlg-rename", ren.dataset.rename, ren.dataset.name, (form) => {
      const input = $("input[name=name]", form);
      input.value = ren.dataset.name;
      input.focus();
      // 拡張子の手前まで選択する
      const dot = ren.dataset.name.lastIndexOf(".");
      input.setSelectionRange(0, dot > 0 ? dot : ren.dataset.name.length);
    });

    const mv = e.target.closest("[data-move]");
    if (mv) openItemDialog("dlg-move", mv.dataset.move, mv.dataset.name, (form) => {
      $("[data-move-name]", form).textContent = "「" + mv.dataset.name + "」を移動します";
      htmx.trigger($("select[name=target]", form), "load-targets");
    });

    const toggle = e.target.closest("[data-nav-toggle]");
    if (toggle) {
      const side = $("#side");
      side.classList.toggle("open");
      toggle.setAttribute("aria-expanded", side.classList.contains("open"));
    }
  });

  function openItemDialog(id, url, name, setup) {
    const dlg = document.getElementById(id);
    if (!dlg) return;
    $$("details.rowmenu[open]").forEach((d) => (d.open = false));
    const form = $("form", dlg);
    form.setAttribute("hx-post", url);
    htmx.process(form);
    dlg.showModal();
    setup(form);
  }

  // 成功したらダイアログを閉じる
  document.addEventListener("htmx:afterRequest", (e) => {
    // フォーム自身の送信だけを対象にする（中の select が候補を読み込む GET では閉じない）
    const form = e.detail.elt;
    if (!form.matches?.("form[data-close-on-success]") || !e.detail.successful) return;
    // サーバーが入力エラーを返した場合（お知らせだけが返る）は閉じない
    if (e.detail.xhr.getResponseHeader("HX-Reswap") === "none") return;
    form.closest("dialog")?.close();
    form.reset();
  });

  // 通常の form 送信の確認
  document.addEventListener("submit", (e) => {
    const msg = e.target.dataset?.confirm;
    if (msg && !confirm(msg)) e.preventDefault();
  });

  // ---- 検索 --------------------------------------------------------------
  // 「/」で検索欄へ（入力中は除く）
  document.addEventListener("keydown", (e) => {
    if (e.key !== "/" || e.ctrlKey || e.metaKey || e.altKey) return;
    const t = e.target;
    if (t.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(t.tagName)) return;
    const box = $("[data-search-shortcut]") || $(".search-form input[name=q]");
    if (box) { e.preventDefault(); box.focus(); box.select(); }
  });
  // 絞り込みを変えたらすぐ検索し直す
  document.addEventListener("change", (e) => {
    const form = e.target.closest?.("form[data-autosubmit]");
    if (form && e.target.name !== "q" && form.q.value.trim()) form.requestSubmit();
  });

  // 文書詳細: #p3 のページを開き、検索語を強調する
  function searchTerms() {
    const q = new URL(location.href).searchParams.get("q");
    if (!q) return [];
    const out = [];
    for (const m of q.normalize("NFKC").matchAll(/-?"[^"]*"|\S+/g)) {
      let t = m[0];
      if (t.startsWith("-") || /^(type|tag|folder|after|before):/.test(t)) continue;
      t = t.replace(/^"|"$/g, "").trim();
      if (t) out.push(t);
    }
    return out.sort((a, b) => b.length - a.length);
  }
  function highlight(el, terms) {
    const text = el.textContent;
    const lower = text.toLowerCase();
    const marks = [];
    for (const t of terms) {
      const tl = t.toLowerCase();
      for (let i = lower.indexOf(tl); i >= 0; i = lower.indexOf(tl, i + tl.length)) {
        if (!marks.some(([s, e]) => i < e && i + tl.length > s)) marks.push([i, i + tl.length]);
      }
    }
    if (!marks.length) return 0;
    marks.sort((a, b) => a[0] - b[0]);
    const frag = document.createDocumentFragment();
    let pos = 0;
    for (const [s, e] of marks) {
      frag.append(text.slice(pos, s));
      const m = document.createElement("mark");
      m.textContent = text.slice(s, e);
      frag.append(m);
      pos = e;
    }
    frag.append(text.slice(pos));
    el.replaceChildren(frag);
    return marks.length;
  }
  document.addEventListener("DOMContentLoaded", () => {
    const terms = searchTerms();
    if (terms.length) $$("[data-highlight]").forEach((el) => highlight(el, terms));
    const m = location.hash.match(/^#p(\d+)$/);
    const target = m ? document.getElementById("p" + m[1]) : null;
    if (target) {
      target.open = true;
      const first = $("mark", target);
      (first || target).scrollIntoView({ block: first ? "center" : "start" });
    }
  });

  // ---- 処理状況の自動更新 -----------------------------------------------
  setInterval(() => {
    const listing = $("#listing[data-has-pending]");
    if (!listing || document.hidden || uploading > 0) return;
    if ($("details.rowmenu[open]") || $("dialog[open]")) return;
    htmx.trigger(listing, "refresh");
  }, 15000);

  // ---- アップロード ------------------------------------------------------
  let uploading = 0;
  window.addEventListener("beforeunload", (e) => { if (uploading > 0) { e.preventDefault(); e.returnValue = ""; } });

  document.addEventListener("click", (e) => {
    if (e.target.closest("[data-upload-pick]")) $("#upload-input")?.click();
    if (e.target.closest("[data-uploads-close]")) {
      $("#uploads").hidden = true;
      $("#uploads-list").replaceChildren();
    }
  });
  document.addEventListener("change", (e) => {
    if (e.target.id === "upload-input" && e.target.files.length) {
      startUploads(Array.from(e.target.files).map((f) => ({ file: f, path: "" })));
      e.target.value = "";
    }
  });

  // ドラッグ＆ドロップ（フォルダごとも可）
  let dragDepth = 0;
  const hasFiles = (e) => Array.from(e.dataTransfer?.types || []).includes("Files");
  window.addEventListener("dragenter", (e) => {
    if (!$("#upload-input") || !hasFiles(e)) return;
    dragDepth++;
    $("#dropzone").hidden = false;
  });
  window.addEventListener("dragleave", () => {
    if (!$("#upload-input")) return;
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) $("#dropzone").hidden = true;
  });
  window.addEventListener("dragover", (e) => { if ($("#upload-input") && hasFiles(e)) e.preventDefault(); });
  window.addEventListener("drop", async (e) => {
    if (!$("#upload-input") || !hasFiles(e)) return;
    e.preventDefault();
    dragDepth = 0;
    $("#dropzone").hidden = true;
    const entries = Array.from(e.dataTransfer.items || []).map((it) => it.webkitGetAsEntry?.()).filter(Boolean);
    const items = entries.length ? await collectEntries(entries) : Array.from(e.dataTransfer.files).map((f) => ({ file: f, path: "" }));
    if (items.length) startUploads(items);
  });

  async function collectEntries(entries, prefix = "") {
    const out = [];
    for (const entry of entries) {
      if (entry.isFile) {
        const file = await new Promise((res, rej) => entry.file(res, rej));
        out.push({ file, path: prefix ? prefix + "/" + file.name : "" });
      } else if (entry.isDirectory) {
        const reader = entry.createReader();
        let batch;
        const children = [];
        do {
          batch = await new Promise((res, rej) => reader.readEntries(res, rej));
          children.push(...batch);
        } while (batch.length);
        out.push(...(await collectEntries(children, prefix ? prefix + "/" + entry.name : entry.name)));
      }
    }
    return out;
  }

  function startUploads(items) {
    const input = $("#upload-input");
    const folderId = input.dataset.folderId;
    const panel = $("#uploads");
    const list = $("#uploads-list");
    panel.hidden = false;
    $("[data-uploads-close]").hidden = true;
    $("#uploads-title").textContent = "アップロード中";
    const queue = items.map((it) => ({ ...it, row: addRow(list, it) }));
    let next = 0;
    let failed = 0;
    const worker = async () => {
      while (next < queue.length) {
        const job = queue[next++];
        if (!(await uploadOne(folderId, job))) failed++;
      }
    };
    uploading += queue.length;
    Promise.all([worker(), worker()]).then(() => {
      const ok = queue.length - failed;
      $("#uploads-title").textContent = failed
        ? `${ok} 件をアップロードしました（${failed} 件は失敗）`
        : `${ok} 件をアップロードしました`;
      $("[data-uploads-close]").hidden = false;
      const listing = $("#listing");
      if (listing) htmx.trigger(listing, "refresh");
    });
  }

  function addRow(list, it) {
    const li = document.createElement("li");
    const name = document.createElement("span");
    name.className = "up-name";
    name.textContent = it.path || it.file.name;
    const bar = document.createElement("progress");
    bar.max = 100; bar.value = 0;
    const state = document.createElement("span");
    state.className = "up-state";
    state.textContent = "待機中";
    li.append(name, bar, state);
    list.append(li);
    return li;
  }

  function uploadOne(folderId, job) {
    return new Promise((resolve) => {
      const fd = new FormData();
      fd.append("folder_id", folderId);
      fd.append("on_conflict", "rename");
      fd.append("files", job.file, job.file.name);
      fd.append("relative_paths", job.path || "");
      const xhr = new XMLHttpRequest();
      const bar = $("progress", job.row);
      const state = $(".up-state", job.row);
      xhr.open("POST", "/api/v1/documents");
      xhr.setRequestHeader("X-CSRF-Token", csrfToken());
      xhr.upload.onprogress = (ev) => {
        if (ev.lengthComputable) {
          bar.value = Math.round((ev.loaded / ev.total) * 100);
          state.textContent = bar.value + "%";
        }
      };
      const fail = (msg) => {
        job.row.classList.add("up-error");
        state.textContent = "失敗";
        const m = document.createElement("span");
        m.className = "up-msg";
        m.textContent = msg;
        job.row.append(m);
        uploading--;
        resolve(false);
      };
      xhr.onload = () => {
        let body = null;
        try { body = JSON.parse(xhr.responseText); } catch { /* 何もしない */ }
        if (xhr.status === 200 && body?.results?.[0]?.status !== "error") {
          const r = body.results[0];
          job.row.classList.add("up-done");
          bar.value = 100;
          state.textContent = r.deduplicated ? "完了（同じ内容あり）" : "完了";
          if (r.document && r.document.filename !== job.file.name && !job.path) {
            $(".up-name", job.row).textContent = job.file.name + " → " + r.document.filename;
          }
          uploading--;
          resolve(true);
        } else if (xhr.status === 401) {
          fail("ログインの有効期限が切れました。再読み込みしてログインしてください");
        } else {
          fail(body?.results?.[0]?.error?.detail || body?.detail || `エラー（${xhr.status}）`);
        }
      };
      xhr.onerror = () => fail("サーバーに接続できません");
      state.textContent = "0%";
      xhr.send(fd);
    });
  }
})();
