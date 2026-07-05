"use strict";

const $ = (id) => document.getElementById(id);

function toast(msg, err = false) {
  const t = document.createElement("div");
  t.className = "toast" + (err ? " err" : "");
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 3500);
}

async function api(path, body) {
  const r = await fetch(path, body ? {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  } : undefined);
  return r.json();
}

/* ---------- вкладки ---------- */
document.querySelectorAll(".nav-btn").forEach((b) => {
  b.onclick = () => {
    document.querySelectorAll(".nav-btn").forEach((x) => x.classList.remove("active"));
    document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    $("tab-" + b.dataset.tab).classList.add("active");
    if (b.dataset.tab === "models") loadModels();
    if (b.dataset.tab === "audit") loadAudit();
    if (b.dataset.tab === "data") loadDataTab();
  };
});

/* ---------- поиск + Стоп ---------- */
let abortCtl = null;

$("btn-ask").onclick = () => runSearch();
$("query").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) runSearch();
});
$("btn-stop").onclick = () => {
  if (abortCtl) abortCtl.abort();
};

/* ---------- вкладки диалогов ---------- */
let chats = [];
let activeChatId = null;
let genChatId = null; // чат, в котором сейчас идёт генерация

function activeChat() { return chats.find((c) => c.id === activeChatId); }

function newChat() {
  return { id: Date.now() + "-" + Math.random().toString(36).slice(2, 7),
           title: "Новый чат", query: "", md: "", parsed: null, visionMd: "",
           sources: null, experts: [], done: false, generating: false };
}

function saveChats() {
  try {
    localStorage.setItem("sciknot_chats",
      JSON.stringify(chats.slice(-20).map(({ generating, ...c }) => c)));
  } catch (e) { /* переполнение localStorage — не критично */ }
}

function renderTabs() {
  const box = $("chat-tabs");
  box.innerHTML = "";
  chats.forEach((c) => {
    const t = document.createElement("span");
    t.className = "chat-tab" + (c.id === activeChatId ? " active" : "");
    t.title = c.query || c.title;
    const label = document.createElement("span");
    label.textContent = (c.generating ? "⏳ " : "") + (c.title || "Новый чат");
    t.appendChild(label);
    const x = document.createElement("span");
    x.className = "tab-close";
    x.textContent = "✕";
    x.title = "Удалить чат";
    x.onclick = (e) => { e.stopPropagation(); deleteChat(c.id); };
    t.appendChild(x);
    t.onclick = () => switchChat(c.id);
    box.appendChild(t);
  });
  const add = document.createElement("button");
  add.id = "chat-add";
  add.className = "btn small";
  add.textContent = "+";
  add.title = "Новый чат";
  add.onclick = () => { const c = newChat(); chats.push(c); switchChat(c.id); saveChats(); };
  box.appendChild(add);
}

function switchChat(id) {
  const cur = activeChat();
  if (cur) cur.query = $("query").value;
  activeChatId = id;
  renderTabs();
  renderChatView();
}

function deleteChat(id) {
  const i = chats.findIndex((c) => c.id === id);
  if (i < 0) return;
  if (id === genChatId && abortCtl) abortCtl.abort();
  chats.splice(i, 1);
  if (!chats.length) chats.push(newChat());
  if (activeChatId === id) activeChatId = chats[Math.min(i, chats.length - 1)].id;
  renderTabs();
  renderChatView();
  saveChats();
}

function renderChatView() {
  const chat = activeChat();
  $("query").value = chat.query || "";
  $("answer").innerHTML = chat.md ? marked.parse(chat.md) : "";
  $("vision-desc").innerHTML = chat.visionMd ? marked.parse(chat.visionMd) : "";
  $("vision-wrap").classList.toggle("hidden", !chat.visionMd);
  if (chat.experts && chat.experts.length) renderExperts(chat.experts);
  else $("experts").innerHTML = "";
  if (chat.sources) renderSources(chat.sources);
  else { $("sources").innerHTML = ""; $("sources-wrap").classList.add("hidden"); }
  if (chat.parsed) { $("parsed").textContent = chat.parsed; $("parsed-wrap").classList.remove("hidden"); }
  else { $("parsed").textContent = ""; $("parsed-wrap").classList.add("hidden"); }
  $("btn-export").classList.toggle("hidden", !(chat.done && chat.sources));
  $("btn-ask").classList.toggle("hidden", !!chat.generating);
  $("btn-stop").classList.toggle("hidden", !chat.generating);
  setStage(chat.generating ? "Генерация…" : "", !!chat.generating);
}

function renderExperts(list) {
  $("experts").innerHTML = list.map((x) =>
    `<span class="expert-chip">👤 ${esc(x.name)}${x.affiliation ? " · " + esc(x.affiliation) : ""} (${x.mentions})</span>`
  ).join("");
}

$("query").addEventListener("input", () => {
  const c = activeChat();
  if (c) c.query = $("query").value;
});

// вложение-картинка к вопросу (vision): кнопка, drag-and-drop, Ctrl+V
let askImage = null; // {b64, mime, name}

function setAskImage(f) {
  if (!f || !f.type.startsWith("image/")) return false;
  const rd = new FileReader();
  rd.onload = () => {
    askImage = { b64: rd.result.split(",")[1], mime: f.type || "image/png",
                 name: f.name || "из буфера обмена" };
    $("ask-image-name").textContent = "🖼 " + askImage.name + " ✕";
  };
  rd.readAsDataURL(f);
  return true;
}

$("ask-image").onchange = () => setAskImage($("ask-image").files[0]);
$("ask-image-name").onclick = () => {
  askImage = null;
  $("ask-image").value = "";
  $("ask-image-name").textContent = "";
};

// drag-and-drop на поле запроса
const searchBox = document.querySelector(".search-box");
searchBox.addEventListener("dragover", (e) => {
  e.preventDefault();
  searchBox.classList.add("drag");
});
searchBox.addEventListener("dragleave", () => searchBox.classList.remove("drag"));
searchBox.addEventListener("drop", (e) => {
  e.preventDefault();
  searchBox.classList.remove("drag");
  const f = [...(e.dataTransfer.files || [])].find((x) => x.type.startsWith("image/"));
  if (setAskImage(f)) toast("Изображение приложено к вопросу");
});

// вставка из буфера обмена (Ctrl+V) в поле запроса
$("query").addEventListener("paste", (e) => {
  const item = [...(e.clipboardData.items || [])].find((x) => x.type.startsWith("image/"));
  if (item && setAskImage(item.getAsFile())) {
    e.preventDefault();
    toast("Изображение из буфера приложено к вопросу");
  }
});

async function runSearch() {
  const q = $("query").value.trim();
  if (!q) return;
  if (abortCtl) {
    toast("Дождитесь завершения или остановите текущую генерацию", true);
    return;
  }

  const chat = activeChat();
  Object.assign(chat, { query: q, title: q.slice(0, 30), md: "", parsed: null,
                        visionMd: "", sources: null, experts: [], done: false, generating: true });
  abortCtl = new AbortController();
  genChatId = chat.id;
  renderTabs();
  renderChatView();
  setStage("Подключаюсь…", true);

  const active = () => chat.id === activeChatId;
  try {
    const body = { query: q };
    if (askImage) {
      body.image_b64 = askImage.b64;
      body.image_mime = askImage.mime;
    }
    const resp = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: abortCtl.signal,
    });
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const line = buf.slice(0, idx).trim();
        buf = buf.slice(idx + 2);
        if (!line.startsWith("data:")) continue;
        handleEvent(JSON.parse(line.slice(5)));
      }
    }
  } catch (e) {
    if (e.name === "AbortError") {
      chat.md += "\n\n> ⏹ *Генерация остановлена пользователем.*";
      if (active()) $("answer").innerHTML = marked.parse(chat.md);
    } else {
      toast("Ошибка: " + e.message, true);
    }
  } finally {
    abortCtl = null;
    genChatId = null;
    chat.generating = false;
    saveChats();
    renderTabs();
    if (active()) {
      $("btn-ask").classList.remove("hidden");
      $("btn-stop").classList.add("hidden");
      setStage("");
      $("btn-export").classList.toggle("hidden", !(chat.done && chat.sources));
    }
  }

  function handleEvent(ev) {
    if (ev.type === "stage") {
      if (active()) setStage(ev.stage, true);
    } else if (ev.type === "parsed") {
      chat.parsed = JSON.stringify(ev.data, null, 2);
      if (active()) {
        $("parsed").textContent = chat.parsed;
        $("parsed-wrap").classList.remove("hidden");
      }
    } else if (ev.type === "vision_desc") {
      chat.visionMd = ev.text;
      if (active()) {
        $("vision-desc").innerHTML = marked.parse(ev.text);
        $("vision-wrap").classList.remove("hidden");
      }
    } else if (ev.type === "sources") {
      chat.sources = ev.data;
      if (active()) renderSources(ev.data);
    } else if (ev.type === "experts" && ev.data.length) {
      chat.experts = ev.data;
      if (active()) renderExperts(ev.data);
    } else if (ev.type === "delta") {
      chat.md += ev.text;
      if (active()) $("answer").innerHTML = marked.parse(chat.md);
    } else if (ev.type === "done") {
      chat.done = true;
      if (active()) setStage("");
    } else if (ev.type === "error") {
      toast(ev.message, true);
      if (active()) setStage("");
    }
  }
}

$("btn-export").onclick = () => {
  const chat = activeChat();
  if (!chat || !chat.sources || !chat.md) return;
  const srcList = chat.sources.map((s, i) => {
    const meta = [s.category, s.journal, s.year, s.location].filter(Boolean).join(", ");
    return `${i + 1}. ${s.source_path} (${meta})`;
  }).join("\n");
  const md = `# Отчёт «Научный клубок»

**Запрос:** ${chat.query}
**Дата:** ${new Date().toLocaleString("ru-RU")}

---

${chat.md}

---

## Источники

${srcList}
`;
  const blob = new Blob([md], { type: "text/markdown;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "sciknot-report.md";
  a.click();
  URL.revokeObjectURL(a.href);
};

function setStage(text, pulse = false) {
  const s = $("stage");
  s.textContent = text;
  s.classList.toggle("pulse", pulse && !!text);
}

function renderSources(list) {
  $("src-count").textContent = list.length;
  $("sources").innerHTML = list.map((s, i) => {
    if (s.hidden) return `<div class="src-card locked">[${i + 1}] ${esc(s.source_path)}</div>`;
    const meta = [s.category, s.journal, s.year, s.location,
      s.score === 0 ? "найден по ключевым словам" : "score " + (+s.score).toFixed(3)]
      .filter(Boolean).join(" · ");
    return `<div class="src-card${s.score === 0 ? " kw" : ""}">
      <div class="path">[${i + 1}] ${esc(s.source_path)}</div>
      <div class="meta">${esc(meta)}</div>
      ${s.summary ? `<div class="meta">${esc(s.summary)}</div>` : ""}</div>`;
  }).join("");
  $("sources-wrap").classList.remove("hidden");
}

const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

/* ---------- граф (3D, в духе Obsidian) ---------- */
const GROUP_COLORS = {
  Material: "#5aa2f7", Process: "#f7a34f", Equipment: "#a3e635",
  Topic: "#c084fc", Expert: "#f87171", Experiment: "#f472b6", Facility: "#22d3ee",
};
const REL_COLORS = {
  contradicts: "#ef4444", expert: "#f87171",
  uses_material: "#5aa2f7", produces_output: "#34d399", uses_equipment: "#a3e635",
  operates_at_condition: "#eab308", applied_for: "#f7a34f", expert_in: "#f87171",
  validated_by: "#34d399", facility: "#22d3ee",
};
const REL_RU = {
  uses_material: "использует материал", produces_output: "даёт на выходе",
  uses_equipment: "использует оборудование", operates_at_condition: "работает при условии",
  applied_for: "применяется для", expert_in: "эксперт в", contradicts: "противоречит",
  expert: "эксперт по теме", validated_by: "подтверждено",
  facility: "организация по теме",
};
let graph3d = null;
let graphNeedsFit = false;
const RESULT_COLOR = "#34d399"; // «результат» = во что упирается produces_output

let zebraMat = null;
function getZebraMat() {
  // красно-белая зебра для рёбер-противоречий
  if (zebraMat) return zebraMat;
  const c = document.createElement("canvas");
  c.width = 16; c.height = 64;
  const ctx = c.getContext("2d");
  for (let i = 0; i < 8; i++) {
    ctx.fillStyle = i % 2 ? "#ffffff" : "#ef4444";
    ctx.fillRect(0, i * 8, 16, 8);
  }
  const tex = new THREE.CanvasTexture(c);
  tex.wrapS = tex.wrapT = THREE.RepeatWrapping;
  tex.repeat.set(1, 6);
  zebraMat = new THREE.MeshBasicMaterial({ map: tex });
  return zebraMat;
}

function clearGraph() {
  if (graph3d) graph3d.graphData({ nodes: [], links: [] });
}
$("btn-graph-clear").onclick = clearGraph;

window.addEventListener("resize", () => {
  if (!graph3d) return;
  const box = $("graph-view").getBoundingClientRect();
  graph3d.width(box.width).height(box.height);
});

$("btn-graph").onclick = async () => {
  const q = $("graph-q").value.trim();
  if (!q) return;
  $("btn-graph").disabled = true;
  const st = $("graph-stage");
  st.textContent = "Ищу в графе…";
  st.classList.add("pulse");
  try {
    const data = await fetch(
      `/api/graph?q=${encodeURIComponent(q)}&limit=${$("graph-limit").value}`
    ).then((r) => r.json());
    if (!data.nodes.length) {
      toast("Сущность не найдена в графе", true);
      return;
    }
    st.textContent = "Строю 3D-граф…";
    clearGraph(); // автоочистка перед новой генерацией — раскладка считается с нуля

    const resultIds = new Set(
      data.edges.filter((e) => e.rtype === "produces_output").map((e) => e.to)
    );
    const nodes = data.nodes.map((n) => ({
      id: n.id, group: n.group, weak: n.weak, root: n.root,
      isResult: resultIds.has(n.id),
      name: `${esc(n.expert_label || n.id)} · ${n.group}` +
            (resultIds.has(n.id) ? " · результат" : "") +
            ` · источников: ${n.coverage}${n.weak ? " · ⚠ слабое покрытие" : ""}`,
      val: n.root ? 5 : 0.6 + Math.min(n.coverage || 1, 12) * 0.28,
      color: n.weak ? "#4a4a63" : (GROUP_COLORS[n.group] || "#8b8ba3"),
    }));
    const links = data.edges.map((e) => ({
      source: e.from, target: e.to, rtype: e.rtype,
      name: `${REL_RU[e.rtype] || e.rtype} · источников: ${e.conf}` +
            (e.upd ? ` · актуализировано: ${e.upd}` : ""),
    }));

    if (!graph3d) {
      graph3d = ForceGraph3D()($("graph-view"))
        .backgroundColor("#08080f")
        .showNavInfo(false)
        .nodeThreeObject((node) => {
          const group = new THREE.Group();
          const r = 2 * Math.cbrt(node.val || 1);
          const matA = new THREE.MeshLambertMaterial({
            color: node.color, transparent: true, opacity: 0.95 });
          if (node.isResult) {
            // полусфера цвета сущности + полусфера цвета «результата»
            const matB = new THREE.MeshLambertMaterial({
              color: RESULT_COLOR, transparent: true, opacity: 0.95 });
            group.add(new THREE.Mesh(new THREE.SphereGeometry(r, 24, 16, 0, Math.PI), matA));
            group.add(new THREE.Mesh(new THREE.SphereGeometry(r, 24, 16, Math.PI, Math.PI), matB));
          } else {
            group.add(new THREE.Mesh(new THREE.SphereGeometry(r, 24, 16), matA));
          }
          const text = node.id.length > 26 ? node.id.slice(0, 25) + "…" : node.id;
          const s = new SpriteText(node.weak ? "⚠ " + text : text);
          s.color = node.weak ? "#6d6d8c" : (node.root ? "#e7e7fa" : "#b9b9d6");
          s.textHeight = node.root ? 3.2 : 2.3;
          s.material.depthWrite = false;
          s.position.set(0, r + 2.4, 0);
          group.add(s);
          return group;
        })
        .linkMaterial((l) => (l.rtype === "contradicts" ? getZebraMat() : false))
        .linkColor((l) => REL_COLORS[l.rtype] || "#4b4b7a")
        .linkOpacity(0.5)
        .linkWidth((l) => (l.rtype === "contradicts" ? 2.4 : 0.5))
        .linkDirectionalParticles((l) => (l.rtype === "expert" ? 0 : 1))
        .linkDirectionalParticleWidth(1.4)
        .linkDirectionalParticleColor(() => "#8b5cf6")
        .cooldownTicks(180)
        .onEngineStop(() => {
          if (graphNeedsFit) {
            graphNeedsFit = false;
            graph3d.zoomToFit(700, 60);
          }
        })
        .onNodeClick((node) => {
          const d = 60;
          const r = 1 + d / Math.hypot(node.x, node.y, node.z);
          graph3d.cameraPosition({ x: node.x * r, y: node.y * r, z: node.z * r }, node, 900);
        });
      const box = $("graph-view").getBoundingClientRect();
      graph3d.width(box.width).height(box.height);
    }
    graphNeedsFit = true;
    graph3d.graphData({ nodes, links });
  } finally {
    $("btn-graph").disabled = false;
    st.textContent = "";
    st.classList.remove("pulse");
  }
};

/* ---------- модели ---------- */
// значение option: "local:<имя>" или "online:<имя подключения>"
function slotSelection(slotName, active, slots, registry) {
  const base = slotName === "chat" ? active.llm_base : active.embed_base;
  if (base.includes("127.0.0.1") || base.includes("localhost")) {
    return "local:" + (slots[slotName].active_model || "");
  }
  const conn = registry.api_connections.find((a) => a.base_url === base);
  return conn ? "online:" + conn.name : "";
}

function fillSlot(slotName, data) {
  const { registry, slots, active } = data;
  const key = slotName === "chat" ? "chat" : "embed";
  const roleWanted = slotName === "chat" ? "chat" : "embedding";
  const st = slots[slotName];
  const selected = slotSelection(slotName, active, slots, registry);

  const locals = registry.local_models.filter((m) => (m.role || "chat") === roleWanted);
  const opts = [
    ...locals.map((m) => ({ v: "local:" + m.name, label: m.name + (m.mmproj_path ? " 🖼️" : "") })),
    ...registry.api_connections.map((a) => ({
      v: "online:" + a.name,
      label: (slotName === "chat" ? a.name : a.embed_model) + " (online)",
    })),
  ];
  $(`slot-${key}-model`).innerHTML = opts.map((o) =>
    `<option value="${esc(o.v)}"${o.v === selected ? " selected" : ""}>${esc(o.label)}</option>`).join("");
  $(`slot-${key}-port`).value = st.port;

  const refresh = () => {
    const isOnline = $(`slot-${key}-model`).value.startsWith("online:");
    $(`slot-${key}-port`).disabled = isOnline;
    $(`slot-${key}-stop`).disabled = isOnline;
    const isActive = $(`slot-${key}-model`).value === selected;
    if (isOnline) {
      $(`slot-${key}-dot`).classList.toggle("on", isActive);
      const visionNote = slotName === "chat" && active.vision_model
        ? `, vision: ${active.vision_model.split("/").pop()}` : "";
      $(`slot-${key}-status`).textContent = isActive
        ? `🟢 онлайн: ${slotName === "chat" ? active.answer_model : active.embed_model}${visionNote}`
        : "облачное подключение — нажмите «Применить»";
    } else {
      $(`slot-${key}-dot`).classList.toggle("on", st.running && isActive);
      $(`slot-${key}-status`).textContent = st.running
        ? (isActive ? `🟢 работает: ${st.alias || st.active_model} (порт ${st.port})`
                    : `сервер на порту ${st.port} занят: ${st.alias}`)
        : `⚪ остановлена (порт ${st.port}) · автозапуск при первом запросе`;
    }
  };
  $(`slot-${key}-model`).onchange = refresh;
  refresh();
}

async function loadModels() {
  const data = await api("/api/models");
  const { registry, active } = data;
  $("models-active").textContent =
    `Активно · чат: ${active.llm_base} (${active.answer_model}) · эмбеддинги: ${active.embed_base} (${active.embed_model})`;

  fillSlot("chat", data);
  fillSlot("embedding", data);

  $("st-server").value = registry.llama_server;
  $("st-launch").value = JSON.stringify(registry.default_launch, null, 2);
}

/* ---------- слоты LLM / Эмбеддинг ---------- */
for (const [slot, key] of [["chat", "chat"], ["embedding", "embed"]]) {
  $(`slot-${key}-apply`).onclick = async () => {
    const value = $(`slot-${key}-model`).value;
    if (!value) { toast("Список моделей пуст", true); return; }
    let r;
    if (value.startsWith("online:")) {
      r = await api("/api/connections/action",
        { name: value.slice(7), action: "activate", scope: slot });
    } else {
      toast("Переключаю модель — загрузка может занять минуты…");
      r = await api("/api/slots/activate",
        { slot, name: value.slice(6), port: +$(`slot-${key}-port`).value });
    }
    toast(r.message, !r.ok);
    loadModels();
  };
  $(`slot-${key}-stop`).onclick = async () => {
    const r = await api("/api/slots/stop", { slot });
    toast(r.message, !r.ok);
    loadModels();
  };
}

$("btn-save-settings").onclick = async () => {
  try {
    await api("/api/settings", {
      llama_server: $("st-server").value.trim(),
      default_launch: JSON.parse($("st-launch").value),
    });
    toast("Сохранено");
  } catch (e) { toast("Некорректный JSON: " + e.message, true); }
};

/* ---------- данные: загрузка и индексация ---------- */
const dz = $("drop-zone");
dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("drag"); });
dz.addEventListener("dragleave", () => dz.classList.remove("drag"));
dz.addEventListener("drop", (e) => {
  e.preventDefault();
  dz.classList.remove("drag");
  $("data-files").files = e.dataTransfer.files;
  showPicked();
});
$("data-files").onchange = showPicked;

function showPicked() {
  const fs = [...$("data-files").files];
  $("dz-list").textContent = fs.length
    ? fs.map((f) => f.name).join(" · ") : "";
}

let dataPoll = null;

$("btn-ingest").onclick = async () => {
  const fs = [...$("data-files").files];
  if (!fs.length) { toast("Выберите файлы", true); return; }
  const fd = new FormData();
  fs.forEach((f) => fd.append("files", f));
  fd.append("index_images", $("opt-index-images").checked);
  const r = await fetch("/api/data/upload", { method: "POST", body: fd }).then((x) => x.json());
  toast(r.message, !r.ok);
  if (r.ok) {
    $("data-files").value = "";
    showPicked();
    pollDataStatus();
  }
};

async function pollDataStatus() {
  clearInterval(dataPoll);
  const render = async () => {
    const s = await fetch("/api/data/status").then((r) => r.json());
    const box = $("data-status");
    if (!s.stage) { box.classList.add("hidden"); return; }
    box.classList.remove("hidden");
    const stage = $("data-stage");
    stage.textContent = s.running ? s.stage : "";
    stage.classList.toggle("pulse", s.running);
    box.innerHTML = `
      <b>${esc(s.stage)}</b>${s.detail ? " — " + esc(s.detail) : ""}<br>
      <span class="hint">файлы: ${esc((s.files || []).join(", ") || "—")}</span>
      ${s.error ? `<div style="color:#fca5a5;margin-top:6px">${esc(s.error)}</div>` : ""}`;
    if (!s.running) {
      clearInterval(dataPoll);
      dataPoll = null;
      if (s.stage === "Готово") { toast("Индексация завершена"); loadStats(); }
      if (s.error) toast("Ошибка индексации", true);
    }
  };
  await render();
  dataPoll = setInterval(render, 2000);
}

$("btn-clear-data").onclick = async () => {
  if (!confirm(
    "⚠ ВНИМАНИЕ: будут удалены ВСЕ проиндексированные данные — граф знаний, " +
    "векторный индекс и загруженные через интерфейс файлы.\n\n" +
    "Исходный корпус документов на диске не пострадает.\n" +
    "Действие необратимо. Продолжить?")) return;
  if (!confirm("Точно очистить все данные? Это последнее предупреждение.")) return;
  const r = await api("/api/data/clear", {});
  toast(r.message, !r.ok);
  loadStats();
  pollDataStatus();
};

$("btn-gaps").onclick = async () => {
  $("btn-gaps").disabled = true;
  $("gaps-stage").textContent = "Анализирую покрытие графа…";
  $("gaps-stage").classList.add("pulse");
  try {
    const data = await fetch("/api/gaps").then((r) => r.json());
    $("gaps").innerHTML = data.gaps.length
      ? data.gaps.map((g) => `<div class="gap-item"><b>${esc(g.name)}</b>
          <div class="type">${esc(g.gap_type)} · документов: ${g.coverage}</div></div>`).join("")
      : '<p class="hint">Пробелы не обнаружены.</p>';
  } finally {
    $("btn-gaps").disabled = false;
    $("gaps-stage").textContent = "";
    $("gaps-stage").classList.remove("pulse");
  }
};

async function loadStats() {
  try {
    const s = await fetch("/api/data/stats").then((r) => r.json());
    $("corpus-stats").innerHTML =
      `<div class="stat-row"><span>Всего документов</span><b>${s.docs}</b></div>
       <div class="stat-row"><span>Чанков в индексе</span><b>${s.chunks}</b></div>` +
      s.by_category.map((c) =>
        `<div class="stat-row"><span>${esc(c.category || "—")}</span><b>${c.docs}</b></div>`).join("");
  } catch { $("corpus-stats").textContent = "нет связи с графом"; }
}

function loadDataTab() {
  loadStats();
  pollDataStatus();
}

/* ---------- аудит ---------- */
async function loadAudit() {
  const data = await fetch("/api/audit").then((r) => r.json());
  $("audit-rows").innerHTML = data.ok
    ? data.rows.map((r) => `<div class="audit-row"><span class="ts">${esc(r.ts)}</span>
        <span class="action">${esc(r.action)}</span>
        <span>${esc(r.payload)}</span></div>`).join("") || '<p class="hint">Журнал пуст.</p>'
    : `<p class="hint">${esc(data.message)}</p>`;
}
$("btn-audit").onclick = loadAudit;

/* ---------- init ---------- */
try { chats = JSON.parse(localStorage.getItem("sciknot_chats") || "[]"); } catch (e) { chats = []; }
chats.forEach((c) => { c.generating = false; });
if (!chats.length) chats.push(newChat());
activeChatId = chats[chats.length - 1].id;
renderTabs();
renderChatView();

loadModels().catch(() => {});
