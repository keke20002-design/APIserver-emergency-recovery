"""지역화폐 알리미 관리자 대시보드 - 정적 HTML(바닐라 JS). 빌드 스텝 없이
main.py의 /blog/admin과 같은 패턴으로 HTMLResponse에 인라인으로 담아 서빙한다.
"""

HTML = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>지역화폐 알리미 관리자</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f5f5f5; color: #222; }
  .header { background: #191f28; color: white; padding: 16px 24px; display: flex; align-items: center; justify-content: space-between; }
  .header h1 { font-size: 18px; font-weight: 600; }
  .header button { background: transparent; color: #bbb; border: 1px solid #444; border-radius: 8px; padding: 6px 12px; cursor: pointer; font-size: 13px; }
  .login-box { background: white; border-radius: 12px; padding: 32px; max-width: 360px; margin: 80px auto; box-shadow: 0 2px 12px rgba(0,0,0,0.1); }
  .login-box h2 { font-size: 20px; margin-bottom: 20px; text-align: center; }
  .form-group { margin-bottom: 14px; }
  .form-group label { display: block; font-size: 13px; color: #666; margin-bottom: 4px; }
  .form-group input, .form-group select, .form-group textarea { width: 100%; padding: 10px 12px; border: 1px solid #ddd; border-radius: 8px; font-size: 14px; }
  .btn { display: inline-block; padding: 9px 16px; border-radius: 8px; border: none; cursor: pointer; font-size: 13px; font-weight: 600; }
  .btn-primary { background: #3182f6; color: white; }
  .btn-primary:hover { background: #1c6fe0; }
  .btn-primary:disabled { opacity: 0.6; cursor: not-allowed; }
  .btn-ghost { background: #eee; color: #333; }
  .btn-block { width: 100%; margin-top: 8px; }
  .container { max-width: 960px; margin: 24px auto; padding: 0 16px 60px; }
  .tab-nav { display: flex; gap: 6px; margin-bottom: 16px; }
  .tab-btn { padding: 8px 16px; border-radius: 8px; border: 1px solid #ddd; background: white; cursor: pointer; font-size: 13px; font-weight: 600; color: #555; }
  .tab-btn.active { background: #191f28; color: white; border-color: #191f28; }
  .tab-panel { display: none; }
  .tab-panel.active { display: block; }
  .card { background: white; border-radius: 10px; padding: 16px 20px; margin-bottom: 10px; box-shadow: 0 1px 4px rgba(0,0,0,0.06); }
  .card-row { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
  .card-title { font-size: 14px; font-weight: 700; margin-bottom: 4px; }
  .card-sub { font-size: 12px; color: #888; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 11px; font-weight: 700; }
  .badge-open { background: #e8f3ff; color: #3182f6; }
  .badge-closed { background: #eee; color: #888; }
  .badge-success { background: #e8f9ee; color: #24a148; }
  .badge-partial { background: #fff6e0; color: #b7791f; }
  .badge-failed { background: #ffe9e9; color: #da1e28; }
  .toolbar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
  .toolbar h2 { font-size: 15px; font-weight: 700; }
  .empty { text-align: center; color: #999; padding: 40px 0; font-size: 13px; }
  .loading { text-align: center; color: #999; padding: 40px 0; font-size: 13px; }
  #toast { position: fixed; bottom: 24px; right: 24px; background: #333; color: white; padding: 12px 20px; border-radius: 8px; font-size: 13px; display: none; z-index: 200; }
  #toast.show { display: block; }
  .modal-backdrop { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.4); align-items: center; justify-content: center; z-index: 100; }
  .modal-backdrop.show { display: flex; }
  .modal { background: white; border-radius: 12px; padding: 24px; max-width: 460px; width: 92%; max-height: 86vh; overflow-y: auto; }
  .modal h3 { font-size: 16px; margin-bottom: 16px; }
  .modal-actions { display: flex; gap: 8px; margin-top: 16px; }
  .modal-actions .btn { flex: 1; }
  .error-msg { color: #da1e28; font-size: 13px; margin-top: 8px; text-align: center; }
  .excerpt { font-size: 12px; color: #777; margin-top: 6px; line-height: 1.5; }
  .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
</style>
</head>
<body>

<div class="header">
  <h1>📍 지역화폐 알리미 관리자</h1>
  <button id="logoutBtn" style="display:none" onclick="doLogout()">로그아웃</button>
</div>

<div id="loginSection" class="login-box">
  <h2>관리자 로그인</h2>
  <div class="form-group">
    <label>아이디</label>
    <input type="text" id="adminId" autocomplete="username">
  </div>
  <div class="form-group">
    <label>비밀번호</label>
    <input type="password" id="adminPw" autocomplete="current-password">
  </div>
  <button class="btn btn-primary btn-block" onclick="doLogin()">로그인</button>
  <div class="error-msg" id="loginError"></div>
</div>

<div id="mainSection" class="container" style="display:none">
  <div class="tab-nav">
    <button class="tab-btn active" data-tab="schedules" onclick="switchTab('schedules')">발행 일정</button>
    <button class="tab-btn" data-tab="notices" onclick="switchTab('notices')">미검토 공지</button>
    <button class="tab-btn" data-tab="sync" onclick="switchTab('sync')">동기화 로그</button>
    <button class="tab-btn" data-tab="sources" onclick="switchTab('sources')">크롤링 대상</button>
  </div>

  <div id="panel-schedules" class="tab-panel active">
    <div class="toolbar">
      <h2>발행 일정 (최근 50건)</h2>
      <button class="btn btn-primary" onclick="openScheduleModal()">+ 새 일정 등록</button>
    </div>
    <div class="grid2" style="margin-bottom:12px">
      <input id="scheduleSearch" placeholder="지역 또는 상품권 검색" oninput="debouncedLoadSchedules()">
      <select id="publicationFilter" onchange="loadSchedules()">
        <option value="">전체 공개 상태</option><option value="DRAFT">임시저장</option>
        <option value="PUBLISHED">공개</option><option value="ARCHIVED">보관</option>
      </select>
    </div>
    <div id="scheduleList" class="loading">불러오는 중...</div>
  </div>

  <div id="panel-notices" class="tab-panel">
    <div class="toolbar">
      <h2>크롤러가 수집한 원문 공지 (미검토)</h2>
    </div>
    <div id="noticeList" class="loading">불러오는 중...</div>
  </div>

  <div id="panel-sync" class="tab-panel">
    <div class="toolbar">
      <h2>동기화 실행 기록</h2>
      <button class="btn btn-primary" id="runSyncBtn" onclick="triggerSync()">지금 실행</button>
    </div>
    <div id="syncLogList" class="loading">불러오는 중...</div>
  </div>

  <div id="panel-sources" class="tab-panel">
    <div class="toolbar">
      <h2>크롤링 대상 사이트</h2>
    </div>
    <div id="sourceList" class="loading">불러오는 중...</div>
  </div>
</div>

<div class="modal-backdrop" id="scheduleModalBackdrop">
  <div class="modal">
    <h3 id="scheduleModalTitle">새 일정 등록</h3>
    <input type="hidden" id="f_id">
    <input type="hidden" id="f_notice_id">
    <div class="form-group">
      <label>지역</label>
      <select id="f_region" onchange="applySelectedRegion()"><option value="">지역을 선택하세요</option></select>
      <input type="hidden" id="f_sigungu_code"><input type="hidden" id="f_region_name">
    </div>
    <div class="form-group">
      <label>상품권명</label>
      <input id="f_product_name" placeholder="예: 마포사랑상품권">
    </div>
    <div class="form-group">
      <label>발행 일시</label>
      <input type="datetime-local" id="f_issuance_datetime">
    </div>
    <div class="grid2">
      <div class="form-group">
        <label>할인율 (%)</label>
        <input type="number" step="0.1" id="f_discount_rate" value="0">
      </div>
      <div class="form-group">
        <label>인당 한도 (원)</label>
        <input type="number" id="f_per_person_limit" value="0">
      </div>
    </div>
    <div class="form-group">
      <label>구매 앱 이름</label>
      <input id="f_app_name" placeholder="예: 서울페이+">
    </div>
    <div class="form-group">
      <label>Android 패키지명</label>
      <input id="f_package_name_android" placeholder="예: com.bizplay.seoul.pay">
    </div>
    <div class="form-group">
      <label>참고 웹 URL (선택)</label>
      <input id="f_web_url" placeholder="크롤링된 공지 원문 링크 등">
    </div>
    <div class="grid2">
      <div class="form-group"><label>일정 상태</label><select id="f_status"><option value="OPEN">진행</option><option value="CLOSED">마감</option></select></div>
      <div class="form-group"><label>공개 상태</label><select id="f_publication_status"><option value="DRAFT">임시저장</option><option value="PUBLISHED">공개</option><option value="ARCHIVED">보관</option></select></div>
    </div>
    <div class="error-msg" id="scheduleModalError"></div>
    <div class="modal-actions">
      <button class="btn btn-ghost" onclick="closeScheduleModal()">취소</button>
      <button class="btn btn-primary" id="scheduleSaveBtn" onclick="saveSchedule()">저장</button>
    </div>
  </div>
</div>

<div id="toast"></div>

<script>
let creds = JSON.parse(sessionStorage.getItem('localcur_admin_creds') || '{"id":"","pw":""}');
let scheduleRows = [];
let noticeRows = [];
let searchTimer;

function showToast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'show';
  setTimeout(() => t.className = '', 2500);
}

function withCreds(path) {
  const sep = path.includes('?') ? '&' : '?';
  return path + sep + 'admin_id=' + encodeURIComponent(creds.id) + '&admin_pw=' + encodeURIComponent(creds.pw);
}

async function api(path, options) {
  options = options || {};
  options.headers = Object.assign({'Content-Type': 'application/json'}, options.headers || {});
  const r = await fetch(withCreds(path), options);
  if (r.status === 401) { doLogout(); throw new Error('인증 만료'); }
  if (!r.ok) { const body = await r.text(); throw new Error('요청 실패 (' + r.status + '): ' + body); }
  return r.json();
}

async function doLogin() {
  const id = document.getElementById('adminId').value.trim();
  const pw = document.getElementById('adminPw').value.trim();
  const errEl = document.getElementById('loginError');
  errEl.textContent = '';
  if (!id || !pw) { errEl.textContent = '아이디와 비밀번호를 입력하세요.'; return; }
  creds = { id, pw };
  try {
    await api('/admin/sync-log?limit=1');
    sessionStorage.setItem('localcur_admin_creds', JSON.stringify(creds));
    document.getElementById('loginSection').style.display = 'none';
    document.getElementById('mainSection').style.display = 'block';
    document.getElementById('logoutBtn').style.display = 'inline-block';
    loadSchedules();
    loadNotices();
    loadSyncLogs();
    loadSources();
    loadRegions();
  } catch (e) {
    errEl.textContent = '아이디 또는 비밀번호가 올바르지 않거나 서버에 연결할 수 없습니다.';
    creds = { id: '', pw: '' };
  }
}

function doLogout() {
  creds = { id: '', pw: '' };
  sessionStorage.removeItem('localcur_admin_creds');
  document.getElementById('loginSection').style.display = 'block';
  document.getElementById('mainSection').style.display = 'none';
  document.getElementById('logoutBtn').style.display = 'none';
}

function switchTab(name) {
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.toggle('active', p.id === 'panel-' + name));
}

function fmtDate(iso) {
  if (!iso) return '-';
  const d = new Date(iso);
  return d.toLocaleString('ko-KR', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
}

function toLocalInputValue(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  const local = new Date(d.getTime() - d.getTimezoneOffset() * 60000);
  return local.toISOString().slice(0, 16);
}

function debouncedLoadSchedules() {
  clearTimeout(searchTimer); searchTimer = setTimeout(loadSchedules, 250);
}

async function loadSchedules() {
  const el = document.getElementById('scheduleList');
  el.className = 'loading'; el.textContent = '불러오는 중...';
  try {
    const q = document.getElementById('scheduleSearch').value.trim();
    const publication = document.getElementById('publicationFilter').value;
    let path = '/admin/schedules?limit=50';
    if (q) path += '&q=' + encodeURIComponent(q);
    if (publication) path += '&publication_status=' + encodeURIComponent(publication);
    const res = await api(path);
    const rows = scheduleRows = res.data || [];
    if (!rows.length) { el.className = 'empty'; el.textContent = '등록된 일정이 없습니다.'; return; }
    el.className = '';
    el.innerHTML = rows.map(s => `
      <div class="card">
        <div class="card-row">
          <div>
            <div class="card-title">${escapeHtml(s.product_name)}
              <span class="badge ${s.status === 'OPEN' ? 'badge-open' : 'badge-closed'}">${s.status}</span>
              <span class="badge ${s.publication_status === 'PUBLISHED' ? 'badge-success' : 'badge-partial'}">${escapeHtml(s.publication_status || 'DRAFT')}</span>
            </div>
            <div class="card-sub">${escapeHtml(s.region_name)} (${escapeHtml(s.sigungu_code)}) · ${fmtDate(s.issuance_datetime)}</div>
            <div class="card-sub">할인 ${s.discount_rate}% · 인당한도 ${Number(s.per_person_limit).toLocaleString()}원 · ${escapeHtml(s.app_name || '-')} · 출처: ${escapeHtml(s.source)}</div>
          </div>
          <button class="btn btn-ghost" onclick="editScheduleById(${Number(s.id)})">수정</button>
        </div>
      </div>
    `).join('');
  } catch (e) {
    el.className = 'empty'; el.textContent = '불러오기 실패: ' + e.message;
  }
}

async function loadNotices() {
  const el = document.getElementById('noticeList');
  el.className = 'loading'; el.textContent = '불러오는 중...';
  try {
    const res = await api('/admin/notices/unreviewed?limit=50');
    const rows = noticeRows = res.data || [];
    if (!rows.length) { el.className = 'empty'; el.textContent = '미검토 공지가 없습니다.'; return; }
    el.className = '';
    el.innerHTML = rows.map(n => `
      <div class="card">
        <div class="card-row">
          <div>
            <div class="card-title"><a href="${escapeAttr(n.url || '#')}" target="_blank" rel="noopener">${escapeHtml(n.title)}</a></div>
            <div class="card-sub">${escapeHtml(n.platform)} · ${n.published_date || '-'}</div>
            ${n.excerpt ? `<div class="excerpt">${escapeHtml(n.excerpt).slice(0, 200)}...</div>` : ''}
          </div>
          <div style="display:flex; flex-direction:column; gap:6px;">
            <button class="btn btn-primary" onclick="useNoticeById(${Number(n.id)})">일정 등록</button>
            <button class="btn btn-ghost" onclick="markReviewed(${n.id})">검토완료</button>
          </div>
        </div>
      </div>
    `).join('');
  } catch (e) {
    el.className = 'empty'; el.textContent = '불러오기 실패: ' + e.message;
  }
}

async function markReviewed(id) {
  try {
    await api('/admin/notices/' + id + '/reviewed', { method: 'POST' });
    showToast('검토완료 처리했습니다');
    loadNotices();
  } catch (e) {
    showToast('처리 실패: ' + e.message);
  }
}

function useNoticeForSchedule(notice) {
  openScheduleModal();
  document.getElementById('f_notice_id').value = notice.id;
  document.getElementById('f_product_name').value = notice.title;
  document.getElementById('f_web_url').value = notice.url || '';
  document.getElementById('f_publication_status').value = 'PUBLISHED';
}

function useNoticeById(id) { const row = noticeRows.find(n => Number(n.id) === Number(id)); if (row) useNoticeForSchedule(row); }
function editScheduleById(id) { const row = scheduleRows.find(s => Number(s.id) === Number(id)); if (row) editSchedule(row); }

async function loadSyncLogs() {
  const el = document.getElementById('syncLogList');
  el.className = 'loading'; el.textContent = '불러오는 중...';
  try {
    const res = await api('/admin/sync-log?limit=20');
    const rows = res.data || [];
    if (!rows.length) { el.className = 'empty'; el.textContent = '실행 기록이 없습니다.'; return; }
    el.className = '';
    const badgeClass = { SUCCESS: 'badge-success', PARTIAL: 'badge-partial', FAILED: 'badge-failed' };
    el.innerHTML = rows.map(r => `
      <div class="card">
        <div class="card-title">${fmtDate(r.run_at)}
          <span class="badge ${badgeClass[r.status] || ''}">${r.status}</span>
        </div>
        <div class="card-sub">수집 공지 ${r.notices_collected}건 · 마감 처리 ${r.schedules_closed}건</div>
        ${r.error_message ? `<div class="excerpt" style="color:#da1e28">${escapeHtml(r.error_message)}</div>` : ''}
      </div>
    `).join('');
  } catch (e) {
    el.className = 'empty'; el.textContent = '불러오기 실패: ' + e.message;
  }
}

async function loadSources() {
  const el = document.getElementById('sourceList');
  el.className = 'loading'; el.textContent = '불러오는 중...';
  try {
    const res = await api('/admin/crawler-sources');
    const rows = res.data || [];
    if (!rows.length) { el.className = 'empty'; el.textContent = '등록된 크롤링 대상이 없습니다.'; return; }
    el.className = '';
    el.innerHTML = rows.map(p => `
      <div class="card">
        <div class="card-row">
          <div>
            <div class="card-title">${escapeHtml(p.label)}
              <span class="badge ${p.implemented ? 'badge-success' : 'badge-partial'}">${p.implemented ? '자동 수집중' : '수기 등록 필요'}</span>
            </div>
            <div class="card-sub">
              ${p.site_url ? `사이트: <a href="${escapeAttr(p.site_url)}" target="_blank" rel="noopener">${escapeHtml(p.site_url)}</a>` : '사이트: 미확인'}
            </div>
            <div class="card-sub">
              ${p.notice_url ? `공지사항: <a href="${escapeAttr(p.notice_url)}" target="_blank" rel="noopener">${escapeHtml(p.notice_url)}</a>` : '공지사항: 미확인'}
            </div>
            <div class="excerpt">${escapeHtml(p.note)}</div>
          </div>
        </div>
      </div>
    `).join('');
  } catch (e) {
    el.className = 'empty'; el.textContent = '불러오기 실패: ' + e.message;
  }
}

async function triggerSync() {
  const btn = document.getElementById('runSyncBtn');
  btn.disabled = true; btn.textContent = '실행 중...';
  try {
    await api('/admin/sync/run', { method: 'POST' });
    showToast('동기화 실행 완료');
    loadSyncLogs();
    loadNotices();
  } catch (e) {
    showToast('실행 실패: ' + e.message);
  } finally {
    btn.disabled = false; btn.textContent = '지금 실행';
  }
}

function openScheduleModal() {
  document.getElementById('scheduleModalTitle').textContent = '새 일정 등록';
  ['f_id','f_notice_id','f_sigungu_code','f_region_name','f_product_name','f_issuance_datetime',
   'f_app_name','f_package_name_android','f_web_url'].forEach(id => document.getElementById(id).value = '');
  document.getElementById('f_discount_rate').value = 0;
  document.getElementById('f_per_person_limit').value = 0;
  document.getElementById('f_region').value = '';
  document.getElementById('f_status').value = 'OPEN';
  document.getElementById('f_publication_status').value = 'DRAFT';
  document.getElementById('scheduleModalError').textContent = '';
  document.getElementById('scheduleModalBackdrop').classList.add('show');
}

function editSchedule(s) {
  openScheduleModal();
  document.getElementById('scheduleModalTitle').textContent = '일정 수정 (#' + s.id + ')';
  document.getElementById('f_id').value = s.id;
  document.getElementById('f_sigungu_code').value = s.sigungu_code || '';
  document.getElementById('f_region_name').value = s.region_name || '';
  document.getElementById('f_product_name').value = s.product_name || '';
  document.getElementById('f_issuance_datetime').value = toLocalInputValue(s.issuance_datetime);
  document.getElementById('f_discount_rate').value = s.discount_rate || 0;
  document.getElementById('f_per_person_limit').value = s.per_person_limit || 0;
  document.getElementById('f_app_name').value = s.app_name || '';
  document.getElementById('f_package_name_android').value = s.package_name_android || '';
  document.getElementById('f_web_url').value = s.web_url || '';
  document.getElementById('f_region').value = s.sigungu_code || '';
  document.getElementById('f_status').value = s.status || 'OPEN';
  document.getElementById('f_publication_status').value = s.publication_status || 'DRAFT';
}

function closeScheduleModal() {
  document.getElementById('scheduleModalBackdrop').classList.remove('show');
}

async function saveSchedule() {
  const errEl = document.getElementById('scheduleModalError');
  errEl.textContent = '';
  const id = document.getElementById('f_id').value;
  const noticeId = document.getElementById('f_notice_id').value;
  const sigunguCode = document.getElementById('f_sigungu_code').value.trim();
  const regionName = document.getElementById('f_region_name').value.trim();
  const productName = document.getElementById('f_product_name').value.trim();
  const issuanceDatetime = document.getElementById('f_issuance_datetime').value;
  if (!sigunguCode || !regionName || !productName || !issuanceDatetime) {
    errEl.textContent = '시군구 코드/지역명/상품권명/발행일시는 필수입니다.';
    return;
  }
  const payload = {
    sigungu_code: sigunguCode,
    region_name: regionName,
    product_name: productName,
    issuance_datetime: new Date(issuanceDatetime).toISOString(),
    discount_rate: parseFloat(document.getElementById('f_discount_rate').value) || 0,
    per_person_limit: parseInt(document.getElementById('f_per_person_limit').value) || 0,
    app_name: document.getElementById('f_app_name').value.trim() || null,
    package_name_android: document.getElementById('f_package_name_android').value.trim() || null,
    web_url: document.getElementById('f_web_url').value.trim() || null,
    status: document.getElementById('f_status').value,
    publication_status: document.getElementById('f_publication_status').value,
  };
  const btn = document.getElementById('scheduleSaveBtn');
  btn.disabled = true; btn.textContent = '저장 중...';
  try {
    if (noticeId) {
      await api('/admin/notices/' + noticeId + '/publish', { method: 'POST', body: JSON.stringify(payload) });
    } else if (id) {
      await api('/admin/schedules/' + id, { method: 'PUT', body: JSON.stringify(payload) });
    } else {
      await api('/admin/schedules', { method: 'POST', body: JSON.stringify(payload) });
    }
    closeScheduleModal();
    showToast('저장했습니다');
    loadSchedules();
    loadNotices();
  } catch (e) {
    errEl.textContent = '저장 실패: ' + e.message;
  } finally {
    btn.disabled = false; btn.textContent = '저장';
  }
}

function escapeHtml(s) {
  if (s === null || s === undefined) return '';
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function escapeAttr(s) { return escapeHtml(s); }

async function loadRegions() {
  try {
    const r = await fetch('/api/v1/regions/hierarchy');
    const body = await r.json();
    const select = document.getElementById('f_region');
    const options = ['<option value="">지역을 선택하세요</option>'];
    (body.data || []).forEach(group => (group.sigungu_list || []).forEach(region => {
      const code = region.sigungu_code;
      const name = group.sido_name + ' ' + region.sigungu_name;
      options.push('<option value="' + escapeAttr(code) + '" data-name="' + escapeAttr(name) + '">' + escapeHtml(name) + '</option>');
    }));
    select.innerHTML = options.join('');
  } catch (_) { showToast('지역 목록을 불러오지 못했습니다'); }
}

function applySelectedRegion() {
  const select = document.getElementById('f_region');
  const option = select.options[select.selectedIndex];
  document.getElementById('f_sigungu_code').value = select.value;
  document.getElementById('f_region_name').value = option ? option.dataset.name || '' : '';
}

if (creds.id && creds.pw) {
  document.getElementById('loginSection').style.display = 'none';
  document.getElementById('mainSection').style.display = 'block';
  document.getElementById('logoutBtn').style.display = 'inline-block';
  loadSchedules(); loadNotices(); loadSyncLogs(); loadSources(); loadRegions();
}
</script>
</body>
</html>
"""
