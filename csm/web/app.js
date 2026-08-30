"use strict";

/* ------------------------------------------------------------------ bridge client */
const csm = (() => {
  let seq = 0;
  const pending = new Map();
  const listeners = new Map();

  window.__csm = {
    _reply(env) {
      const p = pending.get(env.id);
      if (!p) return;
      pending.delete(env.id);
      clearTimeout(p.timer);
      env.ok ? p.resolve(env.result) : p.reject(new Error(env.error));
    },
    _event(env) {
      (listeners.get(env.event) || []).forEach((fn) => {
        try { fn(env.data); } catch (e) { console.error(e); }
      });
    },
  };

  function call(method, params = {}) {
    return new Promise((resolve, reject) => {
      const id = ++seq;
      const timer = setTimeout(() => {
        pending.delete(id);
        reject(new Error(`${method}: timed out`));
      }, 30000);
      pending.set(id, { resolve, reject, timer });
      window.webkit.messageHandlers.csm.postMessage({ id, method, params });
    });
  }

  function on(event, fn) {
    if (!listeners.has(event)) listeners.set(event, []);
    listeners.get(event).push(fn);
  }

  return { call, on };
})();

/* ------------------------------------------------------------------ formatting */
const fmt = {
  money(v) {
    if (!v) return "$0.00";
    if (v >= 1000) return "$" + v.toLocaleString(undefined, { maximumFractionDigits: 0 });
    return "$" + v.toFixed(2);
  },
  bytes(n) {
    if (!n) return "—";
    const u = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (i === 0 ? n.toFixed(0) : n.toFixed(1)) + u[i];
  },
  tokens(n) {
    if (!n) return "0";
    if (n >= 1e9) return (n / 1e9).toFixed(1) + "B";
    if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
    if (n >= 1e3) return (n / 1e3).toFixed(0) + "K";
    return String(n);
  },
  ago(iso) {
    if (!iso) return "—";
    const then = new Date(iso).getTime();
    if (Number.isNaN(then)) return "—";
    const s = (Date.now() - then) / 1000;
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
    if (s < 86400 * 7) return `${Math.floor(s / 86400)}d ago`;
    return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
  },
};

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}

function activateWithKeyboard(node, handler) {
  node.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      handler(event);
    }
  });
}

/* ------------------------------------------------------------------ toasts */
function toast(message, isError = false) {
  const n = el("div", "toast" + (isError ? " error" : ""), message);
  document.getElementById("toasts").appendChild(n);
  setTimeout(() => n.remove(), 3200);
}

/* ------------------------------------------------------------------ theme */
csm.on("appearance", ({ dark, accent }) => {
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  if (accent) document.documentElement.style.setProperty("--accent", accent);
});

/* ------------------------------------------------------------------ index progress */
const progress = (() => {
  const wrap = el("div", "progress");
  const bar = el("div", "bar");
  bar.style.width = "0%";
  wrap.appendChild(bar);
  document.body.appendChild(wrap);
  let hideTimer = null;

  csm.on("indexProgress", ({ done, total }) => {
    clearTimeout(hideTimer);
    wrap.style.opacity = "1";
    bar.style.width = total ? `${(done / total) * 100}%` : "0%";
  });
  csm.on("indexComplete", (stats) => {
    bar.style.width = "100%";
    hideTimer = setTimeout(() => { wrap.style.opacity = "0"; bar.style.width = "0%"; }, 400);
    if (stats && stats.indexed) refresh();
  });
  return wrap;
})();

/* ------------------------------------------------------------------ router */
const root = document.getElementById("root");
const views = {};
let current = { view: "overview", params: {} };
let navigationToken = 0;

function register(name, fn) { views[name] = fn; }
function isCurrentNavigation(token) { return token === navigationToken; }

async function navigate(view, params = {}) {
  if (view === "reindex") {
    csm.call("reindex").catch((err) => toast(err.message, true));
    toast("Rescanning sessions…");
    return;
  }
  const token = ++navigationToken;
  current = { view, params };
  const fn = views[view];
  if (!fn) return;
  try {
    await fn(root, params, token);
  } catch (err) {
    if (!isCurrentNavigation(token)) return;
    console.error(err);
    root.innerHTML = "";
    const v = el("div", "view");
    v.append(el("div", "view-title", "Something broke"),
             el("div", "view-sub", String(err.message || err)));
    root.appendChild(v);
  }
}

function refresh() { navigate(current.view, current.params); }

csm.on("navigate", ({ view, params }) => navigate(view, params));
csm.on("liveSessions", () => {
  if (current.view === "project" || current.view === "overview") refresh();
});

/* ------------------------------------------------------------------ session list */
function sessionRow(s, onOpen) {
  const row = el("div", "session-row");
  row.setAttribute("role", "button");
  row.setAttribute("tabindex", "0");
  row.setAttribute("aria-label", `Open ${s.title || "Untitled session"}`);
  row.append(el("div", "dot" + (s.live ? " live" : "")));

  const title = el("div", "s-title");
  title.append(el("div", null, s.title || "Untitled session"));
  const sub = el("div", "sub");
  const bits = [`${s.humanMsgs} msg${s.humanMsgs === 1 ? "" : "s"}`];
  if (s.branch && s.branch !== "HEAD") bits.push(s.branch);
  if (s.malformed) bits.push(`${s.malformed} bad lines`);
  if (s.live && s.liveStatus) bits.push(s.liveStatus);
  sub.textContent = bits.join(" · ");
  title.append(sub);
  row.append(title);

  const chips = el("div", "s-chips");
  s.models.slice(0, 2).forEach((m) => chips.append(el("span", "chip", m.name)));
  row.append(chips);

  row.append(el("div", "s-meta", fmt.bytes(s.totalBytes)));
  row.append(el("div", "s-meta", fmt.ago(s.lastTs)));
  row.append(el("div", "s-cost" + (s.cost ? "" : " zero"), fmt.money(s.cost)));

  row.addEventListener("click", () => onOpen && onOpen(s));
  activateWithKeyboard(row, () => onOpen && onOpen(s));
  return row;
}

register("project", async (host, params, token) => {
  const sort = params.sort || "recent";
  const data = await csm.call("getSessions", { cwd: params.cwd || null, sort });
  if (!isCurrentNavigation(token)) return;

  host.innerHTML = "";
  const v = el("div", "view");

  const head = el("div", "header-row");
  const left = el("div");
  left.append(el("div", "view-title", data.name || params.name || "Sessions"));
  const p = el("div", "path" + (data.cwd && !data.exists ? " gone" : ""));
  p.textContent = data.cwd ? (data.exists ? data.cwd : data.cwd + "  (deleted)") : "";
  left.append(p);
  head.append(left);

  const sum = el("div", "summary");
  const add = (k, val) => {
    const i = el("div", "summary-item");
    i.append(el("div", "k", k), el("div", "v", val));
    sum.append(i);
  };
  add("Sessions", String(data.totals.count));
  add("Disk", fmt.bytes(data.totals.bytes));
  add("Cost", fmt.money(data.totals.cost));
  head.append(sum);
  v.append(head);

  const controls = el("div", "controls");
  const seg = el("div", "seg");
  [["recent", "Recent"], ["cost", "Cost"], ["size", "Size"], ["messages", "Messages"]]
    .forEach(([key, label]) => {
      const b = el("button", key === sort ? "on" : null, label);
      b.onclick = () => navigate("project", { ...params, sort: key });
      seg.append(b);
    });
  controls.append(seg, el("div", "spacer"),
                  el("div", "hint", "Cost is an API-equivalent estimate"));
  v.append(controls);

  if (!data.sessions.length) {
    const e = el("div", "empty");
    e.append(el("div", "big", "No sessions here"));
    v.append(e);
  } else {
    const list = el("div", "session-list");
    data.sessions.forEach((s) => list.append(sessionRow(s, (x) =>
      navigate("session", { id: x.id }))));
    v.append(list);
  }

  host.append(v);
});

/* ------------------------------------------------------------------ transcript */
const PAGE = 100;
const MAX_MOUNTED = 800;   // beyond this, drop the far end and leave a measured spacer

/* Harness plumbing the CLI injects into the user turn — slash-command echoes, local
   command output, system reminders. It's part of the transcript, but it isn't something
   you wrote, and it clusters at the very start of a session (where the view now opens).
   Collapse it to a one-line chip instead of a full bubble. */
const NOISE_RE = /^\s*(<(local-command|command-name|command-message|command-args|bash-input|bash-stdout|bash-stderr|system-reminder)\b|Caveat: The messages below)/;

function noiseChip(text) {
  const d = el("details", "tool noise");
  const s = el("summary");
  const label = text.match(/<command-name>\s*([^<]+)/);
  s.append(el("span", "tool-name", label ? label[1].trim() : "command"),
           el("span", "tool-peek", text.replace(/\s+/g, " ").slice(0, 110)));
  d.append(s, el("pre", null, text));
  return d;
}

function renderBlocks(m) {
  const frag = document.createDocumentFragment();

  for (const b of m.blocks) {
    if (b.type === "text") {
      if (m.role === "user" && NOISE_RE.test(b.text)) {
        frag.append(noiseChip(b.text));
        continue;
      }
      const bub = el("div", "bubble");
      if (m.role === "assistant") {
        // Claude emits markdown; your prompts are echoed verbatim by the CLI, so only
        // the assistant side is parsed. MD.render builds DOM nodes — never innerHTML.
        bub.classList.add("md");
        bub.append(MD.render(b.text));
      } else {
        bub.textContent = b.text;
      }
      if (b.clipped) bub.append(el("div", "clipped-note", "… truncated"));
      frag.append(bub);
    } else if (b.type === "thinking") {
      const d = el("details", "thinking");
      const s = el("summary");
      s.append(el("span", "tool-name", "Thinking"),
               el("span", "peek", " " + b.text.slice(0, 90).replace(/\s+/g, " ")));
      d.append(s, el("pre", null, b.text));
      frag.append(d);
    } else if (b.type === "tool_use") {
      const d = el("details", "tool");
      const s = el("summary");
      s.append(el("span", "tool-name", b.name),
               el("span", "tool-peek", b.input.replace(/\s+/g, " ").slice(0, 110)));
      d.append(s, el("pre", null, b.input));
      frag.append(d);
    } else if (b.type === "tool_result") {
      const d = el("details", "tool" + (b.isError ? " err" : ""));
      const s = el("summary");
      s.append(el("span", "tool-name", b.isError ? "Error" : "Result"),
               el("span", "tool-peek", (b.text || "").replace(/\s+/g, " ").slice(0, 110)));
      d.append(s, el("pre", null, b.text || "(empty)"));
      if (b.clipped) d.append(el("div", "clipped-note", "… truncated"));
      frag.append(d);
    } else if (b.type === "image") {
      frag.append(el("div", "bubble", "🖼 image"));
    } else {
      frag.append(el("div", "bubble", `[${b.type}]`));
    }
  }
  return frag;
}

function renderMessage(m) {
  const cls = m.isCompactSummary ? "msg msg-compact"
            : m.role === "user" ? "msg msg-user" : "msg msg-assistant";
  const row = el("div", cls);
  row.dataset.idx = m.idx;

  const g = el("div", "gutter");
  g.append(el("div", null, "#" + m.idx));
  if (m.ts) g.append(el("div", null, new Date(m.ts).toLocaleTimeString(
    undefined, { hour: "2-digit", minute: "2-digit" })));
  row.append(g);

  const body = el("div", "body");
  if (m.isCompactSummary) body.append(el("div", "bubble", "Context compacted here"));
  else body.append(renderBlocks(m));
  row.append(body);
  return row;
}

/* A window of pages around the viewport. Pages load on demand via IntersectionObserver;
   when too much is mounted we unmount the far end and substitute a spacer of the exact
   measured height so the scrollbar never jumps. */
function makeTranscript(sessionId, total, jumpTo, token) {
  const wrap = el("div", "transcript");
  const topSentinel = el("div", "sentinel");
  const botSentinel = el("div", "sentinel");
  const topSpacer = el("div", "spacer");
  const botSpacer = el("div", "spacer");
  wrap.append(topSpacer, topSentinel, botSentinel, botSpacer);

  let lo = null, hi = null;      // mounted msg_idx range [lo, hi)
  let busy = false;

  const mountedNodes = () => wrap.querySelectorAll(".msg");

  async function loadPage(start, where) {
    if (busy) return;
    busy = true;
    try {
      const p = await csm.call("getTranscript", { id: sessionId, start, limit: PAGE });
      if (!isCurrentNavigation(token)) return;
      if (!p.messages.length) return;
      const frag = document.createDocumentFragment();
      p.messages.forEach((m) => frag.append(renderMessage(m)));

      if (where === "up") {
        const before = root.scrollHeight, top = root.scrollTop;
        wrap.insertBefore(frag, topSentinel.nextSibling);
        root.scrollTop = top + (root.scrollHeight - before);   // hold the viewport still
        lo = p.start;
      } else {
        wrap.insertBefore(frag, botSentinel);
        hi = p.start + p.count;
      }
      if (lo === null) lo = p.start;
      if (hi === null) hi = p.start + p.count;
      trim(where);
    } catch (e) {
      if (isCurrentNavigation(token)) toast(e.message, true);
    } finally {
      busy = false;
    }
  }

  function trim(where) {
    const nodes = mountedNodes();
    if (nodes.length <= MAX_MOUNTED) return;
    const drop = nodes.length - MAX_MOUNTED;
    if (where === "up") {
      // keep the top (just loaded), drop from the bottom
      let h = 0;
      for (let i = nodes.length - drop; i < nodes.length; i++) h += nodes[i].offsetHeight;
      for (let i = nodes.length - drop; i < nodes.length; i++) nodes[i].remove();
      botSpacer.style.height = (parseFloat(botSpacer.style.height || 0) + h) + "px";
      hi -= drop;
    } else {
      let h = 0;
      for (let i = 0; i < drop; i++) h += nodes[i].offsetHeight;
      const before = root.scrollTop;
      for (let i = 0; i < drop; i++) nodes[i].remove();
      topSpacer.style.height = (parseFloat(topSpacer.style.height || 0) + h) + "px";
      root.scrollTop = before;
      lo += drop;
    }
  }

  const io = new IntersectionObserver((entries) => {
    for (const e of entries) {
      if (!e.isIntersecting || busy) continue;
      if (e.target === topSentinel && lo > 0) loadPage(Math.max(0, lo - PAGE), "up");
      else if (e.target === botSentinel && hi < total) loadPage(hi, "down");
    }
  }, { root, rootMargin: "600px" });
  io.observe(topSentinel);
  io.observe(botSentinel);

  // Open at the START of the session, and never auto-scroll: the header, cost cards and
  // metadata live above the transcript, and jumping to the newest message hid all of it.
  // (Search hits are the one exception — those scroll to the matched message.)
  const first = jumpTo != null ? Math.max(0, jumpTo - 30) : 0;
  loadPage(first, "down").then(() => {
    if (jumpTo != null) {
      const t = wrap.querySelector(`.msg[data-idx="${jumpTo}"]`);
      if (t) { t.scrollIntoView({ block: "center" }); t.classList.add("flash"); }
    }
  });

  return wrap;
}

/** Load the final page and scroll to it — the opt-in version of the old auto-scroll. */
async function jumpToLatest(sessionId, total, host, token) {
  const start = Math.max(0, total - PAGE);
  const p = await csm.call("getTranscript", { id: sessionId, start, limit: PAGE });
  if (!isCurrentNavigation(token)) return;
  const wrap = host.querySelector(".transcript");
  if (!wrap) return;
  wrap.querySelectorAll(".msg").forEach((n) => n.remove());
  wrap.querySelectorAll(".spacer").forEach((s) => (s.style.height = "0px"));
  const frag = document.createDocumentFragment();
  p.messages.forEach((m) => frag.append(renderMessage(m)));
  wrap.insertBefore(frag, wrap.querySelector(".sentinel").nextSibling);
  requestAnimationFrame(() => {
    if (isCurrentNavigation(token)) root.scrollTop = root.scrollHeight;
  });
}

/* ------------------------------------------------------------------ session view */
register("session", async (host, params, token) => {
  const s = await csm.call("getSession", { id: params.id });
  if (!isCurrentNavigation(token)) return;

  host.innerHTML = "";
  const v = el("div", "view");

  const back = el("button", "back", "‹ Back");
  back.onclick = () => navigate("project", { cwd: s.cwd, name: s.project });
  v.append(back);

  const head = el("div", "header-row");
  const left = el("div");
  const t = el("div", "view-title", s.title || "Untitled session");
  left.append(t);
  const p = el("div", "path" + (s.cwdExists ? "" : " gone"));
  p.textContent = (s.cwd || "") + (s.cwdExists ? "" : "  (deleted)");
  left.append(p);
  head.append(left);
  v.append(head);

  const actions = el("div", "btn-row");
  const resumeBtn = el("button", "btn primary", "Resume in Terminal");
  resumeBtn.onclick = async () => {
    resumeBtn.disabled = true;
    try {
      const r = await csm.call("resume", { id: s.id });
      toast(r.cwdMissing ? `Original folder is gone — opened in ${r.cwd}` : "Terminal opened");
    } catch (e) { toast(e.message, true); }
    resumeBtn.disabled = false;
  };
  const copyBtn = el("button", "btn", "Copy command");
  copyBtn.onclick = async () => {
    try { await csm.call("copyResumeCommand", { id: s.id }); toast("Copied"); }
    catch (e) { toast(e.message, true); }
  };
  const revealBtn = el("button", "btn", "Reveal file");
  revealBtn.onclick = async () => {
    try { await csm.call("revealInFinder", { id: s.id }); }
    catch (e) { toast(e.message, true); }
  };
  actions.append(resumeBtn, copyBtn, revealBtn);
  v.append(actions);

  const cards = el("div", "cards");
  const card = (k, val, note) => {
    const c = el("div", "card");
    c.append(el("div", "k", k), el("div", "v", val));
    if (note) c.append(el("div", "note", note));
    return c;
  };
  cards.append(card("Cost", fmt.money(s.cost), "API-equivalent estimate"));
  cards.append(card("Messages", s.messageCount.toLocaleString(),
                    `${s.humanMsgs.toLocaleString()} prompts · ` +
                    `${s.assistantMsgs.toLocaleString()} replies · ` +
                    `${s.toolMsgs.toLocaleString()} tool results`));
  cards.append(card("Tokens", fmt.tokens(s.inTok + s.outTok),
                    `${fmt.tokens(s.cacheRTok)} read from cache`));
  cards.append(card("Disk", fmt.bytes(s.totalBytes),
                    `${fmt.bytes(s.fileBytes)} transcript`));
  v.append(cards);

  const meta = el("div", "card");
  const kv = el("dl", "kv");
  const addKv = (k, val, mono) => {
    kv.append(el("dt", null, k));
    kv.append(el("dd", mono ? "mono" : null, val));
  };
  addKv("Session", s.id, true);
  if (s.branch) addKv("Branch", s.branch);
  addKv("Started", s.firstTs ? new Date(s.firstTs).toLocaleString() : "—");
  addKv("Last active", s.lastTs ? new Date(s.lastTs).toLocaleString() : "—");
  if (s.cliVersion) addKv("Claude Code", "v" + s.cliVersion);
  if (s.live) addKv("Status", `running (${s.liveStatus || "active"})`);
  if (s.byModel.length) {
    addKv("Models", s.byModel.map((m) => `${m.name} · ${fmt.money(m.cost)}`).join("   "));
  }
  if (s.malformed) addKv("Malformed", `${s.malformed} lines skipped`);
  meta.append(kv);
  v.append(meta);

  // Transcript header: says where you are, and offers the jump the old auto-scroll
  // used to force on you.
  const thead = el("div", "chart-head");
  thead.append(el("div", "chart-title", "Transcript"));
  const tnote = el("div", "chart-note");
  if (params.jumpTo != null) {
    tnote.textContent = `${s.messageCount.toLocaleString()} messages · jumped to a search hit`;
  } else {
    tnote.textContent = `${s.messageCount.toLocaleString()} messages · from the start`;
  }
  thead.append(tnote);
  const latest = el("button", "btn", "Jump to latest ↓");
  latest.onclick = () => jumpToLatest(s.id, s.messageCount, host, token);
  thead.append(latest);
  v.append(thead);

  host.append(v);
  v.append(makeTranscript(s.id, s.messageCount, params.jumpTo, token));

  // Navigating from a scrolled list would otherwise inherit that scroll offset and
  // land mid-transcript with the header off-screen.
  if (params.jumpTo == null) root.scrollTop = 0;
});

/* ------------------------------------------------------------------ placeholders */
function soon(title, sub) {
  return async (host, params) => {
    host.innerHTML = "";
    const v = el("div", "view");
    v.append(el("div", "view-title", title), el("div", "view-sub", sub));
    const e = el("div", "empty");
    e.append(el("div", "big", "Coming in the next phase"));
    v.append(e);
    host.append(v);
  };
}

register("overview", async (host, params, token) => {
  const o = await csm.call("getOverview");
  if (!isCurrentNavigation(token)) return;
  const t = o.totals;

  host.innerHTML = "";
  const v = el("div", "view");
  v.append(el("div", "view-title", "Overview"));
  const sub = el("div", "view-sub");
  sub.append(document.createTextNode(
    `${o.totals.sessions} sessions · dollar figures are API-equivalent list prices, not `));
  const planLink = el("button", "inline-link", "your subscription usage");
  planLink.type = "button";
  planLink.onclick = () => { navigate("plan", {}); };
  sub.append(planLink);
  v.append(sub);

  const cards = el("div", "cards");
  const card = (k, val, note) => {
    const c = el("div", "card");
    c.append(el("div", "k", k), el("div", "v", val));
    if (note) c.append(el("div", "note", note));
    cards.append(c);
  };
  card("API-equivalent value", fmt.money(t.cost), "if billed pay-as-you-go");
  card("Sessions", String(t.sessions), `${o.projectCount} projects`);
  card("Tokens", fmt.tokens(t.inTok + t.outTok + t.cacheWTok + t.cacheRTok),
       `${fmt.tokens(t.cacheRTok)} read from cache`);
  card("Disk", fmt.bytes(t.bytes),
       `${fmt.bytes(o.reclaimable.bytes)} in ${o.reclaimable.count} throwaway sessions`);
  v.append(cards);

  const block = (title, note) => {
    const b = el("div", "chart-block");
    const h = el("div", "chart-head");
    h.append(el("div", "chart-title", title));
    if (note) h.append(el("div", "chart-note", note));
    b.append(h);
    v.append(b);
    return b;
  };

  // --- daily spend, stacked by model
  const b1 = block(`Spend per day`, `last ${o.days} days · ${o.costCaption}`);
  const plot1 = el("div");
  b1.append(plot1);
  if (o.models.length > 1) b1.append(Charts.legend(o.models));

  // --- spend per project
  const b2 = block("Spend by project", "top 10");
  const plot2 = el("div");
  b2.append(plot2);

  // --- where the money goes
  const b3 = block("Where the money goes", "cost by token type");
  const plot3 = el("div");
  b3.append(plot3);
  const typeSeries = [
    { name: "Cache read", value: o.costByType.cacheRead, slot: 1 },
    { name: "Cache write", value: o.costByType.cacheWrite, slot: 2 },
    { name: "Output", value: o.costByType.output, slot: 3 },
    { name: "Input", value: o.costByType.input, slot: 4 },
  ].filter((s) => s.value > 0);
  b3.append(Charts.legend(typeSeries));
  const saved = el("div", "callout");
  saved.innerHTML = `Prompt caching saved about <b>${fmt.money(o.cacheSavings)}</b> — ` +
    `${fmt.tokens(t.cacheRTok)} of cache reads billed at a tenth of the input price.`;
  b3.append(saved);

  host.append(v);

  // Draw immediately (works even when requestAnimationFrame is paused — an off-screen
  // WKWebView, e.g. during a snapshot, never ticks rAF), then redraw once laid out so
  // clientWidth is exact. The chart fns replaceChildren, so the second call is idempotent.
  const drawCharts = () => {
    if (!isCurrentNavigation(token)) return;
    if (o.daily.length) Charts.dailyStacked(plot1, o.daily, o.models);
    else plot1.replaceChildren(el("div", "empty", "No spend recorded yet"));
    Charts.hBars(plot2, o.byProject.map((p) => ({
      name: p.name, value: p.cost, note: `${p.count} session${p.count === 1 ? "" : "s"}`,
    })), { slot: 1 });
    Charts.composition(plot3, typeSeries);
  };
  drawCharts();
  requestAnimationFrame(drawCharts);
});
/* ------------------------------------------------------------------ search */
/* Snippets arrive with \x02/\x03 around matches rather than <mark> tags, so the
   highlight is built from DOM nodes — transcript text can never inject markup. */
function snippetNode(s) {
  const box = el("div", "snip");
  const parts = String(s || "").split(/[\x02\x03]/);
  parts.forEach((chunk, i) => {
    if (!chunk) return;
    box.append(i % 2 ? el("mark", null, chunk) : document.createTextNode(chunk));
  });
  return box;
}

register("search", async (host, params, token) => {
  const q = (params.q || "").trim();
  host.innerHTML = "";
  const v = el("div", "view");
  v.append(el("div", "view-title", "Search"));

  if (!q) {
    v.append(el("div", "view-sub", "Type in the toolbar to search every transcript."));
    host.append(v);
    return;
  }

  const r = await csm.call("search", { q });
  if (!isCurrentNavigation(token)) return;
  v.append(el("div", "view-sub",
    r.total ? `${r.total}${r.truncated ? "+" : ""} matches in ${r.sessions.length} ` +
              `session${r.sessions.length === 1 ? "" : "s"} for “${q}”`
            : `No matches for “${q}”`));

  r.sessions.forEach((g) => {
    const block = el("div", "hit-group");
    const h = el("div", "hit-head");
    h.setAttribute("role", "button");
    h.setAttribute("tabindex", "0");
    h.setAttribute("aria-label", `Open ${g.title || "Untitled session"}`);
    h.append(el("span", "hit-title", g.title || "Untitled session"),
             el("span", "hit-proj", g.project));
    const openGroup = () => navigate("session", { id: g.id });
    h.onclick = openGroup;
    activateWithKeyboard(h, openGroup);
    block.append(h);

    g.hits.slice(0, 6).forEach((hit) => {
      const row = el("div", "hit");
      row.append(el("span", "hit-role", hit.role === "user" ? "you"
                                     : hit.role === "title" ? "title" : "claude"));
      row.append(snippetNode(hit.snippet));
      row.setAttribute("role", "button");
      row.setAttribute("tabindex", "0");
      row.setAttribute("aria-label", `Open matching message in ${g.title || "session"}`);
      // Jump straight to the matching message inside the transcript.
      const openHit = () => navigate("session", { id: g.id, jumpTo: hit.msgIdx });
      row.onclick = openHit;
      activateWithKeyboard(row, openHit);
      block.append(row);
    });
    if (g.hits.length > 6) {
      block.append(el("div", "hit-more", `+${g.hits.length - 6} more in this session`));
    }
    v.append(block);
  });

  host.append(v);
});

/* ------------------------------------------------------------------ plan usage */
const SEVERITY_COLOR = {
  // Pill meters get gradient fills (the glass HUD look); green for healthy,
  // amber as a window fills, red-orange when it is effectively spent. Used as
  // plain CSS background values — never parsed or mixed.
  critical: "linear-gradient(90deg, #ff6b35, #ff3b30)",
  warning: "linear-gradient(90deg, #ffd426, #ff9f0a)",
  normal: "linear-gradient(90deg, #30d158, #2ec06a)",
};

function meter(label, percent, severity, sub) {
  const row = el("div", "meter");
  const head = el("div", "meter-head");
  head.append(el("span", "meter-label", label));
  head.append(el("span", "meter-pct", `${percent}%`));
  row.append(head);
  const track = el("div", "meter-track");
  const fill = el("div", "meter-fill");
  const pct = Math.min(100, Math.max(0, percent));
  fill.style.width = pct + "%";
  fill.style.background = SEVERITY_COLOR[severity] || SEVERITY_COLOR.normal;
  // Ramp across the whole TRACK, not the fill. background-size resolves against the
  // fill's own box, so scaling it by track/fill makes a given percentage always the
  // same hue; without this a short bar squeezes the entire ramp into a few pixels and
  // bar length silently drives colour. (Set after `background`, which resets it.)
  fill.style.backgroundSize = pct > 0 ? `${10000 / pct}% 100%` : "100% 100%";
  track.append(fill);
  row.append(track);
  if (sub) row.append(el("div", "meter-sub", sub));
  return row;
}

function relTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const diff = d.getTime() - Date.now();
  const abs = Math.abs(diff);
  const unit = abs < 3600e3 ? `${Math.round(abs / 60e3)}m`
             : abs < 86400e3 ? `${Math.round(abs / 3600e3)}h`
             : `${Math.round(abs / 86400e3)}d`;
  return diff < 0 ? `${unit} ago` : `in ${unit}`;
}

register("plan", async (host, params, token) => {
  const p = await csm.call("getPlan");
  if (!isCurrentNavigation(token)) return;
  host.innerHTML = "";
  const v = el("div", "view");
  v.append(el("div", "view-title", "Plan usage"));

  const st = p.status;
  if (!st.available) {
    v.append(el("div", "view-sub",
      "Claude Code hasn't recorded any plan-usage data yet. Open a session in the CLI."));
    host.append(v);
    return;
  }

  // Source is load-bearing honesty: live = fetched now; cache = a snapshot that lags.
  const sub = el("div", "view-sub");
  if (st.source === "live") {
    const dot = el("span", "live-dot");
    sub.append(dot, document.createTextNode(
      "Live from Anthropic — refreshes every 30 seconds."));
  } else {
    const age = st.ageHours;
    const ageStr = age == null ? "unknown age"
      : age < 1.5 ? "just now"
      : age < 48 ? `${Math.round(age)}h ago`
      : `${Math.round(age / 24)}d ago`;
    sub.textContent = `Cached snapshot, last recorded ${ageStr}` +
      (st.stale ? " — open Claude Code to refresh." : ".");
  }
  v.append(sub);

  // When we fall back to cache, show why the live fetch didn't take — makes a wrong
  // endpoint / expired token diagnosable instead of a silent "still cached".
  const ls = p.liveStatus;
  if (st.source !== "live" && ls) {
    const why = !ls.hasToken ? "no Keychain token (allow access when prompted)"
      : ls.error ? `live fetch: ${ls.error}`
      : ls.haveData ? "live data pending…" : "connecting to live usage…";
    v.append(el("div", "live-note", why));
  }

  // ---- the binding constraint, front and centre
  const b = st.binding;
  if (b) {
    const card = el("div", "constraint " + b.severity);
    const scope = b.scopeModel ? ` for ${b.scopeModel}` : "";
    const kind = b.kind === "session" ? "5-hour session"
               : b.kind.startsWith("weekly") ? "weekly" : b.kind;
    if (b.percent >= 100) {
      card.append(el("div", "constraint-title",
        `Your ${kind} limit${scope} is maxed out`));
      card.append(el("div", "constraint-body",
        `You've used 100% of this window. It resets ${relTime(b.resetsAt)}. ` +
        (b.scopeModel ? `Switch models or wait — other models still have room.`
                      : `New requests may be throttled until then.`)));
    } else {
      card.append(el("div", "constraint-title",
        `${b.percent}% of your ${kind} limit${scope} used`));
      card.append(el("div", "constraint-body",
        `Resets ${relTime(b.resetsAt)}.`));
    }
    v.append(card);
  }

  // ---- all limit meters
  const meters = el("div", "meters");
  st.limits.forEach((l) => {
    const scope = l.scopeModel ? ` · ${l.scopeModel}` : "";
    const name = (l.kind === "session" ? "5-hour session"
               : l.kind === "weekly_all" ? "Weekly (all models)"
               : l.kind === "weekly_scoped" ? "Weekly (scoped)"
               : l.kind) + scope;
    if (l.reset) {
      // Window already rolled over since the snapshot — its old % is meaningless.
      meters.append(meter(name, 0, "normal", "window reset — usage cleared"));
    } else {
      meters.append(meter(name, l.percent, l.severity, `resets ${relTime(l.resetsAt)}`));
    }
  });
  v.append(meters);

  // ---- honesty note: these are percentages, dollars are not exposed
  const note = el("div", "callout");
  note.innerHTML =
    "These are percentages of your plan's rolling windows — Anthropic doesn't expose " +
    "the dollar or token size of a window, so there's no “dollars of plan” figure. " +
    "The dollar amounts elsewhere in this app are <b>API-equivalent list prices</b> " +
    "(what the same usage would cost pay-as-you-go), not what your subscription charges.";
  v.append(note);

  // ---- what's driving the window
  const drv = el("div", "chart-head");
  drv.append(el("div", "chart-title", `What's driving your last ${p.windowDays} days`));
  drv.append(el("div", "chart-note", "share of usage, weighted like billing"));
  v.append(drv);
  const plot = el("div", "chart-block");
  v.append(plot);
  if (p.models.length > 1) v.append(Charts.legend(p.models));

  const list = el("div", "session-list");
  p.sessions.forEach((s) => {
    const row = el("div", "session-row plan-row");
    row.setAttribute("role", "button");
    row.setAttribute("tabindex", "0");
    row.setAttribute("aria-label", `Open ${s.title || "Untitled session"}`);
    row.append(el("div", "dot" + (s.live ? " live" : "")));
    const title = el("div", "s-title");
    title.append(el("div", null, s.title || "Untitled session"));
    title.append(el("div", "sub", s.project));
    row.append(title);
    // a mini share bar
    const barWrap = el("div", "share-bar");
    const bar = el("div", "share-fill");
    bar.style.width = Math.max(2, s.share * 100) + "%";
    barWrap.append(bar);
    row.append(barWrap);
    row.append(el("div", "s-meta", `${(s.share * 100).toFixed(1)}%`));
    row.append(el("div", "s-meta", fmt.tokens(s.tokens)));
    const open = () => navigate("session", { id: s.id });
    row.addEventListener("click", open);
    activateWithKeyboard(row, open);
    list.append(row);
  });
  v.append(list);
  host.append(v);

  // Model split is a composition (2–6 slices of one whole), not a bar chart.
  // Draw immediately then redraw on layout (see the overview note on rAF/snapshots).
  const drawPlan = () => {
    if (!isCurrentNavigation(token)) return;
    Charts.composition(plot, p.models.map((m) => ({
      name: m.name, value: m.share, slot: m.slot,
    })), { valueLabel: "Share" });
  };
  drawPlan();
  requestAnimationFrame(drawPlan);
});

/* ------------------------------------------------------------------ cleanup */
const selected = new Set();

register("cleanup", async (host, params, token) => {
  const sort = params.sort || "size";
  const data = await csm.call("getCleanupList", { sort });
  if (!isCurrentNavigation(token)) return;
  // Drop selections for sessions that no longer exist in the list.
  const alive = new Set(data.sessions.map((s) => s.id));
  [...selected].forEach((id) => { if (!alive.has(id)) selected.delete(id); });

  host.innerHTML = "";
  const v = el("div", "view");

  const head = el("div", "header-row");
  const left = el("div");
  left.append(el("div", "view-title", "Cleanup"));
  left.append(el("div", "view-sub",
    `${data.totals.count} sessions · ${fmt.bytes(data.totals.bytes)} on disk` +
    (data.totals.lockedCount ? ` · ${data.totals.lockedCount} running (locked)` : "")));
  head.append(left);
  v.append(head);

  const controls = el("div", "controls");
  const seg = el("div", "seg");
  [["size", "Largest"], ["age", "Oldest"], ["messages", "Fewest messages"],
   ["cost", "Costliest"]].forEach(([key, label]) => {
    const b = el("button", key === sort ? "on" : null, label);
    b.onclick = () => navigate("cleanup", { ...params, sort: key });
    seg.append(b);
  });
  controls.append(seg, el("div", "spacer"));

  const pick = el("button", "btn",
    `Select ${data.totals.throwawayCount} throwaways (${fmt.bytes(data.totals.throwawayBytes)})`);
  pick.onclick = () => {
    data.sessions.filter((s) => s.throwaway && !s.locked).forEach((s) => selected.add(s.id));
    refresh();
  };
  const none = el("button", "btn", "Clear");
  none.onclick = () => { selected.clear(); refresh(); };
  controls.append(pick, none);
  v.append(controls);

  const list = el("div", "session-list");
  data.sessions.forEach((s) => {
    const row = el("div", "session-row cleanup-row" + (s.locked ? " locked-row" : ""));

    const cb = el("input");
    cb.type = "checkbox";
    cb.checked = selected.has(s.id);
    cb.disabled = s.locked;
    cb.title = s.locked ? "This session is running right now" : "";
    cb.onchange = () => {
      cb.checked ? selected.add(s.id) : selected.delete(s.id);
      row.classList.toggle("selected", cb.checked);
      updateFooter();
    };
    row.append(cb);

    const title = el("div", "s-title");
    title.append(el("div", null, s.title || "Untitled session"));
    const bits = [s.project, `${s.humanMsgs} msg${s.humanMsgs === 1 ? "" : "s"}`];
    if (s.locked) bits.push("running");
    title.append(el("div", "sub", bits.join(" · ")));
    row.append(title);

    const tags = el("div", "s-chips");
    if (s.locked) tags.append(el("span", "chip locked", "locked"));
    else if (s.throwaway) tags.append(el("span", "chip warn", "throwaway"));
    row.append(tags);

    row.append(el("div", "s-meta", fmt.bytes(s.totalBytes)));
    row.append(el("div", "s-meta", fmt.ago(s.lastTs)));
    row.append(el("div", "s-cost" + (s.cost ? "" : " zero"), fmt.money(s.cost)));
    row.addEventListener("click", (event) => {
      if (s.locked || event.target.closest("input, button, a")) return;
      cb.checked = !cb.checked;
      cb.dispatchEvent(new Event("change", { bubbles: false }));
    });
    row.classList.toggle("selected", cb.checked);
    list.append(row);
  });
  v.append(list);
  host.append(v);

  // sticky action bar
  const footer = el("div", "footer");
  const label = el("div", "footer-label");
  const go = el("button", "btn danger", "Move to Trash");
  footer.append(label, el("div", "spacer"), go);
  host.append(footer);

  function updateFooter() {
    const picked = data.sessions.filter((s) => selected.has(s.id));
    const bytes = picked.reduce((a, s) => a + s.totalBytes, 0);
    label.textContent = picked.length
      ? `${picked.length} session${picked.length === 1 ? "" : "s"} · ${fmt.bytes(bytes)}`
      : "Nothing selected";
    go.disabled = !picked.length;
    footer.style.display = picked.length ? "flex" : "none";
  }
  updateFooter();

  go.onclick = async () => {
    const picked = data.sessions.filter((s) => selected.has(s.id));
    const bytes = picked.reduce((a, s) => a + s.totalBytes, 0);
    if (!confirm(`Move ${picked.length} session${picked.length === 1 ? "" : "s"} ` +
                 `(${fmt.bytes(bytes)}) to the Trash?\n\n` +
                 `They stay recoverable from the Trash until you empty it.`)) return;
    go.disabled = true;
    try {
      const r = await csm.call("trashSessions", { ids: [...selected] });
      selected.clear();
      let msg = `Moved ${r.sessions} session${r.sessions === 1 ? "" : "s"} ` +
                `to the Trash — ${fmt.bytes(r.bytesFreed)} freed`;
      if (r.blocked.length) msg += ` · ${r.blocked.length} skipped (running)`;
      if (r.failed.length) msg += ` · ${r.failed.length} failed`;
      toast(msg, r.failed.length > 0);
      refresh();
    } catch (e) {
      toast(e.message, true);
      go.disabled = false;
    }
  };
});

/* ------------------------------------------------------------------ boot */
navigate("overview", {});
