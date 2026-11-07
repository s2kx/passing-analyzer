// 車長変換GUI（length_gui.py）のフロントエンド。
// 画面は2つ: 実行（run_pipeline.py をボタン1つで起動して進捗を見る）と、
// 車長の確認・修正（写真と GPT の判断理由を見て car_lengths.csv を直す）。

const state = {
  initialized: false, polling: false,
  overview: null,
  rows: [], shown: [], selected: -1,
  image: 'crop',                                   // プレビューのタブ: crop / frame
};

const STAGE_STATUS_LABELS = { running: '実行中', ok: '完了', failed: '失敗' };

// デスクトップ版は pywebview の js_api を直接呼ぶ。ブラウザで開いたとき（length_gui.py
// --serve-only）は同じメソッドを POST /api/<名前> 経由で呼ぶ（length_gui.py の do_POST）。
const bridgeApi = new Proxy({}, {
  get: (_, name) => async (...args) => {
    try {
      const response = await fetch(`/api/${name}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ args }) });
      if (!response.ok) return { ok: false, error: `API ${name}: HTTP ${response.status}` };
      return await response.json();
    } catch (error) {
      return { ok: false, error: `API ${name}: ${error}` };
    }
  },
});
const api = () => window.pywebview?.api || bridgeApi;
const $ = id => document.getElementById(id);

function toast(message, error = false) {
  const el = $('toast'); el.textContent = message; el.className = `toast show${error ? ' error' : ''}`;
  clearTimeout(toast.timer); toast.timer = setTimeout(() => el.className = 'toast', 3600);
}
function setStatus(message, busy = false) {
  $('globalStatus').innerHTML = `<span></span>${message}`;
  $('globalStatus').classList.toggle('busy', busy);
}
function escapeHtml(value = '') { const div = document.createElement('div'); div.textContent = String(value); return div.innerHTML; }
function goStep(name) {
  document.querySelectorAll('.step').forEach(x => x.classList.toggle('active', x.dataset.step === name));
  document.querySelectorAll('.panel').forEach(x => x.classList.toggle('active', x.id === `panel-${name}`));
}
const rowKey = row => `${row.session}|${row.event_id}`;
const fixed = (value, digits) => (value === '' || value == null || Number.isNaN(Number(value))) ? '' : Number(value).toFixed(digits);

// ── 実行画面: フォルダと API キー ─────────────────────────────────────────────

function showSettings(settings) {
  $('srcPath').value = settings.src || '';
  $('workPath').value = settings.work || '';
  $('folderHint').textContent = !settings.src ? '解析結果フォルダを選ぶと、その中身を読み込みます。'
    : settings.work_custom ? '保存先を自分で指定しています。空欄にして保存すると既定（解析結果フォルダの隣）に戻ります。'
    : '保存先は既定です（解析結果フォルダの隣に「<名前>_車長変換」を作ります）。';
}
async function saveSettings() {
  const result = await api().save_settings({ src: $('srcPath').value, work: $('workPath').value });
  if (!result.ok) { toast(result.error || '保存できませんでした', true); return; }
  showSettings(result);
  await loadOverview();
  await loadLengths();
}
async function pick(kind) {
  const result = await api().choose_folder(kind);
  if (!result.ok) { if (!result.cancelled) toast(result.error || '選択できませんでした', true); return; }
  $(kind === 'src' ? 'srcPath' : 'workPath').value = result.path;
  await saveSettings();
}
async function saveApiKey() {
  const key = $('apiKeyInput').value;
  if (!key.trim()) { toast('API キーを貼り付けてください', true); return; }
  const result = await api().set_api_key({ key });
  $('apiKeyInput').value = '';
  if (!result.ok) { toast(result.error || '保存できませんでした', true); return; }
  toast('API キーをこの PC に保存しました');
  await loadOverview();
}

async function loadOverview() {
  const info = await api().get_overview();
  if (!info.ok) { toast(info.error || '状態を読めませんでした', true); return; }
  state.overview = info;
  const status = $('apiKeyStatus');
  status.textContent = info.api_key ? '設定済み' : '未設定';
  status.className = `badge ${info.api_key ? 'accepted' : 'warn'}`;
  const message = $('runMessage');
  message.classList.toggle('warn-text', !!info.problem);
  if (!state.polling) {
    if (info.problem) { message.textContent = info.problem; state.problemShown = true; }
    else if (state.problemShown) { message.textContent = '「実行」を押してください。'; state.problemShown = false; }
  }
  $('openConverted').disabled = !info.converted_dir;
  if (!info.src) {
    $('overviewMeta').textContent = '';
    $('overviewRows').innerHTML = '<tr><td colspan="6" class="empty">解析結果フォルダを選んでください</td></tr>';
    return;
  }

  const t = info.totals;
  $('overviewMeta').textContent = `イベント ${t.lengths} 件のうち車長あり ${t.with_length} 件（手入力 ${t.manual}）・写真 ${t.images} 枚・`
    + `最終Excel 変換済み ${t.converted} 行 / 車長未取得 ${t.missing} 行`
    + (info.summary_updated ? `・最後の実行 ${info.summary_updated}` : '・このフォルダではまだ実行していません');
  $('overviewRows').innerHTML = info.sessions.map(s => `<tr>
    <td class="data">${escapeHtml(s.session)}${s.has_clips ? '' : ' <span class="badge">clips なし</span>'}</td>
    <td class="data">${s.images || '<span class="unset">0</span>'}</td>
    <td class="data">${s.with_length} / ${s.lengths}</td>
    <td class="data">${s.manual || '<span class="unset">0</span>'}</td>
    <td class="data">${s.converted || '<span class="unset">0</span>'}</td>
    <td class="data">${s.missing ? `<span class="badge warn">${s.missing}</span>` : '<span class="unset">0</span>'}</td></tr>`).join('')
    || '<tr><td colspan="6" class="empty">解析結果フォルダにセッションがありません</td></tr>';
}

function renderStages(stages) {
  const el = $('runStages');
  if (!stages || !stages.length) { el.innerHTML = '<p class="empty">段階の開始・完了ごとにここへ表示されます</p>'; return; }
  const badge = s => s.status === 'ok' ? 'accepted' : s.status === 'failed' ? 'rejected' : 'running';
  el.innerHTML = stages.map(s => `<div class="pipeline-stage"><span class="badge ${badge(s)}">${escapeHtml(STAGE_STATUS_LABELS[s.status] || s.status)}</span><strong>${escapeHtml(s.title || '')}</strong><span>${escapeHtml(s.message || '')}</span></div>`).join('');
}
function setRunning(running) {
  $('startRun').disabled = running;
  $('cancelRun').disabled = !running;
  $('saveLength').disabled = running || state.selected < 0;
  $('restoreGpt').disabled = running || (state.shown[state.selected]?.source !== 'manual');
}
async function startRun() {
  if (state.polling) { toast('すでに実行中です', true); return; }
  if (!$('srcPath').value.trim()) { toast('解析結果フォルダを選んでください', true); return; }
  const result = await api().start_run();
  if (!result.ok) { toast(result.error || '実行を開始できませんでした', true); return; }
  renderStages([]);
  setRunning(true); setStatus('実行中', true);
  state.polling = true; pollRun();
  toast('実行を開始しました');
}
async function pollRun() {
  if (!state.polling) return;
  const info = await api().get_run_state();
  $('progressBar').style.width = `${info.progress || 0}%`;
  $('progressLabel').textContent = `${info.progress || 0}%`;
  $('runMessage').textContent = info.message || '';
  $('runLog').textContent = info.log || '';
  $('runLog').scrollTop = $('runLog').scrollHeight;
  renderStages(info.stages || []);
  if (info.running) { setTimeout(pollRun, 700); return; }
  state.polling = false; setRunning(false); setStatus('準備完了');
  if (info.exit_code === 0) toast(info.message || '完了しました');
  else if (info.exit_code !== null) toast(info.message || '実行中にエラーが発生しました', true);
  await loadOverview();
  await loadLengths(true);
}
async function cancelRun() {
  const result = await api().cancel_run();
  if (!result.ok) { toast(result.error || '中止できませんでした', true); return; }
  $('cancelRun').disabled = true;
  toast('実行を中止しています…');
}
async function openConverted() {
  const path = state.overview?.converted_dir;
  if (!path) { toast('結果フォルダがまだありません。先に「実行」してください', true); return; }
  const result = await api().open_path(path);
  if (!result.ok) toast(result.error || '開けませんでした', true);
}

// ── 確認・修正画面 ───────────────────────────────────────────────────────────

function sourceBadge(source = '') {
  if (source === 'manual') return '<span class="badge accepted">手入力</span>';
  if (source.startsWith('gpt')) return source.endsWith(':estimate') ? '<span class="badge warn">GPT 推定</span>'
    : source.endsWith(':catalog') ? '<span class="badge gpt">GPT カタログ</span>' : '<span class="badge gpt">GPT</span>';
  if (source) return `<span class="badge">${escapeHtml(source)}</span>`;
  return '<span class="badge">なし</span>';
}
function matchesKind(row, kind) {
  const source = row.source || '';
  switch (kind) {
    case 'image': return !!row.crop_url;
    case 'check': return source.startsWith('gpt') && (source.endsWith(':estimate') || Number(row.confidence || 0) < 0.7);
    case 'manual': return source === 'manual';
    default: return true;
  }
}
async function loadLengths(keepSelection = false) {
  const result = await api().list_lengths();
  if (!result.ok) { toast(result.error || '一覧を読めませんでした', true); return; }
  const key = keepSelection && state.shown[state.selected] ? rowKey(state.shown[state.selected]) : null;
  state.rows = result.rows;
  const select = $('filterSession'); const current = select.value;
  select.innerHTML = '<option value="">すべてのセッション</option>'
    + (result.sessions || []).map(s => `<option value="${escapeHtml(s)}">${escapeHtml(s)}</option>`).join('');
  if ([...select.options].some(o => o.value === current)) select.value = current;
  applyFilter(key);
}
function applyFilter(selectKey = null) {
  const session = $('filterSession').value, kind = $('filterKind').value;
  state.shown = state.rows.filter(r => (!session || r.session === session) && matchesKind(r, kind));
  $('shownCount').textContent = state.shown.length;
  $('withLengthCount').textContent = state.rows.filter(r => r.car_length_m).length;
  let index = selectKey ? state.shown.findIndex(r => rowKey(r) === selectKey) : -1;
  if (index < 0 && state.shown.length) index = 0;
  state.selected = index;
  renderRows();
  renderPreview(state.shown[index]);
}
function renderRows() {
  const body = $('lengthRows');
  if (!state.rows.length) { body.innerHTML = '<tr><td colspan="7" class="empty">実行画面で解析結果フォルダを選び、「実行」で写真と車長を作ると、ここに一覧が出ます</td></tr>'; return; }
  if (!state.shown.length) { body.innerHTML = '<tr><td colspan="7" class="empty">条件に合うイベントがありません</td></tr>'; return; }
  body.innerHTML = state.shown.map((r, i) => `<tr class="event-row${i === state.selected ? ' selected' : ''}" data-index="${i}">
    <td class="data">${escapeHtml(r.session)}</td>
    <td class="data">${escapeHtml(r.event_id)}</td>
    <td>${escapeHtml(r.class || '') || '<span class="unset">-</span>'}</td>
    <td class="data">${r.car_length_m ? fixed(r.car_length_m, 2) : '<span class="unset">-</span>'}</td>
    <td>${sourceBadge(r.source || '')}</td>
    <td class="name-cell" title="${escapeHtml([r.make, r.model].filter(Boolean).join(' '))}">${escapeHtml([r.make, r.model].filter(Boolean).join(' ')) || '<span class="unset">-</span>'}</td>
    <td class="data">${r.confidence ? fixed(r.confidence, 2) : '<span class="unset">-</span>'}</td></tr>`).join('');
}
function selectRow(index, scroll = true) {
  if (index < 0 || index >= state.shown.length) return;
  state.selected = index;
  document.querySelectorAll('#lengthRows tr.event-row').forEach(tr => tr.classList.toggle('selected', Number(tr.dataset.index) === index));
  renderPreview(state.shown[index]);
  if (scroll) document.querySelector(`#lengthRows tr[data-index="${index}"]`)?.scrollIntoView({ block: 'nearest' });
}
function renderPreview(row) {
  const img = $('previewImage'), placeholder = $('imagePlaceholder');
  if (!row) {
    img.hidden = true; placeholder.hidden = false;
    placeholder.querySelector('p').innerHTML = '右の一覧からイベントを選ぶと<br>写真を表示します';
    $('previewId').textContent = 'イベント未選択'; $('previewSource').innerHTML = ''; $('previewLength').textContent = '-- m';
    $('detailList').innerHTML = ''; $('lengthInput').value = ''; $('noteInput').value = '';
    $('saveLength').disabled = true; $('restoreGpt').disabled = true;
    return;
  }
  const url = state.image === 'crop' ? row.crop_url : row.frame_url;
  if (url) { img.src = url; img.hidden = false; placeholder.hidden = true; }
  else {
    img.hidden = true; placeholder.hidden = false;
    placeholder.querySelector('p').innerHTML = row.extract_status && row.extract_status !== 'OK'
      ? `写真がありません<br>切り出しの結果: ${escapeHtml(row.extract_status)}`
      : state.image === 'frame' && row.crop_url ? '全体フレームがありません<br>（手で差し替えた写真かもしれません）'
      : 'このイベントの写真はまだ<br>切り出されていません';
  }
  $('previewId').textContent = `${row.session} / ${row.event_id}`;
  $('previewSource').innerHTML = sourceBadge(row.source || '');
  $('previewLength').textContent = row.car_length_m ? `${fixed(row.car_length_m, 3)} m` : '-- m';
  const items = [
    ['車名', [row.make, row.model].filter(Boolean).join(' '), false],
    ['車体', row.body_type, false],
    ['確度', row.confidence ? fixed(row.confidence, 2) : '', true],
    ['判断理由', row.note, false],
    ['車種(レビュー)', row.class, false],
    ['点群の実測長', row.lidar_length_m ? `${fixed(row.lidar_length_m, 2)} m` : '', true],
    ['変換', row.convert_status || (row.car_length_m ? '未反映（「実行」を押すと反映）' : ''), false],
  ].filter(([, value]) => value);
  $('detailList').innerHTML = items.length
    ? items.map(([label, value, data]) => `<dt>${escapeHtml(label)}</dt><dd${data ? ' class="data"' : ''}>${escapeHtml(value)}</dd>`).join('')
    : '<p class="empty">まだ車長の情報がありません。写真を見て入力してください</p>';
  $('lengthInput').value = row.car_length_m || '';
  $('noteInput').value = row.note || '';
  $('saveLength').disabled = state.polling;
  $('restoreGpt').disabled = state.polling || row.source !== 'manual';
}
async function saveLength() {
  const row = state.shown[state.selected];
  if (!row) { toast('イベントを選んでください', true); return; }
  const value = $('lengthInput').value.trim();
  if (!value && !window.confirm('車長を空欄にします（GPT の値も消え、次の実行で推定し直します）。よろしいですか？')) return;
  const result = await api().set_length({ session: row.session, event_id: row.event_id, car_length_m: value, note: $('noteInput').value });
  if (!result.ok) { toast(result.error || '保存できませんでした', true); return; }
  Object.assign(row, result.row);
  renderRows(); selectRow(state.selected, false);
  toast(result.row.car_length_m ? `車長 ${result.row.car_length_m} m を手入力として保存しました` : '車長を空欄にしました');
}
async function restoreGpt() {
  const row = state.shown[state.selected];
  if (!row) return;
  const result = await api().restore_gpt({ session: row.session, event_id: row.event_id });
  if (!result.ok) { toast(result.error || '取り消せませんでした', true); return; }
  Object.assign(row, result.row);
  renderRows(); selectRow(state.selected, false);
  toast(result.restored ? `GPT の値 ${result.row.car_length_m} m に戻しました` : 'GPT の生応答が無いため空欄にしました（次の実行で推定します）');
}
function moveRow(delta) {
  if (!state.shown.length) return;
  selectRow((state.selected + delta + state.shown.length) % state.shown.length);
}
function handleShortcut(event) {
  if (!document.querySelector('#panel-review').classList.contains('active')) return;
  const typing = ['INPUT', 'SELECT', 'TEXTAREA'].includes(event.target.tagName);
  if (event.key === 'Enter' && typing && (event.target.id === 'lengthInput' || event.target.id === 'noteInput')) { event.preventDefault(); saveLength(); return; }
  if (typing) return;
  if (event.key === 'ArrowLeft') { event.preventDefault(); moveRow(-1); }
  else if (event.key === 'ArrowRight') { event.preventDefault(); moveRow(1); }
}
function setImageTab(name) {
  state.image = name;
  document.querySelectorAll('.stage-tab').forEach(x => x.classList.toggle('active', x.dataset.image === name));
  renderPreview(state.shown[state.selected]);
}

// ── 起動 ─────────────────────────────────────────────────────────────────────

async function initialize() {
  if (state.initialized) return;
  state.initialized = true;

  document.querySelectorAll('.step').forEach(b => b.addEventListener('click', () => goStep(b.dataset.step)));
  showSettings(await api().get_settings());
  document.querySelectorAll('[data-pick]').forEach(b => b.addEventListener('click', () => pick(b.dataset.pick)));
  ['srcPath', 'workPath'].forEach(id => $(id).addEventListener('change', saveSettings));
  $('saveApiKey').addEventListener('click', saveApiKey);
  $('apiKeyInput').addEventListener('keydown', event => { if (event.key === 'Enter') { event.preventDefault(); saveApiKey(); } });
  $('startRun').addEventListener('click', startRun);
  $('cancelRun').addEventListener('click', cancelRun);
  $('openConverted').addEventListener('click', openConverted);

  $('filterSession').addEventListener('change', () => applyFilter());
  $('filterKind').addEventListener('change', () => applyFilter());
  $('lengthRows').addEventListener('click', event => { const tr = event.target.closest('tr.event-row'); if (tr) selectRow(Number(tr.dataset.index), false); });
  document.querySelectorAll('.stage-tab').forEach(b => b.addEventListener('click', () => setImageTab(b.dataset.image)));
  $('saveLength').addEventListener('click', saveLength);
  $('restoreGpt').addEventListener('click', restoreGpt);
  $('prevRow').addEventListener('click', () => moveRow(-1));
  $('nextRow').addEventListener('click', () => moveRow(1));
  document.addEventListener('keydown', handleShortcut);

  await loadOverview();
  await loadLengths();
  const running = await api().get_run_state();
  if (running.running) { state.polling = true; setRunning(true); setStatus('実行中', true); pollRun(); }
}
window.addEventListener('pywebviewready', initialize);
window.addEventListener('DOMContentLoaded', () => setTimeout(() => { if (!state.initialized) initialize(); }, 1500));
