#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PC-Fan Orchestrator 3.1 – FastAPI backend + адаптивный Web-UI
Совместима с прошивкой ESP-контроллера v1.3

• 8 вентиляторов, bulk-PWM, boost, reboot
• Перетаскиваемый макет на фоне static/case.png
• Edit layout ↔ Lock layout (позиции хранятся в fan_config.json)
• Presets, подписи, подсветка ползунка при клике на иконку
• Анимация скорости вращения пропорциональна PWM
• Стартовая страница «/»
"""

from __future__ import annotations
import json, os, asyncio
from pathlib import Path
from typing import Dict, List

import httpx
from fastapi import FastAPI, HTTPException, status
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# ────────────────────────── constants ─────────────────────────
ROOT_DIR      = Path(__file__).parent
STATIC_DIR    = ROOT_DIR / "static"
CONFIG_FILE   = ROOT_DIR / "fan_config.json"
ESP_IP        = "192.168.1.41"           # при необходимости поменяйте
FAN_COUNT     = 8
CONNECT_TO    = 2.0                     # s   connect timeout
READ_TO       = 3.0                     # s   read timeout
MONITOR_URL   = "http://localhost:8085/data.json"  # OHM JSON URL
TEMP_POLL_INTERVAL = 2.0               # s
TARGETS = ["Core Average", "GPU Core"]

STATIC_DIR.mkdir(exist_ok=True)

# ────────────────── persistent configuration ──────────────────
_default_rule = {
    "sensors": [],
    "tmin": 30,
    "tmax": 70,
    "pwm_min": 0,
    "pwm_max": 255,
    "mode": "max",
}

_default_cfg = {
    "labels": [f"Fan {i}" for i in range(FAN_COUNT)],
    "presets": {},                          # name → [8 pwm]
    "layout": [{"x": 10+i*10, "y": 50} for i in range(FAN_COUNT)],
    "rules": [_default_rule.copy() for _ in range(FAN_COUNT)],
}
try:
    config: Dict = {**_default_cfg, **json.loads(CONFIG_FILE.read_text())}
except Exception:
    config = _default_cfg.copy()


def save_cfg() -> None:
    CONFIG_FILE.write_text(json.dumps(config, indent=2), encoding="utf-8")


# ────────────────────────── FastAPI ───────────────────────────
app = FastAPI(title="PC-Fan Orchestrator", version="3.1.0")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

_client = httpx.AsyncClient(
    base_url=f"http://{ESP_IP}",
    timeout=httpx.Timeout(READ_TO, connect=CONNECT_TO),
    limits=httpx.Limits(max_keepalive_connections=8, max_connections=16)
)

_mon_client = httpx.AsyncClient(timeout=5.0)

@app.on_event("shutdown")
async def _close():
    await _client.aclose()
    await _mon_client.aclose()

# ───────────────── helpers к ESP32 ────────────────────────────
async def esp_get(path: str):
    try:
        r = await _client.get(path)
        r.raise_for_status()
        if r.headers.get("content-type","").startswith("application/json"):
            return r.json()
        return r.text
    except httpx.ConnectTimeout:
        raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, "ESP32 connect timeout")
    except httpx.RequestError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"ESP error: {exc!s}")

async def esp_post_json(path: str, data):
    try:
        r = await _client.post(path, json=data)
        r.raise_for_status()
        return r
    except httpx.ConnectTimeout:
        raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, "ESP32 connect timeout")
    except httpx.RequestError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"ESP error: {exc!s}")

# ─────────────── temperature monitor helpers ───────────────
async def fetch_hwmon_data():
    try:
        r = await _mon_client.get(MONITOR_URL)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None

def find_temperatures(tree, targets):
    out = {}
    if isinstance(tree, dict):
        if tree.get("Type") == "Temperature" and tree.get("Text") in targets:
            out[tree["Text"]] = tree.get("Value")
        for ch in tree.get("Children", []):
            out.update(find_temperatures(ch, targets))
    elif isinstance(tree, list):
        for node in tree:
            out.update(find_temperatures(node, targets))
    return out

# ─────────────── in-memory зеркало состояния ─────────────────
class FanState(BaseModel):
    pwm: int = Field(255, ge=0, le=255)

class SysInfo(BaseModel):
    fw: str = "-"
    ip: str = "-"
    upt: int = 0
    boost: int = 30

state: List[FanState] = [FanState() for _ in range(FAN_COUNT)]
info  = SysInfo()
rules: List[Dict] = config.get("rules", [_default_rule.copy() for _ in range(FAN_COUNT)])
temp_sources: List[str] = TARGETS.copy()

# ────────────────────────── API models ────────────────────────
class SetReq(BaseModel):
    fan: int = Field(..., ge=0, lt=FAN_COUNT)
    pwm: int = Field(..., ge=0, le=255)

class BoostReq(BaseModel):
    seconds: int = Field(..., ge=0, le=300)

class PresetReq(BaseModel):
    name: str
    pwms: List[int]

class LabelsReq(BaseModel):
    labels: List[str]

class LayoutReq(BaseModel):
    layout: List[Dict[str, float]]      # [{"x":..,"y":..},…]

# ───────────────────────── REST proxy ─────────────────────────
@app.post("/set", status_code=204)
async def set_pwm(req: SetReq):
    state[req.fan].pwm = req.pwm
    await esp_get(f"/set?fan={req.fan}&pwm={req.pwm}")

@app.post("/bulk", status_code=204)
async def bulk(pwms: List[int]):
    if len(pwms)!=FAN_COUNT or any(not 0<=v<=255 for v in pwms):
        raise HTTPException(400, "Array of 8 PWM values 0-255 expected")
    await esp_post_json("/pwm", pwms)
    for i,v in enumerate(pwms): state[i].pwm = v

@app.get("/status", response_model=List[FanState])
async def get_status():
    try:
        raw = await esp_get("/status")
        if isinstance(raw,list) and len(raw)==FAN_COUNT:
            for i,v in enumerate(raw): state[i].pwm = int(v)
    except Exception: pass
    return state

@app.get("/info", response_model=SysInfo)
async def get_info():
    try:
        raw = await esp_get("/info")
        info.fw   = raw.get("fw","-")
        info.ip   = raw.get("ip","-")
        info.upt  = raw.get("upt",0)
        info.boost= raw.get("boost",30)
    except Exception: pass
    return info

@app.post("/boost", status_code=204)
async def set_boost(req: BoostReq):
    await esp_get(f"/boost?sec={req.seconds}")

@app.post("/reboot", status_code=204)
async def reboot():
    await esp_get("/reboot")

# ─────────────── presets / labels / layout ───────────────────
@app.get("/config")
async def get_config():
    return config

@app.post("/labels", status_code=204)
async def set_labels(req: LabelsReq):
    if len(req.labels)!=FAN_COUNT:
        raise HTTPException(400,"Array of 8 labels expected")
    config["labels"] = req.labels
    save_cfg()

@app.post("/preset", status_code=204)
async def add_preset(p: PresetReq):
    if len(p.pwms)!=FAN_COUNT or any(not 0<=v<=255 for v in p.pwms):
        raise HTTPException(400,"Array of 8 PWM values 0-255 expected")
    config["presets"][p.name] = p.pwms
    save_cfg()

@app.delete("/preset/{name}", status_code=204)
async def del_preset(name: str):
    if name in config["presets"]:
        del config["presets"][name]
        save_cfg()

@app.post("/preset/apply", status_code=204)
async def apply_preset(name: str):
    pwms = config["presets"].get(name)
    if not pwms:
        raise HTTPException(404,"Preset not found")
    await esp_post_json("/pwm", pwms)
    for i,v in enumerate(pwms): state[i].pwm = v

@app.post("/layout", status_code=204)
async def set_layout(req: LayoutReq):
    if len(req.layout)!=FAN_COUNT:
        raise HTTPException(400,"Array of 8 positions expected")
    for p in req.layout:
        if not (0<=p.get("x",-1)<=100 and 0<=p.get("y",-1)<=100):
            raise HTTPException(400,"Positions must be 0-100 %")
    config["layout"] = req.layout
    save_cfg()

# ─────────────────────────── rules / temps ────────────────────
@app.get("/temp_sources")
async def get_sources():
    return temp_sources

@app.get("/rules")
async def get_rules():
    return rules

@app.post("/rules", status_code=204)
async def set_rules(new_rules: List[Dict]):
    if len(new_rules) != FAN_COUNT:
        raise HTTPException(400, "Array of rules for each fan expected")
    for r in new_rules:
        if not isinstance(r.get("sensors", []), list):
            r["sensors"] = []
        r.setdefault("tmin", 30)
        r.setdefault("tmax", 70)
        r.setdefault("pwm_min", 0)
        r.setdefault("pwm_max", 255)
        r.setdefault("mode", "max")
    global rules
    rules = new_rules
    config["rules"] = rules
    save_cfg()

async def auto_loop():
    while True:
        data = await fetch_hwmon_data()
        if data:
            temps = find_temperatures(data, TARGETS)
            for i, r in enumerate(rules):
                if not r.get("sensors"):
                    continue
                vals = [temps.get(s) for s in r["sensors"] if temps.get(s) is not None]
                if not vals:
                    continue
                t = max(vals) if r.get("mode") == "max" else sum(vals)/len(vals)
                if t <= r["tmin"]:
                    pwm = r["pwm_min"]
                elif t >= r["tmax"]:
                    pwm = r["pwm_max"]
                else:
                    k = (t - r["tmin"]) / (r["tmax"] - r["tmin"])
                    pwm = int(r["pwm_min"] + (r["pwm_max"] - r["pwm_min"]) * k)
                pwm = max(0, min(255, int(pwm)))
                if pwm != state[i].pwm:
                    state[i].pwm = pwm
                    try:
                        await esp_get(f"/set?fan={i}&pwm={pwm}")
                    except Exception:
                        pass
        await asyncio.sleep(TEMP_POLL_INTERVAL)

@app.on_event("startup")
async def start_loop():
    asyncio.create_task(auto_loop())

# ─────────────────────────── UI  ──────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>FanCtl</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--b:#10151b;--p:#1e212a;--slot:#10151b;--bd:#3b4048;
 --ac:#79b8ff;--t:#f1f1f1;font-family:system-ui,Arial,sans-serif}
body{margin:0;height:100vh;background:var(--b);color:var(--t);display:flex;flex-direction:column;overflow:hidden}
header{padding:12px 16px;background:var(--p);display:flex;align-items:center;justify-content:space-between;font-size:16px}
header h1{margin:0;font-size:18px;font-weight:600}
header button{margin-left:8px}
main{flex:1;display:flex;overflow:hidden}
#canvas{flex:1;position:relative;background:url('/static/case.jpg') center/contain no-repeat}
#panel{width:320px;background:var(--p);padding:16px;overflow-y:auto}
.fanIcon{position:absolute;width:50px;height:50px;cursor:pointer;transform-origin:50% 50%;transition:outline .2s}
.fanIcon svg{width:100%;height:100%}
.spin{animation:spin 2s linear infinite}
@keyframes spin{from{transform:rotate(0deg)}to{transform:rotate(360deg)}}
.hl{outline:2px solid var(--ac);outline-offset:2px}
input[type=range]{width:100%;height:6px;background:var(--bd);border-radius:3px;appearance:none;margin:0}
input::-webkit-slider-thumb{appearance:none;width:16px;height:16px;border-radius:50%;background:var(--ac);cursor:pointer}
.fanRow{display:flex;align-items:center;margin-bottom:10px;gap:8px}
.fanRow label{width:70px;text-align:right;font-size:13px}
.fanRow .val{width:40px;text-align:right;font-size:13px}
button{padding:4px 10px;background:var(--ac);border:0;border-radius:4px;color:var(--t);cursor:pointer;font-size:13px}
button.locked{opacity:.5;pointer-events:none}
#info{font-size:13px;margin-bottom:14px}
#cfgBox{margin-top:20px;font-size:13px;display:flex;flex-direction:column;gap:14px}
.cfgRow{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.cfgRow input[type=number]{width:70px}
.cfgRow select{flex:1}
.cfgRow input[type=text]{width:100px}
</style></head>
<body>
<header>
  <h1 id="hdr">FanCtl</h1>
  <div>
    <button id="editBtn">Edit layout</button>
    <button id="lockBtn" class="locked">Save & Lock</button>
  </div>
</header>
<main>
  <div id="canvas"></div>
  <div id="panel">
    <div id="info">FW - | IP - | 0s</div>
    <!-- sliders injected here -->
    <div id="sliders"></div>

    <div id="cfgBox">
      <div class="cfgRow">
        <label>Boost (s)</label>
        <input id="boostSec" type="number" min="0" max="300">
        <button id="boostSave">Save</button>
      </div>

      <div class="cfgRow">
        <button id="rebootBtn">Reboot ESP</button>
      </div>

      <div class="cfgRow">
        <select id="presetSel"></select>
        <input id="presetName" placeholder="name">
      </div>
      <div class="cfgRow">
        <button id="loadPreset">Load</button>
        <button id="savePreset">Save</button>
        <button id="delPreset">Delete</button>
      </div>

      <div class="cfgRow">
        <button id="editLabels">Edit labels</button>
      </div>
    </div>
  </div>
</main>

<script>
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
</script>
</body></html>
"""

@app.get("/", response_class=HTMLResponse)
async def root():
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

# ───────────────────────── CLI ────────────────────────────────
if __name__=="__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=5000, reload=True)
