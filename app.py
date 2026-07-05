import os, re, json, uuid, time, io, tempfile, threading
import anthropic, pdfplumber
from flask import Flask, request, jsonify, send_file, render_template_string, abort
from flask_cors import CORS
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

app = Flask(__name__)
CORS(app)

# ── Config ──────────────────────────────────────────────
ANTHROPIC_KEY  = os.environ.get("ANTHROPIC_API_KEY","")
BASE_URL       = os.environ.get("BASE_URL","https://your-app.railway.app")

client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

SESSIONS = {}
LOCK = threading.Lock()

def cleanup():
    now = time.time()
    with LOCK:
        old = [k for k,v in SESSIONS.items() if now-v.get("ts",0)>7200]
        for k in old: del SESSIONS[k]

# ── Fonts ───────────────────────────────────────────────
try:
    pdfmetrics.registerFont(TTFont("DV","/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"))
    pdfmetrics.registerFont(TTFont("DVB","/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"))
    FONTS_OK = True
except: FONTS_OK = False

# ── PDF Analysis ─────────────────────────────────────────
SPEC_KW = ["спецификац","ведомость","выборка","масса","кол-во","количество",
           "наименование","ед.изм","шт.","кг","м2","м3","бетон","арматур",
           "профил","двутавр","швеллер","уголок","итого","всего","гост"]
NUM_RE = re.compile(r'\b\d{2,}[.,]?\d*\b')
DIM_RE = re.compile(r'\b[0-9]{3,5}\b|[øØ]\d+|t\s*=\s*\d+|\+\d+\.\d+')

def has_spec(text):
    tl=text.lower()
    return sum(1 for k in SPEC_KW if k in tl)>=2 and len(NUM_RE.findall(text))>=3

def has_dims(text):
    return len(DIM_RE.findall(text))>=4

def analyze_pdf(path):
    spec_p, draw_p, total = [], [], 0
    with pdfplumber.open(path) as pdf:
        total = len(pdf.pages)
        for i,pg in enumerate(pdf.pages):
            txt = pg.extract_text() or ""
            if len(txt.strip())<20: continue
            if has_spec(txt):   spec_p.append({"p":i+1,"t":txt[:3000]})
            elif has_dims(txt): draw_p.append({"p":i+1,"t":txt[:2000]})
    found_spec = len(spec_p)>0
    parts = [f"ПРОЕКТНАЯ ДОКУМЕНТАЦИЯ\nВсего страниц: {total} | Спецификации: {len(spec_p)} | Чертежи: {len(draw_p)}\n{'='*60}\n"]
    if spec_p:
        parts.append("СПЕЦИФИКАЦИИ:\n")
        for p in spec_p: parts.append(f"\n--- Стр.{p['p']} ---\n{p['t']}\n")
    if draw_p:
        parts.append("\nЧЕРТЕЖИ С РАЗМЕРАМИ:\n")
        for p in draw_p[:20]: parts.append(f"\n--- Стр.{p['p']} ---\n{p['t']}\n")
    return found_spec, "\n".join(parts), {"total":total,"spec":len(spec_p),"draw":len(draw_p)}

# ── Claude ───────────────────────────────────────────────
SYS = """Ты — профессиональный сметчик строительных проектов. Создаёшь ВОР из проектной документации.

АЛГОРИТМ:
1. Кратко опиши объект (1-2 предложения)
2. Задай ВСЕ вопросы по двоичности ОДНИМ сообщением:
   - Две единицы измерения у позиции → спроси в чём считать
   - Одна единица → берёшь как есть
3. После ответов → VOR_JSON

ПРАВИЛА: монтаж=те же единицы что материал | антикор=тонны | котлован=усечённая пирамида V=H/6*(AB+ab+(A+a)(B+b)) | профнастил=м²

ФИНАЛЬНЫЙ ВЫВОД строго после ответов:
VOR_JSON:
{"project":"название","code":"шифр","sections":[{"title":"РАЗДЕЛ 1. МАТЕРИАЛЫ","rows":[{"type":"data","no":1,"name":"Двутавр I40Ш1 С245","unit":"кг","qty":"","vol":"8633","note":"Колонны"},{"type":"subtotal","name":"Итого:","unit":"кг","vol":"8633","note":""},{"type":"total","name":"ИТОГО МК:","unit":"т","vol":"99.08","note":""},{"type":"grand","name":"ВСЕГО с коэф.:","unit":"т","vol":"99.08","note":""}]}]}"""

def ai_first(txt):
    r = client.messages.create(model="claude-sonnet-4-6",max_tokens=1000,system=SYS,
        messages=[{"role":"user","content":txt+"\n\nПроанализируй. Опиши объект, задай вопросы по двоичности. Если вопросов нет — сразу VOR_JSON."}])
    return r.content[0].text

def ai_chat(hist):
    r = client.messages.create(model="claude-sonnet-4-6",max_tokens=1000,system=SYS,messages=hist)
    return r.content[0].text

# ── PDF Generation ───────────────────────────────────────
C={"dark":colors.HexColor("#0A2240"),"hdr":colors.HexColor("#1A3A5C"),
   "sub2":colors.HexColor("#D6E4F0"),"yel":colors.HexColor("#FFF3CD"),
   "alt":colors.HexColor("#F5F9FF"),"wht":colors.white,
   "cyan":colors.HexColor("#00E5FF"),"navy":colors.HexColor("#003366"),
   "red":colors.HexColor("#CC0000"),"grey":colors.HexColor("#666666"),
   "info":colors.HexColor("#EBF5FB")}

def mkp(text,fn="DV",sz=8,col=colors.black,align=0):
    fn_r=(fn if FONTS_OK else("Helvetica-Bold" if fn=="DVB" else"Helvetica"))
    return Paragraph(str(text) if text else "",
        ParagraphStyle("_",fontName=fn_r,fontSize=sz,textColor=col,alignment=align,leading=sz+2))

def gen_pdf(data):
    buf=io.BytesIO()
    doc=SimpleDocTemplate(buf,pagesize=landscape(A4),
        leftMargin=10*mm,rightMargin=10*mm,topMargin=10*mm,bottomMargin=14*mm)
    CW=[9*mm,112*mm,17*mm,20*mm,20*mm,69*mm]
    rows,stys=[],[]
    def add(r,s): rows.append(r);stys.append(s)
    add([mkp("ВЕДОМОСТЬ ОБЪЁМОВ РАБОТ И МАТЕРИАЛОВ","DVB",13,C["wht"],1),"","","","",""],"TTL")
    add([mkp(f"Объект: {data.get('project','—')}   |   Шифр: {data.get('code','—')}",sz=8,col=colors.HexColor("#444444")),"","","","",""],"INF")
    add([mkp("№","DVB",8,C["wht"],1),mkp("Наименование работ и материалов","DVB",8,C["wht"]),
         mkp("Ед.изм.","DVB",8,C["wht"],1),mkp("Кол-во","DVB",8,C["wht"],1),
         mkp("Масса / объём","DVB",8,C["wht"],1),mkp("Примечание","DVB",8,C["wht"])],"CHD")
    num=[0];alt=[False]
    for sec in data.get("sections",[]):
        add([mkp(sec.get("title",""),"DVB",9,C["cyan"]),"","","","",""],"SEC");alt[0]=False
        for row in sec.get("rows",[]):
            t=row.get("type","data")
            bg=C["alt"] if alt[0] else C["wht"]
            if t=="subtotal": bg=C["sub2"]
            elif t=="total":  bg=C["yel"]
            elif t=="grand":  bg=C["dark"]
            nc=C["cyan"] if t=="grand" else C["navy"] if t in("subtotal","total") else colors.black
            vc=C["cyan"] if t=="grand" else C["red"] if t=="total" else C["navy"]
            if t=="data": num[0]+=1;no=mkp(str(num[0]),sz=7,col=C["grey"],align=1)
            else: no=mkp("")
            fn="DVB" if t!="data" else "DV"
            add([no,mkp(row.get("name",""),fn,8 if t!="data" else 7.5,nc),
                 mkp(row.get("unit",""),sz=7.5,align=1),
                 mkp(str(row.get("qty","")) if row.get("qty") else "",sz=7.5,align=2),
                 mkp(str(row.get("vol","")) if row.get("vol") else "","DVB",7.5,vc,2),
                 mkp(str(row.get("note","")) if row.get("note") else "",sz=7,col=C["grey"])],t)
            if t=="data": alt[0]=not alt[0]
    cmds=[("FONTNAME",(0,0),(-1,-1),"DV" if FONTS_OK else "Helvetica"),
          ("FONTSIZE",(0,0),(-1,-1),7.5),("GRID",(0,0),(-1,-1),0.3,colors.HexColor("#CCCCCC")),
          ("VALIGN",(0,0),(-1,-1),"MIDDLE"),("LEFTPADDING",(0,0),(-1,-1),3),
          ("RIGHTPADDING",(0,0),(-1,-1),3),("TOPPADDING",(0,0),(-1,-1),2),("BOTTOMPADDING",(0,0),(-1,-1),2)]
    for i,st in enumerate(stys):
        if st=="TTL":   cmds+=[("BACKGROUND",(0,i),(5,i),C["hdr"]),("SPAN",(0,i),(5,i)),("TOPPADDING",(0,i),(5,i),8),("BOTTOMPADDING",(0,i),(5,i),8)]
        elif st=="INF": cmds+=[("BACKGROUND",(0,i),(5,i),C["info"]),("SPAN",(0,i),(5,i))]
        elif st=="CHD": cmds+=[("BACKGROUND",(0,i),(5,i),C["hdr"]),("ALIGN",(0,i),(5,i),"CENTER"),("TOPPADDING",(0,i),(5,i),4),("BOTTOMPADDING",(0,i),(5,i),4)]
        elif st=="SEC": cmds+=[("BACKGROUND",(0,i),(5,i),C["dark"]),("SPAN",(0,i),(5,i)),("TOPPADDING",(0,i),(5,i),4),("BOTTOMPADDING",(0,i),(5,i),4)]
        elif st=="subtotal": cmds+=[("BACKGROUND",(0,i),(5,i),C["sub2"]),("SPAN",(0,i),(1,i))]
        elif st=="total":    cmds+=[("BACKGROUND",(0,i),(5,i),C["yel"]),("SPAN",(0,i),(1,i)),("TOPPADDING",(0,i),(5,i),3),("BOTTOMPADDING",(0,i),(5,i),3)]
        elif st=="grand":    cmds+=[("BACKGROUND",(0,i),(5,i),C["dark"]),("SPAN",(0,i),(1,i)),("TOPPADDING",(0,i),(5,i),4),("BOTTOMPADDING",(0,i),(5,i),4)]
        elif st=="DA":       cmds+=[("BACKGROUND",(0,i),(5,i),C["alt"])]
    def footer(cv,dc):
        cv.saveState();cv.setFont("DV" if FONTS_OK else "Helvetica",7)
        cv.setFillColor(colors.HexColor("#888888"))
        cv.drawString(10*mm,7*mm,"АйТима — Генератор ВОР")
        cv.drawRightString(287*mm,7*mm,f"Стр. {dc.page}");cv.restoreState()
    t=Table(rows,colWidths=CW,repeatRows=3);t.setStyle(TableStyle(cmds))
    doc.build([t],onFirstPage=footer,onLaterPages=footer)
    return buf.getvalue()

def parse_vor(text):
    idx=text.find("VOR_JSON:")+9
    raw=text[idx:].strip().replace("```json","").replace("```","").strip()
    return json.loads(raw)

# ══════════════════════════════════════════════════════
# ROUTES
# ══════════════════════════════════════════════════════

INDEX_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ВОР Генератор — ПК New TC</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#1a1a1a;color:#f0f0f0;min-height:100vh;display:flex;flex-direction:column;align-items:center;padding:24px 16px}
.logo{display:flex;align-items:center;gap:10px;margin-bottom:20px}
.logo-mark{width:44px;height:44px;border-radius:8px;background:#FFD700;display:flex;align-items:center;justify-content:center;font-weight:800;font-size:13px;color:#1a1a1a;letter-spacing:-.5px}
.logo-name{font-size:20px;font-weight:700;color:#FFD700}
.logo-sub{font-size:12px;color:#888;margin-top:2px}
.wrap{width:100%;max-width:740px;display:flex;flex-direction:column;gap:16px}
h1{font-size:24px;font-weight:700;color:#fff;text-align:center;margin-bottom:4px}
.sub{font-size:14px;color:#888;text-align:center;margin-bottom:8px}
.card{background:#242424;border:1px solid #333;border-radius:16px;padding:20px}
.steps{display:flex;justify-content:center;gap:8px;margin-bottom:16px;flex-wrap:wrap}
.step{display:flex;align-items:center;gap:6px;font-size:12px;color:#555}
.step.active{color:#FFD700}.step.done{color:#4a9060}
.dot{width:8px;height:8px;border-radius:50%;background:#333;flex-shrink:0}
.step.active .dot{background:#FFD700}.step.done .dot{background:#4a9060}
.line{width:16px;height:1px;background:#333}
.dz{border:2px dashed #444;border-radius:12px;padding:36px 20px;text-align:center;cursor:pointer;transition:all .2s;background:#1a1a1a}
.dz:hover,.dz.over{border-color:#FFD700;background:#222}
.dz input{display:none}
.di{font-size:42px;margin-bottom:10px;opacity:.7}
.dl{font-size:15px;font-weight:600;color:#f0f0f0;margin-bottom:4px}
.dh{font-size:13px;color:#666}
.file-badge{display:inline-flex;align-items:center;gap:8px;margin-top:12px;padding:8px 14px;background:#2a2a2a;border:1px solid #444;border-radius:8px;font-size:13px;color:#ccc}
.rm{cursor:pointer;color:#555;font-size:18px;line-height:1}.rm:hover{color:#e05555}
.btn{display:flex;align-items:center;justify-content:center;gap:8px;width:100%;margin-top:14px;padding:13px;font-size:14px;font-weight:600;border-radius:10px;border:none;cursor:pointer;background:#FFD700;color:#1a1a1a;transition:all .15s}
.btn:hover{background:#FFC000;transform:translateY(-1px)}.btn:disabled{opacity:.3;cursor:not-allowed;transform:none}
.alert-info{padding:12px 16px;background:#222;border:1px solid #FFD70044;border-radius:10px;font-size:13px;color:#FFD700;margin-top:12px;line-height:1.6}
.warn{padding:10px 14px;background:#2a1a00;border:1px solid #664400;border-radius:8px;font-size:13px;color:#FFA040;margin-top:10px;line-height:1.5}
.chips{display:flex;gap:10px;margin-top:12px;flex-wrap:wrap}
.chip{padding:5px 12px;background:#1a1a1a;border:1px solid #444;border-radius:8px;font-size:12px;color:#666}
.chip span{color:#FFD700;font-weight:600}
.chat-box{display:flex;flex-direction:column;gap:12px;max-height:400px;overflow-y:auto;padding:4px 0}
.chat-box::-webkit-scrollbar{width:4px}.chat-box::-webkit-scrollbar-track{background:#1a1a1a}.chat-box::-webkit-scrollbar-thumb{background:#444;border-radius:2px}
.msg{display:flex;gap:10px;align-items:flex-start}
.msg.ai{flex-direction:row}.msg.user{flex-direction:row-reverse}
.av{width:30px;height:30px;border-radius:50%;flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:700}
.av.ai{background:#FFD700;color:#1a1a1a}.av.user{background:#333;color:#ccc}
.bub{max-width:86%;padding:10px 14px;border-radius:12px;font-size:14px;line-height:1.6;white-space:pre-wrap;word-break:break-word}
.bub.ai{background:#2a2a2a;color:#f0f0f0;border-bottom-left-radius:4px}
.bub.user{background:#333;color:#fff;border-bottom-right-radius:4px}
.typing{display:flex;gap:4px;padding:8px 12px}
.typing span{width:7px;height:7px;background:#555;border-radius:50%;animation:bn .9s infinite}
.typing span:nth-child(2){animation-delay:.15s}.typing span:nth-child(3){animation-delay:.3s}
@keyframes bn{0%,60%,100%{transform:translateY(0)}30%{transform:translateY(-8px)}}
.ir{display:flex;gap:10px;margin-top:12px}
.inp{flex:1;background:#1a1a1a;border:1px solid #444;border-radius:10px;padding:10px 14px;font-size:14px;color:#f0f0f0;resize:none;outline:none;font-family:inherit;line-height:1.5;min-height:44px;max-height:120px}
.inp:focus{border-color:#FFD700}.inp::placeholder{color:#555}
.snd{padding:10px 18px;background:#FFD700;border:none;border-radius:10px;color:#1a1a1a;font-weight:700;cursor:pointer;font-size:14px;flex-shrink:0}
.snd:hover{background:#FFC000}.snd:disabled{opacity:.3;cursor:not-allowed}
.pw{background:#1a1a1a;border-radius:8px;overflow:hidden;height:6px;margin-top:12px}
.pb{height:100%;background:#FFD700;transition:width .5s}
.pl{font-size:12px;color:#666;margin-top:6px;text-align:center}
.sec-t{font-size:11px;font-weight:600;color:#666;margin-bottom:12px;text-transform:uppercase;letter-spacing:.08em}
.hidden{display:none!important}
</style>
</head>
<body>
<div class="logo">
  <div class="logo-mark">ПК</div>
  <div><div class="logo-name">New TC</div><div class="logo-sub">Генератор ВОР</div></div>
</div>
<div class="wrap">
  <h1>Генератор ВОР</h1>
  <p class="sub">Загрузите проект → ответьте на вопросы → скачайте ВОР в PDF</p>
  <div class="steps">
    <div class="step active" id="s1"><div class="dot"></div>Загрузка</div><div class="line"></div>
    <div class="step" id="s2"><div class="dot"></div>Анализ</div><div class="line"></div>
    <div class="step" id="s3"><div class="dot"></div>Уточнение</div><div class="line"></div>
    <div class="step" id="s4"><div class="dot"></div>PDF</div>
  </div>

  <div class="card" id="blk-upload">
    <div class="sec-t">Загрузите PDF проекта</div>
    <div class="dz" id="dz" onclick="document.getElementById('fi').click()"
         ondragover="doDrag(event,true)" ondragleave="doDrag(event,false)" ondrop="doDrop(event)">
      <input type="file" id="fi" accept=".pdf" onchange="onFile(event)">
      <div class="di">📄</div>
      <div class="dl">Перетащите PDF или нажмите для выбора</div>
      <div class="dh">КМД, КМ, КЖ, АР, ЭОМ, ВК, ОВ — любые разделы</div>
    </div>
    <div id="fb" style="display:none">
      <div class="file-badge">📎 <span id="fn"></span><span class="rm" onclick="clearFile()">×</span></div>
    </div>
    <div id="fw" class="warn hidden"></div>
    <div class="alert-info">⏱ Анализ проекта занимает от 1 до 5 минут. Не закрывайте страницу.</div>
    <button class="btn" id="btn-go" disabled onclick="startUpload()">▶ &nbsp;Анализировать проект</button>
  </div>

  <div class="card hidden" id="blk-chat">
    <div class="sec-t">Анализ и уточнение</div>
    <div class="chat-box" id="chat"></div>
    <div id="chips" class="chips hidden"></div>
    <div class="pw hidden" id="pw"><div class="pb" id="pb" style="width:0%"></div></div>
    <div class="pl hidden" id="pl"></div>
    <div class="ir hidden" id="ir">
      <textarea class="inp" id="ui" placeholder="Ваш ответ..." rows="1"
        onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();sendMsg()}"></textarea>
      <button class="snd" id="bs" onclick="sendMsg()">→</button>
    </div>
  </div>

  <div class="card hidden" id="blk-done">
    <div style="text-align:center;padding:20px 0">
      <div style="font-size:52px;margin-bottom:14px">✅</div>
      <div style="font-size:18px;font-weight:700;color:#FFD700;margin-bottom:8px">ВОР готова!</div>
      <div style="font-size:14px;color:#888;margin-bottom:20px">PDF сформирован и скачивается</div>
      <button class="btn" style="max-width:260px;margin:0 auto" onclick="resetAll()">↩ Новый проект</button>
    </div>
  </div>
</div>

<script>
let pdfFile=null,sid=null;

function doDrag(e,on){e.preventDefault();document.getElementById('dz').classList.toggle('over',on)}
function doDrop(e){e.preventDefault();document.getElementById('dz').classList.remove('over');const f=e.dataTransfer.files[0];if(f&&f.type==='application/pdf')setFile(f)}
function onFile(e){if(e.target.files[0])setFile(e.target.files[0])}
function setFile(f){
  pdfFile=f;const mb=(f.size/1024/1024).toFixed(1);
  document.getElementById('fn').textContent=f.name+' ('+mb+' МБ)';
  document.getElementById('fb').style.display='';
  document.getElementById('btn-go').disabled=false;
  const w=document.getElementById('fw');
  if(f.size>20*1024*1024){w.textContent='⚠️ Файл '+mb+' МБ — система выберет только нужные страницы.';w.classList.remove('hidden')}
  else w.classList.add('hidden');
}
function clearFile(){
  pdfFile=null;document.getElementById('fb').style.display='none';
  document.getElementById('btn-go').disabled=true;document.getElementById('fi').value='';
  document.getElementById('fw').classList.add('hidden');
}
function resetAll(){
  clearFile();sid=null;document.getElementById('chat').innerHTML='';
  ['blk-chat','blk-done'].forEach(id=>document.getElementById(id).classList.add('hidden'));
  document.getElementById('blk-upload').classList.remove('hidden');setStep(1);
}
function setStep(n){[1,2,3,4].forEach(i=>{const e=document.getElementById('s'+i);e.classList.remove('active','done');if(i<n)e.classList.add('done');else if(i===n)e.classList.add('active')})}
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}
function addMsg(role,text){
  const chat=document.getElementById('chat');
  const d=document.createElement('div');d.className='msg '+role;
  d.innerHTML=`<div class="av ${role}">${role==='ai'?'AI':'Вы'}</div><div class="bub ${role}">${esc(text)}</div>`;
  chat.appendChild(d);chat.scrollTop=chat.scrollHeight;
}
function showTyping(){
  const d=document.createElement('div');d.className='msg ai';d.id='typ';
  d.innerHTML='<div class="av ai">AI</div><div class="bub ai"><div class="typing"><span></span><span></span><span></span></div></div>';
  document.getElementById('chat').appendChild(d);document.getElementById('chat').scrollTop=9999;
}
function hideTyping(){const t=document.getElementById('typ');if(t)t.remove()}
function setProg(pct,label){
  document.getElementById('pw').classList.remove('hidden');document.getElementById('pl').classList.remove('hidden');
  document.getElementById('pb').style.width=pct+'%';document.getElementById('pl').textContent=label;
}
function hideProg(){document.getElementById('pw').classList.add('hidden');document.getElementById('pl').classList.add('hidden')}

async function startUpload(){
  if(!pdfFile)return;
  document.getElementById('blk-upload').classList.add('hidden');
  document.getElementById('blk-chat').classList.remove('hidden');
  setStep(2);setProg(10,'Загружаю PDF...');showTyping();
  const fd=new FormData();fd.append('file',pdfFile);
  try{
    setProg(35,'Фильтрую страницы...');
    const r=await fetch('/upload',{method:'POST',body:fd});
    const d=await r.json();if(d.error)throw new Error(d.error);
    sid=d.session_id;
    hideTyping();setProg(90,'Анализирую...');setTimeout(hideProg,1200);
    if(d.stats){
      const c=document.getElementById('chips');c.classList.remove('hidden');
      c.innerHTML=`<div class="chip">Страниц: <span>${d.stats.total}</span></div><div class="chip">Спецификации: <span>${d.stats.spec}</span></div><div class="chip">Чертежи: <span>${d.stats.draw}</span></div>`;
    }
    addMsg('ai',d.reply);
    if(d.has_vor){setTimeout(()=>generatePDF(),600)}
    else{setStep(3);document.getElementById('ir').classList.remove('hidden');document.getElementById('ui').focus()}
  }catch(err){
    hideTyping();hideProg();addMsg('ai','❌ '+err.message);
    document.getElementById('blk-upload').classList.remove('hidden');
    document.getElementById('blk-chat').classList.add('hidden');setStep(1);
  }
}

async function sendMsg(){
  const inp=document.getElementById('ui');const text=inp.value.trim();if(!text||!sid)return;
  inp.value='';addMsg('user',text);document.getElementById('bs').disabled=true;showTyping();
  try{
    const r=await fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:sid,message:text})});
    const d=await r.json();if(d.error)throw new Error(d.error);
    hideTyping();addMsg('ai',d.reply);
    if(d.has_vor){
      document.getElementById('ir').classList.add('hidden');
      addMsg('ai','Все данные получены. Генерирую PDF...');
      setTimeout(()=>generatePDF(),600);
    } else{document.getElementById('bs').disabled=false;document.getElementById('ui').focus()}
  }catch(err){hideTyping();addMsg('ai','❌ '+err.message);document.getElementById('bs').disabled=false}
}

async function generatePDF(){
  setStep(4);document.getElementById('ir').classList.add('hidden');
  try{
    const r=await fetch('/create-payment',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:sid})});
    const d=await r.json();if(d.error)throw new Error(d.error);
    const r2=await fetch('/download/'+sid);
    if(!r2.ok)throw new Error('Ошибка скачивания');
    const blob=await r2.blob();
    const url=URL.createObjectURL(blob);
    const a=document.createElement('a');a.href=url;a.download='VOR.pdf';a.click();
    URL.revokeObjectURL(url);
    document.getElementById('blk-chat').classList.add('hidden');
    document.getElementById('blk-done').classList.remove('hidden');
  }catch(err){addMsg('ai','❌ '+err.message)}
}
</script>
</body>
</html>"""

@app.route("/")
def index():
    return INDEX_HTML

@app.route("/health")
def health():
    return jsonify({"ok":True,"fonts":FONTS_OK})

@app.route("/upload",methods=["POST"])
def upload():
    cleanup()
    if "file" not in request.files: return jsonify({"error":"Файл не загружен"}),400
    f=request.files["file"]
    if not f.filename.lower().endswith(".pdf"): return jsonify({"error":"Только PDF"}),400
    with tempfile.NamedTemporaryFile(suffix=".pdf",delete=False) as tmp:
        f.save(tmp.name);path=tmp.name
    try:
        found_spec,content,stats=analyze_pdf(path)
        os.unlink(path)
    except Exception as e:
        try: os.unlink(path)
        except: pass
        return jsonify({"error":str(e)}),500
    try: reply=ai_first(content)
    except Exception as e: return jsonify({"error":"Claude: "+str(e)}),500
    has_vor="VOR_JSON:" in reply
    sid=str(uuid.uuid4())
    hist=[
        {"role":"user","content":content+"\n\nПроанализируй. Опиши объект, задай вопросы по двоичности. Если нет — сразу VOR_JSON."},
        {"role":"assistant","content":reply}
    ]
    with LOCK:
        SESSIONS[sid]={"status":"chat" if not has_vor else "ready","doc_type":"spec" if found_spec else "drawing",
                       "price":PRICE_SPEC if found_spec else PRICE_NO_SPEC,"history":hist,
                       "vor_text":reply if has_vor else None,"pdf_bytes":None,"ts":time.time()}
    return jsonify({"session_id":sid,"reply":reply,"has_vor":has_vor,
                    "doc_type":"spec" if found_spec else "drawing",
                    "price":PRICE_SPEC if found_spec else PRICE_NO_SPEC,"stats":stats})

@app.route("/chat",methods=["POST"])
def chat():
    d=request.json;sid=d.get("session_id","");msg=d.get("message","").strip()
    with LOCK: sess=SESSIONS.get(sid)
    if not sess: return jsonify({"error":"Сессия не найдена"}),404
    if not msg:  return jsonify({"error":"Пустое сообщение"}),400
    sess["history"].append({"role":"user","content":msg})
    try: reply=ai_chat(sess["history"])
    except Exception as e: return jsonify({"error":str(e)}),500
    sess["history"].append({"role":"assistant","content":reply})
    has_vor="VOR_JSON:" in reply
    if has_vor: sess["vor_text"]=reply;sess["status"]="ready"
    return jsonify({"reply":reply,"has_vor":has_vor})

@app.route("/create-payment",methods=["POST"])
def create_payment():
    d=request.json;sid=d.get("session_id","")
    with LOCK: sess=SESSIONS.get(sid)
    if not sess: return jsonify({"error":"Сессия не найдена"}),404
    if not sess.get("vor_text"): return jsonify({"error":"ВОР ещё не готова"}),400
    try:
        pdf=gen_pdf(parse_vor(sess["vor_text"]))
        sess["pdf_bytes"]=pdf;sess["status"]="paid"
        return jsonify({"free":True,"session_id":sid})
    except Exception as e: return jsonify({"error":"Ошибка генерации: "+str(e)}),500

@app.route("/webhook/yukassa",methods=["POST"])
def webhook():
    try:
        body=request.json
        if body.get("type")!="notification": return "ok",200
        obj=body.get("object",{});status=obj.get("status");sid=obj.get("metadata",{}).get("session_id","")
        if status=="succeeded" and sid:
            with LOCK: sess=SESSIONS.get(sid)
            if sess:
                try:
                    pdf=gen_pdf(parse_vor(sess["vor_text"]))
                    sess["pdf_bytes"]=pdf;sess["status"]="paid"
                except Exception as e:
                    sess["status"]="error";sess["error"]=str(e)
    except: pass
    return "ok",200

RESULT_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Получить ВОР — АйТима</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0A0E1A;color:#e0e6f0;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px}
.card{background:#111827;border:1px solid #1e2d45;border-radius:16px;padding:32px;text-align:center;max-width:440px;width:100%}
.logo{display:flex;align-items:center;justify-content:center;gap:10px;margin-bottom:24px}
.logo-mark{width:36px;height:36px;border-radius:9px;background:linear-gradient(135deg,#00E5FF,#4F46E5);display:flex;align-items:center;justify-content:center;font-weight:700;font-size:13px;color:#0A0E1A}
.logo-name{font-size:16px;font-weight:600;color:#00E5FF}
.icon{font-size:52px;margin-bottom:16px}
.title{font-size:20px;font-weight:600;color:#fff;margin-bottom:8px}
.desc{font-size:14px;color:#6a8aaa;margin-bottom:20px;line-height:1.6}
.pw{background:#0d1929;border-radius:8px;overflow:hidden;height:8px;margin-bottom:8px}
.pb{height:100%;background:linear-gradient(90deg,#00C8E0,#4F46E5);transition:width .5s}
.pl{font-size:13px;color:#4a6080;margin-bottom:16px}
.btn{display:inline-flex;align-items:center;justify-content:center;padding:12px 28px;font-size:14px;font-weight:500;border-radius:10px;border:none;cursor:pointer;background:linear-gradient(135deg,#00C8E0,#4F46E5);color:#fff;text-decoration:none;margin-top:8px}
.btn:hover{opacity:.9}
.btn-green{background:linear-gradient(135deg,#00C040,#007830)}
.hidden{display:none!important}
</style>
</head>
<body>
<div class="card">
  <div class="logo"><div class="logo-mark">AT</div><div class="logo-name">АйТима</div></div>
  <div id="blk-processing">
    <div class="icon">⚙️</div>
    <div class="title">Генерирую ВОР...</div>
    <div class="desc">Оплата получена. Формирую PDF документ.<br>Обычно это занимает 30–60 секунд.</div>
    <div class="pw"><div class="pb" id="pb" style="width:10%"></div></div>
    <div class="pl" id="pl">Проверяю оплату...</div>
  </div>
  <div id="blk-done" class="hidden">
    <div class="icon">✅</div>
    <div class="title">ВОР готова!</div>
    <div class="desc">PDF сформирован. Скачивание начнётся автоматически.</div>
    <a id="dl-btn" href="#" class="btn btn-green">⬇ Скачать ВОР PDF</a>
    <br>
    <a href="/" class="btn" style="margin-top:10px;background:transparent;border:1px solid #1e3a5a;color:#7a9abb;padding:8px 20px;font-size:13px">↩ Новый проект</a>
  </div>
  <div id="blk-error" class="hidden">
    <div class="icon">❌</div>
    <div class="title">Ошибка генерации</div>
    <div class="desc" id="err-msg">Что-то пошло не так. Напишите нам — @tima_sebastian_pereiro</div>
    <a href="/" class="btn">↩ На главную</a>
  </div>
</div>
<script>
const sid='__SID__';
let attempts=0;
const labels=['Проверяю оплату...','Подтверждаю платёж...','Готовлю данные...','Анализирую проект...','Формирую разделы ВОР...','Создаю таблицы...','Генерирую PDF...','Встраиваю шрифты...','Финальная проверка...','Почти готово...','Завершаю...'];
const pcts=[10,20,35,50,62,72,80,87,93,97,99];

function setProg(pct,label){document.getElementById('pb').style.width=pct+'%';document.getElementById('pl').textContent=label}

async function poll(){
  if(!sid||sid==='__SID__'){showError('Сессия не найдена');return}
  attempts++;
  setProg(pcts[Math.min(attempts-1,pcts.length-1)],labels[Math.min(attempts-1,labels.length-1)]);
  try{
    const r=await fetch('/status/'+sid);const d=await r.json();
    if(d.status==='paid'){
      document.getElementById('blk-processing').classList.add('hidden');
      document.getElementById('blk-done').classList.remove('hidden');
      document.getElementById('dl-btn').href='/download/'+sid;
      setTimeout(()=>{const a=document.createElement('a');a.href='/download/'+sid;a.click()},500);
      return;
    }
    if(d.status==='error'){showError(d.error||'Ошибка генерации PDF');return}
    if(attempts>=40){showError('Превышено время ожидания. Напишите нам — вышлем ВОР вручную.');return}
    setTimeout(poll,3000);
  }catch(e){if(attempts<40)setTimeout(poll,3000);else showError('Ошибка соединения')}
}

function showError(msg){
  document.getElementById('blk-processing').classList.add('hidden');
  document.getElementById('blk-error').classList.remove('hidden');
  document.getElementById('err-msg').textContent=msg;
}
poll();
</script>
</body>
</html>
"""

@app.route("/payment-result")
def payment_result():
    sid=request.args.get("session_id","")
    return RESULT_HTML.replace("__SID__",sid)

@app.route("/status/<sid>")
def status(sid):
    with LOCK: sess=SESSIONS.get(sid)
    if not sess: return jsonify({"status":"not_found"}),404
    return jsonify({"status":sess["status"],"error":sess.get("error","")})

@app.route("/download/<sid>")
def download(sid):
    with LOCK: sess=SESSIONS.get(sid)
    if not sess: abort(404)
    if sess["status"]!="paid" or not sess.get("pdf_bytes"): abort(403)
    return send_file(io.BytesIO(sess["pdf_bytes"]),mimetype="application/pdf",
                     as_attachment=True,download_name=f"VOR_{sid[:8]}.pdf")

if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT",5000)))
