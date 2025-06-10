/* ─────────────── GLOBALS ─────────────── */
const N = 8;
let q = {}, timer;
let labels = [], presets = {}, layout = [];
let editMode = false, dragEl = null, offX = 0, offY = 0;
const $ = id => document.getElementById(id);

/* ─────────────── UTILS ─────────────── */
function pwmToDur(p){ if(!p) return '0s';
  const min=0.4,max=4, d=max-(p/255)*(max-min); return d.toFixed(2)+'s'; }
function applyPWM(i,p){
  $("v"+i).textContent=p; $("r"+i).value=p;
  const ic=$("f"+i);
  if(p===0){ic.classList.remove('spin');ic.style.animation='none';}
  else{ic.classList.add('spin');ic.style.animationDuration=pwmToDur(p);}
}
function sendBulk(){
  const arr=[...Array(N)].map((_,i)=>+$("r"+i).value);
  fetch('/bulk',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(arr)});
}

/* ─────────────── BUILD UI ─────────────── */
function build(){
  // sliders
  const sliders=$("sliders");
  const svg=`<svg viewBox="0 0 100 100">
  <g fill="none" stroke="currentColor" stroke-width="8">
   <circle cx="50" cy="50" r="14"/>
   <path d="M50 5 l6 25 a22 22 0 0 1-12 0z"/>
   <path d="M95 50 l-25 6 a22 22 0 0 1 0-12z"/>
   <path d="M50 95 l-6-25 a22 22 0 0 1 12 0z"/>
   <path d="M5 50 l25-6 a22 22 0 0 1 0 12z"/>
  </g></svg>`;
  for(let i=0;i<N;i++){
    const row=document.createElement('div');row.className='fanRow';
    row.innerHTML=`<label id="lab${i}"></label>
      <input id="r${i}" type="range" min="0" max="255">
      <span class="val" id="v${i}"></span>`;
    sliders.appendChild(row);
    $("r"+i).addEventListener('input',e=>{
      applyPWM(i,+e.target.value);
      clearTimeout(timer);timer=setTimeout(sendBulk,80);
    });
    // icons
    const d=document.createElement('div');
    d.id="f"+i; d.className='fanIcon'; d.innerHTML=svg;
    d.addEventListener('click',()=>focusSlider(i));
    d.addEventListener('pointerdown',startDrag);
    $("canvas").appendChild(d);
  }

  $("editBtn").onclick=enableEdit;
  $("lockBtn").onclick=lockLayout;
  $("boostSave").onclick=saveBoost;
  $("rebootBtn").onclick=()=>confirm('Reboot ESP32?')&&fetch('/reboot',{method:'POST'});
  $("loadPreset").onclick=loadPreset;
  $("savePreset").onclick=savePreset;
  $("delPreset").onclick=delPreset;
  $("editLabels").onclick=editLabels;
}

/* ─────────────── FOCUS HIGHLIGHT ─────────────── */
function focusSlider(i){
  document.querySelectorAll('.hl').forEach(el=>el.classList.remove('hl'));
  $("r"+i).classList.add('hl');
  $("r"+i).scrollIntoView({block:'center',behavior:'smooth'});
}

/* ─────────────── DRAGGING ─────────────── */
function enableEdit(){
  editMode=true;$("editBtn").classList.add('locked');
  $("lockBtn").classList.remove('locked');
  document.body.style.cursor='move';
}
function lockLayout(){
  editMode=false;$("editBtn").classList.remove('locked');
  $("lockBtn").classList.add('locked');
  document.body.style.cursor='';
  // save
  fetch('/layout',{method:'POST',headers:{'content-type':'application/json'},
    body:JSON.stringify({layout})});
}
function startDrag(e){
  if(!editMode) return;
  dragEl=e.currentTarget;
  const rect=dragEl.getBoundingClientRect();
  offX=e.clientX-rect.left; offY=e.clientY-rect.top;
  document.addEventListener('pointermove',onDrag);
  document.addEventListener('pointerup',endDrag);
}
function onDrag(e){
  const cvs=$("canvas").getBoundingClientRect();
  let x=e.clientX-cvs.left-offX, y=e.clientY-cvs.top-offY;
  x=Math.max(0,Math.min(cvs.width-dragEl.offsetWidth,x));
  y=Math.max(0,Math.min(cvs.height-dragEl.offsetHeight,y));
  const xp=(x/cvs.width)*100, yp=(y/cvs.height)*100;
  dragEl.style.left=xp+'%'; dragEl.style.top=yp+'%';
  layout[+dragEl.id.slice(1)]={x:xp,y:yp};
}
function endDrag(){
  document.removeEventListener('pointermove',onDrag);
  document.removeEventListener('pointerup',endDrag); dragEl=null;
}

/* ─────────────── CONFIG HANDLERS ─────────────── */
async function loadConfig(){
  const cfg=await fetch('/config').then(r=>r.json());
  labels=cfg.labels??[]; presets=cfg.presets??{}; layout=cfg.layout??[];
  labels.forEach((t,i)=>{$("lab"+i).textContent=t;});
  layout.forEach((p,i)=>{const ic=$("f"+i);ic.style.left=p.x+'%';ic.style.top=p.y+'%';});
  // presets dropdown
  const sel=$("presetSel"); sel.innerHTML='';
  Object.keys(presets).forEach(n=>{
    const o=document.createElement('option');o.textContent=n;sel.appendChild(o);});
}
async function refresh(){
  const st=await fetch('/status').then(r=>r.json());
  const inf=await fetch('/info').then(r=>r.json());
  $("info").textContent=`FW ${inf.fw} | ${inf.ip} | ${inf.upt}s`;
  $("hdr").textContent=`FanCtl ${inf.ip}`;
  $("boostSec").value=inf.boost;
  st.forEach((f,i)=>{applyPWM(i,f.pwm);});
}
function saveBoost(){
  fetch('/boost',{method:'POST',headers:{'content-type':'application/json'},
    body:JSON.stringify({seconds:+$("boostSec").value})});
}
function loadPreset(){
  const n=$("presetSel").value; if(!n||!presets[n])return;
  fetch('/preset/apply?name='+encodeURIComponent(n),{method:'POST'}).then(refresh);
}
function savePreset(){
  const n=$("presetName").value.trim(); if(!n)return alert('name?');
  const arr=[...Array(N)].map((_,i)=>+$("r"+i).value);
  fetch('/preset',{method:'POST',headers:{'content-type':'application/json'},
    body:JSON.stringify({name:n,pwms:arr})}).then(()=>loadConfig());
}
function delPreset(){
  const n=$("presetSel").value; if(!n||!presets[n])return;
  if(!confirm('Delete '+n+'?')) return;
  fetch('/preset/'+encodeURIComponent(n),{method:'DELETE'}).then(()=>loadConfig());
}
async function editLabels(){
  for(let i=0;i<N;i++){
    const t=prompt('Label '+i, labels[i]||'')||labels[i];
    labels[i]=t;
  }
  await fetch('/labels',{method:'POST',headers:{'content-type':'application/json'},
    body:JSON.stringify({labels})});
  loadConfig();
}

/* ─────────────── MAIN ─────────────── */
(async()=>{
  build();
  await loadConfig();
  await refresh();
  setInterval(refresh,2000);
})();
