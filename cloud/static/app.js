"use strict";
// Trang web trạm: gọi API 5 giây/lần (trạng thái, lệnh) và 30 giây/lần (biểu đồ, nhật ký).

const SOURCE = {
  normal:    { name: "Bình thường",            action: "Quạt tắt, còi im",       color: "#2e9e5b" },
  cooking:   { name: "Nấu ăn",                 action: "Chỉ bật quạt",            color: "#d98a1c" },
  pollution: { name: "Ô nhiễm bụi mịn",        action: "Bật quạt và kêu còi",     color: "#d64545" },
  // nhãn của mô hình cũ (trước 28/11/2026), chỉ để hiện lịch sử
  smoking:   { name: "Khói thuốc lá / nhang (cũ)", action: "Chỉ kêu còi",          color: "#8e44ad" },
};
const AQI_COLORS = ["#2e9e5b", "#c9a400", "#e67e22", "#d64545", "#8e44ad", "#7b1f1f"];
const STATUS_TEXT = { pending: "đang chờ Pi…", sent: "Pi đã nhận, đang áp dụng…",
  done: "xong", rejected: "bị từ chối", expired: "hết hạn (Pi không nhận)" };
const CMD_TEXT = { "mode:auto": "Về tự động", "mode:manual": "Chuyển thủ công",
  "fan:on": "Bật quạt", "fan:off": "Tắt quạt", "buzzer:on": "Bật còi", "buzzer:off": "Giữ còi im" };

const $ = (id) => document.getElementById(id);
let state = { loggedIn: false, manual: false, online: false, snap: null };
let histChart = null;

function fmtTime(ts, sec) {
  if (ts == null) return "—";
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString("vi-VN", { hour: "2-digit", minute: "2-digit", second: sec ? "2-digit" : undefined });
}
function fmtDateTime(ts) {
  if (ts == null) return "—";
  return new Date(ts * 1000).toLocaleString("vi-VN", { hour: "2-digit", minute: "2-digit", day: "2-digit", month: "2-digit" });
}
function fmtAge(s) {
  if (s == null) return "—";
  if (s < 60) return `${Math.round(s)} giây`;
  if (s < 3600) return `${Math.round(s / 60)} phút`;
  if (s < 86400) return `${(s / 3600).toFixed(1)} giờ`;
  return `${(s / 86400).toFixed(1)} ngày`;
}
function num(v, d = 1, unit = "") {
  return v == null ? "—" : `${Number(v).toFixed(d)}${unit ? " " + unit : ""}`;
}
function setText(id, text) { $(id).textContent = text; }

async function api(path, opts = {}) {
  const res = await fetch(path, { credentials: "same-origin", ...opts });
  let body = null;
  try { body = await res.json(); } catch (_) { /* không phải JSON */ }
  if (!res.ok) {
    const msg = body && body.detail ? (typeof body.detail === "string" ? body.detail : "Dữ liệu không hợp lệ") : `Lỗi ${res.status}`;
    throw Object.assign(new Error(msg), { status: res.status });
  }
  return body;
}

// ---------------- trạng thái ----------------
function renderStatus(st) {
  state.loggedIn = st.logged_in;
  state.online = st.pi_online;
  state.snap = st.snapshot;
  const conn = $("conn");
  conn.className = "pill " + (st.pi_online ? "ok" : "bad");
  conn.textContent = st.pi_online ? "Pi đang kết nối" : "Pi mất kết nối";
  const off = $("offline");
  if (!st.pi_online) {
    off.classList.remove("hidden");
    off.textContent = st.last_seen
      ? `Pi gửi dữ liệu lần cuối lúc ${fmtDateTime(st.last_seen)} (cách đây ${fmtAge(st.server_time - st.last_seen)}). Số liệu bên dưới là số liệu cũ; lệnh sẽ hết hạn nếu Pi không nhận.`
      : "Chưa nhận được dữ liệu nào từ Pi.";
  } else off.classList.add("hidden");

  $("loginForm").classList.toggle("hidden", st.logged_in);
  $("logoutBtn").classList.toggle("hidden", !st.logged_in);

  const s = st.snapshot;
  if (!s) { renderControls(); return; }
  setText("pm25", num(s.pm25, 1, "µg/m³"));
  setText("pm10", `${num(s.pm10, 0)} / ${num(s.pm1, 0)} µg/m³`);
  setText("temp", num(s.temp, 1, "°C"));
  setText("hum", num(s.hum, 0, "%"));
  setText("lux", num(s.lux, 0, "lux"));
  const aqi = $("aqi");
  if (s.aqi) { aqi.textContent = s.aqi.label; aqi.style.background = AQI_COLORS[s.aqi.index] || "#888"; aqi.classList.remove("hidden"); }
  else aqi.classList.add("hidden");

  const src = s.source || {};
  const card = $("sourceCard");
  const info = SOURCE[src.label];
  if (!src.enabled) {
    setText("sourceLabel", "Bộ nhận diện đang tắt"); setText("sourceAction", "Dùng luật ngưỡng PM2.5");
    card.style.borderLeftColor = "var(--muted)";
  } else if (info && src.fresh) {
    setText("sourceLabel", info.name); setText("sourceAction", `Hành động: ${info.action}`);
    card.style.borderLeftColor = info.color;
  } else {
    setText("sourceLabel", src.fresh ? "Chưa đủ tin cậy" : "Chưa có dự đoán mới");
    setText("sourceAction", "Dùng luật ngưỡng PM2.5 cho quạt và còi");
    card.style.borderLeftColor = "var(--muted)";
  }
  const proba = $("proba");
  proba.innerHTML = "";
  if (src.proba) {
    for (const key of Object.keys(SOURCE)) {
      const p = src.proba[key] ?? 0;
      const row = document.createElement("div");
      row.className = "bar";
      row.innerHTML = `<span></span><div class="track"><div class="fill"></div></div><span></span>`;
      row.children[0].textContent = SOURCE[key].name;
      row.querySelector(".fill").style.width = `${Math.round(p * 100)}%`;
      row.querySelector(".fill").style.background = SOURCE[key].color;
      row.children[2].textContent = `${Math.round(p * 100)}%`;
      proba.appendChild(row);
    }
  }
  setText("sourceNote", src.last_ts
    ? `Dự đoán lúc ${fmtTime(src.last_ts, true)}${src.since && info ? ` · nhãn này từ ${fmtTime(src.since)}` : ""} · mẫu lúc ${fmtTime(s.ts, true)}`
    : "");
  renderControls();
}

function renderControls() {
  const s = state.snap;
  const c = (s && s.control) || {};
  const b = (s && s.buzzer) || {};
  state.manual = c.mode === "manual";
  for (const btn of document.querySelectorAll("#modeSeg button")) {
    btn.classList.toggle("active", s && btn.dataset.value === c.mode);
    btn.disabled = !state.loggedIn;
  }
  setText("modeNote", !s ? "" : state.manual
    ? "Thủ công: quạt/còi theo nút bấm, có thời hạn rồi tự về tự động."
    : "Tự động: AI nhận diện nguồn bụi và luật PM2.5 quyết định.");
  setText("fanState", !s ? "—" : c.fan_on ? "Đang quay" : "Đang tắt");
  setText("fanNote", !s ? "" : c.fan_override
    ? `Ghi đè ${c.fan_override.on ? "bật" : "tắt"} tới ${fmtTime(c.fan_override.until)}`
    : `Luật tự động: ${c.rule_mode === "PROTECT" ? "BẢO VỆ" : "BÌNH THƯỜNG"}${c.fault ? " · cảm biến PM lỗi" : ""}`);
  let bz = "—", bzNote = "";
  if (s) {
    if (!b.enabled) { bz = "Tắt trong cấu hình"; }
    else if (b.override) {
      bz = b.override.on ? "Đang kêu (thủ công)" : "Giữ im (thủ công)";
      bzNote = `Tới ${fmtTime(b.override.until, !!b.override.on)}`;
    } else if (b.sounding) { bz = "Đang báo vượt ngưỡng"; bzNote = "Kêu từng hồi, lặp lại khi còn vượt"; }
    else if (b.muted_until) { bz = "Tắt tiếng"; bzNote = `Tới ${fmtTime(b.muted_until)}`; }
    else bz = "Im";
  }
  setText("buzzerState", bz);
  setText("buzzerNote", bzNote);
  for (const btn of document.querySelectorAll(".manual-only")) {
    btn.disabled = !state.loggedIn || !state.manual || (btn.dataset.kind === "buzzer" && !b.enabled);
    const active = btn.dataset.kind === "fan" ? (c.fan_override && (c.fan_override.on === (btn.dataset.value === "on")))
      : (b.override && (b.override.on === (btn.dataset.value === "on")));
    btn.classList.toggle("active", !!active);
  }
  if (!state.loggedIn) setText("cmdMsg", "Đăng nhập bằng mật khẩu nhóm để điều khiển. Nút quạt và còi chỉ bấm được ở chế độ Thủ công.");
  else if (!state.manual && !$("cmdMsg").dataset.keep) setText("cmdMsg", "Chuyển sang Thủ công để bấm quạt và còi.");
}

function renderCommands(rows) {
  const ul = $("cmdList");
  ul.innerHTML = "";
  for (const r of rows.slice(0, 5)) {
    const li = document.createElement("li");
    const cls = r.status === "done" ? "okc" : (r.status === "expired" || r.status === "rejected") ? "err" : "";
    li.innerHTML = `<span></span> — <span class="${cls}"></span>`;
    li.children[0].textContent = `${fmtTime(r.created_at, true)} ${CMD_TEXT[`${r.kind}:${r.value}`] || r.kind}`;
    li.children[1].textContent = STATUS_TEXT[r.status] + (r.detail && r.status !== "done" ? ` (${r.detail})` : "");
    ul.appendChild(li);
  }
}

async function sendCommand(kind, value) {
  const msg = $("cmdMsg");
  msg.dataset.keep = "1";
  msg.className = "small";
  msg.textContent = "Đang gửi lệnh…";
  try {
    const r = await api("/api/command", {
      method: "POST", headers: { "Content-Type": "application/json", "X-Requested-With": "aiot" },
      body: JSON.stringify({ kind, value }),
    });
    msg.textContent = r.note || "Đã gửi, chờ Pi áp dụng (thường dưới 10 giây).";
    if (r.note) msg.className = "small err";
  } catch (e) {
    msg.className = "small err";
    msg.textContent = e.message;
    if (e.status === 401) await refreshStatus();
  }
  setTimeout(() => { delete msg.dataset.keep; }, 8000);
  refreshCommands();
}

// ---------------- biểu đồ ----------------
function chartColors() {
  const dark = matchMedia("(prefers-color-scheme: dark)").matches;
  return { grid: dark ? "#2a333d" : "#e3e7ec", text: dark ? "#94a0ad" : "#66727f" };
}

// Plugin tô nền theo nhãn AI của từng điểm
const labelBands = {
  id: "labelBands",
  beforeDatasetsDraw(chart) {
    const labels = chart.$aiLabels;
    if (!labels) return;
    const { ctx, chartArea, scales } = chart;
    const n = labels.length;
    ctx.save();
    for (let i = 0; i < n; i++) {
      const info = SOURCE[labels[i]];
      if (!info || labels[i] === "normal") continue;
      const x0 = scales.x.getPixelForValue(i - 0.5), x1 = scales.x.getPixelForValue(i + 0.5);
      ctx.fillStyle = info.color + "33";
      ctx.fillRect(Math.max(x0, chartArea.left), chartArea.top, Math.min(x1, chartArea.right) - Math.max(x0, chartArea.left), chartArea.bottom - chartArea.top);
    }
    ctx.restore();
  },
};

function baseOptions() {
  const c = chartColors();
  return {
    responsive: true, maintainAspectRatio: false, animation: false,
    interaction: { mode: "index", intersect: false },
    plugins: { legend: { labels: { color: c.text } } },
    scales: {
      x: { ticks: { color: c.text, maxTicksLimit: 8 }, grid: { color: c.grid } },
      y: { beginAtZero: true, ticks: { color: c.text }, grid: { color: c.grid }, title: { display: true, text: "µg/m³", color: c.text } },
    },
  };
}

function renderHistory(rows) {
  if (typeof Chart === "undefined") return;
  const labels = rows.map((r) => fmtTime(r.ts));
  const data = {
    labels,
    datasets: [
      { label: "PM2.5", data: rows.map((r) => r.pm25), borderColor: "#2f6fdb", borderWidth: 2, pointRadius: 0, tension: .2 },
      { label: "PM10", data: rows.map((r) => r.pm10), borderColor: "#9aa6b2", borderWidth: 1, pointRadius: 0, tension: .2 },
    ],
  };
  if (!histChart) histChart = new Chart($("histChart"), { type: "line", data, options: baseOptions(), plugins: [labelBands] });
  else { histChart.data = data; }
  histChart.$aiLabels = rows.map((r) => r.label);
  histChart.update();
  const legend = $("legend");
  legend.innerHTML = "";
  for (const key of ["cooking", "pollution"]) {
    const sp = document.createElement("span");
    sp.innerHTML = `<i></i>`;
    sp.querySelector("i").style.background = SOURCE[key].color + "66";
    sp.append(SOURCE[key].name);
    legend.appendChild(sp);
  }
}

const EVENT_TEXT = {
  fan_on: "Quạt bật", fan_off: "Quạt tắt", override: "Ghi đè quạt", override_expired: "Hết ghi đè quạt",
  buzzer: "Còi kêu", buzzer_override: "Ghi đè còi", source_change: "Đổi nguồn bụi", command: "Lệnh",
  sensor_fault: "Lỗi cảm biến PM", sensor_recovered: "Cảm biến PM hoạt động lại", danger: "Khẩn cấp",
  relay_error: "Lỗi relay", relay_recovered: "Relay hoạt động lại", controller_stop: "Controller dừng",
};
function renderEvents(rows) {
  const ul = $("events");
  ul.innerHTML = "";
  if (!rows.length) { ul.innerHTML = "<li class='muted'>Chưa có sự kiện.</li>"; return; }
  for (const r of rows) {
    const li = document.createElement("li");
    li.textContent = `${fmtDateTime(r.ts)} · ${EVENT_TEXT[r.kind] || r.kind}${r.detail ? " — " + r.detail : ""}`;
    ul.appendChild(li);
  }
}

// ---------------- vòng cập nhật ----------------
async function refreshStatus() {
  try { renderStatus(await api("/api/status")); }
  catch (e) { const c = $("conn"); c.className = "pill bad"; c.textContent = "Không kết nối được server"; }
}
async function refreshCommands() {
  try { renderCommands((await api("/api/commands?limit=5")).rows); } catch (_) { /* bỏ qua */ }
}
async function refreshSlow() {
  try {
    const rows = (await api("/api/history?hours=6")).rows;
    renderHistory(rows);
    if (!rows.length) setTimeout(refreshSlow, 5000);   // Pi chưa gửi: thử lại sớm
  } catch (_) {}
  try { renderEvents((await api("/api/events?limit=30")).rows); } catch (_) {}
}

$("loginForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const msg = $("cmdMsg");
  try {
    await api("/api/login", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: $("password").value }) });
    $("password").value = "";
    msg.className = "small okc"; msg.textContent = "Đã đăng nhập.";
  } catch (e) { msg.className = "small err"; msg.textContent = e.message; }
  refreshStatus();
});
$("logoutBtn").addEventListener("click", async () => { await api("/api/logout", { method: "POST" }); refreshStatus(); });
for (const btn of document.querySelectorAll("button[data-kind]")) {
  btn.addEventListener("click", () => sendCommand(btn.dataset.kind, btn.dataset.value));
}

refreshStatus(); refreshCommands(); refreshSlow();
setInterval(() => { refreshStatus(); refreshCommands(); }, 5000);
setInterval(refreshSlow, 30000);
