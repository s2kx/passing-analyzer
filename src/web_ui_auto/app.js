// 全自動実行GUI（auto_gui.py）のフロントエンド。
// 画面は2つ: 実行（run_pipeline を起動して進捗を見る）と、
// 確認・修正（候補ごとにクリップを見て採否・車種・危険感・車線数を直す）。

const state = {
  settings: {}, initialized: false, playbackRate: 1,
  events: [], selected: -1, out: '', offset: 0, summary: null, dirty: false,
  pipeline: { polling: false },
  stage: 'clip',                                   // プレビューのタブ: clip / cloud
};
// 点群ビュー（候補IDごとのデータキャッシュと表示中ビューア）
const cloud = { cache: new Map(), viewer: null, request: 0 };

const DANGER_LABELS = { '0': '', '1': 'やや危険', '2': '危険', '3': '非常に危険' };
const VIEW_LABELS = { front: '前方', rear: '後方' };
const PIPELINE_STATUS_LABELS = { ok: '完了', skipped: 'スキップ', warning: '警告あり', failed: '失敗' };

const demoApi = {
  async get_settings() { return {}; },
  async save_settings() { return { ok: true }; },
  async choose_folder() { return { ok: false, error: 'フォルダ選択はデスクトップアプリでのみ利用できます' }; },
  async start_pipeline() { return { ok: false, error: '全自動実行はデスクトップアプリでのみ利用できます' }; },
  async get_pipeline_state() { return { running: false, progress: 0, log: '待機中', message: '待機中', exit_code: null, stages: [], out: '', summary: null }; },
  async cancel_pipeline() { return { ok: false, error: '実行中の処理はありません' }; },
  async load_results() { return { ok: false, error: '結果の読み込みはデスクトップアプリでのみ利用できます' }; },
  async get_event_points() { return { ok: false, error: '点群表示はデスクトップアプリでのみ利用できます' }; },
  async save_reviews() { return { ok: false, error: '保存はデスクトップアプリでのみ利用できます' }; },
  async generate_final_excel() { return { ok: false, error: '最終Excel生成はデスクトップアプリでのみ利用できます' }; },
  async open_path() { return { ok: false, error: 'デスクトップアプリでのみ利用できます' }; },
};
const api = () => window.pywebview?.api || demoApi;
const $ = id => document.getElementById(id);
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

function toast(message, error = false) {
  const el = $('toast'); el.textContent = message; el.className = `toast show${error ? ' error' : ''}`;
  clearTimeout(toast.timer); toast.timer = setTimeout(() => el.className = 'toast', 3200);
}
function setStatus(message, busy = false) {
  $('globalStatus').innerHTML = `<span></span>${message}`;
  $('globalStatus').classList.toggle('busy', busy);
}
function escapeHtml(value = '') { const div = document.createElement('div'); div.textContent = value; return div.innerHTML; }
function formatMinSec(value) {
  const total = Math.round(Number(value) || 0);
  const sign = total < 0 ? '-' : ''; const t = Math.abs(total);
  return `${sign}${Math.floor(t / 60)}分${t % 60}秒`;
}
function goStep(name) {
  document.querySelectorAll('.step').forEach(x => x.classList.toggle('active', x.dataset.step === name));
  document.querySelectorAll('.panel').forEach(x => x.classList.toggle('active', x.id === `panel-${name}`));
}

// ── 実行画面 ─────────────────────────────────────────────────────────────────

function formData() {
  return {
    session: $('sessionPath').value,
    out: $('outPath').value,
    view: document.querySelector('[name=pipelineView]:checked')?.value || '',
    subject: $('pipelineSubject').value,
    date: $('pipelineDate').value,
    organize_id: $('pipelineOrganizeId').value,
    detect_mode: $('pipelineDetectMode').value,
    lane_count: $('pipelineLane').value,
    offset: $('pipelineOffset').value,
    extra: $('pipelineExtra').value,
  };
}
async function saveSettings() { await api().save_settings(formData()); }

async function pick(kind) {
  const result = await api().choose_folder(kind);
  if (!result.ok) { if (!result.cancelled) toast(result.error || '選択できませんでした', true); return; }
  $(kind === 'session' ? 'sessionPath' : 'outPath').value = result.path;
  await saveSettings();
}

function pipelineStatusBadge(status) {
  return status === 'ok' ? 'accepted' : status === 'failed' ? 'rejected' : status === 'warning' ? 'warn' : '';
}
function renderPipelineStages(stages) {
  const el = $('pipelineStages');
  if (!stages || !stages.length) { el.innerHTML = '<p class="empty">段階の完了ごとにここへ表示されます</p>'; return; }
  el.innerHTML = stages.map(s => `<div class="pipeline-stage"><span class="badge ${pipelineStatusBadge(s.status)}">${escapeHtml(PIPELINE_STATUS_LABELS[s.status] || s.status)}</span><strong>${escapeHtml(s.title || '')}</strong><span>${escapeHtml(s.message || '')}</span></div>`).join('');
  el.scrollTop = el.scrollHeight;
}
function setPipelineRunning(running) {
  $('startPipeline').disabled = running;
  $('cancelPipeline').disabled = !running;
}
async function startPipeline() {
  if (!$('sessionPath').value) { toast('セッションフォルダを選択してください', true); return; }
  if (state.pipeline.polling) { toast('全自動実行はすでに実行中です', true); return; }
  await saveSettings();
  const result = await api().start_pipeline(formData());
  if (!result.ok) { toast(result.error || '全自動実行を開始できませんでした', true); return; }
  renderPipelineStages([]);
  setPipelineRunning(true); setStatus('全自動実行中', true);
  state.pipeline.polling = true; pollPipeline();
  toast('全自動実行を開始しました');
}
async function pollPipeline() {
  if (!state.pipeline.polling) return;
  const info = await api().get_pipeline_state();
  $('pipelineBar').style.width = `${info.progress || 0}%`;
  $('pipelineProgressLabel').textContent = `${info.progress || 0}%`;
  $('pipelineMessage').textContent = info.message || '';
  $('pipelineLog').textContent = info.log || '';
  $('pipelineLog').scrollTop = $('pipelineLog').scrollHeight;
  renderPipelineStages(info.stages || []);
  if (info.running) { setTimeout(pollPipeline, 800); return; }
  state.pipeline.polling = false; setPipelineRunning(false); setStatus('準備完了');
  if (info.exit_code === 0) {
    toast('全自動実行が完了しました。確認画面へ移動します');
    await loadResults(info.out);          // 完了したら候補ごとの確認画面を開く
  } else if (info.exit_code !== null) {
    toast(info.message || '全自動実行でエラーが発生しました', true);
  }
}
async function cancelPipeline() {
  const result = await api().cancel_pipeline();
  if (!result.ok) { toast(result.error || '中止できませんでした', true); return; }
  $('cancelPipeline').disabled = true;
  toast('全自動実行を中止しています…');
}
async function openExisting() {
  const picked = await api().choose_folder('load');
  if (!picked.ok) { if (!picked.cancelled) toast(picked.error || '選択できませんでした', true); return; }
  await loadResults(picked.path);
}

// ── 点群ビュー（report.html のビューアを確認画面用に移植）────────────────────
// Intensity は 0〜255、Z は世界座標(m)。どちらも青→緑→黄→赤の連続色に写す。
function cloudRamp(t) {
  t = t < 0 ? 0 : (t > 1 ? 1 : t);
  let r, g, b;
  if (t < 0.34) { const u = t / 0.34; r = 30; g = 80 + 150 * u; b = 230 - 60 * u; }
  else if (t < 0.67) { const v = (t - 0.34) / 0.33; r = 30 + 210 * v; g = 230; b = 170 - 150 * v; }
  else { const w = (t - 0.67) / 0.33; r = 240; g = 230 - 170 * w; b = 20; }
  return `rgb(${r | 0},${g | 0},${b | 0})`;
}
function cloudNiceStep(span) {
  const raw = span / 6, pow = Math.pow(10, Math.floor(Math.log(raw) / Math.LN10));
  const n = raw / pow;
  return (n >= 5 ? 5 : (n >= 2 ? 2 : 1)) * pow;
}
// 世界座標 → 画面座標。両軸とも同じ m/px（等スケール）で、指定範囲が収まるように取る。
function cloudProjection(canvas, hmin, hmax, vmin, vmax, pad) {
  const w = canvas.width, h = canvas.height;
  const hs = Math.max(hmax - hmin, 0.5), vs = Math.max(vmin === vmax ? 0.5 : vmax - vmin, 0.5);
  const scale = Math.min((w - 2 * pad) / hs, (h - 2 * pad) / vs);
  const hc = (hmin + hmax) / 2, vc = (vmin + vmax) / 2;
  return {
    scale,
    sx: hv => w / 2 + (hv - hc) * scale,
    sy: vv => h / 2 - (vv - vc) * scale,
    hmin: hc - (w / 2) / scale, hmax: hc + (w / 2) / scale,
    vmin: vc - (h / 2) / scale, vmax: vc + (h / 2) / scale,
    w, h,
  };
}
function drawCloudGrid(ctx, pr, hlabel, vlabel) {
  const step = cloudNiceStep(Math.max(pr.hmax - pr.hmin, pr.vmax - pr.vmin));
  ctx.lineWidth = 1;
  ctx.font = '11px monospace';
  ctx.textBaseline = 'top';
  for (let v = Math.ceil(pr.hmin / step) * step; v <= pr.hmax; v += step) {
    const x = pr.sx(v);
    ctx.strokeStyle = Math.abs(v) < 1e-9 ? '#4a5568' : '#232a33';
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, pr.h); ctx.stroke();
    ctx.fillStyle = '#6b7684';
    ctx.fillText(v.toFixed(step < 1 ? 1 : 0), x + 3, pr.h - 15);
  }
  for (let v = Math.ceil(pr.vmin / step) * step; v <= pr.vmax; v += step) {
    const y = pr.sy(v);
    ctx.strokeStyle = Math.abs(v) < 1e-9 ? '#4a5568' : '#232a33';
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(pr.w, y); ctx.stroke();
    ctx.fillStyle = '#6b7684';
    ctx.fillText(v.toFixed(step < 1 ? 1 : 0), 4, y + 2);
  }
  ctx.fillStyle = '#8b97a6';
  ctx.font = '12px sans-serif';
  ctx.fillText(hlabel, pr.w - 150, 6);
  ctx.fillText(vlabel, 6, 6);
  const bx = pr.w - 20 - step * pr.scale, by = pr.h - 26;
  ctx.strokeStyle = '#8b97a6'; ctx.lineWidth = 2;
  ctx.beginPath(); ctx.moveTo(bx, by); ctx.lineTo(bx + step * pr.scale, by); ctx.stroke();
  ctx.fillStyle = '#8b97a6'; ctx.font = '11px monospace';
  ctx.fillText(`${step} m`, bx, by - 15);
}
// 通過区間 |Y| < 3m の帯。横軸はどちらのビューも Y なので同じ描き方でよい。
function drawCloudPassBand(ctx, pr) {
  const x1 = pr.sx(-3), x2 = pr.sx(3);
  ctx.fillStyle = 'rgba(80,140,255,0.10)';
  ctx.fillRect(Math.min(x1, x2), 0, Math.abs(x2 - x1), pr.h);
  ctx.strokeStyle = 'rgba(120,170,255,0.55)';
  ctx.setLineDash([5, 4]); ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(x1, 0); ctx.lineTo(x1, pr.h); ctx.stroke();
  ctx.beginPath(); ctx.moveTo(x2, 0); ctx.lineTo(x2, pr.h); ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle = 'rgba(150,190,255,0.9)'; ctx.font = '11px sans-serif';
  ctx.fillText('通過区間 |Y|<3m', Math.min(x1, x2) + 4, 22);
}
function drawCloudBike(ctx, pr) {
  const x = pr.sx(0), y = pr.sy(0);
  ctx.strokeStyle = '#ff5f8d'; ctx.lineWidth = 1.5;
  ctx.beginPath(); ctx.arc(x, y, 7, 0, Math.PI * 2); ctx.stroke();
  ctx.beginPath(); ctx.moveTo(x - 11, y); ctx.lineTo(x + 11, y);
  ctx.moveTo(x, y - 11); ctx.lineTo(x, y + 11); ctx.stroke();
  ctx.fillStyle = '#ff8fb0'; ctx.font = '11px sans-serif';
  ctx.fillText('自転車(原点)', x + 12, y - 16);
}
function renderCloudView(st, key) {
  const canvas = st.canvas[key], ctx = canvas.getContext('2d');
  const d = st.data, fr = d.frames[st.frame];
  const top = (key === 'top');
  const b = d.bounds;
  // 横軸は両ビューとも Y（前方+）。縦軸は上面図が X（右側+）、側面図が Z（上+）。
  const hmin = Math.min(b.y[0], -4) - 1, hmax = Math.max(b.y[1], 4) + 1;
  let vmin, vmax;
  if (top) { vmin = Math.min(b.x[0], -1) - 1; vmax = Math.max(b.x[1], 1) + 1; }
  else { vmin = Math.min(b.z[0], -2) - 0.5; vmax = Math.max(b.z[1], 1) + 0.5; }
  const pr = cloudProjection(canvas, hmin, hmax, vmin, vmax, 8);
  ctx.fillStyle = '#0d1117'; ctx.fillRect(0, 0, pr.w, pr.h);
  drawCloudGrid(ctx, pr, '→ Y 前方 [m]', top ? '↑ X 右側 [m]' : '↑ Z 高さ [m]');
  drawCloudPassBand(ctx, pr);

  // 離隔距離 LC の補助線（上面図のみ。X = LC の高さ）
  if (top && typeof d.lc === 'number' && isFinite(d.lc)) {
    const ly = pr.sy(d.lc);
    ctx.strokeStyle = '#ffd45e'; ctx.setLineDash([7, 5]); ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.moveTo(0, ly); ctx.lineTo(pr.w, ly); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = '#ffd45e'; ctx.font = '11px sans-serif';
    ctx.fillText(`LC ${d.lc.toFixed(2)} m`, 8, ly - 14);
  }

  // 軌跡（edit CSV の最近接点）とフェーズ点
  if (d.traj && d.traj.length) {
    ctx.strokeStyle = 'rgba(255,255,255,0.30)'; ctx.lineWidth = 1;
    ctx.beginPath();
    d.traj.forEach((p, i) => {
      const px = pr.sx(p.y), py = pr.sy(top ? p.x : p.z);
      if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
    });
    ctx.stroke();
    for (const p of d.traj) {
      if (!p.ph) continue;
      const qx = pr.sx(p.y), qy = pr.sy(top ? p.x : p.z);
      ctx.fillStyle = '#ffffff';
      ctx.beginPath(); ctx.arc(qx, qy, 3, 0, Math.PI * 2); ctx.fill();
      ctx.font = '10px sans-serif';
      ctx.fillText(String(p.ph).replace(/フェーズ/g, 'P'), qx + 5, qy - 12);
    }
  }

  // 点群
  if (fr) {
    const p3 = fr.p, inten = fr.i, n = inten.length;
    const byZ = (st.color === 'z');
    let lo, hi;
    if (byZ) { lo = vmin; hi = vmax; }
    else {
      // Intensity の実効範囲はフレームごとに違うので、フレーム内の最大値で正規化する
      lo = 0; hi = 1;
      for (let m = 0; m < n; m++) { if (inten[m] > hi) hi = inten[m]; }
    }
    const size = pr.scale > 26 ? 2.4 : (pr.scale > 13 ? 1.8 : 1.3);
    for (let i = 0; i < n; i++) {
      const wx = p3[i * 3], wy = p3[i * 3 + 1], wz = p3[i * 3 + 2];
      const sxp = pr.sx(wy), syp = pr.sy(top ? wx : wz);
      if (sxp < -4 || sxp > pr.w + 4 || syp < -4 || syp > pr.h + 4) continue;
      const val = byZ ? wz : inten[i];
      ctx.fillStyle = cloudRamp((val - lo) / (hi - lo || 1));
      ctx.fillRect(sxp - size / 2, syp - size / 2, size, size);
    }
  }
  drawCloudBike(ctx, pr);
}
function renderCloudBoth(st) { renderCloudView(st, 'top'); renderCloudView(st, 'side'); }
function setCloudFrame(st, index) {
  const n = st.data.frames.length;
  if (!n) return;
  st.frame = ((index % n) + n) % n;
  st.range.value = String(st.frame);
  const fr = st.data.frames[st.frame];
  st.pos.textContent = `フレーム ${fr.f} (${st.frame + 1}/${n}) t=${fr.t.toFixed(2)}s ${fr.i.length}点`;
  renderCloudBoth(st);
}
function stopCloudPlay(st) {
  if (!st) return;
  st.playing = false;
  if (st.timer) { clearInterval(st.timer); st.timer = null; }
  if (st.play) st.play.textContent = '▶ 再生';
}
function toggleCloudPlay(st) {
  if (!st) return;
  if (st.playing) { stopCloudPlay(st); return; }
  st.playing = true;
  st.play.textContent = '■ 停止';
  // LiDAR は 10Hz。等速で送ると実時間相当になる。
  st.timer = setInterval(() => setCloudFrame(st, st.frame + 1), 100);
}
function buildCloudViewer(host, data) {
  host.innerHTML = '';
  if (!data.frames || !data.frames.length) {
    host.innerHTML = '<p class="cloud-empty">この候補には点群データがありません</p>';
    return null;
  }
  const st = { data, frame: 0, playing: false, timer: null, color: 'i', canvas: {} };
  const views = document.createElement('div');
  views.className = 'cloud-views';
  // 表示幅に合わせて解像度を決める（CSS で 100% に伸ばすので、低いとぼやける）
  const wide = Math.max(500, Math.min(1200, host.clientWidth || 700));
  [['top', '上面図（X-Y）　横=Y 前方 / 縦=X 右側'],
   ['side', '側面図（Y-Z）　横=Y 前方 / 縦=Z 高さ']].forEach(pair => {
    const box = document.createElement('div');
    box.className = 'cloud-view';
    const cap = document.createElement('div');
    cap.className = 'cloud-cap'; cap.textContent = pair[1];
    const cv = document.createElement('canvas');
    cv.width = wide; cv.height = Math.round(wide * 0.42);
    box.appendChild(cap); box.appendChild(cv); views.appendChild(box);
    st.canvas[pair[0]] = cv;
  });
  host.appendChild(views);

  const bar = document.createElement('div');
  bar.className = 'cloud-player';
  const play = document.createElement('button');
  play.type = 'button'; play.className = 'secondary cloud-btn'; play.textContent = '▶ 再生';
  const range = document.createElement('input');
  range.type = 'range'; range.min = '0'; range.value = '0';
  range.max = String(data.frames.length - 1); range.step = '1';
  const pos = document.createElement('span');
  pos.className = 'cloud-pos';
  const label = document.createElement('label');
  label.className = 'cloud-color';
  const sel = document.createElement('select');
  sel.innerHTML = '<option value="i">Intensity</option><option value="z">高さZ</option>';
  label.appendChild(document.createTextNode('色 '));
  label.appendChild(sel);
  bar.appendChild(play); bar.appendChild(range); bar.appendChild(pos); bar.appendChild(label);
  host.appendChild(bar);

  const legend = document.createElement('div');
  legend.className = 'cloud-legend';
  legend.innerHTML = `点 ${data.points.toLocaleString()} / 元 ${data.rawPoints.toLocaleString()}`
    + `（1フレーム上限 ${data.perFrame} 点に間引き）`
    + '<i style="background:#ff5f8d"></i>自転車=原点'
    + '<i style="background:#ffd45e"></i>離隔距離LC'
    + '<i style="background:rgba(120,170,255,.8)"></i>通過区間|Y|&lt;3m'
    + '<i style="background:#fff"></i>フェーズ点';
  host.appendChild(legend);

  st.play = play; st.range = range; st.pos = pos;
  play.addEventListener('click', () => toggleCloudPlay(st));
  range.addEventListener('input', () => { stopCloudPlay(st); setCloudFrame(st, parseInt(range.value, 10)); });
  sel.addEventListener('change', () => { st.color = sel.value; renderCloudBoth(st); });
  setCloudFrame(st, 0);
  return st;
}
function setStage(name) {
  state.stage = name;
  document.querySelectorAll('.stage-tab').forEach(b => b.classList.toggle('active', b.dataset.stage === name));
  $('videoStage').hidden = name !== 'clip';
  $('cloudStage').hidden = name !== 'cloud';
  if (name === 'cloud') { $('clipPlayer').pause(); ensureCloud(); }
  else stopCloudPlay(cloud.viewer);
}
// 表示中の候補の点群を（未取得なら読み込んで）点群タブへ出す
async function ensureCloud() {
  const host = $('cloudHost');
  stopCloudPlay(cloud.viewer); cloud.viewer = null;
  const event = state.events[state.selected];
  if (!event) { host.innerHTML = '<p class="cloud-empty">候補を選択すると点群を表示できます</p>'; return; }
  const id = event.event_id;
  const cached = cloud.cache.get(id);
  if (cached) { cloud.viewer = buildCloudViewer(host, cached); return; }
  const request = ++cloud.request;
  host.innerHTML = '<p class="cloud-empty">点群を読み込んでいます…</p>';
  let result;
  try { result = await api().get_event_points({ out: state.out, event_id: id }); }
  catch (err) { result = { ok: false, error: String(err && err.message || err) }; }
  if (request !== cloud.request || state.stage !== 'cloud') return;   // 別候補へ移動済み
  if (!result.ok) {
    host.innerHTML = `<p class="cloud-empty">${escapeHtml(result.error || '点群を読み込めませんでした')}</p>`;
    return;
  }
  cloud.cache.set(id, result.data);
  cloud.viewer = buildCloudViewer(host, result.data);
}

// ── 確認・修正画面 ───────────────────────────────────────────────────────────

async function loadResults(outValue) {
  const result = await api().load_results(outValue || '');
  if (!result.ok) { toast(result.error || '結果を読み込めませんでした', true); return false; }
  state.events = result.events || [];
  state.out = result.out || '';
  state.offset = Number(result.offset || 0);
  state.summary = result.summary || null;
  state.dirty = false;
  state.selected = -1;
  cloud.cache.clear(); stopCloudPlay(cloud.viewer); cloud.viewer = null;
  setStage('clip');
  renderSummary(); renderEvents();
  goStep('confirm');
  const first = firstUnconfirmedIndex();
  selectEvent(first >= 0 ? first : (state.events.length ? 0 : -1), false);
  $('finalStatus').textContent = '';
  toast(`結果を読み込みました（候補 ${state.events.length} 件）`);
  return true;
}
function firstUnconfirmedIndex() {
  return state.events.findIndex(e => e.review_status === '未確認');
}
function markDirty() {
  state.dirty = true;
  setStatus('未保存の修正があります');
}
function renderSummary() {
  const total = state.events.length;
  const accepted = state.events.filter(e => e.review_status === '採用').length;
  const rejected = state.events.filter(e => e.review_status === '除外').length;
  const unreviewed = total - accepted - rejected;
  $('summaryCounts').textContent = total
    ? `候補 ${total} 件 — 採用 ${accepted} / 未確認 ${unreviewed} / 除外 ${rejected}`
    : '結果未読込';
  const summary = state.summary;
  const meta = [];
  if (summary?.view) meta.push(`撮影方向 ${VIEW_LABELS[summary.view] || summary.view}`);
  meta.push(`時刻オフセット ${state.offset >= 0 ? '+' : ''}${state.offset.toFixed(3)} 秒`);
  if (summary?.elapsed_seconds != null) meta.push(`実行 ${formatMinSec(summary.elapsed_seconds)}`);
  $('summaryMeta').textContent = meta.join(' ・ ');
  $('openReport').disabled = !(summary && summary.report_html);
  const items = [
    ...(summary?.manual_actions || []).map(m => ({ label: '人手', text: m, cls: 'manual' })),
    ...(summary?.warnings || []).map(w => ({ label: '警告', text: w, cls: 'warn' })),
  ];
  $('manualList').innerHTML = items.length
    ? items.map(i => `<div class="manual-item ${i.cls}"><span class="badge ${i.cls === 'warn' ? 'warn' : ''}">${i.label}</span><span>${escapeHtml(i.text)}</span></div>`).join('')
    : '<p class="empty">項目はありません</p>';
}
function renderEvents() {
  $('eventCount').textContent = state.events.length;
  $('reviewedCount').textContent = state.events.filter(e => e.review_status !== '未確認').length;
  if (!state.events.length) {
    $('eventRows').innerHTML = '<tr><td colspan="6" class="empty">実行画面から結果を読み込んでください</td></tr>';
    return;
  }
  $('eventRows').innerHTML = state.events.map((event, i) => {
    const cls = event.review_status === '採用' ? 'accepted' : event.review_status === '除外' ? 'rejected' : '';
    const danger = DANGER_LABELS[String(event.danger_level)] || '';
    const lane = String(event.lane_count || '');
    return `<tr class="event-row ${i === state.selected ? 'selected' : ''}" data-index="${i}">`
      + `<td>${escapeHtml(event.event_id)}</td>`
      + `<td>${escapeHtml(formatMinSec(event.t_start_s))}</td>`
      + `<td>${escapeHtml(event.class)}</td>`
      + `<td>${danger ? escapeHtml(danger) : '<span class="unset">—</span>'}</td>`
      + `<td>${lane ? escapeHtml(lane) : '<span class="unset">—</span>'}</td>`
      + `<td><span class="badge ${cls}">${escapeHtml(event.review_status)}</span></td></tr>`;
  }).join('');
  document.querySelectorAll('.event-row').forEach(row =>
    row.addEventListener('click', () => selectEvent(Number(row.dataset.index), true)));
}
function selectEvent(index, autoplay = false) {
  state.selected = index;
  document.querySelectorAll('.event-row').forEach(row =>
    row.classList.toggle('selected', Number(row.dataset.index) === index));
  const selectedRow = document.querySelector(`.event-row[data-index="${index}"]`);
  if (selectedRow) selectedRow.scrollIntoView({ block: 'nearest' });
  const event = state.events[index];
  const player = $('clipPlayer'), placeholder = $('videoPlaceholder');
  if (!event) {
    player.pause(); player.removeAttribute('src'); player.hidden = true; placeholder.hidden = false;
    $('previewId').textContent = '候補未選択'; $('previewTime').textContent = '--:--';
    $('previewStatus').textContent = '未確認'; $('previewStatus').className = 'badge';
    $('previewReason').textContent = '';
    return;
  }
  $('previewId').textContent = `候補 ${event.event_id} - ${event.class}`;
  $('previewTime').textContent = `開始 ${formatMinSec(event.t_start_s)} / 終了 ${formatMinSec(event.t_end_s)}`;
  $('previewStatus').textContent = event.review_status;
  $('previewStatus').className = `badge ${event.review_status === '採用' ? 'accepted' : event.review_status === '除外' ? 'rejected' : ''}`;
  $('previewReason').textContent = event.review_reason ? `自動レビュー: ${event.review_reason}` : '';
  $('vehicleSelect').value = event.class;
  $('dangerSelect').value = String(event.danger_level || '0');
  $('laneSelect').value = String(event.lane_count || '');
  if (event.clip_url) {
    player.src = event.clip_url; placeholder.hidden = true; player.hidden = false;
    player.load(); player.defaultPlaybackRate = state.playbackRate; player.playbackRate = state.playbackRate;
    if (autoplay && state.stage === 'clip') player.play().catch(() => {});
  } else {
    player.pause(); player.removeAttribute('src'); player.hidden = true; placeholder.hidden = false;
  }
  if (state.stage === 'cloud') ensureCloud();   // 点群タブ表示中は候補移動に追従する
}
function nextUnconfirmedIndex(current) {
  for (let step = 1; step <= state.events.length; step++) {
    const index = (current + step) % state.events.length;
    if (state.events[index].review_status === '未確認') return index;
  }
  return -1;
}
function decide(status) {
  if (state.selected < 0) return toast('候補を選択してください', true);
  const current = state.selected;
  state.events[current].review_status = status;
  markDirty(); renderEvents(); renderSummary();
  const next = nextUnconfirmedIndex(current);
  if ((status === '採用' || status === '除外') && next >= 0 && next !== current) {
    selectEvent(next, true);
    toast(`${status}にしました。次の未確認候補を再生します`);
  } else {
    selectEvent(current, false);
    toast(`${status}にしました`);
  }
}
function moveCandidate(delta, autoplay = true) {
  if (!state.events.length) return;
  const base = state.selected < 0 ? 0 : state.selected;
  selectEvent((base + delta + state.events.length) % state.events.length, autoplay);
}
function togglePlayback() {
  if (state.stage === 'cloud') { toggleCloudPlay(cloud.viewer); return; }
  const player = $('clipPlayer'); if (player.hidden || !player.src) return;
  if (player.paused) player.play().catch(() => {}); else player.pause();
}
function changeVehicle(value) {
  const event = state.events[state.selected]; if (!event) return;
  event.class = value; event.class_reviewed = true;
  markDirty(); renderEvents(); renderSummary();
  $('previewId').textContent = `候補 ${event.event_id} - ${value}`;
}
function changeDanger(value) {
  const event = state.events[state.selected]; if (!event) return;
  event.danger_level = String(value || '0');
  markDirty(); renderEvents();
}
function changeLane(value) {
  const event = state.events[state.selected]; if (!event) return;
  event.lane_count = String(value || '');
  markDirty(); renderEvents();
}
function handleShortcut(event) {
  if (!document.querySelector('#panel-confirm.active')) return;
  if (['INPUT', 'TEXTAREA', 'SELECT'].includes(event.target.tagName)) return;
  const key = event.key.toLowerCase();
  if (key === 'a') { event.preventDefault(); decide('採用'); }
  else if (key === 'd') { event.preventDefault(); decide('除外'); }
  else if (key === 'w') { event.preventDefault(); decide('未確認'); }
  else if (event.code === 'Space') { if (event.target.tagName === 'VIDEO') return; event.preventDefault(); togglePlayback(); }
  else if (event.key === 'ArrowRight') { event.preventDefault(); moveCandidate(1); }
  else if (event.key === 'ArrowLeft') { event.preventDefault(); moveCandidate(-1); }
}
async function saveReviews(show = true) {
  if (!state.events.length) { toast('保存する候補がありません', true); return false; }
  const result = await api().save_reviews({ out: state.out, events: state.events, offset: state.offset });
  if (!result.ok) { toast(result.error || '保存できませんでした', true); return false; }
  state.dirty = false; setStatus('準備完了');
  if (show) {
    const rejected = state.events.filter(e => e.review_status === '除外').length;
    toast(`保存しました: ${result.path}${rejected ? `（除外 ${rejected} 件はCSVから除かれます）` : ''}`);
  }
  return true;
}
async function generateFinal() {
  if (!state.events.length) { toast('先に結果を読み込んでください', true); return; }
  const statusEl = $('finalStatus');
  statusEl.className = 'final-status-line';
  statusEl.textContent = '修正を保存して最終Excelを生成しています…';
  if (!await saveReviews(false)) { statusEl.textContent = ''; return; }
  setStatus('最終Excelを生成中', true);
  const result = await api().generate_final_excel({ out: state.out, offset: state.offset });
  setStatus('準備完了');
  if (!result.ok) {
    statusEl.className = 'final-status-line error';
    statusEl.textContent = result.error || '最終Excel生成に失敗しました';
    return toast(result.error || '最終Excel生成に失敗しました', true);
  }
  statusEl.textContent = `最終Excelを生成しました（採用 ${result.rows} 件）: ${result.path}`;
  toast(`最終Excelを生成しました（採用 ${result.rows} 件）`);
}
async function openPath(path, missingMessage) {
  if (!path) { toast(missingMessage, true); return; }
  const result = await api().open_path(path);
  if (!result.ok) toast(result.error || '開けませんでした', true);
}

// ── 初期化 ───────────────────────────────────────────────────────────────────

async function initialize() {
  if (state.initialized) return; state.initialized = true;
  state.settings = await api().get_settings(); const s = state.settings;
  $('sessionPath').value = s.session || '';
  $('outPath').value = s.out || '';
  const view = document.querySelector(`[name=pipelineView][value="${s.view || ''}"]`); if (view) view.checked = true;
  $('pipelineSubject').value = s.subject || '';
  $('pipelineDate').value = s.date || '';
  $('pipelineOrganizeId').value = s.organize_id || '';
  $('pipelineDetectMode').value = s.detect_mode || '';
  $('pipelineLane').value = s.lane_count || '';
  $('pipelineOffset').value = s.offset || '';
  $('pipelineExtra').value = s.extra || '';

  document.querySelectorAll('.step').forEach(x => x.addEventListener('click', () => goStep(x.dataset.step)));
  document.querySelectorAll('[data-pick]').forEach(x => x.addEventListener('click', () => pick(x.dataset.pick)));
  document.querySelectorAll('[data-status]').forEach(x => x.addEventListener('click', () => decide(x.dataset.status)));
  $('startPipeline').addEventListener('click', startPipeline);
  $('cancelPipeline').addEventListener('click', cancelPipeline);
  $('openExisting').addEventListener('click', openExisting);
  document.querySelectorAll('[name=pipelineView]').forEach(x => x.addEventListener('change', saveSettings));
  ['pipelineDetectMode', 'pipelineLane', 'pipelineOrganizeId', 'pipelineDate', 'pipelineSubject', 'pipelineOffset', 'pipelineExtra']
    .forEach(id => $(id).addEventListener('change', saveSettings));
  document.querySelectorAll('.stage-tab').forEach(x => x.addEventListener('click', () => setStage(x.dataset.stage)));
  $('saveReviews').addEventListener('click', () => saveReviews(true));
  $('generateFinal').addEventListener('click', generateFinal);
  $('openReport').addEventListener('click', () => openPath(state.summary?.report_html, '閲覧用レポートがありません'));
  $('openOutDir').addEventListener('click', () => openPath(state.out, '出力フォルダがありません'));
  $('previousClip').addEventListener('click', () => moveCandidate(-1));
  $('nextClip').addEventListener('click', () => moveCandidate(1));
  $('speedSelect').addEventListener('change', e => {
    state.playbackRate = Number(e.target.value);
    const player = $('clipPlayer'); if (player) player.playbackRate = state.playbackRate;
  });
  $('vehicleSelect').addEventListener('change', e => changeVehicle(e.target.value));
  $('dangerSelect').addEventListener('change', e => changeDanger(e.target.value));
  $('laneSelect').addEventListener('change', e => changeLane(e.target.value));
  document.addEventListener('keydown', handleShortcut);
  setStatus(window.pywebview ? '準備完了' : 'ブラウザプレビュー');
}

window.addEventListener('pywebviewready', initialize);
window.addEventListener('DOMContentLoaded', () => setTimeout(() => { if (!state.initialized) initialize(); }, 1500));
