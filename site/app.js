/* What's Cooking reader: loads (and if needed decrypts) the data bundle, then lays it out like a paper:
   a front page of headlines, and an article page for each story, problem and long-game challenge. */
(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const page = $("#page");
  const KEY_STORE = "wc:key";
  let DATA = null;
  let wireFilter = "all", wireView = "all", wireQuery = "";

  // ---- small helpers ------------------------------------------------------------
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const safeUrl = (u) => (/^https?:\/\//i.test(u || "") ? u : null);
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode: fine */ } },
    del(k) { try { localStorage.removeItem(k); } catch { /* ignore */ } },
  };
  const dayDate = (day) => new Date(`${day}T00:00:00`);
  const longDate = (day) => dayDate(day).toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long", year: "numeric" });
  const shortDate = (day) => dayDate(day).toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" });
  const ago = (iso) => {
    if (!iso) return "";
    const mins = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
    if (mins < 60) return `${Math.max(mins, 1)} min ago`;
    const h = Math.round(mins / 60);
    return h < 36 ? `${h} hours ago` : `${Math.round(h / 24)} days ago`;
  };
  const ageLabel = (h) => (h == null ? "" : h < 1 ? "now" : h < 48 ? `${Math.round(h)}h` : `${Math.round(h / 24)}d`);
  // "1 story", "10 stories", "2 communities", "3 days"
  const plural = (n, w) => `${n} ${n === 1 ? w : /[^aeiou]y$/.test(w) ? `${w.slice(0, -1)}ies` : `${w}s`}`;
  const firstSentence = (t) => { const m = String(t || "").match(/^.*?[.!?](\s|$)/); return (m ? m[0] : String(t || "")).trim(); };
  const domainOf = (u) => { try { return new URL(u).hostname.replace(/^www\./, ""); } catch { return ""; } };
  const todayLocal = () => { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`; };
  const TYPE = { release: "Release", tool: "Tool", research: "Research", news: "News", funding: "Funding", pain: "Problem" };
  const SIGNAL = { strong: "Strong signal", moderate: "Moderate signal", early: "Early signal" };
  const VERDICT = { "real progress": "Real progress", "small step": "Small step" };

  // ---- theme ----------------------------------------------------------------------
  const themeBtn = $("#theme-toggle");
  const isDark = () => {
    const t = document.documentElement.dataset.theme;
    return t ? t === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  };
  const syncThemeLabel = () => { themeBtn.textContent = isDark() ? "Day edition" : "Night edition"; };
  const saved = store.get("wc:theme");
  if (saved === "dark" || saved === "light") document.documentElement.dataset.theme = saved;
  syncThemeLabel();
  themeBtn.addEventListener("click", () => {
    const next = isDark() ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    store.set("wc:theme", next);
    syncThemeLabel();
  });
  matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", syncThemeLabel);

  // ---- loading & decryption -------------------------------------------------------------
  const b64 = (s) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));
  const toB64 = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf)));

  async function deriveKey(passphrase, blob) {
    const base = await crypto.subtle.importKey("raw", new TextEncoder().encode(passphrase), "PBKDF2", false, ["deriveKey"]);
    return crypto.subtle.deriveKey(
      { name: "PBKDF2", hash: "SHA-256", salt: b64(blob.salt), iterations: blob.iter },
      base, { name: "AES-GCM", length: 256 }, true, ["decrypt"]);
  }
  async function decryptWith(key, blob) {
    const plain = await crypto.subtle.decrypt({ name: "AES-GCM", iv: b64(blob.iv) }, key, b64(blob.ct));
    return JSON.parse(new TextDecoder().decode(plain));
  }
  async function fetchJson(url) {
    const r = await fetch(url, { cache: "no-cache" });
    if (!r.ok) throw new Error(`${url}: ${r.status}`);
    return r.json();
  }

  async function load() {
    if (window.__WC_DATA__) return start(window.__WC_DATA__);
    try { return start(await fetchJson("data.json")); } catch { /* not a plain build */ }
    let blob;
    try { blob = await fetchJson("data.enc.json"); }
    catch {
      page.innerHTML = `<p class="empty">The paper hasn't been printed yet. It appears after the first scheduled collection on GitHub.</p>`;
      return;
    }
    const remembered = store.get(KEY_STORE);
    if (remembered) {
      try {
        const key = await crypto.subtle.importKey("raw", b64(remembered), "AES-GCM", false, ["decrypt"]);
        return start(await decryptWith(key, blob));
      } catch { store.del(KEY_STORE); /* passphrase changed: ask again */ }
    }
    showUnlock(blob);
  }

  function showUnlock(blob) {
    page.replaceChildren($("#unlock-template").content.cloneNode(true));
    $("#nav").hidden = true;
    const form = $("#unlock-form"), btn = $("#unlock-button"), err = $("#unlock-error");
    $("#passphrase").focus();
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      btn.disabled = true; btn.textContent = "Opening…"; err.textContent = "";
      try {
        const key = await deriveKey($("#passphrase").value, blob);
        const data = await decryptWith(key, blob);
        if ($("#remember").checked) store.set(KEY_STORE, toB64(await crypto.subtle.exportKey("raw", key)));
        $("#nav").hidden = false;
        start(data);
      } catch {
        err.textContent = "That passphrase didn't open it. Check for typos; it's case-sensitive.";
        btn.disabled = false; btn.textContent = "Open the paper";
      }
    });
  }

  function start(data) {
    DATA = data;
    renderColophon();
    window.addEventListener("hashchange", route);
    route();
  }

  // ---- routing ------------------------------------------------------------------------------
  // #/today · #/edition/<day> · #/story/<day>/<n> · #/problems · #/problem/<id> · #/longgame
  // #/challenge/<id> · #/wire · #/archive
  const SECTION_OF = { today: "today", edition: "archive", story: "today", problems: "problems", problem: "problems",
                       longgame: "longgame", challenge: "longgame", wire: "wire", archive: "archive" };

  function route() {
    const [, name = "today", a, b] = (location.hash || "#/today").split("/").map(decodeURIComponent);
    const latest = DATA.editions[0];
    const section = name === "story" && latest && a !== latest.day ? "archive" : (SECTION_OF[name] || "today");
    document.querySelectorAll("#nav a").forEach((el) => {
      el.dataset.route === section ? el.setAttribute("aria-current", "page") : el.removeAttribute("aria-current");
    });
    const views = {
      today: () => frontPage(latest),
      edition: () => frontPage(DATA.editions.find((e) => e.day === a)),
      story: () => storyPage(a, Number(b)),
      problems: problemsPage,
      problem: () => problemPage(Number(a)),
      longgame: longGamePage,
      challenge: () => challengePage(a),
      wire: renderWire,
      archive: renderArchive,
    };
    (views[name] || views.today)();
    $("#edition-line").textContent = latest ? `No. ${latest.number} · ${longDate(latest.day)}` : "What's Cooking";
    if (name !== "today") page.focus({ preventScroll: true });
    window.scrollTo(0, 0);
  }

  // ---- links to a Claude chat, pre-filled with the context and every source ------------------
  const CHAT = "https://claude.ai/new?q=";
  const MAX_URL = 7500;              // under the common 8 KB server limit for a URL
  const chatUrl = (text) => CHAT + encodeURIComponent(text);
  const fits = (text) => chatUrl(text).length <= MAX_URL;
  function sourceLines(sources, max = 6) {
    return (sources || []).filter((s) => safeUrl(s.url)).slice(0, max).map((s) =>
      `- ${s.label}: ${s.url}${safeUrl(s.original) ? ` (original: ${s.original})` : ""}`);
  }
  const READ_THEM = "Open and read the linked discussions and original pages before answering, and tell me what they actually say. Keep answers short and plain: I build software but don't follow every niche.";
  const fitBuild = (build, n) => { let text = build(n); while (!fits(text) && n > 0) text = build(--n); return chatUrl(text); };

  const storyAsk = (s) => fitBuild((n) => [
    "I'm reading my daily tech brief and want to understand one story better.", "",
    `Story: ${s.headline}`, s.context, s.what, `Why it matters: ${s.why_it_matters}`, "",
    "Where it was discussed, and the original sources:", ...sourceLines(s.sources, n), "",
    READ_THEM, "Start with a short, simple explanation, then wait for my questions.",
  ].filter((x) => x != null).join("\n"), 6);

  function editionAsk(ed) {
    const stories = ed.top_stories || [];
    const head = [`This is my tech brief for ${longDate(ed.day)}. I'll ask you questions about it.`, READ_THEM, ""];
    const tail = ["", "Start by listing the stories in one short line each, then wait for my questions."];
    const build = (withSummary, linksPer) => [...head, ...stories.flatMap((s, i) => [
      `${i + 1}. ${s.headline}`,
      ...(withSummary ? [`   ${firstSentence(s.what)}`] : []),
      ...sourceLines(s.sources, linksPer).map((l) => `   ${l}`),
    ]), ...tail].join("\n");
    for (const [sum, n] of [[true, 3], [true, 2], [true, 1], [false, 1]]) {
      const text = build(sum, n);
      if (fits(text)) return chatUrl(text);
    }
    return chatUrl(build(false, 1).slice(0, 4000));
  }

  const problemAsk = (p) => fitBuild((n) => {
    const c = p.card || {};
    return [
      "I'm looking for problems worth solving and want to think about this one with you.", "",
      `Problem: ${c.title || p.title}`, c.summary, c.evidence && `Evidence: ${c.evidence}`,
      c.why_unsolved && `Why it's unsolved: ${c.why_unsolved}`,
      (c.solutions || []).length ? `Ideas so far: ${(c.solutions || []).map((x) => x.idea).join("; ")}` : "",
      ...((p.sources || []).length ? ["", "Where people raised it:", ...sourceLines(p.sources, n)] : []), "",
      (p.sources || []).length ? READ_THEM : "",
      "Help me judge whether this is worth building and what a small first version could be. Ask me what I know first if that helps.",
    ].filter(Boolean).join("\n");
  }, 8);

  const challengeAsk = (c) => fitBuild((n) => {
    const entries = (c.entries || []).slice(0, 4);
    return [
      "I'm following a big, long-running problem in tech and want to understand where it stands.", "",
      `Problem: ${c.title}`, c.summary, `What real progress would look like: ${c.progress}`,
      ...(entries.length ? ["", "Recent developments I've seen:", ...entries.flatMap((e) => [
        `- ${e.headline}: ${e.what_changed}`, ...sourceLines(e.sources, n).map((l) => `  ${l}`)])] : []),
      "", entries.length ? READ_THEM : "Keep answers short and plain: I build software but don't follow every niche.",
      "Tell me the current state of the art in a few short paragraphs, what's still missing, and where a solo builder could realistically contribute. Then wait for my questions.",
    ].join("\n");
  }, 3);

  const askLink = (href, label = "Ask Claude about this") =>
    `<a class="ask" href="${esc(href)}" target="_blank" rel="noopener">${label}<span aria-hidden="true"> ↗</span></a>`;
  const requestLink = (label) => safeUrl(DATA.request_url)
    ? `<a href="${esc(DATA.request_url)}" target="_blank" rel="noopener">${label} ↗</a>` : "";

  // ---- shared pieces ------------------------------------------------------------------------
  function sourcesLine(sources) {
    const parts = (sources || []).filter((s) => safeUrl(s.url)).map((s) =>
      `<span><a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.label)}</a>${safeUrl(s.original)
        ? ` → <a href="${esc(s.original)}" target="_blank" rel="noopener">${esc(domainOf(s.original))}</a>` : ""}</span>`);
    return parts.length ? `<p class="sources">${parts.join("")}</p>` : "";
  }
  function sourcesList(sources) {
    const items = (sources || []).filter((s) => safeUrl(s.url)).map((s) => `
      <li><a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.label)}</a>${safeUrl(s.original)
        ? `<span class="via">about <a href="${esc(s.original)}" target="_blank" rel="noopener">${esc(domainOf(s.original))}</a></span>` : ""}</li>`);
    return items.length ? `<section class="source-list"><h3 class="sub">Sources</h3><ul>${items.join("")}</ul></section>` : "";
  }
  const flag = (label, extra = "") => `<h2 class="flag"><span>${label}</span>${extra}</h2>`;
  const crumbs = (href, label, right = "") =>
    `<nav class="crumbs" aria-label="Back"><a href="${href}">← ${label}</a>${right ? `<span>${right}</span>` : ""}</nav>`;

  function readSet(day) {
    try { return new Set(JSON.parse(store.get(`wc:read:${day}`) || "[]")); } catch { return new Set(); }
  }
  function setRead(day, i, on) {
    const set = readSet(day);
    on ? set.add(i) : set.delete(i);
    store.set(`wc:read:${day}`, JSON.stringify([...set]));
  }

  // Between editions: say how old this one is, that the wire is fresher, and where to ask for a new one.
  function staleNotice(ed) {
    if (ed.day >= todayLocal()) return "";
    const last = DATA.collection?.last_run;
    const ask = requestLink("Ask for a new edition");
    return `<p class="notice">This edition is from ${esc(longDate(ed.day))}.${last ? ` The wire, problems and long game are fresher: last collected ${esc(ago(last))}.` : ""}${ask ? ` ${ask} (opens GitHub; press <b>Run workflow</b>).` : ""}</p>`;
  }

  // ---- the front page -------------------------------------------------------------------------
  function storyMeta(s) {
    return `<p class="slug"><span>${esc(TYPE[s.type] || "Story")}</span><span class="sig">${esc(SIGNAL[s.signal] || "")}</span>${s.communities ? `<span>${plural(s.communities, "community")}</span>` : ""}</p>`;
  }

  function frontPage(ed) {
    if (!ed) {
      const ask = requestLink("Open the Run workflow page");
      page.innerHTML = `
        <div class="page-head"><h2>No edition yet</h2>
        <p class="dek">Editions are written when you ask: on GitHub, open Actions, choose whats-cooking and press Run workflow.${ask ? ` ${ask}.` : ""} Meanwhile, <a href="#/wire">the wire</a> shows what's moving right now.</p></div>`;
      return;
    }
    if (ed.fallback) return fallbackPage(ed);
    const stories = ed.top_stories || [];
    const words = stories.reduce((n, s) => n + `${s.what} ${s.why_it_matters} ${s.so_what} ${s.skeptic}`.split(/\s+/).length, 0);
    const minutes = Math.max(3, Math.round(words / 230));
    const read = readSet(ed.day);
    const isLatest = ed === DATA.editions[0];
    const link = (i) => `#/story/${encodeURIComponent(ed.day)}/${i}`;
    const [lead, ...rest] = stories;
    const side = rest.slice(0, 3), more = rest.slice(3);
    const headline = (s, i) => `
      <li class="${read.has(i) ? "is-read" : ""}">
        <a href="${link(i)}">
          ${storyMeta(s)}
          <h3>${esc(s.headline)}</h3>
          <p class="deck">${esc(s.context || firstSentence(s.what))}</p>
          ${read.has(i) ? `<span class="read-mark">Read</span>` : ""}
        </a>
      </li>`;

    page.innerHTML = `
      ${isLatest ? staleNotice(ed) : crumbs("#/archive", "Back issues", `No. ${ed.number} · ${esc(shortDate(ed.day))}`)}
      <p class="edition-meta"><span>${plural(stories.length, "story")} · about ${minutes} min</span><span>from ${(ed.stats?.items || 0).toLocaleString("en")} items, ${ed.stats?.sources || 0} sources</span></p>

      ${(ed.themes || []).length ? `
      <section class="in-brief">
        ${flag("The day in brief")}
        <ul>${ed.themes.map((t) => `<li>${esc(t)}</li>`).join("")}</ul>
      </section>` : ""}

      ${lead ? `
      <section class="front">
        <article class="lead${read.has(0) ? " is-read" : ""}">
          <a href="${link(0)}">
            ${storyMeta(lead)}
            <h2>${esc(lead.headline)}</h2>
            ${lead.context ? `<p class="deck">${esc(lead.context)}</p>` : ""}
            <p class="lead-text">${esc(lead.what)}</p>
            <span class="more">${read.has(0) ? "Read again" : "Read the story"} →</span>
          </a>
        </article>
        ${side.length ? `<ol class="headlines side">${side.map((s, j) => headline(s, j + 1)).join("")}</ol>` : ""}
      </section>` : ""}

      ${more.length ? `
      <section>
        ${flag("Also in this edition")}
        <ol class="headlines grid">${more.map((s, j) => headline(s, j + 4)).join("")}</ol>
      </section>` : ""}

      ${(ed.quick_hits || []).length ? `
      <section>
        ${flag("Briefly")}
        <ul class="briefly">${ed.quick_hits.map((q) => `
          <li><p>${esc(q.line)}</p>${q.link && safeUrl(q.link.url) ? `<a class="wire-text" href="${esc(q.link.url)}" target="_blank" rel="noopener">${esc(q.link.label)}</a>` : ""}</li>`).join("")}
        </ul>
      </section>` : ""}

      <div class="boxes">
        ${problemsBox(ed)}
        ${(ed.long_game || []).length ? `
        <section class="box">
          <h2 class="box-head">The long game</h2>
          <ul class="box-list">${ed.long_game.map((e) => `
            <li><a href="#/challenge/${encodeURIComponent(e.challenge_id)}"><span class="slug"><span>${esc(e.challenge)}</span><span class="sig">${esc(VERDICT[e.verdict] || e.verdict)}</span></span>${esc(e.headline)}</a></li>`).join("")}
          </ul>
          <a class="box-more" href="#/longgame">All the big problems →</a>
        </section>` : ""}
        ${(ed.study || []).length ? `
        <section class="box">
          <h2 class="box-head">For the curious</h2>
          <ul class="box-list">${ed.study.map((x) => `
            <li><b>${esc(x.concept)}</b><span>${esc(x.why)}</span><span class="start"><i>Start with:</i> ${esc(x.start_with)}</span></li>`).join("")}
          </ul>
        </section>` : ""}
      </div>

      <footer class="edition-foot">
        ${stories.length ? askLink(editionAsk(ed), isLatest ? "Discuss today's edition with Claude" : "Discuss this edition with Claude") : ""}
        <div class="end-mark" aria-hidden="true"><span class="thirty">— 30 —</span><p>That's the edition.</p></div>
      </footer>`;
  }

  function problemsBox(ed) {
    const all = DATA.problems?.analysed || [];
    const fresh = all.filter((p) => (ed.problem_cards || []).includes(p.id));
    return `
      <section class="box">
        <h2 class="box-head">Unsolved, still</h2>
        ${fresh.length ? `<ul class="box-list">${fresh.map((p) => `
          <li><a href="#/problem/${p.id}">${esc(p.card?.title || p.title)}<span class="stat"><span><span class="n">${p.people}</span> people</span><span><span class="n">${p.threads}</span> threads</span></span></a></li>`).join("")}</ul>`
          : `<p class="box-note">No new problem crossed the bar for this edition.</p>`}
        <a class="box-more" href="#/problems">${all.length ? `The board: ${plural(all.length, "problem")}` : "The problems board"} →</a>
      </section>`;
  }

  function fallbackPage(ed) {
    page.innerHTML = `
      ${ed === DATA.editions[0] ? staleNotice(ed) : crumbs("#/archive", "Back issues")}
      <p class="notice">This is a signals-only edition: Claude wasn't used (${esc((ed.fallback_reason || "").slice(0, 160))}). The topics below are ranked by code alone.</p>
      <section>
        ${flag("Top topics by signal")}
        <ol class="headlines grid">${(ed.fallback_topics || []).map((t) => `
          <li><div class="plain">
            <p class="slug"><span>${esc(TYPE[t.type] || "Story")}</span><span>${plural(t.communities || 0, "community")}</span></p>
            <h3>${esc(t.title)}</h3>${sourcesLine(t.sources)}
          </div></li>`).join("")}
        </ol>
      </section>`;
  }

  // ---- a story, as its own article page ----------------------------------------------------------
  function storyPage(day, i) {
    const ed = DATA.editions.find((e) => e.day === day);
    const s = ed?.top_stories?.[i];
    if (!s) return frontPage(DATA.editions[0]);
    setRead(day, i, true);
    const n = ed.top_stories.length;
    const isLatest = ed === DATA.editions[0];
    const back = isLatest ? "#/today" : `#/edition/${encodeURIComponent(day)}`;
    const prev = ed.top_stories[i - 1], next = ed.top_stories[i + 1];
    page.innerHTML = `
      ${crumbs(back, isLatest ? "Front page" : `No. ${ed.number}`, `Story ${i + 1} of ${n}`)}
      <article class="article">
        <header class="article-head">
          ${storyMeta(s)}
          <h2>${esc(s.headline)}</h2>
          ${s.context ? `<p class="deck">${esc(s.context)}</p>` : ""}
          <p class="dateline">${esc(shortDate(day))}${s.items ? ` · ${plural(s.items, "report")}` : ""}${s.communities ? ` across ${plural(s.communities, "community")}` : ""}</p>
        </header>
        <div class="article-cols">
          <div class="article-body">
            <h3 class="sub">What happened</h3>
            <p class="drop">${esc(s.what)}</p>
            <h3 class="sub">Why it matters</h3>
            <p>${esc(s.why_it_matters)}</p>
          </div>
          <div class="article-side">
            <aside class="sidebar for-you"><h3>For you</h3><p>${esc(s.so_what)}</p></aside>
            <aside class="sidebar skeptic"><h3>The skeptic</h3><p>${esc(s.skeptic)}</p></aside>
            ${sourcesList(s.sources)}
            <div class="actions">${askLink(storyAsk(s))}</div>
          </div>
        </div>
      </article>
      <nav class="turn" aria-label="More stories">
        ${prev ? `<a class="prev" href="#/story/${encodeURIComponent(day)}/${i - 1}"><span>← Previous</span>${esc(prev.headline)}</a>` : "<span></span>"}
        ${next ? `<a class="next" href="#/story/${encodeURIComponent(day)}/${i + 1}"><span>Next →</span>${esc(next.headline)}</a>`
               : `<a class="next" href="${back}"><span>Done →</span>Back to the front page</a>`}
      </nav>`;
  }

  // ---- problems -----------------------------------------------------------------------------------
  function problemStats(p) {
    return `<p class="stat"><span><span class="n">${p.people}</span> people</span><span><span class="n">${p.threads}</span> threads</span><span><span class="n">${p.days_active}</span> ${p.days_active === 1 ? "day" : "days"}</span>${p.workarounds ? `<span><span class="n">${p.workarounds}</span> working around it</span>` : ""}</p>`;
  }

  function problemsPage() {
    const P = DATA.problems || { analysed: [], emerging: [], requests: [] };
    page.innerHTML = `
      <div class="page-head">
        <h2>Problems worth solving</h2>
        <p class="dek">What people keep complaining about, ranked by how many different people raise it, in how many separate threads, and for how long. A problem gets a full write-up once three or more people raise it in at least two threads.</p>
      </div>
      <section>
        ${flag("On the board")}
        ${P.analysed.length ? `<ol class="headlines grid">${P.analysed.map((p) => `
          <li><a href="#/problem/${p.id}">
            ${p.example ? `<p class="slug"><span class="sig">Example · yours appear after your first edition</span></p>` : problemStats(p)}
            <h3>${esc(p.card?.title || p.title)}</h3>
            <p class="deck">${esc(p.card?.summary || "")}</p>
          </a></li>`).join("")}</ol>`
          : `<p class="empty">Nothing has enough evidence yet. Complaints are collected three times a day; problems appear here as the same need comes up across threads.</p>`}
      </section>
      ${P.emerging.length ? `
      <section>
        ${flag("Still forming")}
        <ul class="briefly">${P.emerging.map((p) => `
          <li><p class="quote">“${esc(p.title)}”</p><span class="stat"><span><span class="n">${p.authors}</span> voices</span><span><span class="n">${p.threads}</span> threads</span></span></li>`).join("")}
        </ul>
      </section>` : ""}
      ${P.requests.length ? `
      <section>
        ${flag("Asked for by investors")}
        <ul class="briefly">${P.requests.map((p) => p.card
          ? `<li><a href="#/problem/${p.id}">${esc(p.card.title || p.title)}</a><span class="stat"><span>YC Request for Startups</span></span></li>`
          : `<li><p>${esc(p.title)}</p><span class="stat"><span>Awaiting a write-up</span></span></li>`).join("")}
        </ul>
      </section>` : ""}`;
  }

  function problemPage(id) {
    const P = DATA.problems || {};
    const p = [...(P.analysed || []), ...(P.requests || [])].find((x) => x.id === id);
    if (!p || !p.card) return problemsPage();
    const c = p.card;
    page.innerHTML = `
      ${crumbs("#/problems", "Problems")}
      <article class="article">
        <header class="article-head">
          ${p.example ? `<p class="slug"><span class="sig">Example card · your real ones appear after your first edition</span></p>`
            : p.kind === "request" ? `<p class="slug"><span>YC Request for Startups</span></p>` : problemStats(p)}
          <h2>${esc(c.title || p.title)}</h2>
          <p class="deck">${esc(c.summary)}</p>
        </header>
        <div class="article-cols">
          <div class="article-body">
            <h3 class="sub">The evidence</h3><p>${esc(c.evidence)}</p>
            <h3 class="sub">What exists today</h3><p>${esc(c.current_options)}</p>
            <h3 class="sub">Why it's unsolved</h3><p>${esc(c.why_unsolved)}</p>
            <h3 class="sub">Possible solutions</h3>
            <ol class="solutions">${(c.solutions || []).map((s) => `
              <li><p><b>${esc(s.idea)}</b><span class="tag">${esc(s.difficulty)}</span></p>
                  <p>${esc(s.how)}</p><p class="notyet"><i>Why not done yet:</i> ${esc(s.why_not_done_yet)}</p></li>`).join("")}
            </ol>
            <h3 class="sub">The opportunity</h3><p>${esc(c.opportunity)}</p>
          </div>
          <div class="article-side">
            <aside class="sidebar for-you"><h3>First step</h3><p>${esc(c.first_step)}</p></aside>
            ${(p.sources || []).length ? sourcesList(p.sources).replace(">Sources<", ">Where it came up<") : ""}
            <div class="actions">${askLink(problemAsk(p), "Think it through with Claude")}</div>
          </div>
        </div>
      </article>`;
  }

  // ---- the long game -----------------------------------------------------------------------------
  const monthLabel = (p) => new Date(`${p}-01T00:00:00`).toLocaleDateString("en-GB", { month: "short", year: "numeric" });

  // Interest over time: one quiet line (share of HN stories per month), the current month in the accent.
  // The full version adds a hover readout and a table of the numbers for screen readers.
  function sparkline(c, compact = false) {
    const s = c.trend || [];
    const valid = s.map((p, i) => ({ i, v: p.share, p })).filter((p) => p.v != null);
    if (valid.length < 3) return compact ? "" : `<p class="dek">The two-year history is still being filled in; it appears after a few collections.</p>`;
    const W = 600, H = 64, top = 6, base = H - 2;
    const max = Math.max(...valid.map((p) => p.v)) || 1;
    const x = (i) => (i / (s.length - 1)) * W;
    const y = (v) => base - (v / max) * (base - top);
    let line = "", started = false;
    s.forEach((p, i) => { if (p.share == null) { started = false; return; } line += `${started ? "L" : "M"}${x(i).toFixed(1)},${y(p.share).toFixed(1)}`; started = true; });
    const first = valid[0], last = valid[valid.length - 1];
    const area = `M${x(first.i).toFixed(1)},${base} ${line.replace(/^M/, "L")} L${x(last.i).toFixed(1)},${base} Z`;
    const svg = `
      <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true">
        <line class="spark-base" x1="0" y1="${base}" x2="${W}" y2="${base}" vector-effect="non-scaling-stroke"></line>
        <path class="spark-area" d="${area}"></path>
        <path class="spark-line" d="${line}" vector-effect="non-scaling-stroke"></path>
      </svg>
      <span class="spark-dot" style="left:${(x(last.i) / W * 100).toFixed(2)}%;top:${(y(last.v) / H * 100).toFixed(2)}%"></span>`;
    if (compact) return `<span class="spark mini" aria-hidden="true">${svg}</span>`;
    const peak = valid.reduce((a, b) => (b.v > a.v ? b : a));
    return `
      <figure class="spark-fig">
        <div class="spark" data-points='${esc(JSON.stringify(s.map((p) => [p.period, p.share, p.count])))}'>
          ${svg}<span class="spark-cross" hidden></span><span class="spark-tip" hidden></span>
        </div>
        <figcaption class="stat"><span>HN stories per 10,000, by month</span><span>now <span class="n">${last.v}</span></span><span>peak <span class="n">${peak.v}</span> in ${esc(monthLabel(peak.p.period))}</span>${changeText(c) ? `<span>${esc(changeText(c))}</span>` : ""}</figcaption>
        <table class="sr-only"><caption>${esc(c.title)}: Hacker News stories per 10,000 by month</caption>
          <tr><th>Month</th><th>Per 10,000 stories</th><th>Stories</th></tr>
          ${valid.map((p) => `<tr><td>${esc(monthLabel(p.p.period))}</td><td>${p.v}</td><td>${p.p.count}</td></tr>`).join("")}
        </table>
      </figure>`;
  }
  const changeText = (c) => c.change_pct == null ? "" : `${c.change_pct > 0 ? "up" : "down"} ${Math.abs(c.change_pct)}% in six months`;

  function wireSparklines(root) {
    root.querySelectorAll(".spark[data-points]").forEach((el) => {
      const pts = JSON.parse(el.dataset.points), cross = el.querySelector(".spark-cross"), tip = el.querySelector(".spark-tip");
      const show = (clientX) => {
        const r = el.getBoundingClientRect();
        const i = Math.max(0, Math.min(pts.length - 1, Math.round(((clientX - r.left) / r.width) * (pts.length - 1))));
        const [period, share, count] = pts[i];
        const left = (i / (pts.length - 1)) * 100;
        cross.hidden = tip.hidden = false;
        cross.style.left = `${left}%`;
        tip.style.left = `${Math.min(Math.max(left, 14), 86)}%`;
        tip.textContent = share == null ? `${monthLabel(period)} · not fetched yet`
          : `${monthLabel(period)}${i === pts.length - 1 ? " (so far)" : ""} · ${count} stories · ${share} per 10k`;
      };
      el.addEventListener("pointermove", (e) => show(e.clientX));
      el.addEventListener("pointerdown", (e) => show(e.clientX));
      el.addEventListener("pointerleave", () => { cross.hidden = tip.hidden = true; });
    });
  }

  function longGamePage() {
    const all = DATA.long_game || [];
    const row = (c) => `
      <li><a href="#/challenge/${encodeURIComponent(c.id)}" class="challenge-row">
        <div>
          <h3>${esc(c.title)}</h3>
          <p class="stat">${changeText(c) ? `<span>${esc(changeText(c))}</span>` : "<span>History filling in</span>"}<span><span class="n">${(c.entries || []).length}</span> recent moves</span>${(c.watching || []).length ? `<span><span class="n">${c.watching.length}</span> watching</span>` : ""}</p>
        </div>
        ${sparkline(c, true)}
      </a></li>`;
    page.innerHTML = `
      <div class="page-head">
        <h2>The long game</h2>
        <p class="dek">Big problems the whole field keeps working on: how much people talk about them over two years, what recently moved them forward, and who's complaining right now.</p>
      </div>
      ${all.some((c) => c.focus) ? `<section>${flag("Your focus")}<ol class="challenges">${all.filter((c) => c.focus).map(row).join("")}</ol></section>` : ""}
      ${all.some((c) => !c.focus) ? `<section>${flag("Also watching")}<ol class="challenges">${all.filter((c) => !c.focus).map(row).join("")}</ol></section>` : ""}
      ${all.length ? "" : `<p class="empty">No challenges configured.</p>`}`;
  }

  function challengePage(id) {
    const c = (DATA.long_game || []).find((x) => x.id === id);
    if (!c) return longGamePage();
    const comp = c.complaints || {};
    page.innerHTML = `
      ${crumbs("#/longgame", "The long game")}
      <article class="article">
        <header class="article-head">
          <p class="slug">${c.focus ? `<span class="sig">Your focus</span>` : "<span>Long-running problem</span>"}</p>
          <h2>${esc(c.title)}</h2>
          <p class="deck">${esc(c.summary)}</p>
        </header>
        <div class="article-cols">
        <div class="article-body">
          ${sparkline(c)}
          <h3 class="sub">What moved it recently</h3>
          ${(c.entries || []).length ? c.entries.map((e) => `
            <div class="progress">
              <p class="slug"><span class="sig">${esc(VERDICT[e.verdict] || e.verdict)}</span></p>
              <h4>${esc(e.headline)}</h4>
              <p>${esc(e.what_changed)} ${esc(e.why_it_helps)}</p>
              <p class="for-you-inline"><b>For you:</b> ${esc(e.for_you)}</p>
              ${sourcesLine(e.sources)}
            </div>`).join("")
          : `<p class="dek">Nothing judged yet. New releases that touch this are collected every run and judged when you ask for an edition.</p>`}
          ${(c.watching || []).length ? `<h3 class="sub">Being watched</h3>
            <ul class="briefly">${c.watching.map((w) => `<li><p>${esc(w.title)}</p>${sourcesLine(w.sources)}</li>`).join("")}</ul>` : ""}
          ${comp.people ? `<h3 class="sub">People complaining about it now</h3>
            <p class="stat"><span><span class="n">${comp.people}</span> people</span></p>
            <ul class="briefly">${(comp.top || []).map((p) => `<li><p>${esc(p.title)}</p></li>`).join("")}</ul>` : ""}
        </div>
        <div class="article-side">
          <aside class="sidebar for-you"><h3>Real progress would look like</h3><p>${esc(c.progress)}</p></aside>
          <div class="actions">${askLink(challengeAsk(c), "Ask Claude where this stands")}</div>
        </div>
        </div>
      </article>`;
    wireSparklines(page);
  }

  // ---- the wire -----------------------------------------------------------------------------
  const COMMUNITY_ORDER = ["hackernews", "github", "reddit", "huggingface"];
  const matchesQuery = (text) => {
    const words = wireQuery.toLowerCase().split(/\s+/).filter(Boolean);
    const t = String(text || "").toLowerCase();
    return words.every((w) => t.includes(w));
  };

  function renderWire() {
    const comms = DATA.communities || {};
    const views = [["all", "Everything, ranked"], ...COMMUNITY_ORDER.filter((k) => comms[k]?.sections?.length).map((k) => [k, comms[k].label])];
    page.innerHTML = `
      <div class="page-head">
        <h2>The wire</h2>
        <p class="dek">What's moving right now. "Everything" ranks stories across all communities by signal; the other tabs show what's on top inside one community. Refreshed with every collection, no Claude involved.</p>
      </div>
      <div class="chips" role="group" aria-label="Community">
        ${views.map(([k, label]) => `<button type="button" data-view="${k}" aria-pressed="${k === wireView}">${esc(label)}</button>`).join("")}
      </div>
      <div class="search">
        <label for="wire-search">Search</label>
        <input id="wire-search" type="search" value="${esc(wireQuery)}" placeholder="Saw something elsewhere? Check if it's here" autocomplete="off">
      </div>
      <div id="wire-list"></div>`;
    page.querySelectorAll("[data-view]").forEach((b) => b.addEventListener("click", () => { wireView = b.dataset.view; wireFilter = "all"; renderWire(); }));
    const input = $("#wire-search");
    input.addEventListener("input", () => { wireQuery = input.value; renderWireList(); });
    renderWireList();
  }

  function wireRow(w) {
    const top = (w.sources || []).find((s) => safeUrl(s.url));
    return `<li><span class="age">${ageLabel(w.age_hours)}</span>
      <div class="item">
        ${top ? `<a class="headline" href="${esc(top.url)}" target="_blank" rel="noopener">${esc(w.title)}</a>` : `<span>${esc(w.title)}</span>`}
        <span class="stat"><span>${esc(TYPE[w.type] || "")}</span><span><span class="n">${(w.communities || []).length}</span> communities</span><span><span class="n">${w.items}</span> items</span>${w.comments ? `<span><span class="n">${w.comments}</span> comments</span>` : ""}</span>
        ${sourcesLine((w.sources || []).slice(1))}
      </div></li>`;
  }

  function renderWireList() {
    const box = $("#wire-list");
    if (!box) return;
    if (wireView !== "all") {
      const c = (DATA.communities || {})[wireView];
      const sections = (c?.sections || []).map((s) => ({ ...s, items: s.items.filter((i) => matchesQuery(`${i.title} ${i.channel}`)) }))
        .filter((s) => s.items.length);
      box.innerHTML = `
        <p class="dek">Straight from ${esc(c.label)}: what's on top there right now, before any ranking of ours. <a href="${esc(c.home)}" target="_blank" rel="noopener">Open ${esc(c.label)} itself ↗</a> to compare.</p>
        ${sections.map((s) => `
          <section class="community">
            ${flag(esc(s.title), s.home ? `<a class="wire-text" href="${esc(s.home)}" target="_blank" rel="noopener">original ↗</a>` : "")}
            <ol class="ranked">${s.items.map((i) => `
              <li><a href="${esc(i.url)}" target="_blank" rel="noopener">${esc(i.title)}</a>
                <span class="stat">${i.meta ? `<span>${esc(i.meta)}</span>` : ""}${safeUrl(i.original) ? `<span><a href="${esc(i.original)}" target="_blank" rel="noopener">${esc(domainOf(i.original))}</a></span>` : ""}</span></li>`).join("")}
            </ol>
          </section>`).join("") || `<p class="empty">Nothing matches.</p>`}`;
      return;
    }
    if (wireQuery.trim()) {
      const hits = (DATA.index || []).filter((t) => matchesQuery(t.title));
      box.innerHTML = `
        <p class="dek">Searching all ${(DATA.topics_scored || 0).toLocaleString("en")} topics collected in the last two weeks. The rank is where it stands right now; the wire shows the top 40.</p>
        <ul class="wire">${hits.slice(0, 40).map((t) => `
          <li><span class="age">#${t.rank}</span><div class="item">
            ${safeUrl(t.url) ? `<a class="headline" href="${esc(t.url)}" target="_blank" rel="noopener">${esc(t.title)}</a>` : `<span>${esc(t.title)}</span>`}
            <span class="stat"><span>${t.rank <= 40 ? "On the wire" : "Collected, ranked lower"}</span><span>${esc((t.communities || []).join(", "))}</span></span>
          </div></li>`).join("") || `<li><span></span><p class="empty">Not among the ${(DATA.index || []).length} highest-ranked topics. Either it wasn't collected, or it ranked very low.</p></li>`}
        </ul>`;
      return;
    }
    const items = DATA.wire || [];
    const types = ["all", ...Object.keys(TYPE).filter((t) => items.some((w) => w.type === t))];
    const shown = items.filter((w) => wireFilter === "all" || w.type === wireFilter);
    box.innerHTML = `
      <div class="chips subtle" role="group" aria-label="Filter by kind">
        ${types.map((t) => `<button type="button" data-t="${t}" aria-pressed="${t === wireFilter}">${t === "all" ? "All kinds" : esc(TYPE[t])}</button>`).join("")}
      </div>
      <ul class="wire">${shown.map(wireRow).join("") || `<li><span></span><p class="empty">Nothing of this kind right now.</p></li>`}</ul>`;
    box.querySelectorAll("[data-t]").forEach((b) => b.addEventListener("click", () => { wireFilter = b.dataset.t; renderWireList(); }));
  }

  // ---- back issues -----------------------------------------------------------------------------
  function renderArchive() {
    const eds = DATA.editions || [];
    page.innerHTML = `
      <div class="page-head"><h2>Back issues</h2>
        <p class="dek">Every edition you've asked for, most recent first.</p></div>
      ${eds.length ? `<ul class="issues">${eds.map((e) => {
        const lead = e.fallback ? "Signals-only edition" : (e.top_stories?.[0]?.headline || "Edition");
        return `<li><a href="#/edition/${esc(e.day)}"><span class="no">No. ${e.number}</span><span class="lead-line">${esc(lead)}</span><span class="when">${esc(shortDate(e.day))}${e.top_stories ? ` · ${plural(e.top_stories.length, "story")}` : ""}</span></a></li>`;
      }).join("")}</ul>` : `<p class="empty">No editions yet.</p>`}`;
  }

  // ---- colophon -----------------------------------------------------------------------------------
  function renderColophon() {
    const c = DATA.collection || {};
    const locked = !window.__WC_DATA__ && store.get(KEY_STORE);
    $("#colophon").innerHTML = `
      <span>Last collected ${esc(ago(c.last_run) || "—")}${c.sources ? ` · ${c.sources} sources` : ""}</span>
      ${locked ? `<button type="button" class="linkish" id="forget">Lock this device</button>` : ""}`;
    $("#forget")?.addEventListener("click", () => { store.del(KEY_STORE); location.reload(); });
  }

  load();
})();
