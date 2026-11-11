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
      setItemInputs(form, []);
      $("[data-move-name]", form).textContent = "「" + mv.dataset.name + "」を移動します";
      htmx.trigger($("select[name=target]", form), "load-targets");
    });

    const copy = e.target.closest("[data-copy]");
    if (copy) {
      navigator.clipboard?.writeText(copy.dataset.copy).then(
        () => showToast("リンクをコピーしました"),
        () => showToast("コピーできませんでした。リンクを選んでコピーしてください", "error"));
    }

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

  // 通常の form 送信の確認（押したボタンに data-confirm があれば、そちらを使う）
  document.addEventListener("submit", (e) => {
    const msg = e.submitter?.dataset?.confirm || e.target.dataset?.confirm;
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
    if ($("details.rowmenu[open]") || $("dialog[open]") || dragItems) return;
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
    if (dragDepth === 0) { $("#dropzone").hidden = true; clearDropTarget(); }
  });
  window.addEventListener("dragover", (e) => {
    if (!$("#upload-input") || !hasFiles(e)) return;
    e.preventDefault();
    const row = e.target.closest?.("tr[data-drop]");
    setDropTarget(row);
    $("#dropzone").classList.toggle("dropzone-folder", !!row);
  });
  window.addEventListener("drop", async (e) => {
    if (!$("#upload-input") || !hasFiles(e)) return;
    e.preventDefault();
    dragDepth = 0;
    $("#dropzone").hidden = true;
    // フォルダの行の上で離したら、そのフォルダへアップロードする
    const row = e.target.closest?.("tr[data-drop]");
    clearDropTarget();
    const entries = Array.from(e.dataTransfer.items || []).map((it) => it.webkitGetAsEntry?.()).filter(Boolean);
    const items = entries.length ? await collectEntries(entries) : Array.from(e.dataTransfer.files).map((f) => ({ file: f, path: "" }));
    if (items.length) startUploads(items, row?.dataset.drop, row?.dataset.name);
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

  function startUploads(items, toFolder, toName) {
    const input = $("#upload-input");
    const folderId = toFolder || input.dataset.folderId;
    const panel = $("#uploads");
    const list = $("#uploads-list");
    panel.hidden = false;
    $("[data-uploads-close]").hidden = true;
    $("#uploads-title").textContent = "アップロード中";
    const queue = items.map((it) => ({ ...it, row: addRow(list, it, toName) }));
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

  function addRow(list, it, into) {
    const li = document.createElement("li");
    const name = document.createElement("span");
    name.className = "up-name";
    name.textContent = (into ? into + "/" : "") + (it.path || it.file.name);
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

  // ---- エクスプローラー風の選択と、ドラッグ＆ドロップでの移動 -------------
  // クリックで選択、Ctrl(⌘)+クリックで追加・解除、Shift+クリックで範囲選択、ダブルクリックで開く。
  // 選んだ行をフォルダの行・パンくず・サイドの「マイファイル」へドラッグすると移動する。
  // キー: Ctrl+A（すべて選択）、Esc（解除）、↑↓（Shift で範囲）、Enter（開く）、F2（名前を変更）、Delete（ゴミ箱へ）。
  const DRAG_MIME = "application/x-docseed-items";
  const INTERACTIVE = "a, button, input, select, textarea, label, summary, details";
  const selected = new Set();   // "folder:12" / "document:34"
  let anchor = null;            // Shift で範囲選択するときの起点
  let cursor = null;            // 矢印キーで動かす現在位置
  let dragItems = null;         // ドラッグ中の項目（dragover では dataTransfer の中身を読めないため持っておく）
  let dropEl = null;

  const listingEl = () => $("#listing");
  const editable = () => !!$("#listing[data-can-edit]");
  const itemRows = () => $$("#listing tr[data-item]");
  const rowOf = (key) => itemRows().find((r) => r.dataset.item === key);
  const selectedRows = () => itemRows().filter((r) => selected.has(r.dataset.item));
  const describe = (rows) => (rows.length === 1 ? "「" + rows[0].dataset.name + "」" : rows.length + " 件");

  function renderSelection() {
    const rows = itemRows();
    const keys = new Set(rows.map((r) => r.dataset.item));
    // 一覧が描き直されたら、なくなった項目は選択から外す
    for (const k of [...selected]) if (!keys.has(k)) selected.delete(k);
    if (anchor && !keys.has(anchor)) anchor = null;
    if (cursor && !keys.has(cursor)) cursor = null;
    rows.forEach((r) => {
      const on = selected.has(r.dataset.item);
      r.classList.toggle("selected", on);
      r.classList.toggle("cursor", on && r.dataset.item === cursor);
      if (editable()) r.setAttribute("aria-selected", on ? "true" : "false");
    });
    const bar = $("#selbar");
    const show = selected.size > 0 && editable();
    if (bar) {
      bar.hidden = !show;
      $("[data-sel-count]", bar).textContent = selected.size;
    }
    document.body.classList.toggle("has-selection", show);
  }

  function clearSelection() {
    if (!selected.size) return;
    selected.clear();
    anchor = cursor = null;
    renderSelection();
  }

  function selectItem(key, { toggle = false, range = false } = {}) {
    const keys = itemRows().map((r) => r.dataset.item);
    if (range && anchor && keys.includes(anchor)) {
      const a = keys.indexOf(anchor), b = keys.indexOf(key);
      if (!toggle) selected.clear();
      keys.slice(Math.min(a, b), Math.max(a, b) + 1).forEach((k) => selected.add(k));
    } else if (toggle) {
      if (selected.has(key)) selected.delete(key); else selected.add(key);
      anchor = key;
    } else {
      selected.clear();
      selected.add(key);
      anchor = key;
    }
    cursor = key;
    renderSelection();
  }

  function openRow(row) {
    const a = row && $("a.name", row);
    if (a) location.href = a.href;
  }

  // 一括の操作は、画面に置いた隠しフォームを htmx で送る（CSRF のヘッダーとお知らせの扱いを揃えるため）
  function setItemInputs(form, keys) {
    $$("input[name=item]", form).forEach((i) => i.remove());
    for (const k of keys) {
      const i = document.createElement("input");
      i.type = "hidden"; i.name = "item"; i.value = k;
      form.append(i);
    }
  }
  function submitBatch(url, keys, target) {
    const form = $("#batch-form");
    if (!form || !keys.length) return;
    setItemInputs(form, keys);
    let t = $("input[name=target]", form);
    if (!t) {
      t = document.createElement("input");
      t.type = "hidden"; t.name = "target";
      form.append(t);
    }
    t.value = target ?? "";
    t.disabled = target == null;
    form.setAttribute("hx-post", url);
    htmx.process(form);
    form.requestSubmit();
  }

  function deleteSelected() {
    const rows = selectedRows();
    if (!rows.length) return;
    const hasFolder = rows.some((r) => r.dataset.item.startsWith("folder:"));
    if (!confirm(describe(rows) + "をゴミ箱に移動しますか？" + (hasFolder ? "（フォルダは中身ごと移動します）" : ""))) return;
    submitBatch("/x/items/delete", rows.map((r) => r.dataset.item));
  }

  document.addEventListener("htmx:afterSettle", renderSelection);

  document.addEventListener("click", (e) => {
    if (e.target.closest("[data-sel-clear]")) { clearSelection(); return; }
    if (e.target.closest("[data-sel-delete]")) { deleteSelected(); return; }
    if (e.target.closest("[data-sel-move]")) {
      const rows = selectedRows();
      if (rows.length) openItemDialog("dlg-move", "/x/items/move", "", (form) => {
        setItemInputs(form, rows.map((r) => r.dataset.item));
        $("[data-move-name]", form).textContent = describe(rows) + "を移動します";
        htmx.trigger($("select[name=target]", form), "load-targets");
      });
      return;
    }

    const listing = listingEl();
    if (!listing || !editable() || e.button !== 0) return;
    if (e.target.closest("dialog, .selbar, .reco, .reco-bar, .auto-moves, .uploads")) return;
    const mod = e.ctrlKey || e.metaKey;
    const row = e.target.closest("tr[data-item]");
    if (!row || !listing.contains(row)) {
      // 一覧の外の空いたところをクリックしたら選択を外す
      if (e.target.closest("#main") && !e.target.closest(INTERACTIVE) && !mod && !e.shiftKey) clearSelection();
      return;
    }
    const control = e.target.closest(INTERACTIVE);
    if (control && !(control.matches("a.name") && (mod || e.shiftKey))) return; // 名前のリンクは今までどおり開く
    if (control) e.preventDefault(); // Ctrl/Shift + 名前のクリックは選択に使う
    selectItem(row.dataset.item, { toggle: mod, range: e.shiftKey });
  });

  document.addEventListener("dblclick", (e) => {
    const row = e.target.closest("#listing tr[data-item]");
    if (!row || e.target.closest(INTERACTIVE)) return;
    window.getSelection()?.removeAllRanges();
    openRow(row);
  });

  document.addEventListener("keydown", (e) => {
    if (!listingEl() || !editable() || e.altKey) return;
    const t = e.target;
    if (t.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(t.tagName) || $("dialog[open]")) return;
    const keys = itemRows().map((r) => r.dataset.item);
    if (!keys.length) return;
    const mod = e.ctrlKey || e.metaKey;
    const key = e.key;
    if (mod && key.toLowerCase() === "a") {
      e.preventDefault();
      keys.forEach((k) => selected.add(k));
      cursor = cursor || keys[0];
      renderSelection();
      return;
    }
    if (key === "Escape") { clearSelection(); return; }
    if (key === "ArrowDown" || key === "ArrowUp") {
      if (t.closest?.("details[open]")) return;
      e.preventDefault();
      const i = keys.indexOf(cursor);
      const step = key === "ArrowDown" ? 1 : -1;
      const j = i < 0 ? (step > 0 ? 0 : keys.length - 1) : Math.max(0, Math.min(keys.length - 1, i + step));
      selectItem(keys[j], { range: e.shiftKey });
      rowOf(keys[j])?.scrollIntoView({ block: "nearest" });
      return;
    }
    if (!selected.size || t.closest?.(INTERACTIVE)) return;
    const rows = selectedRows();
    if (key === "Enter" && rows.length === 1) { e.preventDefault(); openRow(rows[0]); }
    else if (key === "Delete" || (key === "Backspace" && e.metaKey)) { e.preventDefault(); deleteSelected(); }
    else if (key === "F2" && rows.length === 1) { e.preventDefault(); $("[data-rename]", rows[0])?.click(); }
  });

  function setDropTarget(el) {
    if (dropEl === el) return;
    dropEl?.classList.remove("drop-target");
    dropEl = el || null;
    dropEl?.classList.add("drop-target");
  }
  function clearDropTarget() { setDropTarget(null); }

  function dragGhost(rows) {
    let g = $("#drag-ghost");
    if (!g) {
      g = document.createElement("div");
      g.id = "drag-ghost";
      g.className = "drag-ghost";
      g.setAttribute("aria-hidden", "true");
      document.body.append(g);
    }
    g.textContent = rows.length === 1 ? rows[0].dataset.name : rows.length + " 項目を移動";
    return g;
  }

  document.addEventListener("dragstart", (e) => {
    const row = e.target.closest?.("#listing[data-can-edit] tr[data-item][draggable=true]");
    if (!row) return;
    // 選んでいない行をつかんだら、その行だけを選び直す（Explorer と同じ）
    if (!selected.has(row.dataset.item)) selectItem(row.dataset.item);
    const rows = selectedRows();
    dragItems = rows.map((r) => r.dataset.item);
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData(DRAG_MIME, JSON.stringify(dragItems));
    e.dataTransfer.setData("text/plain", rows.map((r) => r.dataset.name).join("\n"));
    e.dataTransfer.setDragImage(dragGhost(rows), 14, 14);
    // ドラッグの開始を描き終えてから薄くする（すぐ変えると Chrome で開始が取り消されることがある）
    requestAnimationFrame(() => rows.forEach((r) => r.classList.add("dragging")));
  });

  function internalDropTarget(e) {
    const el = e.target.closest?.("[data-drop]");
    if (!el || !dragItems) return null;
    const id = el.dataset.drop;
    if (dragItems.includes("folder:" + id)) return null;      // 自分自身の中へは移動しない
    if (id === listingEl()?.dataset.folderId) return null;    // 今いるフォルダ
    return el;
  }
  document.addEventListener("dragover", (e) => {
    if (!dragItems) return;
    const el = internalDropTarget(e);
    setDropTarget(el);
    if (el) {
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
    }
  });
  document.addEventListener("drop", (e) => {
    if (!dragItems) return;
    const el = internalDropTarget(e);
    clearDropTarget();
    if (!el) return;
    e.preventDefault();
    submitBatch("/x/items/move", dragItems, el.dataset.drop);
  });
  document.addEventListener("dragend", () => {
    dragItems = null;
    clearDropTarget();
    $$("#listing tr.dragging").forEach((r) => r.classList.remove("dragging"));
  });
})();
