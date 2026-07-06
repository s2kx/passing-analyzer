const state = { settings: {}, videos: [], events: [], selected: -1, offset: 0, playbackRate: 1, polling: false, initialized: false,
  lidar: { loaded:false, frameCount:0, frameTimes:[], index:0, timestamp:0, points:[], selection:null, drag:null, scale:8, panX:0, panY:0, playing:false, request:0, playStartWall:0, playStartSensor:0, tracking:false, vel:{x:0,y:0}, laserCount:0, lasers:new Set(), savedLasers:null, selSet:null } };
state.record = { active:false, eventId:null, eventIndex:-1, frames:new Map() };  // 追い越し記録セッション
state.exported = new Set();  // CSV出力済みの event_id
const TRACK_MARGIN = 1.2;       // 探索ウィンドウの余白 (m)
const TRACK_MIN_POINTS = 8;     // これ未満なら見失いとして枠を動かさない
const TRACK_MAX_STEP = 6;       // 1フレームの最大移動量 (m)。暴走防止
const TRACK_CLUSTER_EPS = 0.6;  // クラスタ分離距離 (m)。小さいほど車と自転車を分けやすい

const DEFAULT_SHORTCUTS = { accept:'a', reject:'d', unreview:'w', kei:'b', normal:'n', large:'m' };
state.shortcuts = { ...DEFAULT_SHORTCUTS };

const demoApi = {
  async get_settings() { return { view: 'rear', out: 'out', overlay: false, road_roi: false }; },
  async save_settings() { return { ok: true }; },
  async choose_path() { return { ok: false, error: 'ファイル選択はデスクトップアプリでのみ利用できます' }; },
  async choose_videos() { return { ok: false, error: '動画選択はデスクトップアプリでのみ利用できます' }; },
  async get_detection_state() { return { running: false, progress: 0, log: '待機中', message: '準備完了', exit_code: null }; },
  async load_events() { return { ok: true, events: [] }; },
  async load_events_file() { return { ok: true, events: [] }; },
  async save_reviews() { return { ok: false, error: '保存する候補がありません' }; },
  async validate_lidar() { return { ok: false, checked: 0, problems: [] }; },
  async open_pcap() { return { ok: false, error: 'PCAP読み込みはデスクトップアプリでのみ利用できます' }; },
  async get_lidar_frame() { return { ok: false, error: 'PCAPが読み込まれていません' }; },
  async seek_lidar_time() { return { ok: false, error: 'PCAPが読み込まれていません' }; },
  async export_lidar_roi() { return { ok: false, error: 'PCAPが読み込まれていません' }; },
  async export_overtaking() { return { ok: false, error: 'PCAPが読み込まれていません' }; },
  async generate_all_edit_csv() { return { ok: false, error: 'edit.csv一括生成はデスクトップアプリでのみ利用できます' }; },
  async generate_final_excel() { return { ok: false, error: '最終Excel生成はデスクトップアプリでのみ利用できます' }; },
  async auto_sync_gps() { return { ok: false, error: 'GPS自動同期はデスクトップアプリでのみ利用できます' }; }
};
const api = () => window.pywebview?.api || demoApi;
const $ = id => document.getElementById(id);

function toast(message, error=false) {
  const el = $('toast'); el.textContent = message; el.className = `toast show${error ? ' error' : ''}`;
  clearTimeout(toast.timer); toast.timer = setTimeout(() => el.className = 'toast', 3200);
}
function setStatus(message, busy=false) { $('globalStatus').innerHTML = `<span></span>${message}`; $('globalStatus').classList.toggle('busy', busy); }
function escapeHtml(value='') { const div = document.createElement('div'); div.textContent = value; return div.innerHTML; }
function parseTime(value) {
  const parts = String(value).trim().split(':').map(Number);
  if (!parts.length || parts.some(Number.isNaN) || parts.some(x => x < 0) || parts.length > 3) throw new Error('時刻は秒または HH:MM:SS で入力してください');
  return parts.reduce((sum, value) => sum * 60 + value, 0);
}
function formatTime(total) {
  const sign = total < 0 ? '-' : ''; total = Math.abs(total);
  const h = Math.floor(total / 3600), m = Math.floor(total % 3600 / 60), s = total % 60;
  return `${sign}${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${s.toFixed(3).padStart(6,'0')}`;
}
function formatMinSec(value) {
  const total = Math.round(Number(value) || 0);
  const sign = total < 0 ? '-' : ''; const t = Math.abs(total);
  return `${sign}${Math.floor(t / 60)}分${t % 60}秒`;
}
function inferFinalDate(outPath) {
  const name = String(outPath || '').split(/[\\/]/).pop() || '';
  const m = name.match(/^(\d{2})(\d{2})(\d{2})_/);
  return m ? `20${m[1]}-${m[2]}-${m[3]}` : '';
}
function inferFinalId(outPath) {
  const name = String(outPath || '').split(/[\\/]/).pop() || '';
  const m = name.match(/_(\d+)$/);
  return m ? m[1] : '';
}
function fillFinalDefaults(overwrite=false) {
  const out = $('outPath')?.value || '';
  if ($('finalDate') && (overwrite || !$('finalDate').value)) $('finalDate').value = inferFinalDate(out);
  if ($('finalId') && (overwrite || !$('finalId').value)) $('finalId').value = inferFinalId(out);
}
function formData() {
  return { videos: state.videos, video: state.videos[0] || '', out: $('outPath').value, view: document.querySelector('[name=view]:checked').value,
    overlay: $('overlay').checked, road_roi: $('roadRoi').checked,
    offset: String(state.offset), pcap: $('pcapPath').value,
    edit_source: $('editSource').value,
    final_id: $('finalId')?.value || '',
    final_date: $('finalDate')?.value || '',
    final_subject: $('finalSubject')?.value || '',
    lasers: state.lidar.savedLasers || [],
    shortcuts: state.shortcuts };
}
async function saveSettings() { await api().save_settings(formData()); }

function goStep(name) {
  document.querySelectorAll('.step').forEach(x => x.classList.toggle('active', x.dataset.step === name));
  document.querySelectorAll('.panel').forEach(x => x.classList.toggle('active', x.id === `panel-${name}`));
  if (name !== 'lidar') setLidarFullscreen(false);
  if (name === 'lidar') setTimeout(resizeLidarCanvas, 0);
}

function setLidarFullscreen(enabled) {
  document.body.classList.toggle('lidar-fullscreen', !!enabled);
  const btn = $('lidarFullscreen');
  if (btn) btn.textContent = enabled ? '通常表示' : 'フルスクリーン';
  if ($('panel-lidar')?.classList.contains('active')) setTimeout(resizeLidarCanvas, 0);
}
function toggleLidarFullscreen() {
  setLidarFullscreen(!document.body.classList.contains('lidar-fullscreen'));
}

async function pick(kind) {
  const result = await api().choose_path(kind);
  if (!result.ok) { if (!result.cancelled) toast(result.error || '選択できませんでした', true); return; }
  const map = { out:'outPath', pcap:'pcapPath', edit_source:'editSource' };
  $(map[kind]).value = result.path; await saveSettings();
  if (kind === 'out') fillFinalDefaults(false);
}

function videoName(path) { return String(path).split(/[\\/]/).pop(); }
function renderVideoList() {
  const list = $('videoList');
  if (!state.videos.length) { list.innerHTML = '<li class="empty">動画を追加してください</li>'; return; }
  list.innerHTML = state.videos.map((path, i) => `
    <li class="video-item" title="${escapeHtml(path)}">
      <span class="video-order">${i + 1}</span>
      <span class="video-name">${escapeHtml(videoName(path))}</span>
      <span class="video-buttons">
        <button class="icon-btn" data-move="up" data-index="${i}" ${i === 0 ? 'disabled' : ''} aria-label="上へ">↑</button>
        <button class="icon-btn" data-move="down" data-index="${i}" ${i === state.videos.length - 1 ? 'disabled' : ''} aria-label="下へ">↓</button>
        <button class="icon-btn remove" data-remove="${i}" aria-label="削除">✕</button>
      </span>
    </li>`).join('');
  list.querySelectorAll('[data-move]').forEach(btn => btn.addEventListener('click', () =>
    moveVideo(Number(btn.dataset.index), btn.dataset.move === 'up' ? -1 : 1)));
  list.querySelectorAll('[data-remove]').forEach(btn => btn.addEventListener('click', () =>
    removeVideo(Number(btn.dataset.remove))));
}
async function addVideos() {
  const result = await api().choose_videos();
  if (!result.ok) { if (!result.cancelled) toast(result.error || '動画を選択できませんでした', true); return; }
  const existing = new Set(state.videos);
  const added = (result.paths || []).filter(p => !existing.has(p));
  if (!added.length) { toast('追加する新しい動画はありませんでした'); return; }
  state.videos.push(...added);
  renderVideoList(); await saveSettings();
  toast(`動画を ${added.length} 件追加しました（計 ${state.videos.length} 件）`);
}
function moveVideo(index, delta) {
  const target = index + delta;
  if (target < 0 || target >= state.videos.length) return;
  const [item] = state.videos.splice(index, 1);
  state.videos.splice(target, 0, item);
  renderVideoList(); saveSettings();
}
function removeVideo(index) {
  state.videos.splice(index, 1);
  renderVideoList(); saveSettings();
}
function clearVideos() {
  if (!state.videos.length) return;
  state.videos = [];
  renderVideoList(); saveSettings();
}

async function startDetection() {
  if (!state.videos.length) { toast('GoPro動画を追加してください', true); return; }
  const result = await api().start_detection(formData());
  if (!result.ok) { toast(result.error, true); return; }
  if (result.out) document.getElementById('outPath').value = result.out;
  setStatus('動画を解析中', true); $('startDetection').disabled = true; state.polling = true; pollDetection();
}
async function pollDetection() {
  if (!state.polling) return;
  const info = await api().get_detection_state();
  $('progressBar').style.width = `${info.progress || 0}%`; $('progressLabel').textContent = `${info.progress || 0}%`;
  $('processMessage').textContent = info.message || ''; $('processLog').textContent = info.log || ''; $('processLog').scrollTop = $('processLog').scrollHeight;
  if (info.running) { setTimeout(pollDetection, 600); return; }
  state.polling = false; $('startDetection').disabled = false;
  if (info.exit_code === 0) { setStatus('検出が完了しました'); toast('検出結果を作成しました'); await loadEvents(); goStep('review'); }
  else if (info.exit_code !== null) { setStatus('検出でエラーが発生しました'); toast('処理ログを確認してください', true); }
}

function applyEventsResult(result) {
  if (!result.ok) { toast(result.error, true); return false; }
  state.events = (result.events || []).map(event => ({ ...event, danger_level: normalizeDangerLevel(event.danger_level) }));
  state.selected = state.events.length ? 0 : -1; state.exported.clear(); renderEvents(); renderMapped();
  if (result.vehicle_reanalysis_required) {
    setStatus('旧形式の結果です');
    toast('このCSVは旧バージョンの検出で作成されています。車種を更新するには検出をやり直してください。', true);
    return false;
  }
  setStatus(`候補 ${state.events.length} 件`);
  if (!state.events.length) { toast('結果CSVに候補が見つかりませんでした', true); return false; }
  return true;
}

async function loadEvents() {
  return applyEventsResult(await api().load_events($('outPath').value));
}

async function chooseAndLoadEvents() {
  const result = await api().choose_path('events_csv');
  if (!result.ok) { if (!result.cancelled) toast(result.error || 'ファイルを選択できませんでした', true); return; }
  document.getElementById('outPath').value = result.folder || result.path;
  await saveSettings();
  if (applyEventsResult(await api().load_events_file(result.path))) toast(`既存の結果を読み込みました（候補 ${state.events.length} 件）`);
}
function renderEvents() {
  $('eventCount').textContent = state.events.length;
  $('reviewedCount').textContent = state.events.filter(x => x.review_status !== '未確認').length;
  if (!state.events.length) { $('eventRows').innerHTML = '<tr><td colspan="6" class="empty">結果CSVを読み込んでください</td></tr>'; selectEvent(-1); return; }
  $('eventRows').innerHTML = state.events.map((event, i) => {
    const cls = event.review_status === '採用' ? 'accepted' : event.review_status === '除外' ? 'rejected' : '';
    const vehicleOptions = ['大型','普通','軽'].map(type => `<option value="${type}" ${event.class===type?'selected':''}>${type}</option>`).join('');
    const dangerOptions = [0,1,2,3].map(level => `<option value="${level}" ${String(event.danger_level)===String(level)?'selected':''}>${level}</option>`).join('');
    return `<tr class="event-row ${i===state.selected?'selected':''}" data-index="${i}"><td>${escapeHtml(event.event_id)}</td><td><select class="vehicle-type-select" data-index="${i}" aria-label="候補${escapeHtml(event.event_id)}の車種">${vehicleOptions}</select></td><td><select class="danger-level-select" data-index="${i}" aria-label="候補${escapeHtml(event.event_id)}の危険感">${dangerOptions}</select></td><td>${escapeHtml(formatMinSec(event.t_start_s))}</td><td>${escapeHtml(formatMinSec(event.t_end_s))}</td><td><span class="badge ${cls}">${escapeHtml(event.review_status)}</span></td></tr>`;
  }).join('');
  document.querySelectorAll('.event-row').forEach(row => row.addEventListener('click', () => selectEvent(Number(row.dataset.index), true)));
  document.querySelectorAll('.vehicle-type-select').forEach(select => {
    select.addEventListener('click', event => event.stopPropagation());
    select.addEventListener('change', event => {
      event.stopPropagation();
      changeVehicleType(Number(select.dataset.index), select.value);
    });
  });
  document.querySelectorAll('.danger-level-select').forEach(select => {
    select.addEventListener('click', event => event.stopPropagation());
    select.addEventListener('change', event => {
      event.stopPropagation();
      changeDangerLevel(Number(select.dataset.index), select.value);
    });
  });
  selectEvent(state.selected, false);
}
function normalizeDangerLevel(value) {
  const level = Number(value);
  return [0,1,2,3].includes(level) ? String(level) : '0';
}
function changeDangerLevel(index, level) {
  if (!state.events[index]) return;
  state.events[index].danger_level = normalizeDangerLevel(level);
  const select = document.querySelector(`.danger-level-select[data-index="${index}"]`);
  if (select) select.value = state.events[index].danger_level;
}
function changeVehicleType(index, vehicleType) {
  if (!['大型','普通','軽'].includes(vehicleType) || !state.events[index]) return;
  state.events[index].class = vehicleType;
  state.events[index].class_reviewed = true;
  const select = document.querySelector(`.vehicle-type-select[data-index="${index}"]`);
  if (select) select.value = vehicleType;
  if (index === state.selected) $('previewId').textContent = `候補 ${state.events[index].event_id} - ${vehicleType}`;
  toast(`車種を${vehicleType}に変更しました`);
}
function decideVehicle(type) {
  if (state.selected < 0) return toast('候補を選択してください', true);
  changeVehicleType(state.selected, type);
}
function updateShortcutHints() {
  document.querySelectorAll('[data-keyhint]').forEach(el => {
    el.textContent = (state.shortcuts[el.dataset.keyhint] || '').toUpperCase();
  });
}
function openKeyConfig() {
  document.querySelectorAll('.key-field').forEach(f => { f.value = (state.shortcuts[f.dataset.action] || '').toUpperCase(); });
  renderLaserGrid();
  $('keyModalError').textContent = '';
  const backdrop = $('keyModalBackdrop');
  backdrop.hidden = false; requestAnimationFrame(() => backdrop.classList.add('show'));
}
function closeKeyConfig() {
  const backdrop = $('keyModalBackdrop');
  backdrop.classList.remove('show'); setTimeout(() => { backdrop.hidden = true; }, 200);
}
function resetKeyConfig() {
  document.querySelectorAll('.key-field').forEach(f => { f.value = (DEFAULT_SHORTCUTS[f.dataset.action] || '').toUpperCase(); });
  $('keyModalError').textContent = '';
}
async function saveKeyConfig() {
  const next = {};
  for (const f of document.querySelectorAll('.key-field')) {
    const v = (f.value || '').trim().toLowerCase();
    if (!/^[a-z0-9]$/.test(v)) { $('keyModalError').textContent = 'すべての項目に英数字1文字を入力してください。'; return; }
    next[f.dataset.action] = v;
  }
  const values = Object.values(next);
  if (new Set(values).size !== values.length) { $('keyModalError').textContent = '同じキーを複数の操作に割り当てることはできません。'; return; }
  state.shortcuts = next;
  await saveSettings();
  updateShortcutHints();
  closeKeyConfig();
  toast('ショートカットキーを保存しました');
}
function setPlaybackRate(rate) {
  state.playbackRate = rate;
  const player = $('clipPlayer');
  if (player) player.playbackRate = rate;
  const select = $('speedSelect');
  if (select) select.value = String(rate);
}
function selectEvent(index, autoplay=false) {
  state.selected = index;
  document.querySelectorAll('.event-row').forEach(row => row.classList.toggle('selected', Number(row.dataset.index) === index));
  const selectedRow = document.querySelector(`.event-row[data-index="${index}"]`);
  if (selectedRow) selectedRow.scrollIntoView({ block:'nearest' });
  const event = state.events[index]; const player = $('clipPlayer'), placeholder = $('videoPlaceholder');
  if (!event) { player.pause(); player.removeAttribute('src'); player.hidden = true; placeholder.hidden = false; $('previewId').textContent = '候補未選択'; $('previewTime').textContent = '--:--'; $('previewStatus').textContent = '未確認'; return; }
  $('previewId').textContent = `候補 ${event.event_id} - ${event.class}`; $('previewTime').textContent = `開始 ${formatMinSec(event.t_start_s)} / 終了 ${formatMinSec(event.t_end_s)}`;
  $('previewStatus').textContent = event.review_status; $('previewStatus').className = `badge ${event.review_status === '採用' ? 'accepted' : event.review_status === '除外' ? 'rejected' : ''}`;
  if (event.clip_url) { player.src = event.clip_url; placeholder.hidden = true; player.hidden = false; player.load(); player.defaultPlaybackRate = state.playbackRate; player.playbackRate = state.playbackRate; if (autoplay) player.play().catch(() => {}); }
  else { player.hidden = true; placeholder.hidden = false; }
}
function nextUnreviewedIndex(current) {
  for (let step = 1; step <= state.events.length; step++) {
    const index = (current + step) % state.events.length;
    if (state.events[index].review_status === '未確認') return index;
  }
  return -1;
}
function decide(status) {
  if (state.selected < 0) return toast('候補を選択してください', true);
  const current = state.selected; state.events[current].review_status = status; renderEvents(); renderMapped();
  const next = nextUnreviewedIndex(current);
  if (next >= 0 && next !== current) { selectEvent(next, true); toast(`${status}にしました。次の未確認候補を再生します`); }
  else { selectEvent(current, false); toast(`${status}にしました`); }
}
function moveCandidate(delta, autoplay=true) {
  if (!state.events.length) return;
  const base = state.selected < 0 ? 0 : state.selected;
  selectEvent((base + delta + state.events.length) % state.events.length, autoplay);
}
function togglePlayback() {
  const player = $('clipPlayer'); if (player.hidden || !player.src) return;
  if (player.paused) player.play().catch(() => {}); else player.pause();
}
function handleShortcut(event) {
  if (!document.querySelector('#panel-review.active')) return;
  if (['INPUT','TEXTAREA','SELECT'].includes(event.target.tagName)) return;
  const key = event.key.toLowerCase(); const sc = state.shortcuts;
  if (key && key === sc.accept) { event.preventDefault(); decide('採用'); }
  else if (key && key === sc.reject) { event.preventDefault(); decide('除外'); }
  else if (key && key === sc.unreview) { event.preventDefault(); decide('未確認'); }
  else if (key && key === sc.kei) { event.preventDefault(); decideVehicle('軽'); }
  else if (key && key === sc.normal) { event.preventDefault(); decideVehicle('普通'); }
  else if (key && key === sc.large) { event.preventDefault(); decideVehicle('大型'); }
  else if (event.code === 'Space') { if (event.target.tagName === 'VIDEO') return; event.preventDefault(); togglePlayback(); }
  else if (event.key === 'ArrowRight') { event.preventDefault(); moveCandidate(1); }
  else if (event.key === 'ArrowLeft') { event.preventDefault(); moveCandidate(-1); }
}
function eventSeconds(event, key, fallbackKey='peak_t_s') {
  const raw = event?.[key];
  const value = Number(raw);
  if (raw !== undefined && raw !== null && String(raw).trim() !== '' && Number.isFinite(value)) return value;
  const fallback = Number(event?.[fallbackKey] || 0);
  return Number.isFinite(fallback) ? fallback : 0;
}
function candidatePcapTime(event) { return eventSeconds(event, 't_start_s') + state.offset; }
function renderMapped() {
  const usable = state.events.map((x, i) => ({ x, i })).filter(e => e.x.review_status !== '除外');
  const sorted = [...usable].sort((a, b) => (a.x.review_status === '採用' ? -1 : 1) - (b.x.review_status === '採用' ? -1 : 1));
  $('mappedTimes').innerHTML = sorted.length ? sorted.map(({ x }) => `<div class="mapped-item"><span>候補 ${escapeHtml(x.event_id)} - ${escapeHtml(x.review_status)} / 開始</span><strong>${formatTime(candidatePcapTime(x))}</strong></div>`).join('') : '<p class="empty">対象の候補がありません（除外は表示されません）</p>';
  const candidateOptions = '<option value="">選択してください</option>' + usable.map(({ x, i }) => `<option value="${i}">${state.exported.has(x.event_id) ? '✓ ' : ''}候補 ${escapeHtml(x.event_id)} - 開始 ${formatTime(candidatePcapTime(x))}</option>`).join('');
  $('lidarCandidate').innerHTML = candidateOptions;
  const recordSelect = $('recordCandidate');
  if (recordSelect) { const keep = recordSelect.value; recordSelect.innerHTML = candidateOptions; recordSelect.value = keep; }
}
async function saveReviews(show=true) {
  const result = await api().save_reviews({ out:$('outPath').value, events:state.events, offset:state.offset });
  if (!result.ok) { toast(result.error, true); return false; } if (show) toast(`保存しました: ${result.path}`); return true;
}
function resizeLidarCanvas() {
  const canvas=$('lidarCanvas'), rect=canvas.parentElement.getBoundingClientRect(), ratio=window.devicePixelRatio||1;
  canvas.width=Math.max(1, Math.round(rect.width*ratio)); canvas.height=Math.max(1, Math.round(rect.height*ratio)); canvas.style.width=`${rect.width}px`; canvas.style.height=`${rect.height}px`; drawLidar();
}
function lidarScreen(point) { const c=$('lidarCanvas'), r=window.devicePixelRatio||1, l=state.lidar; return [c.width/(2*r)+l.panX+point[0]*l.scale, c.height/(2*r)+l.panY-point[1]*l.scale]; }
function lidarWorld(x,y) { const c=$('lidarCanvas'), r=window.devicePixelRatio||1, l=state.lidar; return [(x-c.width/(2*r)-l.panX)/l.scale, -(y-c.height/(2*r)-l.panY)/l.scale]; }
// 2Dグリッドで近接点を連結し、クラスタ（点の配列）の一覧に分ける
function clusterPoints(pts, eps) {
  const cells = new Map();
  for (const p of pts) {
    const k = `${Math.floor(p[0] / eps)},${Math.floor(p[1] / eps)}`;
    let arr = cells.get(k); if (!arr) { arr = []; cells.set(k, arr); } arr.push(p);
  }
  const visited = new Set(), clusters = [];
  for (const start of cells.keys()) {
    if (visited.has(start)) continue;
    visited.add(start); const stack = [start], group = [];
    while (stack.length) {
      const ck = stack.pop(); for (const p of cells.get(ck)) group.push(p);
      const [cx, cy] = ck.split(',').map(Number);
      for (let dx = -1; dx <= 1; dx++) for (let dy = -1; dy <= 1; dy++) {
        if (!dx && !dy) continue; const nk = `${cx + dx},${cy + dy}`;
        if (cells.has(nk) && !visited.has(nk)) { visited.add(nk); stack.push(nk); }
      }
    }
    clusters.push(group);
  }
  return clusters;
}
function trackSelection(points) {
  const l = state.lidar, s = l.selection; if (!s) return;
  const halfW = (s.max_x - s.min_x) / 2, halfH = (s.max_y - s.min_y) / 2;
  const cx = (s.min_x + s.max_x) / 2, cy = (s.min_y + s.max_y) / 2;
  const lasers = l.lasers, laserCount = l.laserCount;
  // 直前の速度で次の中心を予測し、その周囲(探索ウィンドウ)の点を候補に集める
  const mxc = cx + l.vel.x, myc = cy + l.vel.y;
  const winW = halfW + TRACK_MARGIN + Math.abs(l.vel.x), winH = halfH + TRACK_MARGIN + Math.abs(l.vel.y);
  const cand = [];
  for (const p of points) {
    if (laserCount && !lasers.has(p[4])) continue;
    if (p[0] >= mxc - winW && p[0] <= mxc + winW && p[1] >= myc - winH && p[1] <= myc + winH) cand.push(p);
  }
  if (cand.length < TRACK_MIN_POINTS) { l.selSet = null; return; } // 見失い: 枠(矩形)選択に戻す
  // 候補をクラスタに分け、予測位置にいちばん近い塊(=追い越し車)を選ぶ。自転車は別クラスタとして除外
  let best = null, bestScore = Infinity;
  for (const c of clusterPoints(cand, TRACK_CLUSTER_EPS)) {
    if (c.length < TRACK_MIN_POINTS) continue;
    let sx = 0, sy = 0; for (const p of c) { sx += p[0]; sy += p[1]; }
    const ccx = sx / c.length, ccy = sy / c.length, d = Math.hypot(ccx - mxc, ccy - myc);
    if (d < bestScore) { bestScore = d; best = { c, ccx, ccy }; }
  }
  if (!best) { l.selSet = null; return; }
  // 1フレームの移動量を制限（暴走防止）
  let ncx = best.ccx, ncy = best.ccy;
  const dx = ncx - cx, dy = ncy - cy, dist = Math.hypot(dx, dy);
  if (dist > TRACK_MAX_STEP) { const k = TRACK_MAX_STEP / dist; ncx = cx + dx * k; ncy = cy + dy * k; }
  l.vel = { x: ncx - cx, y: ncy - cy };
  l.selection = { min_x: ncx - halfW, max_x: ncx + halfW, min_y: ncy - halfH, max_y: ncy + halfH };
  l.selSet = new Set(best.c); // 追い越し車クラスタの点だけを選択集合にする
}
function drawLidar() {
  const canvas=$('lidarCanvas'), ctx=canvas.getContext('2d'), ratio=window.devicePixelRatio||1, w=canvas.width/ratio, h=canvas.height/ratio; ctx.setTransform(ratio,0,0,ratio,0,0); ctx.fillStyle='#0d1b1e'; ctx.fillRect(0,0,w,h);
  ctx.strokeStyle='rgba(160,200,195,.12)'; ctx.lineWidth=1; const step=10*state.lidar.scale; if(step>8){ const ox=w/2+state.lidar.panX, oy=h/2+state.lidar.panY; for(let x=ox%step;x<w;x+=step){ctx.beginPath();ctx.moveTo(x,0);ctx.lineTo(x,h);ctx.stroke()} for(let y=oy%step;y<h;y+=step){ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(w,y);ctx.stroke()} }
  const s=state.lidar.selection, selSet=state.lidar.selSet; let selCount=0; const lasers=state.lidar.lasers, laserCount=state.lidar.laserCount;
  for(const p of state.lidar.points){ if(laserCount && !lasers.has(p[4])) continue; const [x,y]=lidarScreen(p); if(x<0||x>w||y<0||y>h) continue;
    const inSel = selSet ? selSet.has(p) : (s && p[0]>=s.min_x && p[0]<=s.max_x && p[1]>=s.min_y && p[1]<=s.max_y);
    if(inSel){ selCount++; ctx.fillStyle='#ffd34d'; ctx.fillRect(x-0.6,y-0.6,2.8,2.8); continue; }
    const z=Math.max(-2,Math.min(3,p[2])); const hue=185-(z+2)*22; ctx.fillStyle=`hsl(${hue} 80% ${48+Math.min(18,p[3]/14)}%)`; ctx.fillRect(x,y,1.7,1.7); }
  if(selSet){ $('lidarSelection').textContent=`追尾中 ・ ${selCount}点(表示中)`; }
  else if(s){ const a=lidarScreen([s.min_x,s.min_y]), b=lidarScreen([s.max_x,s.max_y]); ctx.fillStyle='rgba(255,183,77,.08)';ctx.strokeStyle='#ffb74d';ctx.lineWidth=1.5;ctx.fillRect(a[0],a[1],b[0]-a[0],b[1]-a[1]);ctx.strokeRect(a[0],a[1],b[0]-a[0],b[1]-a[1]);
    $('lidarSelection').textContent=`X ${s.min_x.toFixed(2)}〜${s.max_x.toFixed(2)} m / Y ${s.min_y.toFixed(2)}〜${s.max_y.toFixed(2)} m ・ ${selCount}点(表示中)`; }
  drawEgoBicycle(ctx);
}
// 自転車（LiDAR設置位置=原点）を赤い単色の丸で表示する
function drawEgoBicycle(ctx){
  const [x,y]=lidarScreen([0,0]);
  const radius=Math.max(2,Math.min(14,state.lidar.scale*0.625));
  ctx.save();
  ctx.beginPath(); ctx.arc(x,y,radius*1.55,0,Math.PI*2); ctx.fillStyle='rgba(216,58,64,.10)'; ctx.fill();
  ctx.beginPath(); ctx.arc(x,y,radius*1.15,0,Math.PI*2); ctx.strokeStyle='rgba(216,58,64,.34)'; ctx.lineWidth=Math.max(1,radius*0.14); ctx.stroke();
  ctx.beginPath(); ctx.arc(x,y,radius*0.7,0,Math.PI*2); ctx.fillStyle='rgba(216,58,64,.68)'; ctx.fill();
  ctx.restore();
}
function updateLaserLabel(){ $('laserCountLabel').textContent = state.lidar.laserCount ? `${state.lidar.lasers.size} / ${state.lidar.laserCount}` : '—'; }
function renderLaserGrid(){
  const grid=$('laserGrid'), count=state.lidar.laserCount;
  if(!count){ grid.innerHTML='<p class="empty">PCAP未読込</p>'; updateLaserLabel(); return; }
  grid.innerHTML='';
  for(let i=0;i<count;i++){
    const chip=document.createElement('button');
    chip.type='button'; chip.className='laser-chip '+(state.lidar.lasers.has(i)?'on':'off');
    chip.textContent=i; chip.setAttribute('aria-pressed', state.lidar.lasers.has(i)?'true':'false');
    chip.addEventListener('click',()=>toggleLaser(i));
    grid.appendChild(chip);
  }
  updateLaserLabel();
}
function rememberLasers(){ state.lidar.savedLasers=[...state.lidar.lasers]; saveSettings(); }
function toggleLaser(i){ const s=state.lidar.lasers; if(s.has(i))s.delete(i); else s.add(i); rememberLasers(); renderLaserGrid(); drawLidar(); }
function setAllLasers(on){ state.lidar.lasers=new Set(on?Array.from({length:state.lidar.laserCount},(_,i)=>i):[]); rememberLasers(); renderLaserGrid(); drawLidar(); }
function updateLidarPositionDisplay(index, timestamp=null){
  const l=state.lidar;
  index=Math.max(0,Math.min(Math.max(0,l.frameCount-1),Number(index)||0));
  const seconds=timestamp===null?Number(l.frameTimes[index]??l.timestamp??0):Number(timestamp||0);
  $('lidarTime').textContent=formatTime(seconds);
  $('lidarFrame').textContent=l.frameCount?`${index+1} / ${l.frameCount}`:'0 / 0';
}
async function openPcap(){ const path=$('pcapPath').value;if(!path)return toast('PCAPファイルを選択してください',true);setStatus('PCAPを読み込み中',true);const result=await api().open_pcap(path);setStatus('準備完了');if(!result.ok)return toast(result.error,true);const l=state.lidar;l.loaded=true;l.frameCount=result.frame_count;l.frameTimes=result.frame_times||[];l.index=0;l.laserCount=result.laser_count||0;const savedLasers=Array.isArray(l.savedLasers)?l.savedLasers.filter(i=>i>=0&&i<l.laserCount):[];l.lasers=new Set(savedLasers.length?savedLasers:Array.from({length:l.laserCount},(_,i)=>i));renderLaserGrid();$('lidarSlider').max=Math.max(0,l.frameCount-1);$('lidarRangeStart').max=$('lidarRangeEnd').max=Math.max(0,l.frameCount-1);$('lidarInfo').textContent=`${result.model} ・ ${result.frame_count} フレーム ・ ${result.duration.toFixed(3)}秒`;resizeLidarCanvas();await loadLidarFrame(0);toast('PCAPを読み込みました'); }
async function loadLidarFrame(index, options={}){ const l=state.lidar;if(!l.loaded)return;index=Math.max(0,Math.min(l.frameCount-1,Number(index)));if(state.record.active&&index!==l.index)recordSnapshot();const request=++l.request,loading=$('lidarLoading'),showLoading=options.showLoading!==false,maxPoints=options.maxPoints||45000;loading.hidden=true;const loadingTimer=showLoading?setTimeout(()=>{if(request===l.request)loading.hidden=false;},180):null;const resultPromise=api().get_lidar_frame(index,maxPoints);const wait=Math.max(0,Number(options.notBefore||0)-performance.now());const [result]=await Promise.all([resultPromise,new Promise(resolve=>setTimeout(resolve,wait))]);if(loadingTimer)clearTimeout(loadingTimer);if(request!==l.request)return;loading.hidden=true;if(!result.ok)return toast(result.error,true);l.index=index;l.timestamp=Number(result.timestamp||0);l.points=result.points||[];if(l.tracking)trackSelection(l.points);$('lidarSlider').value=index;updateLidarPositionDisplay(index,result.timestamp);$('lidarRangeEnd').value=index;drawLidar(); }
function previewLidarFrame(index){ if(!state.lidar.loaded)return toast('先にPCAPを読み込んでください',true);stopLidarPlay();updateLidarPositionDisplay(index);loadLidarFrame(index); }
async function seekLidar(value){ if(!state.lidar.loaded)return toast('先にPCAPを読み込んでください',true);let seconds;try{seconds=parseTime(value)}catch(e){return toast(e.message,true)}const result=await api().seek_lidar_time(seconds);result.ok?loadLidarFrame(result.index):toast(result.error,true); }
async function jumpToCandidate(){
  const index=Number($('lidarCandidate').value), event=state.events[index];
  if(!event)return toast('候補を選択してください',true);
  if(event.review_status==='除外')return toast('除外した候補は使用できません',true);
  if(!state.synced&&Math.abs(state.offset)<0.001)return toast('先にStep 3でGPS自動同期を実行してください',true);
  const seconds=candidatePcapTime(event);
  $('lidarSeekTime').value=formatTime(seconds);
  await seekLidar(String(seconds));
}
function stopLidarPlay(){state.lidar.playing=false;$('lidarPlay').textContent='再生';}
function toggleLidarPlay(){ const l=state.lidar;if(!l.loaded)return;if(l.playing){stopLidarPlay();return}l.playing=true;l.playStartWall=performance.now();l.playStartSensor=l.timestamp;$('lidarPlay').textContent='停止';advanceLidar(); }
async function advanceLidar(){ const l=state.lidar;if(!l.playing)return;if(l.index>=l.frameCount-1){stopLidarPlay();return}const next=l.index+1,nextTime=Number(l.frameTimes[next]??l.timestamp),notBefore=l.playStartWall+(nextTime-l.playStartSensor)*1000;await loadLidarFrame(next,{showLoading:false,maxPoints:12000,notBefore});if(l.playing)setTimeout(advanceLidar,0); }
function handleLidarShortcut(event){
  if(!document.querySelector('#panel-lidar.active')||['INPUT','TEXTAREA','SELECT'].includes(event.target.tagName))return;
  if(event.key==='Enter'&&state.record.active&&event.target.tagName!=='BUTTON'){event.preventDefault();if(!event.repeat)recordFinish();return;}
  if(!['ArrowLeft','ArrowRight'].includes(event.key))return;
  event.preventDefault();if(event.repeat)return;stopLidarPlay();loadLidarFrame(state.lidar.index+(event.key==='ArrowRight'?1:-1));
}
function recordSnapshot(){
  const r=state.record, l=state.lidar;
  if(!r.active||!l.selection)return;
  r.frames.set(l.index,{ index:l.index,
    bounds:{ min_x:l.selection.min_x, max_x:l.selection.max_x, min_y:l.selection.min_y, max_y:l.selection.max_y },
    lasers:l.laserCount?[...l.lasers]:null });
  updateRecordStatus();
}
function updateRecordStatus(){ $('recordFrames').textContent=`記録フレーム: ${state.record.frames.size}`; }
function resetRecordButtons(active){
  $('recordStart').disabled=active; $('recordCancel').disabled=!active; $('recordFinish').disabled=!active;
}
async function recordStart(){
  if(!state.lidar.loaded)return toast('先にPCAPを読み込んでください',true);
  const sel=$('recordCandidate').value, index=Number(sel), event=state.events[index];
  if(sel===''||!event)return toast('追い越し候補を選択してください',true);
  if(event.review_status==='除外')return toast('除外した候補は使用できません',true);
  if(!state.synced&&Math.abs(state.offset)<0.001)return toast('先にStep 3でGPS自動同期を実行してください',true);
  await seekLidar(String(candidatePcapTime(event)));
  state.record={ active:true, eventId:event.event_id, eventIndex:index, frames:new Map() };
  // 開始フレームで車に枠を引き直してもらうため、古い選択枠はクリアする
  const l=state.lidar; l.tracking=true; $('lidarTrack').checked=true; l.vel={x:0,y:0}; l.selection=null; l.selSet=null;
  drawLidar();
  resetRecordButtons(true);
  $('recordStatus').textContent=`記録中（候補 ${event.event_id}）`; $('recordStatus').className='badge accepted';
  updateRecordStatus();
  toast('開始フレームで車を枠で囲ってください。以降は枠が追尾し、進めるたびに自動記録します');
}
function stopRecordSession(message){
  state.record={ active:false, eventId:null, eventIndex:-1, frames:new Map() };
  resetRecordButtons(false);
  $('recordStatus').textContent='未開始'; $('recordStatus').className='badge';
  updateRecordStatus();
  if(message)toast(message);
}
function recordCancel(){ if(state.record.active)stopRecordSession('記録を中止しました'); }
function nextUnexportedCandidate(currentIndex){
  // currentIndex より後ろの、除外でない・未出力の候補を順に探す（なければ全体から探す）
  const usable = i => state.events[i] && state.events[i].review_status!=='除外' && !state.exported.has(state.events[i].event_id);
  for(let i=currentIndex+1;i<state.events.length;i++) if(usable(i)) return i;
  for(let i=0;i<state.events.length;i++) if(usable(i)) return i;
  return -1;
}
async function recordFinish(){
  const r=state.record;
  if(!r.active)return toast('先に追い越し記録を開始してください',true);
  if(!$('outPath').value)return toast('先に出力フォルダを選択してください',true);
  recordSnapshot();
  if(!r.frames.size)return toast('記録されたフレームがありません。枠を車に合わせてください',true);
  const frames=[...r.frames.values()].sort((a,b)=>a.index-b.index);
  const eventId=r.eventId, currentIndex=r.eventIndex;
  const result=await api().export_overtaking({ out:$('outPath').value, event_id:eventId, frames });
  if(!result.ok)return toast(result.error,true);
  state.exported.add(eventId);
  const next=nextUnexportedCandidate(currentIndex);
  renderMapped();  // ✓表示と次候補の選択状態を更新
  if(next>=0){
    $('recordCandidate').value=String(next);
    await recordStart();  // 次の候補の開始フレームへ移動し記録開始
    toast(`候補 ${eventId} を出力（${result.written_frames}フレーム）。次の候補 ${state.events[next].event_id} へ移動しました`);
  }else{
    stopRecordSession();
    toast(`候補 ${eventId} を出力しました。未出力の候補はありません（全 ${state.exported.size} 件完了）`);
  }
}
async function exportLidar(){ const l=state.lidar;if(!l.selection)return toast('先に点群上で範囲をドラッグして選択してください',true);if(l.laserCount&&l.lasers.size===0)return toast('レーザーが全て解除されています',true);const result=await api().export_lidar_roi({out:$('outPath').value,first:Number($('lidarRangeStart').value),last:Number($('lidarRangeEnd').value),bounds:l.selection,lasers:l.laserCount?[...l.lasers]:null});result.ok?toast(`${result.frames} フレーム ・ ${result.points} 点を出力しました`):toast(result.error,true); }
function setupLidarCanvas(){ const canvas=$('lidarCanvas');canvas.addEventListener('mousedown',e=>{const rect=canvas.getBoundingClientRect(),x=e.clientX-rect.left,y=e.clientY-rect.top;state.lidar.drag={x,y,world:lidarWorld(x,y),pan:e.altKey||e.button!==0,startPan:[state.lidar.panX,state.lidar.panY]};if(!state.lidar.drag.pan){state.lidar.vel={x:0,y:0};state.lidar.selSet=null;}});canvas.addEventListener('mousemove',e=>{const d=state.lidar.drag;if(!d)return;const rect=canvas.getBoundingClientRect(),x=e.clientX-rect.left,y=e.clientY-rect.top;if(d.pan){state.lidar.panX=d.startPan[0]+x-d.x;state.lidar.panY=d.startPan[1]+y-d.y}else{const w=lidarWorld(x,y);state.lidar.selection={min_x:Math.min(d.world[0],w[0]),max_x:Math.max(d.world[0],w[0]),min_y:Math.min(d.world[1],w[1]),max_y:Math.max(d.world[1],w[1])};$('lidarSelection').textContent=`X ${state.lidar.selection.min_x.toFixed(2)}-${state.lidar.selection.max_x.toFixed(2)} m / Y ${state.lidar.selection.min_y.toFixed(2)}-${state.lidar.selection.max_y.toFixed(2)} m`;}drawLidar();});window.addEventListener('mouseup',()=>{const d=state.lidar.drag;state.lidar.drag=null;if(d&&!d.pan&&state.lidar.selection){state.lidar.selSet=null;state.lidar.vel={x:0,y:0};drawLidar();}});canvas.addEventListener('contextmenu',e=>e.preventDefault());canvas.addEventListener('wheel',e=>{e.preventDefault();const rect=canvas.getBoundingClientRect(),x=e.clientX-rect.left,y=e.clientY-rect.top,before=lidarWorld(x,y);state.lidar.scale=Math.max(1,Math.min(80,state.lidar.scale*(e.deltaY<0?1.18:.85)));const after=lidarScreen(before);state.lidar.panX+=x-after[0];state.lidar.panY+=y-after[1];drawLidar();},{passive:false});window.addEventListener('resize',resizeLidarCanvas);setTimeout(resizeLidarCanvas,0); }
async function generateEditCsv(){ const source=$('editSource').value; if(!source)return toast('追い越し記録CSV（overtaking_*.csv）を選択してください',true); setStatus('edit.csvを生成中',true); const result=await api().generate_edit_csv(source,$('outPath').value); setStatus('準備完了'); if(!result.ok)return toast(result.error,true); if(result.analysis_path)toast(`edit.csv と分析Excelを生成しました（${result.rows} フレーム）: ${result.analysis_path}`); else toast(`edit.csv を生成しました。分析Excelは未生成: ${result.analysis_error||'フェーズ判定できませんでした'}`,true); }
async function generateAllEditCsv(){
  const status=$('editBulkStatus');
  status.textContent='一括生成中...';
  setStatus('edit.csvを一括生成中',true);
  const result=await api().generate_all_edit_csv($('outPath').value);
  setStatus('準備完了');
  if(!result.ok){
    status.textContent=result.error||'一括生成に失敗しました';
    status.className='edit-bulk-status error';
    return toast(result.error||'一括生成に失敗しました',true);
  }
  status.className='edit-bulk-status';
  const failed=result.failed_count?` / 失敗 ${result.failed_count} 件`:'';
  const analysisFailed=result.analysis_failed_count?` / 分析未生成 ${result.analysis_failed_count} 件`:'';
  status.textContent=`生成 ${result.count} 件${failed}${analysisFailed}`;
  toast(`edit.csv を一括生成しました（${result.count} 件${failed}${analysisFailed}）`);
}
async function generateFinalExcel(){
  const status=$('finalStatus');
  status.className='final-status';
  status.textContent='レビュー結果を保存中...';
  if(state.events.length){
    const saved=await saveReviews(false);
    if(!saved){ status.className='final-status error'; status.textContent='レビュー結果を保存できませんでした'; return; }
  }
  await saveSettings();
  status.textContent='最終Excelを生成中...';
  setStatus('最終Excelを生成中',true);
  const result=await api().generate_final_excel(formData());
  setStatus('準備完了');
  if(!result.ok){
    status.className='final-status error';
    status.textContent=result.error||'最終Excel生成に失敗しました';
    return toast(result.error||'最終Excel生成に失敗しました',true);
  }
  const warn=(result.warnings||[]).length?`\n警告 ${result.warnings.length} 件:\n${(result.warnings||[]).slice(0,4).join('\n')}`:'';
  status.textContent=`生成完了: ${result.rows} 件\n${result.path}${warn}`;
  toast(`最終Excelを生成しました（${result.rows} 件）`);
}

async function autoSyncGps() {
  const btn = $('autoSyncGps'), status = $('gpsSyncStatus');
  btn.disabled = true;
  status.textContent = 'GPMFテレメトリを解析中...';
  status.className = 'gps-sync-status';
  const firstVideo = state.videos[0] || '';
  if (!firstVideo) { status.textContent = 'Step 1で動画を追加してください'; status.className = 'gps-sync-status error'; btn.disabled = false; toast('Step 1で動画を追加してください', true); return; }
  const result = await api().auto_sync_gps(firstVideo);
  btn.disabled = false;
  if (!result.ok) {
    status.textContent = result.error || 'GPS同期に失敗しました';
    status.className = 'gps-sync-status error';
    toast(result.error || 'GPS同期に失敗しました', true);
    return;
  }
  state.offset = result.offset; state.synced = true;
  $('offsetDisplay').textContent = `${result.offset >= 0 ? '+' : ''}${result.offset.toFixed(3)} 秒`;
  renderMapped();
  saveSettings();
  status.textContent = `同期完了 — GoPro t=0: ${result.gopro_utc}  /  PCAP t=0: ${result.pcap_utc}`;
  toast(`GPSオフセット自動設定: ${result.offset >= 0 ? '+' : ''}${result.offset.toFixed(3)} 秒`);
}

async function initialize() {
  if (state.initialized) return; state.initialized = true;
  state.settings = await api().get_settings(); const s = state.settings;
  state.videos = Array.isArray(s.videos) ? s.videos.slice() : (s.video ? [s.video] : []); renderVideoList();
  $('outPath').value=s.out||'out'; $('overlay').checked=!!s.overlay; $('roadRoi').checked=!!s.road_roi;
  const view = document.querySelector(`[name=view][value="${s.view||'rear'}"]`); if(view) view.checked=true;
  state.offset=Number(s.offset||0); state.synced=Math.abs(state.offset)>0.001; $('offsetDisplay').textContent=`${state.offset>=0?'+':''}${state.offset.toFixed(3)} 秒`;
  $('pcapPath').value=s.pcap||''; $('editSource').value=s.edit_source||'';
  $('finalId').value=s.final_id||''; $('finalDate').value=s.final_date||''; $('finalSubject').value=s.final_subject||''; fillFinalDefaults(false);
  state.lidar.savedLasers = (Array.isArray(s.lasers) && s.lasers.length) ? s.lasers.map(Number) : null;
  state.shortcuts = { ...DEFAULT_SHORTCUTS, ...(s.shortcuts || {}) }; updateShortcutHints();
  document.querySelectorAll('.step').forEach(x => x.addEventListener('click', () => goStep(x.dataset.step)));
  document.querySelectorAll('[data-pick]').forEach(x => x.addEventListener('click', () => pick(x.dataset.pick)));
  document.querySelectorAll('[data-status]').forEach(x => x.addEventListener('click', () => decide(x.dataset.status)));
  $('speedSelect').addEventListener('change', e => setPlaybackRate(Number(e.target.value)));
  $('previousClip').addEventListener('click', () => moveCandidate(-1)); $('nextClip').addEventListener('click', () => moveCandidate(1));
  document.addEventListener('keydown', handleShortcut); document.addEventListener('keydown', handleLidarShortcut);
  $('addVideos').addEventListener('click', addVideos); $('clearVideos').addEventListener('click', clearVideos);
  $('startDetection').addEventListener('click', startDetection); $('loadExisting').addEventListener('click', chooseAndLoadEvents); $('autoSyncGps').addEventListener('click', autoSyncGps);
  $('saveReviews').addEventListener('click', () => saveReviews(true));
  $('openSettings').addEventListener('click', openKeyConfig); $('keyModalCancel').addEventListener('click', closeKeyConfig); $('keyModalSave').addEventListener('click', saveKeyConfig); $('keyModalReset').addEventListener('click', resetKeyConfig);
  $('keyModalBackdrop').addEventListener('click', e => { if (e.target === $('keyModalBackdrop')) closeKeyConfig(); });
  $('keyModalBackdrop').addEventListener('keydown', e => { if (e.key === 'Escape') closeKeyConfig(); });
  document.querySelectorAll('.key-field').forEach(f => f.addEventListener('input', () => { f.value = f.value.slice(-1).toUpperCase(); }));
  $('openPcap').addEventListener('click',openPcap);$('lidarPrevious').addEventListener('click',()=>loadLidarFrame(state.lidar.index-1));$('lidarNext').addEventListener('click',()=>loadLidarFrame(state.lidar.index+1));$('lidarPlay').addEventListener('click',toggleLidarPlay);$('lidarSlider').addEventListener('input',e=>previewLidarFrame(e.target.value));$('lidarSeek').addEventListener('click',()=>seekLidar($('lidarSeekTime').value));$('jumpCandidate').addEventListener('click',jumpToCandidate);$('exportLidar').addEventListener('click',exportLidar);$('lidarFullscreen').addEventListener('click',toggleLidarFullscreen);$('generateEdit').addEventListener('click',generateEditCsv);$('generateAllEdits').addEventListener('click',generateAllEditCsv);$('generateFinal').addEventListener('click',generateFinalExcel);$('recordStart').addEventListener('click',recordStart);$('recordCancel').addEventListener('click',recordCancel);$('recordFinish').addEventListener('click',recordFinish);$('lidarTrack').addEventListener('change',e=>{const l=state.lidar;l.tracking=e.target.checked;l.vel={x:0,y:0};l.selSet=null;if(l.tracking){if(!l.selection)toast('先に点群上で車を矩形選択してください',true);else trackSelection(l.points);}drawLidar();});$('laserAll').addEventListener('click',()=>setAllLasers(true));$('laserNone').addEventListener('click',()=>setAllLasers(false));setupLidarCanvas();
  setStatus(window.pywebview ? '準備完了' : 'ブラウザプレビュー');
}

window.addEventListener('pywebviewready', initialize);
window.addEventListener('DOMContentLoaded', () => setTimeout(() => { if (!state.initialized) initialize(); }, 1500));
