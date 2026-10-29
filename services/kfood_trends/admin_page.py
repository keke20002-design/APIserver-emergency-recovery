"""Small authenticated review UI. The HTML shell is public; every data mutation is authenticated."""

HTML = r'''<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>K-Food Trend 관리자</title><style>
*{box-sizing:border-box}body{margin:0;background:#f5f6f8;color:#191f28;font:14px system-ui,sans-serif}
header{background:#191f28;color:#fff;padding:18px 24px}main{max-width:1050px;margin:24px auto;padding:0 16px}
.box,.card{background:#fff;border-radius:12px;padding:18px;margin-bottom:14px;box-shadow:0 1px 5px #00000012}
.login{max-width:390px;margin:70px auto}.row{display:flex;gap:9px;flex-wrap:wrap}.grow{flex:1;min-width:180px}
input,textarea,select,button{font:inherit;border:1px solid #d8dce3;border-radius:8px;padding:9px}textarea{width:100%;min-height:90px}
button{cursor:pointer;background:#fff}button.primary{background:#3182f6;color:#fff;border:0}button.danger{color:#c33}.muted{color:#697386;font-size:12px}.badge{padding:3px 8px;border-radius:10px;background:#eef3ff}.sources a{display:block;margin:5px 0;overflow-wrap:anywhere}.hidden{display:none}h2,h3{margin-top:0}
</style></head><body><header><b>今、韓国で話題のグルメ — 관리자</b></header><main>
<section id="login" class="box login"><h2>관리자 로그인</h2><div class="row"><input id="id" class="grow" placeholder="아이디"><input id="pw" class="grow" type="password" placeholder="비밀번호"></div><p><button class="primary" onclick="signIn()">로그인</button></p><div id="loginError" class="muted"></div></section>
<section id="app" class="hidden"><div class="box row"><select id="filter" onchange="load()"><option value="draft">Draft</option><option value="published">Published</option><option value="rejected">Rejected</option><option value="">전체</option></select><button onclick="load()">새로고침</button><button class="primary" onclick="collect()">지금 수집</button><span id="notice" class="muted"></span></div><div id="items"></div></section>
</main><script>
let auth={};const q=()=>new URLSearchParams(auth).toString();
async function api(path,opt={}){const join=path.includes('?')?'&':'?';const r=await fetch(path+join+q(),{headers:{'Content-Type':'application/json'},...opt});const j=await r.json();if(!r.ok)throw Error(j.detail||'요청 실패');return j}
async function signIn(){auth={admin_id:id.value.trim(),admin_pw:pw.value};try{await api('/admin/kfood-trends/items?status=draft');sessionStorage.setItem('kfoodAdmin',JSON.stringify(auth));login.classList.add('hidden');app.classList.remove('hidden');load()}catch(e){loginError.textContent=e.message}}
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function jsonList(v){return Array.isArray(v)?v.join('\n'):''}
async function load(){try{const s=filter.value;const j=await api('/admin/kfood-trends/items'+(s?'?status='+s:''));items.innerHTML=j.data.map(card).join('')||'<div class="box muted">콘텐츠가 없습니다.</div>'}catch(e){notice.textContent=e.message}}
function card(x){const ev=x.evidence||[];return `<article class="card" id="c-${x.id}"><div class="row"><h3 class="grow">${esc(x.title_ko)} / ${esc(x.title_ja)}</h3><span class="badge">${esc(x.status)}</span></div><label>일본어 소개</label><textarea data-k="description_ja">${esc(x.description_ja)}</textarea><label>유행 근거</label><textarea data-k="trend_reason_ja">${esc(x.trend_reason_ja)}</textarea><div class="row"><input class="grow" data-k="title_ja" value="${esc(x.title_ja)}" placeholder="일본어 제목"><input class="grow" data-k="image_url" value="${esc(x.image_url)}" placeholder="대표 이미지 URL (승인 필수)"></div><p><label>재료 (한 줄에 하나)</label><textarea data-k="ingredients_ja">${esc(jsonList(x.ingredients_ja))}</textarea><label>만드는 방법 (한 줄에 하나)</label><textarea data-k="steps_ja">${esc(jsonList(x.steps_ja))}</textarea></p><div class="sources">${ev.map(e=>`<a target="_blank" rel="noopener" href="${esc(e.url)}">${esc(e.publisher)} — ${esc(e.title)}</a>`).join('')}</div><p class="row"><button onclick="save('${x.id}')">수정 저장</button><button class="primary" onclick="act('${x.id}','publish')">승인</button><button class="danger" onclick="act('${x.id}','reject')">거절</button></p></article>`}
async function save(x){const el=document.getElementById('c-'+x),body={};el.querySelectorAll('[data-k]').forEach(i=>{let v=i.value.trim();if(i.dataset.k.endsWith('_ja')&&(i.dataset.k==='ingredients_ja'||i.dataset.k==='steps_ja'))v=v?v.split('\n').map(s=>s.trim()).filter(Boolean):null;body[i.dataset.k]=v||null});try{await api('/admin/kfood-trends/'+x,{method:'PUT',body:JSON.stringify(body)});notice.textContent='저장했습니다.'}catch(e){alert(e.message)}}
async function act(x,a){try{await save(x);await api('/admin/kfood-trends/'+x+'/'+a,{method:'POST'});load()}catch(e){if(a==='publish'&&e.message.includes('대표 이미지')&&confirm(e.message+'\n그래도 승인할까요?')){await api('/admin/kfood-trends/'+x+'/publish?allow_missing_image=true',{method:'POST'});load()}else alert(e.message)}}
async function collect(){notice.textContent='수집 중…';try{const j=await api('/admin/kfood-trends/collect',{method:'POST'});notice.textContent=JSON.stringify(j.data);load()}catch(e){notice.textContent=e.message}}
try{auth=JSON.parse(sessionStorage.getItem('kfoodAdmin')||'{}');if(auth.admin_id){login.classList.add('hidden');app.classList.remove('hidden');load()}}catch(e){}
</script></body></html>'''
