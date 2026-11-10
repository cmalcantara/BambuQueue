# /// script
# requires-python = ">=3.12"
# dependencies = []
#
# [tool.orcaslicer.plugin]
# name = "BambuQueue"
# description = "Queue sliced plates to a Bambuddy print server: wraps each plate into a .gcode.3mf, uploads it, queues it with plate-swap G-code injection, and shows the live queue with ETA, auto-eject on stop, and reorder / duplicate / delete."
# author = "cmalcantara"
# version = "0.4.2"
# ///
"""BambuQueue for OrcaSlicer -> Bambuddy.

Capabilities
  * CapturePlate (SlicingPipeline) - on every slice, stage the working G-code
    to data_dir()/bambuddy_queue/staging.gcode (+staging.json).
  * Panel (Script) - docked panel. "Add plate" wraps the staged G-code into a
    .gcode.3mf (template.3mf shipped next to this file, entries swapped like
    Bambuddy's own injector does), uploads POST /api/v1/library/files, then
    queues POST /api/v1/queue/ with gcode_injection=true. Live queue list:
    move up/down, duplicate (loop N via re-add), delete.

Naming: YYMMDD-<project name>.gcode.3mf (date-first, FAT32-safe).
Config: data_dir()/bambuddy_queue/config.json {"url": ..., "printer_id": ...}
"""
import os, json, time, uuid, shutil, hashlib, threading, zipfile, io
import urllib.request, urllib.error
import orca

def _data_dir():
    p = os.path.abspath(__file__)
    for _ in range(3):
        p = os.path.dirname(p)
    return p

STORE_ROOT  = os.path.join(_data_dir(), "bambuddy_queue")
STAGING     = os.path.join(STORE_ROOT, "staging.gcode")
TEMPLATE    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "template.3mf")
RELEASE_3MF = os.path.join(os.path.dirname(os.path.abspath(__file__)), "release.3mf")
CONFIG_PATH = os.path.join(STORE_ROOT, "config.json")
DEFAULT_URL = "http://localhost:2111"   # edit in the panel to point at your Bambuddy host

# ------------------------------------------------------------------ config
def load_config():
    try:
        with open(CONFIG_PATH) as f:
            c = json.load(f)
    except Exception:
        c = {}
    c.setdefault("url", DEFAULT_URL)
    c.setdefault("printer_id", None)
    return c

def save_config(c):
    os.makedirs(STORE_ROOT, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(c, f, indent=2)

# ------------------------------------------------------------------ http helpers (stdlib only)
def _req(method, url, body=None, content_type="application/json", timeout=20):
    if isinstance(body, (bytes, bytearray)):
        data = body
    elif body is not None:
        data = json.dumps(body).encode()
    else:
        data = None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError("HTTP %s: %s" % (e.code, e.read().decode("utf-8", "ignore")[:200]))
    except Exception as e:
        raise RuntimeError("Bambuddy unreachable: %s" % e)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None

def api_get(cfg, path):
    return _req("GET", cfg["url"].rstrip("/") + "/api/v1" + path)

def api_post(cfg, path, body=None):
    return _req("POST", cfg["url"].rstrip("/") + "/api/v1" + path, body)

def api_delete(cfg, path):
    return _req("DELETE", cfg["url"].rstrip("/") + "/api/v1" + path)

def upload_3mf(cfg, filename, blob):
    """multipart upload to /library/files; returns file id."""
    boundary = "----bq" + uuid.uuid4().hex
    parts = []
    parts.append(("--" + boundary + "\r\n"
                  'Content-Disposition: form-data; name="file"; filename="%s"\r\n'
                  "Content-Type: application/octet-stream\r\n\r\n" % filename).encode())
    parts.append(blob)
    parts.append(("\r\n--" + boundary + "--\r\n").encode())
    body = b"".join(parts)
    url = cfg["url"].rstrip("/") + "/api/v1/library/files?generate_stl_thumbnails=false"
    resp = _req("POST", url, body,
                content_type="multipart/form-data; boundary=" + boundary, timeout=60)
    fid = None
    if isinstance(resp, dict):
        fid = resp.get("id") or resp.get("file_id")
    if not fid:
        raise RuntimeError("upload returned no file id: %r" % (resp,))
    return fid

# ------------------------------------------------------------------ wrap staged gcode
def sanitize(name):
    keep = []
    for ch in (name or "").strip():
        keep.append(ch if (ch.isalnum() or ch in "-._") else "-")
    s = "".join(keep).strip("-.")
    while "--" in s:
        s = s.replace("--", "-")
    return (s[:60] or "plate").rstrip("-.")

def default_name(meta):
    """YYMMDD-HHMM-<project>-pN — readable, unique, FAT32-safe."""
    project = sanitize(meta.get("project_name") or "")[:40].rstrip("-.")
    if not project:
        project = "plate"
    return "%s-%s-p%s" % (time.strftime("%y%m%d-%H%M"), project,
                          meta.get("plate_no") or "1")

def parse_gcode_estimates(gcode):
    """Pull real per-plate estimates out of the staged gcode.

    Returns dict with seconds, grams, meters, first_layer_s, colour, ftype.
    E-movement integration matches Bambuddy's own layer_filament parser
    (positive extrusion only, relative E, PETG/PLA density from header)."""
    import math
    import re
    txt = gcode.decode("utf-8", "ignore")
    out = {}
    m = re.search(r"total estimated time:\s*((?:\d+h\s*)?(?:\d+m\s*)?(?:\d+s?)?)", txt)
    if m:
        secs = 0
        for v, u in re.findall(r"(\d+)\s*([hms])", m.group(1)):
            secs += int(v) * {"h": 3600, "m": 60, "s": 1}[u]
        out["seconds"] = secs
    m = re.search(r"; estimated first layer printing time[^=]*=\s*([\dhms ]+)", txt)
    if m:
        secs = 0
        for v, u in re.findall(r"(\d+)\s*([hms])", m.group(1)):
            secs += int(v) * {"h": 3600, "m": 60, "s": 1}[u]
        out["first_layer_s"] = secs
    m = re.search(r"; filament_density:\s*([\d.]+)", txt)
    density = float(m.group(1)) if m else 1.27
    m = re.search(r"; filament_diameter:\s*([\d.]+)", txt)
    diam = float(m.group(1)) if m else 1.75
    area = math.pi * (diam / 2.0) ** 2          # mm^2 per mm of extrusion
    e_mm = 0.0
    relative = True          # Orca emits M83; M82 switches to absolute E
    last_e = 0.0
    for line in txt.splitlines():
        stripped = line.strip()
        if stripped == "M83":
            relative = True
            continue
        if stripped == "M82":
            relative = False
            continue
        if stripped.startswith("G92"):
            em = re.search(r"E([-\d.]+)", stripped)
            last_e = float(em.group(1)) if em else 0.0
            continue
        if not (stripped.startswith("G1") or stripped.startswith("G0")) or " E" not in line:
            continue
        em = re.search(r"\sE([-\d.]+)", line)
        if not em:
            continue
        e = float(em.group(1))
        if relative:
            if e > 0:
                e_mm += e
        else:
            if e > last_e:
                e_mm += e - last_e
            last_e = e
    grams = e_mm * area * density / 1000.0      # mm^3 * (g/cm^3) / 1000 = g
    out["grams"] = round(grams, 2)
    out["meters"] = round(e_mm / 1000.0, 2)
    m = re.search(r"^; filament_type = ([A-Z0-9;&; -]+)$", txt, re.M)
    if m:
        out["ftype"] = m.group(1).split(";")[0].strip()
    m = re.search(r"^; filament_colour = (#\w+)", txt, re.M)
    if m:
        out["colour"] = m.group(1)
    return out


def patch_slice_info(slice_xml, est):
    """Rewrite prediction/weight/first_layer_time/<filament> in
    slice_info.config with real values so Bambuddy UI shows per-plate truth."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(slice_xml)
    except ET.ParseError:
        return slice_xml
    plate = root.find(".//plate")
    if plate is None:
        # pre-export project .3mf carries only <header>; Bambuddy still reads
        # prediction/weight from <plate> — synthesize a minimal plate record.
        plate = ET.SubElement(root, "plate")
        ET.SubElement(plate, "metadata", {"key": "index", "value": "1"})
    def _set_meta(key, val):
        for meta in plate.findall("metadata"):
            if meta.get("key") == key:
                meta.set("value", val)
                return
        ET.SubElement(plate, "metadata", {"key": key, "value": val})
    if "seconds" in est:
        _set_meta("prediction", str(int(est["seconds"])))
    if "grams" in est:
        _set_meta("weight", "%.2f" % est["grams"])
    if "first_layer_s" in est:
        _set_meta("first_layer_time", str(float(est["first_layer_s"])))
    filaments = plate.findall("filament")
    if not filaments and "grams" in est:
        filaments = [ET.SubElement(plate, "filament", {"id": "1"})]
    for fil in filaments:
        if "grams" in est:
            fil.set("used_g", "%.2f" % est["grams"])
            fil.set("used_m", "%.2f" % est["meters"])
        if est.get("ftype"):
            fil.set("type", est["ftype"])
        if est.get("colour"):
            fil.set("color", est["colour"])
    return ET.tostring(root, encoding="unicode").encode()


def package_staged(cfg, name=None):
    """Wrap staging.gcode into template.3mf -> bytes; returns (bytes, filename).

    Template supplies only the preview PNGs (Orca renders those at export
    time); gcode, model, configs and filament sequence come from the real
    sliced project when staged."""
    gcode = open(STAGING, "rb").read()
    if not gcode.strip():
        raise RuntimeError("staged plate is empty")
    try:
        meta = json.load(open(STAGING + ".json"))
    except Exception:
        meta = {}
    fname = sanitize(name or default_name(meta)) + ".gcode.3mf"
    real = {}
    pj = meta.get("project_3mf") or os.path.join(STORE_ROOT, "project.3mf")
    if pj and os.path.isfile(pj):
        try:
            with zipfile.ZipFile(pj) as rz:
                for n in rz.namelist():
                    if n.startswith("Metadata/") or n in (
                            "3D/3dmodel.model", "3D/_rels/3dmodel.model.rels",
                            "Metadata/_rels/model_settings.config.rels"):
                        real[n] = rz.read(n)
        except (OSError, zipfile.BadZipFile):
            real = {}
    md5 = hashlib.md5(gcode).hexdigest().upper().encode()
    est = parse_gcode_estimates(gcode)
    out = io.BytesIO()
    with zipfile.ZipFile(TEMPLATE) as zin, \
         zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            if item.filename == "Metadata/plate_1.gcode":
                zout.writestr("Metadata/plate_1.gcode", gcode)
            elif item.filename == "Metadata/plate_1.gcode.md5":
                zout.writestr(item, md5)
            elif item.filename == "Metadata/slice_info.config":
                data = real.get(item.filename) or zin.read(item.filename)
                zout.writestr(item, patch_slice_info(data, est))
            elif item.filename in real:
                zout.writestr(item, real[item.filename])
            else:
                zout.writestr(item, zin.read(item.filename))
    return out.getvalue(), fname

# ------------------------------------------------- workdir auto-capture (no preset needed)
_seen_gcode = {}  # path -> (size, mtime); only stage size-stable outputs

def _model_roots():
    """Candidate <tmp>/orcaslicer_<uid>/orcaslicer_model roots, cross-platform.
    The <uid> suffix is the runtime user id — never hardcode it. Windows sets
    TEMP/LOCALAPPDATA rather than TMPDIR, so check several temp homes."""
    tmps = []
    for env in ("TMPDIR", "TEMP", "TMP"):
        v = os.environ.get(env)
        if v and v not in tmps:
            tmps.append(v)
    for extra in ("/tmp", os.path.join(os.path.expanduser("~"),
                                       "AppData", "Local", "Temp")):
        if extra not in tmps:
            tmps.append(extra)
    roots = []
    for tmp in tmps:
        try:
            entries = os.listdir(tmp)
        except OSError:
            continue
        for d in entries:
            if d.startswith("orcaslicer_"):
                r = os.path.join(tmp, d, "orcaslicer_model")
                if os.path.isdir(r) and r not in roots:
                    roots.append(r)
    return roots

def _newest_gcode():
    best = None
    for root in _model_roots():
        for day in os.listdir(root):
            try:
                sess_list = os.listdir(os.path.join(root, day))
            except OSError:
                continue
            for sess in sess_list:
                gdir = os.path.join(root, day, sess, "Metadata")
                if not os.path.isdir(gdir):
                    continue
                try:
                    files = os.listdir(gdir)
                except OSError:
                    continue
                for g in files:
                    if not (g.endswith(".gcode") and g.startswith(".")):
                        continue
                    p = os.path.join(gdir, g)
                    try:
                        stt = os.stat(p)
                    except OSError:
                        continue
                    if best is None or stt.st_mtime > best[1]:
                        best = (p, stt.st_mtime, stt.st_size)
    return best

def stage_workdir(gcode_path):
    try:
        os.makedirs(STORE_ROOT, exist_ok=True)
        shutil.copyfile(gcode_path, STAGING)
        workdir = os.path.dirname(os.path.dirname(os.path.abspath(gcode_path)))
        meta = {"output_name": gcode_path}
        cand = os.path.join(workdir, ".3mf")
        if os.path.isfile(cand):
            shutil.copyfile(cand, os.path.join(STORE_ROOT, "project.3mf"))
            meta["project_3mf"] = os.path.join(STORE_ROOT, "project.3mf")
        orig = os.path.join(workdir, "origin.txt")
        if os.path.isfile(orig):
            try:
                with open(orig, encoding="utf-8", errors="ignore") as f:
                    op = f.read().strip()
                if op:
                    meta["project_name"] = os.path.splitext(os.path.basename(op))[0]
            except OSError:
                pass
        import re as _re
        m = _re.search(r"\.(\d+)\.gcode$", gcode_path)
        if m:
            meta["plate_no"] = str(int(m.group(1)) + 1)
        with open(STAGING + ".json", "w") as f:
            json.dump(meta, f)
        return True
    except Exception:
        return False

def auto_stage(force=False):
    """Stage the newest stable sliced plate if newer than our staging.
    force=True (user pressed Add): skip the two-tick settle wait — if the
    user is clicking, the slice is done; never queue yesterday's plate."""
    b = _newest_gcode()
    if not b:
        return False
    path, mtime, size = b
    try:
        smtime = os.path.getmtime(STAGING)
    except OSError:
        smtime = 0
    if mtime <= smtime + 0.5:
        return False
    prev = _seen_gcode.get(path)
    if not force and prev != (size, mtime):   # wait one tick to ensure write finished
        _seen_gcode[path] = (size, mtime)
        return False
    return stage_workdir(path)

# ------------------------------------------------------------------ panel page
PAGE = r"""<!doctype html><html><head><meta charset=utf8>
<style>
 body{font-family:var(--orca-font);color:var(--orca-fg);margin:0;padding:8px;font-size:13px;min-width:320px;box-sizing:border-box}
 h2{font-size:14px;margin:0 0 6px}
 .row{display:flex;gap:4px;align-items:center;margin:4px 0}
 label{color:var(--orca-muted);min-width:44px}
 button{background:var(--orca-accent);color:var(--orca-accent-fg);border:0;border-radius:4px;padding:6px 10px;cursor:pointer;font:inherit}
 button.ghost{background:transparent;color:var(--orca-fg);border:1px solid var(--orca-border);border-radius:4px;padding:2px 8px;line-height:1.5}
 button.ghost:hover:not(:disabled){background:rgba(128,128,128,.14)}
 button:disabled{opacity:.45;cursor:default}
 input,select{background:var(--orca-bg);color:var(--orca-fg);border:1px solid var(--orca-border);border-radius:4px;padding:4px 6px;font:inherit}
 .sect{color:var(--orca-muted);font-size:12px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;margin:10px 0 2px}
 .item{border:0;border-bottom:1px solid var(--orca-border);padding:5px 4px;margin:0}
 .item:last-child{border-bottom:0}
 .item[data-pid]{cursor:grab}
 .item.dragging{opacity:.4;cursor:grabbing}
 .grip{color:var(--orca-muted);margin-right:4px}
 .item .top{display:flex;gap:6px;align-items:center}
 .item .nm{flex:1;white-space:normal;word-break:break-word;line-height:1.35}
 .st{font-size:12px;padding:1px 6px;border-radius:8px;border:1px solid var(--orca-border);color:var(--orca-muted)}
 .st.running{color:#e8a33d;border-color:#e8a33d}
 .st.pending{color:#5bbf6a;border-color:#5bbf6a}
 .st.dispatched{color:#5bbf6a;border-color:#5bbf6a}
 #status{margin-top:8px;font-size:12px;color:var(--orca-muted);min-height:16px;word-break:break-all}
 .muted{color:var(--orca-muted)}
 .inj{font-size:11px;color:#5bbf6a}
</style></head><body>
<div id=main>
<div class=row style="margin:0 0 4px"><span id=ver class=muted style="font-size:11px"></span></div>
<div class=row><label>Server</label><input id=url style="flex:1">
  <label style="min-width:0">Printer</label><select id=printer style="max-width:120px"></select></div>
<div class=row><input id=name style="flex:1" placeholder="job name — set when you slice"></div>
<button id=add style="width:100%">＋ Add current plate to Bambuddy queue</button>
<div class=sect>Queue</div>
<div id=queue style="margin-top:2px"></div>
<div class=row><button id=refresh class=ghost>Refresh</button>
  <span id=sum class=muted style="font-size:13px"></span></div>
<div id=status></div>
</div>
<script>
const $=id=>document.getElementById(id);
let S={url:"",printer_id:null,printers:[],items:[],staged:false,ok:false,name:""};
let prevDefault="";
function btn(t,fn,dis){const b=document.createElement("button");b.className="ghost";b.textContent=t;if(dis)b.disabled=true;b.onclick=fn;return b}
function render(){
  if(document.activeElement!==$("url"))$("url").value=S.url;
  const sel=$("printer");const cur=sel.value;sel.innerHTML="";
  (S.printers||[]).forEach(p=>{const o=document.createElement("option");o.value=p.id;o.textContent=p.name;sel.appendChild(o)});
  sel.value=S.printer_id||cur||(S.printers[0]&&S.printers[0].id);
  // prefill name on NEW slice only; manual edits survive refresh ticks
  if(S.name && S.name!==prevDefault){ $("name").value=S.name; }
  else if(!S.name && prevDefault){ $("name").value=""; }
  prevDefault=S.name;
  $("add").disabled=!S.staged||!S.ok;
  const q=$("queue");q.innerHTML="";
  const items=S.items||[];let pend=0;
  items.forEach((it,i)=>{
    if(it.status==="pending")pend++;
    const d=document.createElement("div");d.className="item";
    const top=document.createElement("div");top.className="top";
    const idx=document.createElement("span");idx.className="muted";idx.textContent=(i+1)+".";
    const nm=document.createElement("span");nm.className="nm";nm.textContent=it.library_file_name||("#"+it.id);
    top.append(idx,nm);    const meta=document.createElement("div");meta.className="muted";
    meta.style.cssText="font-size:12px;margin:1px 0 4px 16px";
    if(it.status==="printing"){
      const badge=document.createElement("span");badge.textContent="PRINTING";
      badge.style.cssText="font-size:11px;font-weight:700;color:#e8a33d;background:rgba(232,163,61,.14);border-radius:8px;padding:1px 7px;margin-left:8px";
      top.append(badge);
      const bits=[];
      if(S.active&&S.active.progress!=null)bits.push(S.active.progress+"%");
      meta.textContent=bits.join(" · ");
      if(S.active&&S.active.remaining_min!=null){
        meta.append(" · ETA ");
        const sp=document.createElement("span");sp.className="eta";
        sp.dataset.dl=String((S.__recv||Date.now())+S.active.remaining_min*60000);
        meta.append(sp);
      }
      const row=document.createElement("div");row.className="row";
      row.append(btn("\u29c9",()=>orca.postMessage({type:"dupe",id:it.id})));
      const sb=btn("STOP",()=>orca.postMessage({type:"stop",id:it.id}));
      sb.style.cssText="color:#e5484d;border-color:#e5484d";
      row.append(sb);
      d.append(top,meta,row);q.appendChild(d);return;
    }
    d.append(top);
    meta.textContent=(it.pts?Math.round(it.pts/60)+"m":"")+((it.grams!==null&&it.grams!==undefined)?" · "+(+it.grams).toFixed(1)+"g":"");
    d.append(meta);
    const row=document.createElement("div");row.className="row";
    row.append(btn("\u29c9",()=>orca.postMessage({type:"dupe",id:it.id}),it.status!=="pending"));
    row.append(btn("×",()=>orca.postMessage({type:"del",id:it.id}),it.status!=="pending"));
    d.append(row);
    if(it.status==="pending"){
      const grip=document.createElement("span");grip.className="grip";grip.textContent="\u283f";
      top.prepend(grip);
      d.dataset.pid=it.id;
    }
    q.appendChild(d);
  });
  $("sum").textContent=items.length?items.length+" items ("+pend+" pending)":"queue empty";
  if(!items.length)q.innerHTML='<div class=muted>Nothing queued.</div>';
}
$("add").onclick=()=>orca.postMessage({type:"add",name:$("name").value.trim()});
// pointer-based reorder: HTML5 dragstart never fires inside Orca's webview
(function(){
  let drag=null;
  $("queue").addEventListener("pointerdown",e=>{
    const card=e.target.closest?e.target.closest(".item[data-pid]"):null;
    if(!card||e.target.closest("button"))return;
    e.preventDefault();
    drag={card:card,y:e.clientY,moved:false};
    card.classList.add("dragging");
    card.setPointerCapture&&card.setPointerCapture(e.pointerId);
  });
  $("queue").addEventListener("pointermove",e=>{
    if(!drag)return;
    if(Math.abs(e.clientY-drag.y)<4)return;
    drag.moved=true;
    const q=$("queue");
    const cards=[...q.querySelectorAll(".item[data-pid]")].filter(c=>c!==drag.card);
    let target=null;
    for(const c of cards){const r=c.getBoundingClientRect();if(e.clientY<r.top+r.height/2){target=c;break;}}
    if(target)q.insertBefore(drag.card,target);
    else if(cards.length)q.insertBefore(drag.card,cards[cards.length-1].nextSibling);
  });
  $("queue").addEventListener("pointerup",endDrag);
  document.addEventListener("pointerup",endDrag);
  document.addEventListener("pointercancel",endDrag);
  function endDrag(e){
    if(!drag)return;
    drag.card.classList.remove("dragging");
    if(drag.moved){
      const ids=[...$("queue").querySelectorAll(".item[data-pid]")].map(c=>+c.dataset.pid);
      orca.postMessage({type:"reorder",ids:ids});
    }
    drag=null;
  }
})();
$("refresh").onclick=()=>orca.postMessage({type:"refresh"});
$("url").onchange=e=>orca.postMessage({type:"url",url:e.target.value.trim()});
$("printer").onchange=e=>orca.postMessage({type:"printer",printer_id:parseInt(e.target.value,10)});
orca.onMessage(m=>{
  if(m.type==="state"){
    if(m.v)$("ver").textContent="v"+m.v;m.__recv=Date.now();S=m;render();tickEta();}
  else if(m.type==="status"){$("status").textContent=m.message;}
});
function tickEta(){document.querySelectorAll(".eta").forEach(e=>{
  const dl=+e.dataset.dl;if(!dl)return;
  e.textContent=new Date(dl).toLocaleTimeString([],{hour:"2-digit",minute:"2-digit",hour12:true});});}
setInterval(tickEta,1000);
orca.postMessage({type:"refresh"});
setInterval(()=>orca.postMessage({type:"refresh"}),6000);
</script></body></html>
"""

# ------------------------------------------------------------------ capabilities
PANEL = None   # live Panel capability instance; CapturePlate uses it to restore the dock

class CapturePlate(orca.slicing.SlicingPipelineCapabilityBase):
    def get_name(self):
        return "BambuQueue Capture"
    def execute(self, ctx):
        if ctx.step != orca.slicing.Step.psGCodePostProcess:
            return orca.ExecutionResult.success()
        try:
            os.makedirs(STORE_ROOT, exist_ok=True)
            shutil.copyfile(ctx.gcode_path, STAGING)
            meta = {"output_name": getattr(ctx, "output_name", "") or ""}
            # slice working dir: <workdir>/Metadata/<gcode>; the real sliced
            # project (model/configs/filament) is <workdir>/.3mf, the original
            # file path is <workdir>/origin.txt. Staging both gives Bambuddy
            # true per-plate metadata instead of template leftovers.
            workdir = os.path.dirname(os.path.dirname(os.path.abspath(str(ctx.gcode_path))))
            cand = os.path.join(workdir, ".3mf")
            if os.path.isfile(cand):
                shutil.copyfile(cand, os.path.join(STORE_ROOT, "project.3mf"))
                meta["project_3mf"] = os.path.join(STORE_ROOT, "project.3mf")
            orig = os.path.join(workdir, "origin.txt")
            if os.path.isfile(orig):
                try:
                    with open(orig, encoding="utf-8", errors="ignore") as f:
                        op = f.read().strip()
                    if op:
                        meta["project_name"] = os.path.splitext(os.path.basename(op))[0]
                except OSError:
                    pass
            import re as _re
            m = _re.search(r"\.(\d+)\.gcode$", meta["output_name"])
            if m:
                meta["plate_no"] = str(int(m.group(1)) + 1)
            with open(STAGING + ".json", "w") as f:
                json.dump(meta, f)
            return orca.ExecutionResult.success("staged")
        except Exception as e:
            return orca.ExecutionResult.failure(
                orca.PluginResult.RecoverableError, f"stage failed: {e}")

class Panel(orca.script.ScriptPluginCapabilityBase):
    def get_name(self):
        return "BambuQueue"
    def on_load(self):
        global PANEL
        PANEL = self
        self.cfg = load_config()
        self._open_panel()
        self._push()
    def on_close(self):
        self.panel = None
    def _open_panel(self):
        self.panel = orca.host.ui.create_dock_panel(
            html=PAGE, title="BambuQueue", width=360, height=600,
            on_message=self.on_message, on_close=self.on_close, dock="right")
    def _is_open(self):
        p = getattr(self, "panel", None)
        if p is None:
            return False
        try:
            v = getattr(p, "is_open", True)
            if callable(v):
                v = v()
            return bool(v)
        except Exception:
            return False

    # ---- state push
    def _collect(self):
        auto_stage()
        st = {"type": "state", "v": "0.4.2", "url": self.cfg["url"], "printer_id": self.cfg["printer_id"],
              "printers": [], "items": [], "staged": os.path.exists(STAGING), "ok": False,
              "name": ""}
        try:
            st["name"] = default_name(json.load(open(STAGING + ".json")))
        except Exception:
            pass
        self.last_default = st["name"]
        errs = []
        try:
            pr = api_get(self.cfg, "/printers/")
            lst = pr.get("printers") if isinstance(pr, dict) else pr
            st["printers"] = [{"id": p["id"], "name": p.get("name")}
                              for p in (lst or []) if p.get("is_active", True)]
            # default-select the first active printer so Add works without a click
            if not self.cfg.get("printer_id") and st["printers"]:
                self.cfg["printer_id"] = st["printers"][0]["id"]
                save_config(self.cfg)
                st["printer_id"] = self.cfg["printer_id"]
        except Exception as e:
            errs.append("printers: " + str(e))
        try:
            q = api_get(self.cfg, "/queue/")
            st["items"] = [{"id": i["id"], "library_file_name": i.get("library_file_name"),
                            "status": i["status"],
                            "pts": i.get("print_time_seconds"),
                            "grams": i.get("filament_used_grams"),
                            "gcode_injection": i.get("gcode_injection")}
                           for i in (q or []) if i["status"] in
                           ("pending", "printing", "running", "dispatched", "paused")]
            st["ok"] = True
        except Exception as e:
            errs.append("queue: " + str(e))
        # live print state: Bambu mc_remaining_time is MINUTES; ETA = recv + remaining.
        try:
            ps = api_get(self.cfg, "/printers/%s/status" % (self.cfg.get("printer_id") or 1))
            if (ps.get("state") or "").upper() in ("RUNNING", "PAUSED", "PRINTING"):
                st["active"] = {"state": ps.get("state"), "progress": ps.get("progress"),
                                "remaining_min": ps.get("remaining_time"),
                                "gcode_file": ps.get("gcode_file")}
        except Exception:
            pass
        if errs:
            st["_err"] = "; ".join(errs)
        return st
    def _post(self, obj):
        if not self._is_open():
            return
        try:
            self.panel.post(obj)
        except Exception:
            pass
    def _push(self, err=None):
        if not self._is_open():
            return
        st = self._collect()
        self._post(st)
        if err:
            self._post({"type": "status", "message": "✗ " + err})
        elif st.get("_err"):
            self._post({"type": "status", "message": "✗ " + st["_err"]})
    def _async(self, fn):
        def run():
            try:
                msg = fn()
            except Exception as e:
                msg = str(e)
            self._post({"type": "status", "message": ("✓ " if not msg.startswith(("✗", "!")) else "") + msg})
            self._push()
        threading.Thread(target=run, daemon=True).start()

    # ---- messages
    def on_message(self, msg):
        if getattr(self, "panel", None) is None:
            return
        t = msg.get("type")
        try:
            if t == "url":
                self.cfg["url"] = msg["url"] or DEFAULT_URL
                save_config(self.cfg); self._push(); return
            if t == "printer":
                self.cfg["printer_id"] = msg["printer_id"]
                save_config(self.cfg); self._push(); return
            if t == "refresh":
                self._push(); return
        except Exception as e:
            self._post({"type": "status", "message": "✗ " + str(e)}); return

        if t == "add":
            self._async(lambda: self._add(msg.get("name")))
        elif t == "del":
            self._async(lambda: (api_delete(self.cfg, "/queue/%s" % msg["id"]), "deleted")[1])
        elif t == "reorder":
            self._async(lambda: self._reorder(msg.get("ids") or []))
        elif t == "dupe":
            self._async(lambda: self._dupe(msg["id"]))
        elif t == "move":
            self._async(lambda: self._move(msg["id"], int(msg["dir"])))
        elif t == "stop":
            self._async(lambda: self._stop(msg["id"]))

    def _add(self, name=None):
        auto_stage(force=True)   # never queue a stale plate because ticks lagged
        if name and name == getattr(self, "last_default", ""):
            name = None          # box still shows auto default → regenerate fresh
        if not os.path.exists(STAGING):
            return "! slice a plate first"
        if not self.cfg.get("printer_id"):
            pr = api_get(self.cfg, "/printers/")
            lst = pr.get("printers") if isinstance(pr, dict) else pr
            act = [p for p in (lst or []) if p.get("is_active", True)]
            if not act:
                return "! no active printers in Bambuddy"
            self.cfg["printer_id"] = act[0]["id"]
            save_config(self.cfg)
        blob, fname = package_staged(self.cfg, name)
        fid = upload_3mf(self.cfg, fname, blob)
        api_post(self.cfg, "/queue/", {"library_file_id": fid,
                                       "printer_id": self.cfg["printer_id"],
                                       "gcode_injection": True, "quantity": 1})
        return "queued %s" % fname

    def _dupe(self, item_id):
        items = api_get(self.cfg, "/queue/") or []
        it = next((i for i in items if i["id"] == item_id), None)
        if not it or not it.get("library_file_id"):
            return "! source item not found"
        api_post(self.cfg, "/queue/", {"library_file_id": it["library_file_id"],
                                       "printer_id": it["printer_id"],
                                       "gcode_injection": it.get("gcode_injection", True),
                                       "quantity": 1})
        return "duplicated"

    def _ensure_release_file(self):
        """One-time upload of the plate-release job (end block + swap motion,
        no extrusion) so a stopped print can still eject its plate by itself."""
        if self.cfg.get("release_file_id"):
            return self.cfg["release_file_id"]
        with open(RELEASE_3MF, "rb") as f:
            blob = f.read()
        fid = upload_3mf(self.cfg, "ZZ-plate-release.gcode.3mf", blob)
        self.cfg["release_file_id"] = fid
        save_config(self.cfg)
        return fid

    def _stop(self, item_id):
        """Stop + self-eject: queue the plate-release job directly behind the
        printing item, reorder it there, THEN stop — so the scheduler's next
        dispatch is the eject, after which the next plate proceeds normally.
        (A stop aborts the job, so the injected swap never runs otherwise.)"""
        items = api_get(self.cfg, "/queue/") or []
        cur = next((i for i in items if i["id"] == item_id), None)
        if not cur or cur["status"] != "printing":
            return "! item is not printing"
        if not os.path.exists(RELEASE_3MF):
            # no plate-release job shipped (plain setup) — plain stop only
            api_post(self.cfg, "/queue/%s/stop" % item_id, {})
            return "stopped (no release.3mf installed — plate NOT ejected)"
        fid = self._ensure_release_file()
        api_post(self.cfg, "/queue/", {"library_file_id": fid,
                                       "printer_id": cur["printer_id"],
                                       "gcode_injection": False, "quantity": 1})
        items2 = api_get(self.cfg, "/queue/") or []
        cands = [i for i in items2 if i.get("library_file_id") == fid
                 and i["status"] == "pending"]
        if cands:
            rid = max(i["id"] for i in cands)
            live = [i["id"] for i in items2
                    if i["status"] in ("pending", "printing", "running", "dispatched", "paused")]
            if rid in live and item_id in live:
                live.remove(rid)
                live.insert(live.index(item_id) + 1, rid)
                api_post(self.cfg, "/queue/reorder",
                         {"items": [{"id": i, "position": n} for n, i in enumerate(live)]})
        api_post(self.cfg, "/queue/%s/stop" % item_id, {})
        return "stopped — ejecting plate, next plate starts after eject"

    def _reorder(self, ids):
        """Drag-order of pending ids -> absolute positions. The server only
        repositions items whose status is pending, so pending items reuse the
        existing pending position slot values and the printing job is never
        touched. Old code swapped view-adjacent rows and could interleave the
        queue around the print anchor."""
        items = api_get(self.cfg, "/queue/") or []
        pend = [i for i in items if i["status"] == "pending"]
        byid = {i["id"]: i for i in pend}
        order = [byid[int(i)] for i in ids if int(i) in byid]
        for x in pend:                       # unseen ids keep relative tail
            if x not in order:
                order.append(x)
        positions = sorted(x.get("position") or 0 for x in pend)
        api_post(self.cfg, "/queue/reorder",
                 {"items": [{"id": it["id"], "position": positions[n]}
                            for n, it in enumerate(order)]})
        return "reordered"

    def _move(self, item_id, direction):
        items = api_get(self.cfg, "/queue/") or []
        live = [i for i in items if i["status"] in ("pending", "running", "dispatched", "paused")]
        ids = [i["id"] for i in live]
        pos = ids.index(item_id)
        new = max(0, min(len(ids) - 1, pos + direction))
        if new == pos:
            return "already at edge"
        ids.insert(new, ids.pop(pos))
        api_post(self.cfg, "/queue/reorder",
                 {"items": [{"id": i, "position": n} for n, i in enumerate(ids)]})
        return "moved"

    def execute(self):
        return orca.ExecutionResult.success("BambuQueue panel active")

@orca.plugin
class BambuQueuePlugin(orca.base):
    def register_capabilities(self):
        orca.register_capability(CapturePlate)
        orca.register_capability(Panel)
