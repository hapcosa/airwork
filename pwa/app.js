// airwork: PWA para manejar Claude Code desde el celular. Sin build ni dependencias.
// Todo texto que viene del servidor se inserta como texto (textContent), nunca como HTML.

const MODELS = ["", "fable", "opus", "sonnet", "haiku"];
const EFFORTS = ["", "low", "medium", "high", "xhigh", "max"];
const MODES = ["default", "acceptEdits", "plan"];
const MODE_LABEL = { default: "preguntar", acceptEdits: "aceptar ediciones", plan: "plan" };
const LIVE_KINDS = ["prompt", "init", "text", "thinking", "tool_use", "tool_result", "permission_request",
  "permission_resolved", "status", "compact", "rate_limit", "result", "error", "closed"];
const PAGE = 150;

// --- utilidades ----------------------------------------------------------------------

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : String(c));
  }
  return el;
}

const $ = (sel, root = document) => root.querySelector(sel);

function ago(ts) {
  if (!ts) return "";
  const t = typeof ts === "number" ? ts * 1000 : Date.parse(ts);
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return "recién";
  if (s < 3600) return `hace ${Math.floor(s / 60)} min`;
  if (s < 86400) return `hace ${Math.floor(s / 3600)} h`;
  if (s < 86400 * 30) return `hace ${Math.floor(s / 86400)} d`;
  return new Date(t).toLocaleDateString();
}

function clock(epoch) {
  if (!epoch) return "";
  return new Date(epoch * 1000).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" });
}

function toast(msg, ms = 3500) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.remove("show"), ms);
}

function setTop(title, subtitle = "", back = "#/") {
  $("#title").textContent = title;
  $("#subtitle").textContent = subtitle;
  const b = $("#back");
  b.hidden = !back;
  if (back) b.setAttribute("href", back);
  document.title = title === "airwork" ? "airwork" : `${title} · airwork`;
}

// Texto con bloques ``` renderizados como <pre>; el resto como párrafos de texto plano.
function richText(text) {
  const out = h("div", { class: "rich" });
  const parts = String(text || "").split(/```/);
  parts.forEach((part, i) => {
    if (i % 2 === 1) {
      const body = part.replace(/^[^\n]*\n/, "");
      out.append(h("pre", { class: "code" }, body));
    } else if (part.trim()) {
      out.append(h("div", { class: "para" }, part.replace(/^\n+|\n+$/g, "")));
    }
  });
  return out;
}

// --- API -------------------------------------------------------------------------------

class ApiError extends Error {
  constructor(status, data) {
    super((data && data.detail) || `HTTP ${status}`);
    this.status = status;
    this.data = data || {};
  }
}

async function api(path, { method = "GET", body } = {}) {
  let r;
  try {
    r = await fetch("/api" + path, {
      method, credentials: "same-origin", cache: "no-store",
      headers: body !== undefined ? { "Content-Type": "application/json" } : {},
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new ApiError(0, { detail: "Sin conexión, o la sesión de Cloudflare Access expiró: recarga la página." });
  }
  const ct = r.headers.get("content-type") || "";
  const data = ct.includes("application/json") ? await r.json() : null;
  if (!r.ok) {
    const detail = data && (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail));
    throw new ApiError(r.status, { ...(data || {}), detail: detail || `HTTP ${r.status}` });
  }
  if (data === null) throw new ApiError(0, { detail: "Respuesta inesperada (¿sesión de Access expirada? recarga)." });
  return data;
}

const cache = { pc: null, accounts: null };

async function loadAccounts(force = false) {
  if (force || !cache.accounts) cache.accounts = await api("/accounts");
  return cache.accounts;
}

function accountName(a) {
  return a.label ? `${a.label} (${a.alias})` : a.alias;
}

function errorBox(e) {
  return h("div", { class: "banner error" }, e.message || String(e));
}

// --- router -----------------------------------------------------------------------------

let cleanup = null;
let skipRoute = false;

const routes = [
  [/^#?\/?$/, viewPCs],
  [/^#\/projects$/, viewProjects],
  [/^#\/accounts$/, viewAccounts],
  [/^#\/p\/([^/]+)$/, viewSessions],
  [/^#\/p\/([^/]+)\/handoff\/([^/]+)$/, viewHandoff],
  [/^#\/s\/([^/]+)\/([^/]+)$/, viewChat],
];

async function route() {
  if (skipRoute) { skipRoute = false; return; }
  if (cleanup) { try { cleanup(); } catch { /* nada */ } cleanup = null; }
  const view = $("#view");
  view.replaceChildren();
  view.className = "";
  const hash = location.hash || "#/";
  for (const [re, fn] of routes) {
    const m = hash.match(re);
    if (m) {
      try {
        await fn(view, ...m.slice(1).map(decodeURIComponent));
      } catch (e) {
        view.append(errorBox(e));
      }
      return;
    }
  }
  view.append(h("p", {}, "Ruta desconocida. ", h("a", { href: "#/" }, "Inicio")));
}

function replaceHash(hash) {
  if (location.hash === hash) return;
  skipRoute = true;
  location.replace(hash);
}

// --- PCs --------------------------------------------------------------------------------

function knownPCs() {
  try { return JSON.parse(localStorage.getItem("airwork.pcs") || "[]"); } catch { return []; }
}

function saveKnownPCs(list) {
  try { localStorage.setItem("airwork.pcs", JSON.stringify(list)); } catch { /* sin storage */ }
}

async function viewPCs(view) {
  setTop("airwork", "PCs", null);
  const pc = cache.pc = await api("/pc");
  const others = knownPCs().filter((p) => new URL(p.url).origin !== location.origin);

  const current = h("a", { class: "card link", href: "#/projects" },
    h("div", { class: "card-title" }, pc.name, h("span", { class: "badge ok" }, "este")),
    h("div", { class: "muted" }, `Claude Code ${pc.claude_version} · agente ${pc.agent_version}`),
    h("div", { class: "muted" }, `${pc.active_runs}/${pc.max_runs} procesos activos · headroom ${pc.headroom_up ? "arriba" : "abajo"}`),
    h("div", { class: "muted" }, pc.email));

  const list = h("div", {}, others.map((p, i) => h("div", { class: "card row" },
    h("a", { class: "grow link", href: p.url }, h("div", { class: "card-title" }, p.name), h("div", { class: "muted" }, p.url)),
    h("button", { class: "ghost", onclick: () => { const all = knownPCs(); all.splice(all.findIndex((x) => x.url === p.url), 1); saveKnownPCs(all); route(); } }, "Quitar"))));

  const name = h("input", { placeholder: "Nombre (ej. oficina)", required: true, maxlength: 40 });
  const url = h("input", { placeholder: "https://pc2.tudominio.cl", required: true, type: "url" });
  const form = h("form", { class: "stack", onsubmit: (ev) => {
    ev.preventDefault();
    let u;
    try { u = new URL(url.value.trim()); } catch { return toast("URL inválida"); }
    if (u.protocol !== "https:") return toast("La URL debe ser https");
    const all = knownPCs().filter((x) => x.url !== u.origin);
    all.push({ name: name.value.trim(), url: u.origin });
    saveKnownPCs(all);
    route();
  } }, name, url, h("button", { type: "submit" }, "Agregar PC"));

  const kill = h("button", { class: "danger", onclick: async () => {
    if (!confirm("¿Cerrar todos los procesos de Claude en este PC?")) return;
    try { const r = await api("/kill", { method: "POST" }); toast(`Cerrados: ${r.closed}`); route(); } catch (e) { toast(e.message); }
  } }, "Cerrar todos los procesos");

  view.append(current, list,
    h("details", { class: "card" }, h("summary", {}, "Agregar otro PC"),
      h("p", { class: "muted" }, "Cada PC tiene su propio hostname detrás de Cloudflare Access; la lista se guarda solo en este teléfono."), form),
    h("div", { class: "actions" }, h("a", { class: "button", href: "#/accounts" }, "Cuentas"), kill));
}

// --- proyectos y sesiones ------------------------------------------------------------------

async function viewProjects(view) {
  setTop("Proyectos", cache.pc ? cache.pc.name : "", "#/");
  const rows = await api("/projects");
  if (!rows.length) view.append(h("p", { class: "muted" }, "No hay proyectos con git o historial en ~/programacion."));
  view.append(h("div", {}, rows.map((p) => h("a", { class: "card link", href: `#/p/${encodeURIComponent(p.name)}` },
    h("div", { class: "card-title" }, p.name, p.git ? null : h("span", { class: "badge" }, "sin git")),
    h("div", { class: "muted" }, `${p.sessions} conversaciones${p.last_activity ? " · " + ago(p.last_activity) : ""}`)))));
}

async function viewSessions(view, project) {
  setTop(project, "Conversaciones", "#/projects");
  const [accounts, prefs] = await Promise.all([loadAccounts(), api(`/prefs/project/${encodeURIComponent(project)}`)]);

  const accSel = h("select", { onchange: async () => {
    try {
      await api(`/prefs/project/${encodeURIComponent(project)}`, { method: "PUT",
        body: { ...prefs, account_id: accSel.value ? Number(accSel.value) : null } });
      toast("Cuenta por defecto del proyecto guardada");
    } catch (e) { toast(e.message); }
  } }, h("option", { value: "" }, "— sin cuenta por defecto —"),
  accounts.map((a) => h("option", { value: a.id, selected: prefs.account_id === a.id }, accountName(a))));

  const search = h("input", { type: "search", placeholder: "Buscar por título, prompt o rama" });
  const list = h("div");
  const more = h("button", { class: "ghost wide", hidden: true }, "Cargar más");
  let rows = [];

  const render = () => {
    const q = search.value.trim().toLowerCase();
    const shown = q ? rows.filter((r) => [r.title, r.first_prompt, r.last_prompt, r.branch, r.id]
      .some((x) => (x || "").toLowerCase().includes(q))) : rows;
    list.replaceChildren(...shown.map((r) => h("a", { class: "card link", href: `#/s/${encodeURIComponent(project)}/${r.id}` },
      h("div", { class: "card-title" }, r.title || "(sin título)",
        r.active_run ? h("span", { class: "badge ok" }, "activa") : null,
        r.worktree ? h("span", { class: "badge" }, "worktree") : null),
      h("div", { class: "muted" }, [ago(r.mtime), `${r.prompts} ${r.prompts === 1 ? "prompt" : "prompts"}`, r.branch && `rama ${r.branch}`,
        r.entrypoint === "sdk-cli" ? "desde airwork" : null].filter(Boolean).join(" · ")),
      r.continued_in ? h("div", { class: "muted" }, "continuó en otra conversación") : null)));
    if (!shown.length) list.append(h("p", { class: "muted" }, q ? "Sin coincidencias." : "Sin conversaciones."));
  };

  const load = async (before) => {
    const page = await api(`/projects/${encodeURIComponent(project)}/sessions?limit=50${before ? `&before=${before}` : ""}`);
    rows = rows.concat(page);
    more.hidden = page.length < 50;
    render();
  };
  more.addEventListener("click", () => load(rows[rows.length - 1].mtime).catch((e) => toast(e.message)));
  search.addEventListener("input", render);

  const handoffs = h("div");
  api(`/projects/${encodeURIComponent(project)}/handoffs`).then((hs) => {
    if (!hs.length) return;
    handoffs.append(h("details", { class: "card" }, h("summary", {}, `Handoffs (${hs.length})`),
      hs.slice(0, 20).map((f) => h("a", { class: "row link", href: `#/p/${encodeURIComponent(project)}/handoff/${encodeURIComponent(f.name)}` },
        h("span", { class: "grow" }, f.name), h("span", { class: "muted" }, ago(f.mtime))))));
  }).catch(() => {});

  view.append(
    h("a", { class: "button primary wide", href: `#/s/${encodeURIComponent(project)}/new` }, "+ Nueva conversación"),
    h("label", { class: "field" }, h("span", {}, "Cuenta por defecto del proyecto"), accSel),
    handoffs, search, list, more);
  await load();
}

async function viewHandoff(view, project, name) {
  setTop(name, project, `#/p/${encodeURIComponent(project)}`);
  const f = await api(`/projects/${encodeURIComponent(project)}/handoffs?name=${encodeURIComponent(name)}`);
  const startWith = (draft) => () => {
    try { sessionStorage.setItem("airwork.draft", draft); } catch { /* nada */ }
    location.hash = `#/s/${encodeURIComponent(project)}/new`;
  };
  view.append(
    h("button", { class: "primary wide", onclick: startWith(`/retomar ${f.path}`) }, "Retomar en una conversación nueva"),
    // Alternativa para PCs sin la skill /retomar: pega el handoff completo.
    h("button", { class: "wide", onclick: startWith(`Retoma el trabajo según ${f.path}:\n\n${f.content}`) },
      "Pegar el handoff como prompt"),
    h("pre", { class: "doc" }, f.content));
}

// --- cuentas --------------------------------------------------------------------------------

async function viewAccounts(view) {
  setTop("Cuentas", "Perfiles de Claude en este PC", "#/");
  const [accounts, profiles, pc] = await Promise.all([loadAccounts(true), api("/profiles"), cache.pc || api("/pc")]);
  cache.pc = pc;

  const card = (a) => {
    const warn = [];
    if (!a.profile.exists) warn.push("el perfil no existe");
    else if (!a.profile.logged_in && !a.has_token) warn.push("sin login: corre `claude auth login` con este perfil");
    if (a.profile.settings_base_url) warn.push("su settings.json fija ANTHROPIC_BASE_URL");
    const headroom = h("input", { type: "checkbox", checked: a.use_headroom, onchange: async () => {
      try { await api(`/accounts/${a.id}`, { method: "PATCH", body: { use_headroom: headroom.checked } }); toast("Guardado"); loadAccounts(true); }
      catch (e) { toast(e.message); headroom.checked = !headroom.checked; }
    } });
    return h("div", { class: "card" },
      h("div", { class: "card-title" }, accountName(a), a.has_token ? h("span", { class: "badge" }, "token") : null),
      warn.map((w) => h("div", { class: "banner warn" }, w)),
      a.last_error ? h("div", { class: "banner error" }, `Último error (${ago(a.last_error_at)}): ${a.last_error}`) : null,
      h("label", { class: "check" }, headroom, "Pasar por headroom (", pc.headroom_up ? "arriba" : "abajo", ")"),
      h("div", { class: "actions" },
        h("button", { class: "ghost", onclick: async () => {
          const label = prompt("Nombre visible", a.label || "");
          if (label === null) return;
          try { await api(`/accounts/${a.id}`, { method: "PATCH", body: { label } }); route(); } catch (e) { toast(e.message); }
        } }, "Renombrar"),
        h("button", { class: "ghost danger", onclick: async () => {
          if (!confirm(`¿Quitar la cuenta ${a.alias} de airwork? El perfil en disco no se toca.`)) return;
          try { await api(`/accounts/${a.id}`, { method: "DELETE" }); cache.accounts = null; route(); } catch (e) { toast(e.message); }
        } }, "Quitar")));
  };

  const free = profiles.filter((p) => !p.registered);
  const alias = h("select", {}, free.map((p) => h("option", { value: p.alias },
    `${p.alias}${p.logged_in ? "" : " (sin login)"}${p.settings_base_url ? " (fija BASE_URL)" : ""}`)));
  const label = h("input", { placeholder: "Nombre visible (opcional)", maxlength: 60 });
  const token = pc.allow_tokens ? h("input", { type: "password", placeholder: "Token sk-ant-oat01-… (opcional)", autocomplete: "off" }) : null;
  const add = h("form", { class: "stack", onsubmit: async (ev) => {
    ev.preventDefault();
    try {
      await api("/accounts", { method: "POST", body: { alias: alias.value, label: label.value.trim(),
        token: token && token.value.trim() ? token.value.trim() : null } });
      if (token) token.value = "";
      cache.accounts = null;
      route();
    } catch (e) { toast(e.message); }
  } }, alias, label, token, h("button", { type: "submit", disabled: !free.length }, "Agregar cuenta"));

  view.append(
    ...(accounts.length ? accounts.map(card) : [h("p", { class: "muted" }, "Todavía no hay cuentas registradas.")]),
    h("div", { class: "card" }, h("div", { class: "card-title" }, "Agregar"),
      free.length ? add : h("p", { class: "muted" }, "No hay perfiles libres en ~/.claude-accounts."),
      h("p", { class: "muted small" }, "Para una cuenta nueva, en el PC: ",
        h("code", {}, "bin/rc-profile <alias>"), " y luego ",
        h("code", {}, "CLAUDE_CONFIG_DIR=~/.claude-accounts/<alias> claude auth login"), ".")));
}

// --- chat -------------------------------------------------------------------------------------

async function viewChat(view, project, sidParam) {
  view.className = "chat";
  const isNew = sidParam === "new";
  const c = {
    project, sid: isNew ? null : sidParam, run: null, es: null, histOffset: 0,
    sel: { account_id: null, model: "", effort: "", permission_mode: "default" },
    toolEls: new Map(), permEls: new Map(), lastPrompt: "",
  };
  setTop(isNew ? "Nueva conversación" : "Conversación", project, `#/p/${encodeURIComponent(project)}`);

  // --- estructura
  const log = h("div", { class: "log" });
  const banners = h("div", { class: "banners" });
  const status = h("div", { class: "status muted" });
  const input = h("textarea", { rows: 2, placeholder: "Escribe un prompt… (Ctrl+Enter envía)" });
  const sendBtn = h("button", { class: "primary", type: "submit" }, "Enviar");
  const accSel = h("select", { "aria-label": "Cuenta" });
  const modelSel = h("select", { "aria-label": "Modelo" }, MODELS.map((m) => h("option", { value: m }, m || "modelo por defecto")));
  const effortSel = h("select", { "aria-label": "Effort" }, EFFORTS.map((m) => h("option", { value: m }, m ? `effort ${m}` : "effort por defecto")));
  const modeSel = h("select", { "aria-label": "Permisos" }, MODES.map((m) => h("option", { value: m }, MODE_LABEL[m])));
  const ctxBtn = h("button", { class: "ghost small" }, "contexto");
  const interruptBtn = h("button", { class: "ghost small" }, "Interrumpir");
  const compactBtn = h("button", { class: "ghost small" }, "Compactar");
  const closeBtn = h("button", { class: "ghost small danger" }, "Cerrar proceso");

  const controls = h("details", { class: "controls" },
    h("summary", {}, h("span", { class: "summary-line" })),
    h("div", { class: "grid" }, accSel, modelSel, effortSel, modeSel),
    h("div", { class: "actions" }, ctxBtn, compactBtn, interruptBtn, closeBtn));
  const composer = h("form", { class: "composer" }, input, sendBtn);
  view.append(controls, status, log, banners, composer);

  const summaryLine = () => {
    const a = (cache.accounts || []).find((x) => x.id === c.sel.account_id);
    $(".summary-line", controls).textContent = [a ? accountName(a) : "sin cuenta", c.sel.model || "modelo por defecto",
      c.sel.effort && `effort ${c.sel.effort}`, MODE_LABEL[c.sel.permission_mode]].filter(Boolean).join(" · ");
  };
  const setStatus = () => {
    const r = c.run;
    const parts = [];
    if (r) parts.push(r.state === "busy" ? "trabajando…" : r.state === "starting" ? "iniciando…" : "proceso activo");
    else parts.push("sin proceso");
    if (c.limits) parts.push(c.limits);
    if (c.sid) parts.push(c.sid.slice(0, 8));
    status.textContent = parts.join(" · ");
    interruptBtn.disabled = !r;
    closeBtn.disabled = !r;
    compactBtn.disabled = !r && !c.sid;
    ctxBtn.disabled = !c.sid;
  };
  const nearBottom = () => log.scrollHeight - log.scrollTop - log.clientHeight < 120;
  const append = (el) => {
    if (!el) return;
    const stick = nearBottom();
    log.append(el);
    if (stick) log.scrollTop = log.scrollHeight;
  };
  const banner = (cls, ...children) => {
    const b = h("div", { class: `banner ${cls}` }, ...children,
      h("button", { class: "ghost small close", "aria-label": "Cerrar aviso", onclick: () => b.remove() }, "×"));
    banners.append(b);
    return b;
  };

  // --- render de eventos (historial y en vivo comparten formato)
  const renderEvent = (ev) => {
    switch (ev.kind) {
      case "prompt":
        c.lastPrompt = ev.text;
        return h("div", { class: "msg user" }, richText(ev.text));
      case "text":
        return h("div", { class: "msg assistant" + (ev.sidechain ? " side" : "") }, richText(ev.text));
      case "thinking":
        return ev.text ? h("details", { class: "thinking" }, h("summary", {}, "pensamiento"), h("pre", {}, ev.text)) : null;
      case "tool_use": {
        let input = ev.input;
        let brief = "";
        try {
          const o = JSON.parse(ev.input);
          brief = o.command || o.file_path || o.pattern || o.url || o.description || o.prompt || "";
          input = JSON.stringify(o, null, 2);
        } catch { /* recortado: no es JSON válido */ }
        const mark = h("span", { class: "mark" }, "…");
        const el = h("details", { class: "tool" + (ev.sidechain ? " side" : "") },
          h("summary", {}, mark, " ", h("b", {}, ev.name), brief ? " " + String(brief).split("\n")[0].slice(0, 90) : ""),
          h("pre", {}, input), ev.truncated ? h("div", { class: "muted small" }, "(entrada recortada)") : null);
        c.toolEls.set(ev.id, { el, mark });
        return el;
      }
      case "tool_result": {
        const res = h("div", { class: "result" + (ev.is_error ? " err" : "") },
          h("pre", {}, ev.text || "(vacío)"), ev.truncated ? h("div", { class: "muted small" }, "(salida recortada)") : null);
        const t = c.toolEls.get(ev.tool_use_id);
        if (t) {
          t.mark.textContent = ev.is_error ? "✗" : "✓";
          t.el.append(res);
          return null;
        }
        return h("details", { class: "tool" }, h("summary", {}, ev.is_error ? "✗ resultado" : "✓ resultado"), res);
      }
      case "command":
        return h("div", { class: "meta" }, ev.name);
      case "command_output":
        return h("pre", { class: "meta-pre" }, ev.text);
      case "compact":
        return h("div", { class: "sep" }, `contexto compactado (${ev.trigger || "?"}${ev.pre_tokens ? `, ${Math.round(ev.pre_tokens / 1000)}k tokens antes` : ""})`);
      case "compact_summary":
        return h("details", { class: "thinking" }, h("summary", {}, "resumen de la compactación"), h("pre", {}, ev.text));
      case "image":
        return h("div", { class: "meta" }, "[imagen]");
      case "error":
        return h("div", { class: "msg error" }, ev.text || "error");
      case "meta":
        return h("div", { class: "meta" }, ev.text);
      case "continued_in":
        return h("div", { class: "meta" }, "continuó en ",
          h("a", { href: `#/s/${encodeURIComponent(project)}/${ev.session_id}` }, "otra conversación"));
      default:
        return null;
    }
  };

  // --- historial
  const olderBtn = h("button", { class: "ghost wide", onclick: async () => {
    const start = Math.max(0, c.histOffset - PAGE);
    try {
      const d = await api(`/sessions/${c.sid}?offset=${start}&limit=${c.histOffset - start}`);
      const frag = document.createDocumentFragment();
      const saved = c.toolEls;
      c.toolEls = new Map();
      d.events.forEach((ev) => { const el = renderEvent(ev); if (el) frag.append(el); });
      c.toolEls = new Map([...c.toolEls, ...saved]);
      const before = log.scrollHeight;
      olderBtn.after(frag);
      log.scrollTop += log.scrollHeight - before;
      c.histOffset = start;
      olderBtn.hidden = start === 0;
    } catch (e) { toast(e.message); }
  } }, "Cargar mensajes anteriores");
  log.append(olderBtn);
  olderBtn.hidden = true;

  // --- preferencias iniciales
  const accounts = await loadAccounts();
  const [pprefs, sprefs] = await Promise.all([
    api(`/prefs/project/${encodeURIComponent(project)}`),
    c.sid ? api(`/prefs/session/${c.sid}`) : Promise.resolve({}),
  ]);
  for (const k of Object.keys(c.sel)) {
    const v = sprefs[k] ?? pprefs[k];
    if (v !== null && v !== undefined) c.sel[k] = v;
  }

  if (c.sid) {
    const d = await api(`/sessions/${c.sid}?limit=${PAGE}`);
    setTop(d.summary.title || "Conversación", [project, d.summary.branch && `rama ${d.summary.branch}`,
      d.summary.worktree && "worktree"].filter(Boolean).join(" · "), `#/p/${encodeURIComponent(project)}`);
    d.events.forEach((ev) => { const el = renderEvent(ev); if (el) log.append(el); });
    c.histOffset = d.offset;
    olderBtn.hidden = d.offset === 0;
    if (d.active_run) {
      try {
        c.run = await api(`/runs/${d.active_run}`);
        Object.assign(c.sel, { account_id: c.run.account_id, model: c.run.model || "", effort: c.run.effort || "",
          permission_mode: c.run.permission_mode });
        subscribe(c.run.last_seq);
      } catch { c.run = null; }
    }
    requestAnimationFrame(() => { log.scrollTop = log.scrollHeight; });
  } else {
    log.append(h("p", { class: "muted center" }, `Conversación nueva en ${project}. Se abre en la raíz del proyecto.`));
    try {
      const draft = sessionStorage.getItem("airwork.draft");
      if (draft) { input.value = draft; sessionStorage.removeItem("airwork.draft"); }
    } catch { /* nada */ }
  }

  const fillAccounts = () => {
    accSel.replaceChildren(h("option", { value: "" }, "— elige cuenta —"),
      ...(cache.accounts || []).map((a) => h("option", { value: a.id, selected: a.id === c.sel.account_id },
        accountName(a) + (a.last_error ? " ⚠" : ""))));
  };
  fillAccounts();
  modelSel.value = c.sel.model || "";
  effortSel.value = c.sel.effort || "";
  modeSel.value = c.sel.permission_mode || "default";
  summaryLine();
  setStatus();
  if (!accounts.length) {
    banner("warn", "No hay cuentas registradas. ", h("a", { href: "#/accounts" }, "Agrega una"), ".");
  }

  const savePrefs = async () => {
    if (!c.sid) return;
    try {
      await api(`/prefs/session/${c.sid}`, { method: "PUT", body: { ...c.sel, model: c.sel.model || null, effort: c.sel.effort || null } });
    } catch (e) { toast(e.message); }
  };

  accSel.addEventListener("change", async () => {
    c.sel.account_id = accSel.value ? Number(accSel.value) : null;
    summaryLine();
    await savePrefs();
    if (c.run) banner("info", "La cuenta nueva se usa al reiniciar el proceso. ",
      h("button", { class: "small", onclick: (ev) => { ev.target.closest(".banner").remove(); closeRun(); } }, "Cerrar proceso ahora"));
  });
  modelSel.addEventListener("change", async () => {
    c.sel.model = modelSel.value;
    summaryLine();
    if (c.run && c.sel.model) {
      try { c.run = await api(`/runs/${c.run.run_id}`, { method: "PATCH", body: { model: c.sel.model } }); toast("Modelo cambiado"); }
      catch (e) { toast(e.message); }
    } else await savePrefs();
  });
  effortSel.addEventListener("change", async () => {
    c.sel.effort = effortSel.value;
    summaryLine();
    await savePrefs();
    if (c.run) toast("El effort se aplica al próximo arranque del proceso");
  });
  modeSel.addEventListener("change", async () => {
    c.sel.permission_mode = modeSel.value;
    summaryLine();
    if (c.run) {
      try { c.run = await api(`/runs/${c.run.run_id}`, { method: "PATCH", body: { permission_mode: c.sel.permission_mode } }); toast("Modo cambiado"); }
      catch (e) { toast(e.message); }
    } else await savePrefs();
  });

  // --- permisos
  const permCard = (ev) => {
    const answer = async (decision, scope) => {
      try {
        await api(`/permissions/${ev.req_id}`, { method: "POST", body: { decision, scope } });
      } catch (e) { toast(e.message); }
    };
    let pretty = ev.input;
    try { pretty = JSON.stringify(JSON.parse(ev.input), null, 2); } catch { /* recortado */ }
    const buttons = h("div", { class: "actions" },
      h("button", { class: "primary", onclick: () => answer("allow", "once") }, "Permitir"),
      h("button", { class: "danger", onclick: () => answer("deny", "once") }, "Denegar"),
      h("button", { class: "ghost", onclick: () => answer("allow", "session") }, `Permitir ${ev.tool} en esta sesión`));
    const el = h("div", { class: "perm" },
      h("div", { class: "card-title" }, "Permiso: ", ev.tool),
      ev.title ? h("div", {}, ev.title) : null,
      ev.description ? h("div", { class: "muted" }, ev.description) : null,
      ev.blocked_path ? h("div", { class: "muted" }, "Ruta: ", ev.blocked_path) : null,
      h("pre", {}, pretty), buttons);
    c.permEls.set(ev.req_id, { el, buttons });
    if (navigator.vibrate) navigator.vibrate(200);
    return el;
  };

  // --- cuota agotada
  const quotaBanner = (text) => {
    const sel = accSel.cloneNode(true);
    sel.value = "";
    const b = banner("error", h("div", {}, text),
      h("div", { class: "row" }, sel, h("button", { class: "primary small", onclick: async () => {
        if (!sel.value) return toast("Elige otra cuenta");
        c.sel.account_id = Number(sel.value);
        accSel.value = sel.value;
        summaryLine();
        await savePrefs();
        b.remove();
        await closeRun();
        if (c.lastPrompt) await start(c.lastPrompt, false);
      } }, "Reintentar con esta cuenta")));
    loadAccounts(true).then(fillAccounts).catch(() => {});
  };

  // --- eventos en vivo
  function onEvent(ev, skipUntil) {
    const old = ev.seq <= skipUntil;
    if (old && !["permission_request", "permission_resolved", "init", "rate_limit"].includes(ev.kind)) return;
    switch (ev.kind) {
      case "init":
        if (ev.session_id && ev.session_id !== c.sid) {
          c.sid = ev.session_id;
          replaceHash(`#/s/${encodeURIComponent(project)}/${c.sid}`);
          savePrefs();
        }
        break;
      case "permission_request":
        append(permCard(ev));
        break;
      case "permission_resolved": {
        const p = c.permEls.get(ev.req_id);
        if (p) {
          p.buttons.replaceChildren(h("span", { class: ev.decision === "allow" ? "ok" : "err" },
            ev.decision === "allow" ? (ev.scope === "session" ? "Permitido en esta sesión" : "Permitido") : "Denegado"));
          p.el.classList.add("done");
        }
        break;
      }
      case "status":
        if (ev.status === "compacting") append(h("div", { class: "meta" }, "compactando…"));
        break;
      case "rate_limit": {
        const w = ev.windows || {};
        const pct = (x) => (x && typeof x.utilization === "number" ? `${Math.round(x.utilization * 100)}%` : "?");
        if (w.five_hour || w.seven_day) c.limits = `5 h ${pct(w.five_hour)} · 7 d ${pct(w.seven_day)}`;
        if (ev.status === "rejected" && !old) {
          quotaBanner(`La cuenta agotó su límite (${ev.rate_limit_type || "?"}); se reinicia ${clock(ev.resets_at)}. Elige otra cuenta:`);
        }
        break;
      }
      case "result":
        if (c.run) c.run.state = "idle";
        if (ev.is_error) {
          append(h("div", { class: "msg error" }, ev.result || ev.subtype || "error"));
          if ([401, 403, 429].includes(ev.api_error_status) && !banners.querySelector(".banner.error")) {
            quotaBanner(`Error de cuenta (${ev.api_error_status}). Elige otra cuenta:`);
          }
        } else if (ev.usage) {
          const u = ev.usage;
          const inTok = (u.input_tokens || 0) + (u.cache_read_input_tokens || 0) + (u.cache_creation_input_tokens || 0);
          append(h("div", { class: "meta" }, `listo · ${ev.num_turns} turnos · ${Math.round(inTok / 1000)}k entrada · ${u.output_tokens || 0} salida`));
        }
        break;
      case "closed":
        append(h("div", { class: "meta" }, `proceso cerrado (${ev.reason})`));
        c.run = null;
        if (c.es) { c.es.close(); c.es = null; }
        break;
      default: {
        if (ev.kind === "prompt" && c.run) c.run.state = "busy";
        append(renderEvent(ev));
      }
    }
    setStatus();
  }

  function subscribe(skipUntil = 0) {
    if (c.es) c.es.close();
    const es = new EventSource(`/api/runs/${c.run.run_id}/events`);
    c.es = es;
    for (const k of LIVE_KINDS) {
      es.addEventListener(k, (m) => {
        try { onEvent(JSON.parse(m.data), skipUntil); } catch (e) { console.error(e); }
      });
    }
    es.onerror = () => {
      if (es.readyState === EventSource.CLOSED && c.es === es) {
        c.es = null;
        if (c.run) { c.run = null; setStatus(); toast("Se perdió la conexión con el proceso"); }
      }
    };
  }

  // --- acciones
  async function start(text, fork) {
    if (!c.sel.account_id) {
      banner("warn", "Elige una cuenta para esta conversación (arriba, en los controles).");
      controls.open = true;
      throw new Error("sin cuenta");
    }
    let run;
    try {
      run = await api("/runs", { method: "POST", body: {
        project, prompt: text, session_id: c.sid || undefined, fork,
        account_id: c.sel.account_id, model: c.sel.model || undefined, effort: c.sel.effort || undefined,
        permission_mode: c.sel.permission_mode,
      } });
    } catch (e) {
      if (e.status === 409 && e.data.reasons) {
        const b = banner("warn",
          h("div", {}, "Esta conversación parece abierta en otro lugar: ", e.data.reasons.join("; "), "."),
          h("div", { class: "muted" }, "Puedes abrir una copia (fork): una conversación nueva con el mismo historial."),
          h("button", { class: "small", onclick: async () => { b.remove(); try { await start(text, true); input.value = ""; } catch (err) { if (err.message !== "sin cuenta") toast(err.message); } } }, "Abrir como fork"));
      } else banner("error", e.message);
      throw e;
    }
    const had = c.run && c.run.run_id === run.run_id && c.es;
    c.run = run;
    if (!fork && run.session_id && run.session_id !== c.sid) {
      c.sid = run.session_id;
      replaceHash(`#/s/${encodeURIComponent(project)}/${c.sid}`);
      setTop("Conversación", project, `#/p/${encodeURIComponent(project)}`);
    }
    if (fork) {
      c.sid = null;
      log.append(h("div", { class: "sep" }, "fork: a partir de aquí es una conversación nueva"));
    }
    if (!had) {
      if (run.reused) { append(renderEvent({ kind: "prompt", text })); subscribe(run.last_seq); }
      else subscribe(0);
    }
    setStatus();
  }

  async function send(text) {
    if (c.run) {
      try {
        c.run = await api(`/runs/${c.run.run_id}/messages`, { method: "POST", body: { prompt: text } });
      } catch (e) {
        if (e.status !== 404 && e.status !== 409) throw e;
        c.run = null;  // el proceso ya no existe: arranca otro
        return start(text, false);
      }
    } else {
      await start(text, false);
    }
  }

  async function closeRun() {
    if (!c.run) return;
    try { await api(`/runs/${c.run.run_id}`, { method: "DELETE" }); } catch (e) { toast(e.message); }
    c.run = null;
    setStatus();
  }

  composer.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const text = input.value.trim();
    if (!text) return;
    sendBtn.disabled = true;
    try {
      await send(text);
      input.value = "";
    } catch (e) {
      if (e.message !== "sin cuenta" && !(e instanceof ApiError)) toast(e.message);
    } finally {
      sendBtn.disabled = false;
    }
  });
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) composer.requestSubmit();
  });
  interruptBtn.addEventListener("click", async () => {
    if (!c.run) return;
    try { await api(`/runs/${c.run.run_id}/interrupt`, { method: "POST" }); toast("Interrupción enviada"); } catch (e) { toast(e.message); }
  });
  compactBtn.addEventListener("click", async () => {
    if (!confirm("¿Compactar el contexto de esta conversación?")) return;
    try { await send("/compact"); } catch (e) { if (!(e instanceof ApiError)) toast(e.message); }
  });
  closeBtn.addEventListener("click", closeRun);
  ctxBtn.addEventListener("click", async () => {
    if (!c.sid) return;
    ctxBtn.disabled = true;
    ctxBtn.textContent = "…";
    try {
      const acc = c.sel.account_id ? `?account_id=${c.sel.account_id}` : "";
      const u = await api(`/sessions/${c.sid}/context${acc}`);
      const pct = u.maxTokens ? `${Math.round((100 * (u.totalTokens || 0)) / u.maxTokens)}%` : "?";
      ctxBtn.textContent = `contexto ${pct}`;
      const cats = (u.categories || []).filter((x) => x.tokens > 0)
        .map((x) => h("li", {}, `${x.name}: ${Math.round(x.tokens / 1000)}k`));
      banner("info", h("div", {}, `Contexto: ${Math.round((u.totalTokens || 0) / 1000)}k de ${Math.round((u.maxTokens || 0) / 1000)}k tokens (${pct}) · ${u.model || ""}`),
        cats.length ? h("ul", { class: "small" }, cats) : null);
    } catch (e) {
      ctxBtn.textContent = "contexto";
      toast(e.message);
    } finally {
      ctxBtn.disabled = false;
    }
  });

  cleanup = () => { if (c.es) c.es.close(); };
}

// --- arranque -----------------------------------------------------------------------------------

window.addEventListener("hashchange", route);
route();

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => { /* sin SW: la app funciona igual */ });
}
