// docseed 画面の小さなスクリプト（ダイアログ・アップロード・お知らせ）。
// CSP で inline script を禁止しているため、動きはすべてこのファイルでデータ属性に結びつける。
(() => {
  "use strict";

  // URL の起点（DOCSEED_BASE_PATH。例 "/docseed"）。base.html の <meta name="docseed-base"> から読む
  const BASE = (document.querySelector('meta[name="docseed-base"]') || {}).content || "";

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
    if (toggle) setNav(!$("#side")?.classList.contains("open"));
    else if (e.target.closest("[data-nav-close]") || ($("#side.open") && e.target.closest("#side a"))) setNav(false);

    // スマホ: 検索の絞り込みを開く・閉じる
    const ft = e.target.closest("[data-filters-toggle]");
    if (ft) {
      const form = ft.closest("form");
      form.classList.toggle("filters-open");
      ft.setAttribute("aria-expanded", form.classList.contains("filters-open"));
    }
  });

  // サイドバー（スマホでは引き出し）の開閉。開いている間は後ろを暗くし、外側・Esc で閉じる
  function setNav(open) {
    const side = $("#side");
    if (!side) return;
    side.classList.toggle("open", open);
    $$("[data-nav-toggle]").forEach((b) => b.setAttribute("aria-expanded", open));
    const bd = $(".side-backdrop");
    if (bd) bd.hidden = !open;
    document.body.classList.toggle("nav-open", open);
  }
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && $("#side.open")) setNav(false); });

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
  // ---- キーボード操作（? で一覧を出す） ------------------------------------
  // g に続けて押すと画面を移る。s: お気に入り、u: アップロード、n: 新しいフォルダ、検索結果では j / k
  const GOTO = { m: "[data-goto=files]", f: "[data-goto=favorites]", s: "[data-goto=shared]",
                 t: "[data-goto=tags]", d: "[data-goto=trash]", a: "[data-goto=admin]",
                 h: "[data-goto=help]" };
  let gPressed = 0;
  const typing = (t) => t.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(t.tagName);
  document.addEventListener("keydown", (e) => {
    if (e.ctrlKey || e.metaKey || e.altKey || typing(e.target) || $("dialog[open]") || e.defaultPrevented) return;
    const k = e.key;
    if (gPressed && Date.now() - gPressed < 1500) {
      gPressed = 0;
      const link = GOTO[k] && $(GOTO[k]);
      if (link) { e.preventDefault(); location.href = link.href; }
      return;
    }
    gPressed = 0;
    if (k === "g") { gPressed = Date.now(); return; }
    if (k === "?") { e.preventDefault(); $("#dlg-keys")?.showModal(); return; }
    if (k === "s") { const b = $(".page-head .fav-btn, .doc-title .fav-btn"); if (b) { e.preventDefault(); b.click(); } return; }
    if (k === "u") { const b = $("[data-upload-pick]"); if (b) { e.preventDefault(); b.click(); } return; }
    if (k === "n") { const b = $("[data-open-dialog=dlg-folder]"); if (b) { e.preventDefault(); b.click(); } return; }
    if (k === "j" || k === "k") {                 // 検索結果を上下に移る（Enter で開く）
      const links = $$(".results .result-name");
      if (!links.length) return;
      e.preventDefault();
      const i = links.indexOf(document.activeElement);
      const j = i < 0 ? 0 : Math.max(0, Math.min(links.length - 1, i + (k === "j" ? 1 : -1)));
      links[j].focus();
      links[j].closest(".result")?.scrollIntoView({ block: "nearest" });
    }
  });

  // ---- 質問に答える（検索結果の資料をもとに LLM が答える。/api/v1/ask の stream） --------
  document.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-ask]");
    if (!btn) return;
    const box = btn.closest("#ask");
    const out = $(".ask-out", box), answer = $(".ask-answer", box), status = $(".ask-status", box),
          list = $(".ask-sources", box);
    btn.disabled = true;
    out.hidden = false;
    answer.textContent = "";
    list.replaceChildren();
    status.textContent = "関係する文書を探しています…";
    let sources = [], text = "", started = Date.now();
    const tick = setInterval(() => {
      if (!text) status.textContent = `答えを作っています…（${Math.round((Date.now() - started) / 1000)} 秒）`;
    }, 1000);
    const showSources = (cited) => {
      list.replaceChildren();
      for (const s of sources) {
        const li = document.createElement("li");
        li.id = `ask-src-${s.n}`;
        li.value = s.n;
        if (cited && !cited.includes(s.n)) li.classList.add("uncited");
        const a = document.createElement("a");
        const q = encodeURIComponent(box.dataset.question);
        a.href = BASE + `/d/${s.document_id}?q=${q}` + (s.page_no ? `#p${s.page_no}` : "");
        a.textContent = s.filename + (s.page_no ? `（${s.page_no} ページ）` : "");
        const where = document.createElement("span");
        where.className = "muted small";
        where.textContent = " " + s.path;
        const ex = document.createElement("div");
        ex.className = "muted small ask-excerpt";
        ex.textContent = s.text.length > 160 ? s.text.slice(0, 160) + "…" : s.text;
        li.append(a, where, ex);
        list.append(li);
      }
    };
    // 答えの [n] を資料へのリンクにする（文字列は textContent で入れ、HTML としては解釈しない）
    const linkCitations = () => {
      const frag = document.createDocumentFragment();
      let last = 0;
      for (const m of text.matchAll(/\[(\d{1,2})\]/g)) {
        const n = Number(m[1]);
        if (!sources.some((s) => s.n === n)) continue;
        frag.append(text.slice(last, m.index));
        const a = document.createElement("a");
        a.href = `#ask-src-${n}`;
        a.className = "cite";
        a.textContent = `[${n}]`;
        frag.append(a);
        last = m.index + m[0].length;
      }
      frag.append(text.slice(last));
      answer.replaceChildren(frag);
    };
    try {
      const res = await fetch(BASE + "/api/v1/ask", {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
        body: JSON.stringify({ question: box.dataset.question, filters: JSON.parse(box.dataset.filters || "{}"),
                               stream: true }),
      });
      if (!res.ok) {
        let msg = `答えを作れませんでした（HTTP ${res.status}）`;
        try { msg = (await res.json()).detail || msg; } catch { /* JSON でない */ }
        throw new Error(msg);
      }
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let i;
        while ((i = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, i).trim();
          buf = buf.slice(i + 1);
          if (!line) continue;
          const ev = JSON.parse(line);
          if (ev.type === "sources") {
            sources = ev.sources;
            showSources(null);
            status.textContent = sources.length ? "答えを作っています…" : "";
          } else if (ev.type === "text") {
            text += ev.text;
            answer.textContent = text;
          } else if (ev.type === "done") {
            if (ev.no_sources) {
              answer.textContent = "質問に関係しそうな文書が見つかりませんでした。言い方を変えるか、絞り込みを外してみてください。";
              status.textContent = "";
            } else {
              linkCitations();
              showSources(ev.cited);
              status.textContent = `${(ev.took_ms / 1000).toFixed(1)} 秒` +
                (ev.cited.length ? "" : "（答えに資料の番号がありません。内容を資料で確かめてください）");
            }
          } else if (ev.type === "error") {
            throw new Error(ev.message);
          }
        }
      }
    } catch (err) {
      status.textContent = err.message || "答えを作れませんでした";
      status.classList.add("err-text");
    } finally {
      clearInterval(tick);
      btn.disabled = false;
    }
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
      // 検索から来たら、そのページの画像に読み取った位置を重ね、当たった行を光らせる
      const btn = terms.length && $("[data-ocr-toggle]", target);
      if (btn) showOcrOverlay(btn, terms, true);
    }
  });

  // OCR の行の位置を、ページの画像に重ねて表示する（位置は画像の幅・高さに対する割合で置く）
  async function showOcrOverlay(btn, terms, scrollToHit = false) {
    const box = btn.closest("details").querySelector(".ocr-overlay");
    if (!box) return;
    if (!box.hidden) { box.hidden = true; btn.setAttribute("aria-expanded", "false"); return; }
    box.hidden = false;
    btn.setAttribute("aria-expanded", "true");
    if (box.dataset.loaded) return;
    box.textContent = "読み込んでいます…";
    const base = BASE + `/api/v1/documents/${btn.dataset.doc}`;
    let data;
    try {
      const r = await fetch(`${base}/ocr?page=${btn.dataset.page}`, { headers: { Accept: "application/json" } });
      if (!r.ok) throw new Error(String(r.status));
      data = await r.json();
    } catch {
      box.textContent = "読み取った位置を読み込めませんでした";
      return;
    }
    const stage = document.createElement("div");
    stage.className = "ocr-stage";
    const img = document.createElement("img");
    img.alt = `${btn.dataset.page} ページの画像`;
    img.src = `${base}/pages/${btn.dataset.page}/image`;
    stage.append(img);
    const caption = document.createElement("p");
    caption.className = "ocr-caption small muted";
    caption.textContent = "枠を押すと、その行の読み取り結果を表示します。点線の枠は本文に使っていない行です。";
    const lowered = terms.map((t) => t.toLowerCase());
    let firstHit = null, hits = 0;
    const W = data.width || 1, H = data.height || 1;
    for (const ln of data.lines) {
      const xs = ln.bbox.map((p) => p[0]), ys = ln.bbox.map((p) => p[1]);
      const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
      const el = document.createElement("button");
      el.type = "button";
      el.className = "ocr-box";
      if (ln.confidence < data.min_score) el.classList.add("low");
      const text = ln.text.normalize("NFKC").toLowerCase();
      if (lowered.some((t) => text.includes(t))) {
        el.classList.add("hit");
        hits += 1;
        firstHit ??= el;
      }
      el.style.left = `${(x0 / W) * 100}%`;
      el.style.top = `${(y0 / H) * 100}%`;
      el.style.width = `${((x1 - x0) / W) * 100}%`;
      el.style.height = `${((y1 - y0) / H) * 100}%`;
      el.title = ln.text;
      el.setAttribute("aria-label", `${ln.text}（信頼度 ${ln.confidence.toFixed(2)}）`);
      el.addEventListener("click", () => {
        $$(".ocr-box.active", stage).forEach((b) => b.classList.remove("active"));
        el.classList.add("active");
        caption.textContent = `${ln.text}（信頼度 ${ln.confidence.toFixed(2)}）`;
      });
      stage.append(el);
    }
    const note = document.createElement("p");
    note.className = "small muted";
    note.textContent = `${data.lines.length} 行` + (lowered.length ? `・検索語を含む行 ${hits} 行（色の付いた枠）` : "");
    box.replaceChildren(note, stage, caption);
    box.dataset.loaded = "1";
    img.addEventListener("error", () => { box.replaceChildren("ページの画像を読み込めませんでした"); delete box.dataset.loaded; });
    if (scrollToHit && firstHit) img.addEventListener("load", () => firstHit.scrollIntoView({ block: "center" }));
  }
  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-ocr-toggle]");
    if (btn) showOcrOverlay(btn, searchTerms());
  });

  // ---- Markdown のプレビュー: 「表示」と「元の文字」を切り替える ---------
  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-md-view]");
    if (!btn) return;
    const box = btn.closest(".preview");
    $$("[data-md-view]", box).forEach((b) => b.setAttribute("aria-pressed", b === btn ? "true" : "false"));
    $$("[data-md-pane]", box).forEach((p) => { p.hidden = p.dataset.mdPane !== btn.dataset.mdView; });
  });

  // ---- 処理状況の自動更新 -----------------------------------------------
  setInterval(() => {
    const listing = $("#listing[data-has-pending]");
    if (!listing || document.hidden || uploading > 0) return;
    if ($("details.rowmenu[open]") || $("dialog[open]") || dragItems) return;
    htmx.trigger(listing, "refresh");
  }, 15000);

  // 文書の画面: 文字の読み取り・要約の作り直しの間だけ状態を問い合わせる。進み具合は書き換え、
  // 終わったら（key が変わったら）読み直す。入力中・ダイアログを開いている間は読み直さない
  const live = $("[data-live-url]");
  if (live) {
    let busyEditing = false;
    document.addEventListener("input", (e) => { if (e.target.closest("form")) busyEditing = true; });
    const tick = async () => {
      if (document.hidden) return;
      let st;
      try {
        const r = await fetch(live.dataset.liveUrl, { headers: { Accept: "application/json" } });
        if (!r.ok) return;
        st = await r.json();
      } catch { return; }
      const prog = $("[data-live-progress]");
      if (prog && st.total) prog.textContent = `${st.done}/${st.total}`;
      if (st.key === live.dataset.liveKey) return;
      if (busyEditing || $("dialog[open]") || document.activeElement?.matches("input, textarea, select")) {
        live.dataset.liveKey = st.key;   // 読み直さず、次の変化を待つ
        showToast("処理が終わりました。再読み込みすると反映されます");
        return;
      }
      location.reload();
    };
    const timer = setInterval(async () => {
      await tick();
      if (!document.body.contains(live)) clearInterval(timer);
    }, 5000);
  }

  // ---- アップロード ------------------------------------------------------
  let uploading = 0;
  window.addEventListener("beforeunload", (e) => { if (uploading > 0) { e.preventDefault(); e.returnValue = ""; } });

  document.addEventListener("click", (e) => {
    if (e.target.closest("[data-upload-pick]")) $("#upload-input")?.click();
    if (e.target.closest("[data-camera-pick]")) $("#camera-input")?.click();
    if (e.target.closest("[data-uploads-close]")) {
      $("#uploads").hidden = true;
      $("#uploads-list").replaceChildren();
    }
  });
  document.addEventListener("change", (e) => {
    if ((e.target.id === "upload-input" || e.target.id === "camera-input") && e.target.files.length) {
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
    const queue = items.map((it) => ({ ...it, folderId, row: addRow(list, it, toName) }));
    let next = 0;
    const worker = async () => {
      while (next < queue.length) await uploadOne(queue[next++]);
    };
    uploading += queue.length;
    Promise.all([worker(), worker()]).then(() => {
      updateUploadsTitle();
      const listing = $("#listing");
      if (listing) htmx.trigger(listing, "refresh");
    });
  }

  // 一覧の見出し（全部終わったときだけ書き換える。失敗した行を「再試行」したときにも呼ぶ）
  function updateUploadsTitle() {
    if (uploading > 0) return;
    const rows = $$("#uploads-list li");
    const failed = rows.filter((li) => li.classList.contains("up-error")).length;
    const ok = rows.filter((li) => li.classList.contains("up-done")).length;
    $("#uploads-title").textContent = failed
      ? `${ok} 件をアップロードしました（${failed} 件は失敗）`
      : `${ok} 件をアップロードしました`;
    $("[data-uploads-close]").hidden = false;
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

  // 失敗した行の「再試行」。受け取り済みの分はサーバーに残っているので、続きから送る
  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-upload-retry]");
    if (!btn) return;
    const li = btn.closest("li");
    const job = li && li._uploadJob;
    if (!job) return;
    li.classList.remove("up-error");
    $$(".up-msg, [data-upload-retry]", li).forEach((el) => el.remove());
    $("[data-uploads-close]").hidden = true;
    $("#uploads-title").textContent = "アップロード中";
    uploading++;
    uploadOne(job).then(() => {
      updateUploadsTitle();
      const listing = $("#listing");
      if (listing) htmx.trigger(listing, "refresh");
    });
  });

  // ---- 分割・再開できるアップロード（tus 1.0。docs/design/api.md 3.3.2） ------
  // 1 ファイルを CHUNK ごとの PATCH で送る。通信が切れたら、待ってから HEAD で受け取り済みの位置を聞いて続きを送る。
  // 送り先の URL は localStorage に覚えておき、再読み込みのあとに同じファイルを選び直すと続きから送る。
  const TUS = BASE + "/api/v1/uploads";
  const CHUNK = 8 * 1024 * 1024;                     // Cloudflare Tunnel など 1 リクエストの上限がある経路でも通る大きさ
  const RETRY_DELAYS = [1, 2, 4, 8, 15, 30, 30, 60, 60, 60];   // 秒。前に進んだら最初からやり直す
  const STALL_MS = 60000;                            // この間まったく進まなければ切れたとみなす
  const SAVE_WAIT_MS = 10 * 60000;                   // 最後まで送ったあと、サーバーが保存し終えるのを待つ上限
  const RETRYABLE = new Set([0, 408, 423, 429, 500, 502, 503, 504]);
  const RESUME_KEY = "docseed.upload:";

  const resumeKey = (job) => RESUME_KEY + [job.folderId, job.path || job.file.name, job.file.size,
    job.file.lastModified, job.file.type].join("|");
  const remember = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* 覚えられなくても送れる */ } },
    del(k) { try { localStorage.removeItem(k); } catch { /* 何もしない */ } },
  };

  function b64(s) {
    let bin = "";
    for (const b of new TextEncoder().encode(s)) bin += String.fromCharCode(b);
    return btoa(bin);
  }

  class UploadError extends Error {
    constructor(status, code, detail) { super(detail); this.status = status; this.code = code; }
  }

  // XMLHttpRequest を Promise にする（fetch では送った量の進み具合を取れない）。
  // 通信の失敗・止まったままのときは status 0 で返す（例外にしない）。
  function tusRequest(method, url, { headers = {}, body = null, onProgress, onSent } = {}) {
    return new Promise((resolve) => {
      const xhr = new XMLHttpRequest();
      let timer = null;
      const arm = (ms) => { clearTimeout(timer); timer = setTimeout(() => xhr.abort(), ms); };
      xhr.open(method, url);
      xhr.setRequestHeader("Tus-Resumable", "1.0.0");
      if (method !== "HEAD" && method !== "GET") xhr.setRequestHeader("X-CSRF-Token", csrfToken());
      for (const [k, v] of Object.entries(headers)) xhr.setRequestHeader(k, v);
      xhr.upload.onprogress = (ev) => { arm(STALL_MS); onProgress?.(ev.loaded); };
      xhr.upload.onload = () => { arm(SAVE_WAIT_MS); onSent?.(); };
      const done = (status) => {
        clearTimeout(timer);
        let json = null;
        try { json = xhr.responseText ? JSON.parse(xhr.responseText) : null; } catch { /* 本文なし */ }
        resolve({ status, json, header: (n) => xhr.getResponseHeader(n) });
      };
      xhr.onload = () => done(xhr.status);
      xhr.onerror = xhr.onabort = xhr.ontimeout = () => done(0);
      arm(STALL_MS);
      xhr.send(body);
    });
  }

  function failure(r) {
    const e = new UploadError(r.status, r.json?.code || "",
      r.json?.detail || (r.status ? `エラー（${r.status}）` : "サーバーに接続できません"));
    e.retryAfter = parseInt(r.header("Retry-After") || "", 10) || 0;
    return e;
  }

  // 回線が戻るか、delay 秒たつのを待つ。待っている間は残り秒数を出す
  function waitToRetry(delay, retryAfter, setState) {
    return new Promise((resolve) => {
      let left = Math.max(delay, retryAfter || 0);
      const finish = () => { clearInterval(tick); window.removeEventListener("online", onOnline); resolve(); };
      const onOnline = () => setTimeout(finish, 1000);
      // 状態の欄は狭いので短く出し、説明は title に入れる
      const show = () => (navigator.onLine === false
        ? setState("オフライン", "回線が戻ったら、続きから自動で再開します")
        : setState(`再開まで ${left}秒`, `接続が切れました。${left} 秒後に、届いたところの続きから再開します`));
      const tick = setInterval(() => {
        if (navigator.onLine === false) { show(); return; }   // オフラインの間は数えない
        if (--left <= 0) finish(); else show();
      }, 1000);
      window.addEventListener("online", onOnline);
      show();
    });
  }

  async function tusUpload(job, onProgress, setState) {
    const file = job.file;
    const key = resumeKey(job);
    let url = remember.get(key);
    let offset = null;          // null = サーバーに聞く（HEAD）
    let attempt = 0;
    let restarted = false;
    for (;;) {
      try {
        if (!url) {
          const meta = { filename: file.name, folder_id: String(job.folderId), on_conflict: "rename" };
          if (job.path) meta.relative_path = job.path;
          const r = await tusRequest("POST", TUS, { headers: {
            "Upload-Length": String(file.size),
            "Upload-Metadata": Object.entries(meta).map(([k, v]) => `${k} ${b64(v)}`).join(","),
          } });
          if (r.status !== 201) throw failure(r);
          url = new URL(r.header("Location"), location.href).href;
          remember.set(key, url);
          offset = 0;
          if (r.json?.result) return r.json.result;          // 0 バイトのファイルは作った時点で保存済み
        }
        if (offset === null) {
          const r = await tusRequest("HEAD", url);
          if (r.status !== 200) throw failure(r);
          offset = parseInt(r.header("Upload-Offset"), 10) || 0;
          onProgress(offset);
        }
        for (;;) {
          const end = Math.min(offset + CHUNK, file.size);
          setState(null);
          // 最後まで届いているのに結果を受け取れていない場合は、空の PATCH で結果を聞く
          const r = await tusRequest("PATCH", url, {
            headers: { "Content-Type": "application/offset+octet-stream", "Upload-Offset": String(offset) },
            body: file.slice(offset, end),
            onProgress: (loaded) => onProgress(offset + loaded),
            onSent: () => { if (end === file.size) setState("保存中"); },
          });
          if (r.status === 200 && r.json?.result) { remember.del(key); return r.json.result; }
          if (r.status !== 204) throw failure(r);
          const next = parseInt(r.header("Upload-Offset"), 10);
          if (next > offset) attempt = 0;                     // 前に進んだので、待ち時間を最初に戻す
          offset = next;
          onProgress(offset);
        }
      } catch (err) {
        if (!(err instanceof UploadError)) throw err;
        if ((err.status === 404 || err.status === 410) && url && !restarted) {
          remember.del(key);                                 // 期限切れ: 最初から送り直す（1 回だけ）
          url = null; offset = null; restarted = true;
          continue;
        }
        if (err.code === "offset_mismatch" || RETRYABLE.has(err.status)) {
          if (attempt >= RETRY_DELAYS.length) throw err;
          await waitToRetry(RETRY_DELAYS[attempt++], err.retryAfter, setState);
          offset = null;                                     // 切れた時点で届いていた分は、サーバーに聞き直す
          continue;
        }
        if (err.status !== 401) remember.del(key);           // ログインし直せば続きから送れるよう 401 は覚えておく
        throw err;
      }
    }
  }

  async function uploadOne(job) {
    const row = job.row;
    row._uploadJob = job;
    const bar = $("progress", row);
    const state = $(".up-state", row);
    const size = job.file.size || 1;
    let waiting = false;
    const onProgress = (sent) => {
      bar.value = Math.min(100, Math.round((sent / size) * 100));
      if (!waiting) state.textContent = bar.value + "%";
    };
    const setState = (msg, title = "") => {
      waiting = !!msg;
      row.classList.toggle("up-wait", waiting);
      state.textContent = msg || bar.value + "%";
      state.title = title;
    };
    state.textContent = bar.value + "%";
    let ok = false;
    try {
      const r = await tusUpload(job, onProgress, setState);
      if (r.status === "error") throw new UploadError(200, r.error?.code, r.error?.detail || "保存できませんでした");
      row.classList.remove("up-wait");
      row.classList.add("up-done");
      bar.value = 100;
      state.textContent = r.deduplicated ? "完了（同じ内容あり）" : "完了";
      if (r.document && r.document.filename !== job.file.name && !job.path) {
        $(".up-name", row).textContent = job.file.name + " → " + r.document.filename;
      }
      ok = true;
    } catch (err) {
      row.classList.remove("up-wait");
      row.classList.add("up-error");
      state.textContent = "失敗";
      const m = document.createElement("span");
      m.className = "up-msg";
      m.textContent = err.status === 401
        ? "ログインの有効期限が切れました。再読み込みしてログインし、同じファイルを選ぶと続きから送ります"
        : (err.message || "アップロードできませんでした");
      row.append(m);
      if (err.status === 0 || err.status === 401 || RETRYABLE.has(err.status)) {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "linkish up-retry";
        b.dataset.uploadRetry = "";
        b.textContent = "再試行";
        m.append(" ", b);
      }
    }
    uploading--;
    return ok;
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
      // ファイルだけが対象の操作（タグ・再処理・やり直し）は、ファイルを選んでいなければ押せなくする
      const hasDoc = [...selected].some((k) => k.startsWith("document:"));
      $$("[data-sel-docs]", bar).forEach((b) => (b.disabled = !hasDoc));
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
    submitBatch(BASE + "/x/items/delete", rows.map((r) => r.dataset.item));
  }

  document.addEventListener("htmx:afterSettle", renderSelection);

  document.addEventListener("click", (e) => {
    if (e.target.closest("[data-sel-clear]")) { clearSelection(); return; }
    if (e.target.closest("[data-sel-delete]")) { deleteSelected(); return; }
    const docRows = () => selectedRows().filter((r) => r.dataset.item.startsWith("document:"));
    if (e.target.closest("[data-sel-tags]")) {
      const rows = docRows();
      if (rows.length) openItemDialog("dlg-tags", BASE + "/x/items/tags", "", (form) => {
        form.reset();
        setItemInputs(form, rows.map((r) => r.dataset.item));
        $("[data-tags-name]", form).textContent = describe(rows) + "のタグを変更します";
        $("input[name=add]", form).focus();
      });
      return;
    }
    if (e.target.closest("[data-sel-reprocess]")) {
      const rows = docRows();
      if (rows.length && confirm(describe(rows) + "の文字の読み取りをやり直しますか？")) {
        submitBatch(BASE + "/x/items/reprocess", rows.map((r) => r.dataset.item));
      }
      return;
    }
    if (e.target.closest("[data-sel-redo]")) {
      const rows = docRows();
      if (rows.length && confirm(describe(rows) + "の要約とタグの候補を作り直しますか？（今の要約は上書きされます）")) {
        submitBatch(BASE + "/x/items/redo", rows.map((r) => r.dataset.item));
      }
      return;
    }
    if (e.target.closest("[data-sel-move]")) {
      const rows = selectedRows();
      if (rows.length) openItemDialog("dlg-move", BASE + "/x/items/move", "", (form) => {
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

  // ---- タッチ操作（スマホ）での選択 ----------------------------------------
  // 行を長押しすると選択を始める。選択中は行のタップで選択に足す・外す（名前のリンクも開かずに選ぶ）。
  // 行のメニューの「選択」からも始められる。タッチではドラッグでの移動を使わない（長押しと重なるため）
  const coarse = window.matchMedia?.("(pointer: coarse)");
  let lastPointer = "mouse";
  let pressTimer = null, pressStart = null, suppressClick = false;
  const touchSelecting = () => lastPointer !== "mouse" && selected.size > 0;

  function syncTouchDrag() {
    if (!coarse?.matches) return;
    $$("#listing tr[data-item][draggable]").forEach((r) => r.removeAttribute("draggable"));
  }
  document.addEventListener("DOMContentLoaded", syncTouchDrag);
  document.addEventListener("htmx:afterSettle", syncTouchDrag);

  document.addEventListener("pointerdown", (e) => {
    lastPointer = e.pointerType || "mouse";
    clearTimeout(pressTimer);
    if (e.pointerType !== "touch" || !editable()) return;
    const row = e.target.closest("#listing tr[data-item]");
    if (!row || e.target.closest("details, button, input, select, textarea, label")) return;
    pressStart = [e.clientX, e.clientY];
    pressTimer = setTimeout(() => {
      pressTimer = null;
      suppressClick = true;                // 指を離したときのクリック（リンクを開く）を止める
      const first = selected.size === 0;
      selectItem(row.dataset.item, { toggle: true });
      navigator.vibrate?.(15);
      if (first) showToast("ほかの行をタップすると選択に足せます");
    }, 450);
  }, { passive: true });
  const cancelPress = () => { clearTimeout(pressTimer); pressTimer = null; };
  document.addEventListener("pointermove", (e) => {
    if (pressTimer && pressStart && Math.hypot(e.clientX - pressStart[0], e.clientY - pressStart[1]) > 10) cancelPress();
  }, { passive: true });
  document.addEventListener("pointerup", cancelPress, { passive: true });
  document.addEventListener("pointercancel", cancelPress, { passive: true });
  // 長押しで出る端末のメニュー（リンクを開く・画像を保存など）は、選択の邪魔になるので出さない
  document.addEventListener("contextmenu", (e) => {
    if (lastPointer === "touch" && editable() && e.target.closest("#listing tr[data-item]")) e.preventDefault();
  });

  // キャプチャ段階で受け、選択中のタップをリンクやメニューより先に選択の切り替えに使う
  document.addEventListener("click", (e) => {
    const sel = e.target.closest("[data-select-row]");
    if (sel) {
      const row = sel.closest("tr[data-item]");
      sel.closest("details").open = false;
      if (row) selectItem(row.dataset.item, { toggle: true });
      e.preventDefault();
      e.stopPropagation();
      return;
    }
    if (suppressClick) {
      suppressClick = false;
      if (e.target.closest("#listing tr[data-item]")) { e.preventDefault(); e.stopPropagation(); }
      return;
    }
    if (lastPointer === "mouse") return;
    const row = e.target.closest("#listing tr[data-item]");
    if (!row || e.target.closest("details, button, input, select, textarea, .reco")) return;
    if (!touchSelecting()) {
      // 選択していないときの行のタップは、名前の外でも開く（PC の「クリックで選択」はタッチでは使わない）
      if (!e.target.closest("a")) { e.stopPropagation(); openRow(row); }
      return;
    }
    e.preventDefault();
    e.stopPropagation();
    selectItem(row.dataset.item, { toggle: true });
  }, true);

  document.addEventListener("dblclick", (e) => {
    const row = e.target.closest("#listing tr[data-item]");
    if (!row || e.target.closest(INTERACTIVE)) return;
    window.getSelection()?.removeAllRanges();
    openRow(row);
  });

  document.addEventListener("keydown", (e) => {
    // 上下の移動と開くことは閲覧だけのフォルダでもできる。選択の操作（すべて選択・削除・名前の変更）は編集できるときだけ
    if (!listingEl() || e.altKey) return;
    const t = e.target;
    if (t.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(t.tagName) || $("dialog[open]")) return;
    const keys = itemRows().map((r) => r.dataset.item);
    if (!keys.length) return;
    const mod = e.ctrlKey || e.metaKey;
    const key = !mod && e.key === "j" ? "ArrowDown" : !mod && e.key === "k" ? "ArrowUp" : e.key;
    if (!editable() && (mod || !["ArrowDown", "ArrowUp", "Enter", "Escape"].includes(key))) return;
    if (!mod && key === "x" && cursor) {           // カーソルの行を選択に足す・外す
      e.preventDefault();
      selectItem(cursor, { toggle: true });
      return;
    }
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
      selectItem(keys[j], { range: e.shiftKey && editable() });
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
    submitBatch(BASE + "/x/items/move", dragItems, el.dataset.drop);
  });
  document.addEventListener("dragend", () => {
    dragItems = null;
    clearDropTarget();
    $$("#listing tr.dragging").forEach((r) => r.classList.remove("dragging"));
  });
})();
