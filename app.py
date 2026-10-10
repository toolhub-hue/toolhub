"""ToolHub - single-file Flask app (website + backend + admin panel).
Env vars: MONGO_URI, GEMINI_API_KEY, ADMIN_PASSWORD
Optional: SECRET_KEY, GEMINI_MODEL, GEMINI_IMAGE_MODEL, MONGO_DB
Install: pip install flask "pymongo[srv]" requests gunicorn
Run: python app.py   |   Production: gunicorn app:app --timeout 120
"""
import os, re, json, time, uuid, hmac, base64, hashlib
import requests
from flask import Flask, request, jsonify, Response, session
from werkzeug.exceptions import HTTPException

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024  # 12 MB uploads
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = bool(os.environ.get("RENDER"))
app.secret_key = os.environ.get("SECRET_KEY") or hashlib.sha256(
    ("toolhub" + os.environ.get("ADMIN_PASSWORD", "")).encode()).hexdigest()

TEXT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
IMAGE_MODEL = os.environ.get("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image")
_db = None


class Err(Exception):
    def __init__(self, msg, code=400):
        self.msg, self.code = msg, code


def get_db():
    global _db
    if _db is None:
        uri = os.environ.get("MONGO_URI")
        if not uri:
            return None
        from pymongo import MongoClient
        _db = MongoClient(uri, serverSelectionTimeoutMS=5000)[os.environ.get("MONGO_DB", "toolhub")]
    return _db


def need_db():
    db = get_db()
    if db is None:
        raise Err("MONGO_URI is not set on the server", 503)
    return db


hits = {}  # small per-IP limits to protect your Gemini quota


def limit(kind, n):
    ip = (request.headers.get("X-Forwarded-For") or request.remote_addr or "x").split(",")[0].strip()
    now = time.time()
    h = hits[(kind, ip)] = [t for t in hits.get((kind, ip), []) if now - t < 3600]
    if len(h) >= n:
        raise Err("Limit reached (%d per hour). Try again later." % n, 429)
    h.append(now)


def gemini(model, parts, cfg=None, tools=None):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise Err("Server has no GEMINI_API_KEY set", 500)
    body = {"contents": [{"parts": parts}]}
    if cfg:
        body["generationConfig"] = cfg
    if tools:
        body["tools"] = tools
    r = requests.post("https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent" % model,
                      headers={"x-goog-api-key": key, "Content-Type": "application/json"}, json=body, timeout=120)
    try:
        j = r.json()
    except ValueError:
        j = {}
    if r.status_code != 200:
        raise Err("Gemini: " + str(j.get("error", {}).get("message", "request failed"))[:160], 502)
    cands = j.get("candidates") or [{}]
    return (cands[0].get("content") or {}).get("parts") or []


# ---------- public pages & stats ----------
@app.get("/")
def home():
    return Response(HTML, mimetype="text/html")


@app.get("/health")
def health():
    e = os.environ.get
    return jsonify(ok=True, mongo=bool(e("MONGO_URI")), gemini=bool(e("GEMINI_API_KEY")), admin=bool(e("ADMIN_PASSWORD")))


@app.post("/api/visit")
def visit():
    db = need_db()
    vid = request.cookies.get("vid") or uuid.uuid4().hex
    db.visitors.update_one({"_id": vid}, {"$setOnInsert": {"first": time.time()}, "$inc": {"visits": 1}}, upsert=True)
    db.stats.update_one({"_id": "views"}, {"$inc": {"total": 1}}, upsert=True)
    total = (db.stats.find_one({"_id": "views"}) or {}).get("total", 0)
    resp = jsonify(unique=db.visitors.count_documents({}), total=total)
    resp.set_cookie("vid", vid, max_age=31536000, httponly=True, samesite="Lax")
    return resp


# ---------- site config (editable from the admin panel) ----------
DEFAULT = {"name": "ToolHub", "c1": "#2563eb", "c2": "#7c3aed",
           "tk": ["Welcome to ToolHub", "New: AI Photo Generator & Prompt Generator", "Question Paper Solver is live"],
           "lk": [{"t": "Home", "u": "#"}], "ov": {}, "ct": []}


def s(v, n=80):
    return str(v if v is not None else "")[:n]


def lst(v):
    return v if isinstance(v, list) else []


def url_ok(u):
    return u if re.match(r"^(https?://\S+|/f/[0-9a-f]{24}|#)$", u) else "#"


def hexc(c, d):
    return c if re.match(r"^#[0-9a-fA-F]{6}$", c) else d


def grad(g):
    return g if re.match(r"^#[0-9a-fA-F]{6},#[0-9a-fA-F]{6}$", g) else "#bfdbfe,#3b82f6"


def clean(d):
    d = d if isinstance(d, dict) else {}
    ov = {}
    for k, v in (d["ov"] if isinstance(d.get("ov"), dict) else {}).items():
        if re.match(r"^\w{1,12}$", k) and isinstance(v, dict):
            ov[k] = {"n": s(v.get("n"), 40), "d": s(v.get("d"), 90), "i": s(v.get("i"), 8),
                     "c": s(v.get("c"), 20), "hide": bool(v.get("hide"))}
    ct = []
    for t in lst(d.get("ct"))[:60]:
        if isinstance(t, dict):
            ct.append({"id": re.sub(r"\W", "", s(t.get("id"), 20)) or uuid.uuid4().hex[:8],
                       "n": s(t.get("n"), 40) or "Tile", "d": s(t.get("d"), 90), "i": s(t.get("i"), 8) or "🔗",
                       "c": s(t.get("c"), 20) or "Links", "g": grad(s(t.get("g"), 20)), "h": url_ok(s(t.get("h"), 300))})
    lk = [{"t": s(l.get("t"), 30), "u": url_ok(s(l.get("u"), 300))} for l in lst(d.get("lk"))[:12] if isinstance(l, dict)]
    return {"name": s(d.get("name"), 40) or "ToolHub", "c1": hexc(s(d.get("c1"), 7), DEFAULT["c1"]),
            "c2": hexc(s(d.get("c2"), 7), DEFAULT["c2"]), "tk": [s(x, 150) for x in lst(d.get("tk"))[:20]],
            "lk": lk, "ov": ov, "ct": ct}


@app.get("/api/config")
def get_config():
    try:
        db = get_db()
        doc = (db.config.find_one({"_id": "site"}) if db is not None else None) or {}
    except Exception:
        doc = {}
    doc.pop("_id", None)
    return jsonify({**DEFAULT, **doc})


# ---------- admin ----------
def need_admin():
    if not session.get("admin"):
        raise Err("Not logged in", 401)


@app.post("/api/admin/login")
def login():
    pw = os.environ.get("ADMIN_PASSWORD")
    if not pw:
        raise Err("Set ADMIN_PASSWORD on the server first", 503)
    limit("login", 10)
    given = str((request.get_json(silent=True) or {}).get("pw", ""))
    if not hmac.compare_digest(given.encode(), pw.encode()):
        raise Err("Wrong password", 403)
    session["admin"] = True
    session.permanent = True
    return jsonify(ok=True)


@app.get("/api/admin/me")
def me():
    need_admin()
    return jsonify(ok=True)


@app.post("/api/admin/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


@app.post("/api/admin/config")
def save_config():
    need_admin()
    db = need_db()
    cfg = clean(request.get_json(silent=True))
    db.config.replace_one({"_id": "site"}, {**cfg, "_id": "site"}, upsert=True)
    refs = set(re.findall(r"/f/([0-9a-f]{24})", repr((cfg["ct"], cfg["lk"]))))
    for f in list(db.files.find({}, {"_id": 1})):  # delete PDFs no tile uses any more
        if str(f["_id"]) not in refs:
            db.files.delete_one({"_id": f["_id"]})
    return jsonify(cfg)


@app.post("/api/admin/upload")
def upload():
    need_admin()
    db = need_db()
    f = request.files.get("file")
    data = f.read() if f else b""
    if not data.startswith(b"%PDF-"):
        raise Err("Please upload a PDF file", 400)
    from bson import Binary
    r = db.files.insert_one({"data": Binary(data), "name": s(f.filename, 80), "t": time.time()})
    return jsonify(url="/f/" + str(r.inserted_id))


@app.get("/f/<fid>")
def pdf(fid):
    from bson import ObjectId
    db = need_db()
    if not re.match(r"^[0-9a-f]{24}$", fid):
        raise Err("Not found", 404)
    d = db.files.find_one({"_id": ObjectId(fid)})
    if not d:
        raise Err("Not found", 404)
    nm = re.sub(r"[^\w.-]", "_", d.get("name") or "file.pdf")
    return Response(bytes(d["data"]), mimetype="application/pdf", headers={
        "Content-Disposition": 'inline; filename="%s"' % nm, "X-Content-Type-Options": "nosniff",
        "Cache-Control": "public, max-age=3600"})


# ---------- AI tools (Gemini) ----------
SOLVE = ("This is a question paper. First detect its language. Then answer EVERY question and give a clear "
         "step-by-step explanation. Write the answers and explanations in the SAME language as the question paper "
         "(Telugu paper -> Telugu, Hindi -> Hindi, English -> English). Keep the original question numbers. "
         "For maths show the steps. If something is unreadable, say so instead of guessing. "
         "Use plain text only: no markdown symbols like ** or #.")


@app.post("/api/solve")
def solve():
    limit("solve", 10)
    f = request.files.get("file")
    if not f:
        raise Err("No file uploaded")
    mt = f.mimetype
    if mt not in ("application/pdf", "image/jpeg", "image/png", "image/webp"):
        raise Err("Upload a PDF or an image (JPG/PNG)")
    data = base64.b64encode(f.read()).decode()
    parts = gemini(TEXT_MODEL, [{"inline_data": {"mime_type": mt, "data": data}}, {"text": SOLVE}])
    return jsonify(answer="".join(p.get("text", "") for p in parts).strip() or "No answer returned")


ANALYSE = ("You are analysing an exam response sheet. SRC Read it fully. Find the exam name, the candidate name if shown, "
           "every subject or section, and the marking scheme (marks for each correct answer and negative marks for each wrong "
           "answer) from the page or its instructions. For EACH subject count correct, wrong and unattempted questions: if the "
           "sheet shows the correct answer, compare it with the candidate's answer; if it shows a status such as "
           "Correct, Wrong or Not Attempted, use that. If correct answers are not available anywhere, set correct and wrong to 0, "
           "count the unattempted ones and explain in note. Count carefully and only from what is visible. "
           'Reply with ONLY this JSON, no markdown: {"exam":"","candidate":"","correct_mark":4,"negative_mark":1,'
           '"subjects":[{"name":"","correct":0,"wrong":0,"unattempted":0}],"note":""}. '
           "negative_mark must be a positive number (0 if there is no negative marking).")


def ni(v):
    try:
        return max(0, min(1000, int(v)))
    except (TypeError, ValueError):
        return 0


def fl(v):
    try:
        return max(0.0, min(100.0, float(v)))
    except (TypeError, ValueError):
        return 0.0


def parse_json(t):
    try:
        return json.loads(t)
    except ValueError:
        m = re.search(r"\{.*\}", t, re.S)
        try:
            return json.loads(m.group(0)) if m else {}
        except ValueError:
            return {}


@app.post("/api/analyse")
def analyse():
    limit("analyse", 10)
    url, key = s(request.form.get("url"), 500).strip(), s(request.form.get("key"), 500).strip()
    f = request.files.get("file")
    tools = None
    if url:
        for u in (url, key):
            if u and not re.match(r"^https?://\S+$", u):
                raise Err("Please paste the full link, starting with https://")
        src = "Open this response sheet link: " + url + "." + (" The answer key link is: " + key + "." if key else "")
        parts, tools = [{"text": ANALYSE.replace("SRC", src)}], [{"url_context": {}}]
    elif f:
        if f.mimetype not in ("application/pdf", "image/jpeg", "image/png", "image/webp"):
            raise Err("Upload a PDF or an image (JPG/PNG)")
        parts = [{"inline_data": {"mime_type": f.mimetype, "data": base64.b64encode(f.read()).decode()}},
                 {"text": ANALYSE.replace("SRC", "The response sheet is attached.")}]
    else:
        raise Err("Paste your response sheet link first")
    text = "".join(p.get("text", "") for p in gemini(TEXT_MODEL, parts, {"temperature": 0}, tools))
    j = parse_json(text)
    cm, nm = fl(j.get("correct_mark")), fl(j.get("negative_mark"))
    note = s(j.get("note"), 300)
    if not cm:
        cm = 1.0
        note = (note + " Marking scheme not found on the sheet, so +1 / 0 was assumed.").strip()
    subs, tot, mx = [], 0.0, 0.0
    for x in lst(j.get("subjects"))[:15]:
        if not isinstance(x, dict):
            continue
        c, w, u = ni(x.get("correct")), ni(x.get("wrong")), ni(x.get("unattempted"))
        m, top = round(c * cm - w * nm, 2), round((c + w + u) * cm, 2)
        subs.append({"name": s(x.get("name"), 40) or "Subject", "correct": c, "wrong": w, "unattempted": u,
                     "marks": m, "max": top})
        tot, mx = tot + m, mx + top
    if not subs:
        raise Err("Could not read the sheet from this link (it may need a login). Try the upload option. " + s(text, 100), 422)
    return jsonify(exam=s(j.get("exam"), 60), candidate=s(j.get("candidate"), 60), cm=cm, nm=nm,
                   subjects=subs, total=round(tot, 2), max=round(mx, 2), note=note)


@app.post("/api/prompt")
def prompt_gen():
    limit("prompt", 30)
    j = request.get_json(silent=True) or {}
    idea, style = s(j.get("idea"), 500).strip(), s(j.get("style"), 40)
    if not idea:
        raise Err("Write your idea first")
    ask = ("You are an expert prompt engineer for AI image generators. Turn the idea below into ONE detailed, vivid "
           "image prompt in English (60-120 words), style: %s. Include subject details, setting, lighting, "
           "camera and lens, composition, mood, colour palette and realistic textures. "
           "Output only the prompt text, no quotes, no markdown.\nIdea: %s" % (style, idea))
    parts = gemini(TEXT_MODEL, [{"text": ask}])
    return jsonify(prompt="".join(p.get("text", "") for p in parts).strip())


@app.post("/api/image")
def image_gen():
    limit("image", 10)
    j = request.get_json(silent=True) if request.is_json else None
    p = s((j or request.form).get("prompt"), 1200).strip()
    if not p:
        raise Err("Describe the photo first")
    imgs = []
    for f in request.files.getlist("photos")[:3]:
        if f.mimetype not in ("image/jpeg", "image/png", "image/webp"):
            raise Err("Upload JPG, PNG or WebP photos")
        imgs.append({"inline_data": {"mime_type": f.mimetype, "data": base64.b64encode(f.read()).decode()}})
    if imgs:
        text = ("Use the uploaded photo(s) as the reference. Apply this change while keeping the result photorealistic, "
                "natural and sharp, and keep the people's faces and identity recognizable: " + p)
    else:
        text = "Create a photorealistic, ultra-detailed, natural-looking photograph, sharp focus, realistic lighting. " + p
    for part in gemini(IMAGE_MODEL, imgs + [{"text": text}], {"responseModalities": ["TEXT", "IMAGE"]}):
        d = part.get("inlineData") or part.get("inline_data")
        if d:
            return jsonify(image="data:%s;base64,%s" % (d.get("mimeType") or d.get("mime_type") or "image/png", d["data"]))
    raise Err("No image returned. The prompt may have been blocked, or image generation needs billing enabled on your Gemini key.", 502)


@app.errorhandler(Exception)
def oops(e):
    if isinstance(e, Err):
        return jsonify(error=e.msg), e.code
    if isinstance(e, HTTPException):
        return jsonify(error="File too large (max 12 MB)" if e.code == 413 else e.description), e.code
    return jsonify(error="Server error: " + str(e)[:120]), 500


# ----- Website (HTML/CSS/JS) -----
HTML = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+Telugu&family=Noto+Sans+Devanagari&family=Noto+Sans+Tamil&family=Noto+Sans+Kannada&family=Noto+Sans+Malayalam&family=Noto+Sans+Arabic&display=swap" rel="stylesheet">
<title>ToolHub – All-in-One Tools</title>
<style>
:root{--bg:#f4f6fb;--card:#fff;--tx:#0f172a;--mu:#64748b;--ac:#2563eb;--ac2:#7c3aed;--bd:#e5e9f2;--sh:0 4px 18px rgba(37,99,235,.08);box-sizing:border-box;padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}
@media(prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#0b1020;--card:#151b2e;--tx:#e8ecf8;--mu:#93a0bd;--bd:#232b44;--sh:0 4px 18px rgba(0,0,0,.35)}}
:root[data-theme="dark"]{--bg:#0b1020;--card:#151b2e;--tx:#e8ecf8;--mu:#93a0bd;--bd:#232b44;--sh:0 4px 18px rgba(0,0,0,.35)}
*{box-sizing:border-box}
html{scroll-padding-top:env(safe-area-inset-top,0px)}
body{margin:0;background:var(--bg);color:var(--tx);font-family:Inter,system-ui,-apple-system,Segoe UI,Roboto,sans-serif;line-height:1.4}
.wrap{max-width:1000px;margin:auto;padding:16px}
header{display:flex;align-items:center;justify-content:space-between;gap:10px}
.logo{font-weight:800;font-size:20px;display:flex;align-items:center;gap:8px}
.logo i{font-style:normal;width:34px;height:34px;border-radius:10px;background:linear-gradient(135deg,var(--ac),var(--ac2));display:grid;place-items:center;color:#fff}
.tg{border:1px solid var(--bd);background:var(--card);color:var(--tx);border-radius:50%;width:38px;height:38px;font-size:17px;cursor:pointer}
.hero{margin:18px 0;padding:26px 20px;border-radius:22px;background:linear-gradient(135deg,var(--ac),var(--ac2));color:#fff;box-shadow:var(--sh);animation:up .6s both}
.hero h1{margin:0 0 6px;font-size:clamp(22px,5vw,34px)}
.hero p{margin:0 0 16px;opacity:.9}
.search{width:100%;border:0;border-radius:14px;padding:14px 16px;font-size:16px;outline:none;background:#fff;color:#0f172a}
.tick{overflow:hidden;background:var(--card);border:1px solid var(--bd);border-radius:14px;display:flex;align-items:center}
.tick b{background:var(--ac);color:#fff;padding:9px 12px;font-size:12px;letter-spacing:.5px;flex:none}
.tr{overflow:hidden;flex:1}
.tm{display:inline-flex;gap:40px;white-space:nowrap;padding-left:20px;animation:mq 25s linear infinite}
.tm a{color:var(--tx);font-size:14px;cursor:pointer}
.tr:hover .tm{animation-play-state:paused}
.chips{display:flex;gap:8px;overflow-x:auto;padding:16px 0 8px;scrollbar-width:none}
.chip{flex:none;border:1px solid var(--bd);background:var(--card);color:var(--tx);padding:8px 16px;border-radius:99px;font-size:14px;cursor:pointer}
.chip.on{background:var(--ac);color:#fff;border-color:var(--ac)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:14px;margin-top:8px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:20px;padding:16px;cursor:pointer;box-shadow:var(--sh);transition:transform .2s,box-shadow .2s;animation:up .5s both}
.card:hover{transform:translateY(-5px);box-shadow:0 12px 28px rgba(37,99,235,.2)}
.card:active{transform:scale(.97)}
.ic{width:48px;height:48px;border-radius:14px;display:grid;place-items:center;font-size:24px;margin-bottom:12px}
.card h3{margin:0 0 4px;font-size:15px}
.card p{margin:0;color:var(--mu);font-size:12.5px}
.tag{display:inline-block;margin-top:10px;font-size:11px;color:var(--ac);background:rgba(37,99,235,.1);padding:3px 8px;border-radius:99px}
.ov{position:fixed;inset:0;background:rgba(5,10,25,.55);display:none;align-items:flex-end;justify-content:center;z-index:9}
.ov.on{display:flex}
.sh{background:var(--card);width:100%;max-width:520px;border-radius:24px 24px 0 0;padding:20px 20px calc(20px + env(safe-area-inset-bottom,0px));max-height:90%;overflow:auto;animation:sl .3s both}
@media(min-width:600px){.ov{align-items:center}.sh{border-radius:24px}}
.sh h2{margin:0 0 14px;display:flex;justify-content:space-between;align-items:center;font-size:19px}
.x{border:0;background:var(--bg);color:var(--tx);width:34px;height:34px;border-radius:50%;font-size:16px;cursor:pointer}
label{display:block;font-size:13px;color:var(--mu);margin:10px 0 4px}
input,select,textarea{width:100%;padding:12px;border-radius:12px;border:1px solid var(--bd);background:var(--bg);color:var(--tx);font-size:16px;font-family:inherit}
.go{width:100%;margin-top:16px;border:0;border-radius:14px;padding:14px;font-size:16px;font-weight:700;color:#fff;background:linear-gradient(135deg,var(--ac),var(--ac2));cursor:pointer}
.res{margin-top:14px;padding:14px;border-radius:14px;background:rgba(37,99,235,.1);font-weight:600;word-break:break-word;display:none}
.empty{text-align:center;color:var(--mu);padding:30px;display:none}
footer{text-align:center;color:var(--mu);font-size:13px;padding:24px 0 8px}
@keyframes up{from{opacity:0;transform:translateY(16px)}to{opacity:1;transform:none}}
@keyframes sl{from{transform:translateY(40px);opacity:0}to{transform:none;opacity:1}}
@keyframes mq{to{transform:translateX(-50%)}}
.hb{display:flex;gap:8px}.lk{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}.pl{background:rgba(255,255,255,.22);color:#fff;text-decoration:none;padding:7px 14px;border-radius:99px;font-size:13px}
.pop{animation:pp .45s}@keyframes pp{0%{transform:scale(.8);opacity:0}60%{transform:scale(1.05)}100%{transform:none;opacity:1}}
.big{font-size:30px;color:var(--ac);font-weight:800}.bar{height:10px;border-radius:9px;background:var(--bd);margin-top:10px;overflow:hidden}.bar b{display:block;height:100%;width:0;background:linear-gradient(90deg,var(--ac),var(--ac2))}
.cf{position:absolute;bottom:0;font-style:normal;animation:fl 1.2s both}@keyframes fl{to{transform:translateY(-70px) rotate(40deg);opacity:0}}
.lv{animation:pu 1s infinite;background:#ef4444!important}@keyframes pu{50%{transform:scale(1.03)}}
.vc{display:inline-flex;gap:6px;align-items:center;flex-wrap:wrap;justify-content:center;background:var(--card);border:1px solid var(--bd);border-radius:99px;padding:8px 16px;margin-bottom:10px;font-size:14px;box-shadow:var(--sh)}.vc b{color:var(--ac)}
.ar{border:1px solid var(--bd);border-radius:14px;padding:10px;margin:8px 0;display:grid;gap:6px}.ar small{color:var(--mu);word-break:break-all}.r2{display:grid;grid-template-columns:70px 1fr;gap:6px}input[type=checkbox]{width:auto}
.hot{position:relative;background:linear-gradient(var(--card),var(--card)) padding-box,linear-gradient(135deg,var(--ac),var(--ac2)) border-box;border:2px solid transparent}.hot::after{content:'🔥 HOT';position:absolute;top:10px;right:10px;font-size:10px;font-weight:800;background:#ef4444;color:#fff;padding:2px 7px;border-radius:99px}
.vs{display:none;margin:26px auto 6px;max-width:520px;border-radius:24px;padding:20px;background:linear-gradient(135deg,var(--ac),var(--ac2));color:#fff;box-shadow:var(--sh);text-align:center}.vs .t{font-size:12px;opacity:.92;display:flex;gap:8px;align-items:center;justify-content:center;letter-spacing:1px;font-weight:700}.vs .dt{width:9px;height:9px;border-radius:50%;background:#4ade80;animation:lp 1.6s infinite}.vs .r{display:flex;justify-content:space-around;margin-top:14px}.vs .n{font-size:36px;font-weight:800;line-height:1.1}.vs .l{font-size:12px;opacity:.88}
@keyframes lp{0%{box-shadow:0 0 0 0 rgba(74,222,128,.8)}70%{box-shadow:0 0 0 10px rgba(74,222,128,0)}100%{box-shadow:0 0 0 0 rgba(74,222,128,0)}}
.r3{display:grid;grid-template-columns:1fr 78px 78px;gap:6px;margin-top:6px}.sc{border-radius:16px;padding:12px;color:#fff;text-align:center;font-size:13px;animation:up .5s both}.sc b{display:block;font-size:30px}
</style>
</head>
<body>
<div class="wrap">
<header><div class="logo"><i>⚡</i><span id="nm">ToolHub</span></div><div class="hb"><button class="tg" id="ad" aria-label="Admin">⚙️</button><button class="tg" id="tg" aria-label="Toggle theme">🌓</button></div></header>
<section class="hero"><h1>Every tool you need, in one place</h1><p>Fast, free and simple calculators &amp; utilities.</p><input class="search" id="q" placeholder="🔍 Search tools..."><div class="lk" id="lk"></div></section>
<div class="tick"><b>LATEST</b><div class="tr"><div class="tm" id="tm"></div></div></div>
<div class="chips" id="chips"></div>
<div class="grid" id="grid"></div>
<div class="empty" id="em">No tools found 😕</div>
<div class="vs" id="vcw"><div class="t"><span class="dt"></span>LIVE VISITOR STATS</div><div class="r"><div><div class="n" id="vc">0</div><div class="l">👥 Total visitors</div></div><div><div class="n" id="vt">0</div><div class="l">👁 Page views</div></div></div></div>
<footer>Made with ❤️ · ToolHub</footer>
</div>
<div class="ov" id="ov"><div class="sh" id="sh"></div></div>
<script>
var N=function(i){return{key:i}};
var T=[
{id:'age',n:'Age Calculator',d:'Exact age in years, months, days',i:'🎂',c:'Education',g:'#fde68a,#f59e0b',f:[{k:'dob',l:'Date of birth',t:'date'}],r:function(v){var d=new Date(v.dob),n=new Date();if(isNaN(d))return'Pick a date';var y=n.getFullYear()-d.getFullYear(),m=n.getMonth()-d.getMonth(),x=n.getDate()-d.getDate();if(x<0){m--;x+=new Date(n.getFullYear(),n.getMonth(),0).getDate()}if(m<0){y--;m+=12}return y+' years, '+m+' months, '+x+' days'}},
{id:'sgpa',n:'SGPA to Percentage',d:'Convert SGPA to marks %',i:'🎓',c:'Education',g:'#bfdbfe,#3b82f6',f:[{k:'s',l:'SGPA (out of 10)',t:'number'}],r:function(v){var s=+v.s;if(!(s>0&&s<=10))return'Enter SGPA 0-10';return'Percentage: '+((s-0.75)*10).toFixed(2)+'%'}},
{id:'att',n:'Attendance',d:'Check 75% attendance status',i:'📅',c:'Education',g:'#a7f3d0,#10b981',f:[{k:'a',l:'Classes attended',t:'number'},{k:'t',l:'Total classes',t:'number'}],r:function(v){var a=+v.a,t=+v.t;if(!(t>0))return'Enter total classes';var p=a/t*100,n=Math.max(0,Math.ceil((0.75*t-a)/0.25));return'Attendance: '+p.toFixed(2)+'% — '+(p>=75?'Safe ✅':'Attend next '+n+' classes to reach 75% ⚠️')}},
{id:'emi',n:'EMI Calculator',d:'Monthly loan instalment',i:'💰',c:'Finance',g:'#fecdd3,#f43f5e',f:[{k:'p',l:'Loan amount (₹)',t:'number'},{k:'r',l:'Interest rate (% per year)',t:'number'},{k:'y',l:'Years',t:'number'}],r:function(v){var P=+v.p,r=+v.r/1200,n=+v.y*12;if(!(P>0&&r>0&&n>0))return'Fill all fields';var q=Math.pow(1+r,n),e=P*r*q/(q-1);return'EMI: ₹'+e.toFixed(0)+'/month · Total interest: ₹'+(e*n-P).toFixed(0)}},
{id:'wc',n:'Word Counter',d:'Words, characters, sentences',i:'📝',c:'Text',g:'#ddd6fe,#8b5cf6',f:[{k:'x',l:'Paste your text',t:'area'}],r:function(v){var x=v.x.trim();if(!x)return'Type some text';return'Words: '+x.split(/\s+/).length+' · Characters: '+x.length+' · Sentences: '+(x.split(/[.!?]+/).filter(Boolean).length)}},
{id:'pw',n:'Password Generator',d:'Strong random passwords',i:'🔐',c:'Text',g:'#fed7aa,#f97316',f:[{k:'l',l:'Length (8-64)',t:'number',v:16}],r:function(v){var l=Math.min(64,Math.max(8,+v.l||16)),s='abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789!@#$%&*?',a=new Uint32Array(l);crypto.getRandomValues(a);return Array.from(a,function(z){return s[z%s.length]}).join('')}},
{id:'bmi',n:'BMI Calculator',d:'Body mass index check',i:'❤️',c:'Health',g:'#fbcfe8,#ec4899',f:[{k:'w',l:'Weight (kg)',t:'number'},{k:'h',l:'Height (cm)',t:'number'}],r:function(v){var w=+v.w,h=+v.h/100;if(!(w>0&&h>0))return'Fill both fields';var b=w/(h*h);return'BMI: '+b.toFixed(1)+' — '+(b<18.5?'Underweight':b<25?'Normal':b<30?'Overweight':'Obese')}},
{id:'unit',n:'Unit Converter',d:'Length, weight, temperature',i:'🔄',c:'Utility',g:'#a5f3fc,#06b6d4',f:[{k:'v',l:'Value',t:'number'},{k:'m',l:'Convert',t:'select',o:['km → miles','kg → lbs','°C → °F','cm → inches']}],r:function(v){var x=+v.v;if(isNaN(x)||v.v==='')return'Enter a value';var m=v.m;return x+' '+m.split(' → ')[0]+' = '+(m[0]=='k'&&m[1]=='m'?x*0.621371:m[1]=='g'?x*2.20462:m[0]=='°'?x*9/5+32:x/2.54).toFixed(2)+' '+m.split(' → ')[1]}},
{id:'pct',n:'Marks Percentage',d:'SSC, Inter groups & university',i:'📊',c:'Education',g:'#c7d2fe,#6366f1',u:mk},
{id:'imgc',n:'Image Compressor',d:'Reduce photo size, keep quality',i:'🖼️',c:'Utility',g:'#bbf7d0,#22c55e',u:imgc},
{id:'pdfc',n:'PDF Compressor',d:'Optimize PDF file size',i:'📄',c:'Utility',g:'#fecaca,#ef4444',u:pdfc},
{id:'tv',n:'Telugu → English Voice',d:'Speak Telugu, get English text',i:'🎙️',c:'Voice',g:'#e9d5ff,#a855f7',u:function(b){mic(b,1)}},
{id:'stt',n:'Speech to Text',d:'Speak & download as Word doc',i:'🗣️',c:'Voice',g:'#fef08a,#eab308',u:function(b){mic(b,0)}},
{id:'ff',n:'Form Filler',d:'Auto-fill PDF forms',i:'🧾',c:'Utility',g:'#99f6e4,#14b8a6',u:ff},
{id:'qa',n:'Question Paper Solver',d:'Upload paper, get answers + explanations',i:'🧠',c:'Education',g:'#fbcfe8,#d946ef',u:qa},
{id:'pg',n:'AI Prompt Generator',d:'Turn an idea into a pro prompt',i:'✨',c:'AI',g:'#fde68a,#f472b6',u:pgen},
{id:'img',n:'AI Photo Generator',d:'Realistic photo from your prompt',i:'🎨',c:'AI',g:'#c4b5fd,#ec4899',u:igen},
{id:'th',n:'Thumbnail Maker',d:'YouTube-style thumbnails in seconds',i:'🖼️',c:'Creator',g:'#fdba74,#ef4444',u:thg},
{id:'rsa',n:'Response Sheet Analyser',d:'Correct, wrong & skipped by subject',i:'📈',c:'Education',g:'#bae6fd,#0ea5e9',u:rsa}
];
var cur='All';
var $=function(i){return document.getElementById(i)};
function draw(){
var q=$('q').value.toLowerCase(),L=tiles().filter(function(t){return(cur=='All'||t.c==cur)&&(t.n+t.d).toLowerCase().indexOf(q)>-1});
$('grid').innerHTML=L.map(function(t,i){var g=t.g.split(',');return'<div class="card'+(/^(pg|img)$/.test(t.id)?' hot':'')+'" style="animation-delay:'+i*60+'ms" onclick="openT(\''+t.id+'\')"><div class="ic" style="background:linear-gradient(135deg,'+g[0]+','+g[1]+')">'+esc(t.i)+'</div><h3>'+esc(t.n)+'</h3><p>'+esc(t.d)+'</p><span class="tag">'+esc(t.c)+'</span></div>'}).join('');
$('em').style.display=L.length?'none':'block'}
function chips(){var cs=['All'];tiles().forEach(function(t){if(cs.indexOf(t.c)<0)cs.push(t.c)});$('chips').innerHTML=cs.map(function(c){return'<button class="chip'+(c==cur?' on':'')+'" data-c="'+esc(c)+'">'+esc(c)+'</button>'}).join('')}
$('chips').onclick=function(e){var c=e.target.dataset.c;if(!c)return;cur=c;[].forEach.call(document.querySelectorAll('.chip'),function(b){b.classList.toggle('on',b.dataset.c==c)});draw()};
$('q').oninput=draw;
function openT(id){var t=tiles().filter(function(x){return x.id==id})[0];if(!t)return;if(t.h){window.open(t.h,'_blank','noopener');return}
if(t.u){$('sh').innerHTML='<h2>'+t.i+' '+t.n+'<button class="x" onclick="closeT()">✕</button></h2><div id="bx"></div>';$('ov').classList.add('on');t.u($('bx'));return}
var h='<h2>'+t.i+' '+t.n+'<button class="x" onclick="closeT()">✕</button></h2>';
t.f.forEach(function(f){h+='<label>'+f.l+'</label>';
if(f.t=='area')h+='<textarea rows="5" id="f_'+f.k+'"></textarea>';
else if(f.t=='select')h+='<select id="f_'+f.k+'">'+f.o.map(function(o){return'<option>'+o+'</option>'}).join('')+'</select>';
else h+='<input id="f_'+f.k+'" type="'+f.t+'" inputmode="decimal" value="'+(f.v||'')+'">'});
h+='<button class="go" id="go">Calculate</button><div class="res" id="res"></div>';
$('sh').innerHTML=h;$('ov').classList.add('on');
$('go').onclick=function(){var v={};t.f.forEach(function(f){v[f.k]=$('f_'+f.k).value});var r=$('res');show(r,t.r(v))}}
function closeT(){if(window.rec)try{rec.stop()}catch(e){}$('ov').classList.remove('on')}
$('ov').onclick=function(e){if(e.target.id=='ov')closeT()};
$('tg').onclick=function(){var r=document.documentElement,d=r.dataset.theme?r.dataset.theme=='dark':matchMedia('(prefers-color-scheme:dark)').matches;r.dataset.theme=d?'light':'dark';try{localStorage.setItem('th',r.dataset.theme)}catch(e){}};
try{var s=localStorage.getItem('th');if(s)document.documentElement.dataset.theme=s}catch(e){}
function esc(s){return String(s).replace(/[&<>"]/g,function(c){return'&#'+c.charCodeAt(0)+';'})}
function kb(n){return(n/1024).toFixed(1)+' KB'}
function dl(b,n){var a=document.createElement('a');a.href=URL.createObjectURL(b);a.download=n;a.click()}
function boom(r){r.style.position='relative';for(var i=0;i<14;i++){var e=document.createElement('i');e.className='cf';e.textContent=['🎉','✨','⭐','🎊'][i%4];e.style.left=(5+i*7)+'%';e.style.animationDelay=i*40+'ms';r.appendChild(e);setTimeout(function(x){return function(){x.remove()}}(e),1500)}}
function show(r,s){r.style.display='block';r.classList.remove('pop');void r.offsetWidth;r.classList.add('pop');
var m=s.match(/\d+(\.\d+)?/),p=m?parseFloat(m[0]):NaN;if(isNaN(p)||!/\s/.test(s)||s.length>150){r.textContent=s;return}
var d=(m[1]||'').length,t0=performance.now(),pc=/%/.test(s)&&p<=100,bar=pc?'<div class="bar"><b id="bf"></b></div>':'';
function f(n){var e=Math.min(1,(n-t0)/900),v=p*(1-Math.pow(1-e,3));r.innerHTML=s.replace(m[0],'<span class="big">'+v.toFixed(d)+'</span>')+bar;var q=document.getElementById('bf');if(q)q.style.width=v+'%';if(e<1)requestAnimationFrame(f);else if(pc&&p>=60)boom(r)}
requestAnimationFrame(f)}
var CF={name:'ToolHub',c1:'#2563eb',c2:'#7c3aed',ov:{},ct:[],tk:['Welcome to ToolHub','New: Image & PDF compressor','Telugu voice translator is live'],lk:[{t:'Home',u:'#'},{t:'Telegram',u:'https://t.me/'}]};
function tick(){var h=CF.tk.map(function(x){return'<a>📢 '+esc(x)+'</a>'}).join('');$('tm').innerHTML=h+h}
function applyCfg(){var s=document.documentElement.style;s.setProperty('--ac',CF.c1);s.setProperty('--ac2',CF.c2);$('nm').textContent=CF.name;document.title=CF.name;tick();
$('lk').innerHTML=CF.lk.map(function(l){return/^(https?:\/\/|#)/.test(l.u)?'<a class="pl" target="_blank" rel="noopener" href="'+l.u.replace(/"/g,'')+'">'+esc(l.t)+'</a>':''}).join('')}
var J={'Content-Type':'application/json'};
function api(u,o){o=o||{};o.credentials='same-origin';return fetch(u,o).then(function(r){return r.json().then(function(j){if(!r.ok)throw j.error||'Error';return j})})}
function tiles(){var r=[];T.forEach(function(t){var o=CF.ov[t.id]||{};if(o.hide)return;var x=Object.assign({},t);['n','d','i','c'].forEach(function(k){if(o[k])x[k]=o[k]});r.push(x)});return r.concat(CF.ct)}
$('ad').onclick=function(){api('/api/admin/me').then(function(){adm(JSON.parse(JSON.stringify(CF)))}).catch(login)};
function login(){$('sh').innerHTML='<h2>🔐 Admin Login<button class="x" onclick="closeT()">✕</button></h2><label>Password</label><input id="pw" type="password"><button class="go" id="lgn">Login</button><div class="res" id="rl"></div>';$('ov').classList.add('on');
$('lgn').onclick=function(){api('/api/admin/login',{method:'POST',headers:J,body:JSON.stringify({pw:$('pw').value})}).then(function(){adm(JSON.parse(JSON.stringify(CF)))}).catch(function(e){show($('rl'),String(e))})}}
function col(){var E={name:$('a1').value||'ToolHub',c1:$('a2').value,c2:$('a3').value,tk:$('a4').value.split('\n').filter(Boolean),lk:$('a5').value.split('\n').filter(Boolean).map(function(x){var p=x.split('|');return{t:p[0].trim(),u:(p[1]||'#').trim()}}),ov:{},ct:[]};
[].forEach.call(document.querySelectorAll('.ar'),function(r){var g=function(f){return r.querySelector('[data-f='+f+']').value},o={i:g('i'),n:g('n'),d:g('d'),c:g('c')};
if(r.dataset.b){o.hide=r.querySelector('.hd').checked;E.ov[r.dataset.id]=o}else{o.id=r.dataset.id;o.g=r.dataset.g;o.h=r.dataset.h;E.ct.push(o)}});return E}
function adm(E){E.ov=E.ov||{};E.ct=E.ct||[];
var h='<h2>⚙️ Admin Panel<button class="x" onclick="closeT()">✕</button></h2><label>Site name</label><input id="a1" value="'+esc(E.name)+'"><label>Main colour</label><input id="a2" type="color" value="'+E.c1+'"><label>Second colour</label><input id="a3" type="color" value="'+E.c2+'"><label>Scrolling news (one per line)</label><textarea id="a4" rows="3">'+esc(E.tk.join('\n'))+'</textarea><label>Links under search (Title | URL per line)</label><textarea id="a5" rows="3">'+esc(E.lk.map(function(l){return l.t+' | '+l.u}).join('\n'))+'</textarea><label>Tiles: icon, name, description, category</label>';
function ar(id,o,b,x){return'<div class="ar" data-id="'+id+'"'+(b?' data-b="1"':' data-g="'+esc(x.g)+'" data-h="'+esc(x.h)+'"')+'><div class="r2"><input data-f="i" value="'+esc(o.i)+'"><input data-f="n" value="'+esc(o.n)+'"></div><input data-f="d" value="'+esc(o.d)+'"><input data-f="c" value="'+esc(o.c)+'">'+(b?'<label><input type="checkbox" class="hd"'+(o.hide?' checked':'')+'> Hide this tile</label>':'<small>'+esc(x.h)+'</small><button class="x dl" style="width:auto;padding:0 12px">🗑 Remove tile</button>')+'</div>'}
T.forEach(function(t){var o=E.ov[t.id]||{};h+=ar(t.id,{i:o.i||t.i,n:o.n||t.n,d:o.d||t.d,c:o.c||t.c,hide:o.hide},1)});
E.ct.forEach(function(t){h+=ar(t.id,t,0,t)});
h+='<button class="go" id="al" style="background:#0ea5e9">➕ Add link tile</button><button class="go" id="ap" style="background:#ef4444">➕ Add PDF tile</button><input type="file" id="apf" accept="application/pdf" style="display:none"><button class="go" id="sv">💾 Save all changes</button><button class="go" id="lo" style="background:#64748b">Logout</button><div class="res" id="ra"></div>';
$('sh').innerHTML=h;$('ov').classList.add('on');
function nt(x){var E2=col();E2.ct.push(Object.assign({id:'c'+Date.now().toString(36),n:'New tile',d:'Edit me',i:'🔗',c:'Links',g:'#bfdbfe,#3b82f6',h:'#'},x));adm(E2)}
$('al').onclick=function(){var u=prompt('Link URL (https://...)');if(u)nt({h:u})};
$('ap').onclick=function(){$('apf').click()};
$('apf').onchange=function(){var f=this.files[0];if(!f)return;var fd=new FormData();fd.append('file',f);show($('ra'),'Uploading...');
api('/api/admin/upload',{method:'POST',body:fd}).then(function(j){nt({h:j.url,i:'📄',c:'PDFs',g:'#fecaca,#ef4444',n:f.name.replace(/\.pdf$/i,'').slice(0,40),d:'Tap to open PDF'})}).catch(function(e){show($('ra'),'Error: '+e)})};
[].forEach.call(document.querySelectorAll('.dl'),function(b){b.onclick=function(){b.parentNode.remove()}});
$('sv').onclick=function(){api('/api/admin/config',{method:'POST',headers:J,body:JSON.stringify(col())}).then(function(j){CF=Object.assign(CF,j);applyCfg();chips();draw();closeT()}).catch(function(e){show($('ra'),'Error: '+e)})};
$('lo').onclick=function(){api('/api/admin/logout',{method:'POST'}).then(closeT)}}
function pgen(b){b.innerHTML='<label>Your idea</label><textarea id="pi" rows="3" placeholder="e.g. old man selling tea at sunrise"></textarea><label>Style</label><select id="ps">'+o(['Ultra realistic photo','Cinematic','Portrait','Product photo','Anime','3D render','Digital painting'])+'</select><button class="go" id="g7">✨ Generate prompt</button><div class="res" id="r7"></div><textarea id="po" rows="6" style="display:none;margin-top:10px"></textarea><button class="go" id="c7" style="display:none;background:#64748b">📋 Copy prompt</button><button class="go" id="m7" style="display:none;background:#10b981">🎨 Create photo from this prompt</button>';
$('g7').onclick=function(){var g=this;g.disabled=true;g.textContent='Writing...';api('/api/prompt',{method:'POST',headers:J,body:JSON.stringify({idea:$('pi').value,style:$('ps').value})}).then(function(j){$('r7').style.display='none';var p=$('po');p.value=j.prompt;['po','c7','m7'].forEach(function(i){$(i).style.display='block'})}).catch(function(e){show($('r7'),'Error: '+e)}).then(function(){g.disabled=false;g.textContent='✨ Generate prompt'})};
$('c7').onclick=function(){try{navigator.clipboard.writeText($('po').value);this.textContent='✅ Copied'}catch(e){$('po').select()}};
$('m7').onclick=function(){window.pp=$('po').value;openT('img')}}
function shrink(f){return new Promise(function(ok){var im=new Image();im.onload=function(){var k=Math.min(1,1280/Math.max(im.width,im.height)),c=document.createElement('canvas');c.width=im.width*k;c.height=im.height*k;c.getContext('2d').drawImage(im,0,0,c.width,c.height);c.toBlob(ok,'image/jpeg',.88)};im.onerror=function(){ok(null)};im.src=URL.createObjectURL(f)})}
function igen(b){b.innerHTML='<label>Describe the photo you want</label><textarea id="ip" rows="4" placeholder="Describe what you want to see"></textarea><label>📷 Upload your photo(s) - optional, up to 3</label><input type="file" id="ipf" accept="image/*" multiple><div id="pv" style="display:flex;gap:8px;flex-wrap:wrap;margin-top:8px"></div><p style="font-size:12px;color:var(--mu);margin:8px 0 0">With a photo, describe the change, e.g. "me in a black suit at a beach sunset".</p><button class="go" id="g8">🎨 Generate photo</button><div class="res" id="r8"></div><img id="im" alt="Generated photo" style="width:100%;border-radius:14px;margin-top:12px;display:none"><button class="go" id="d8" style="display:none;background:#64748b">⬇ Download image</button>';
if(window.pp){$('ip').value=window.pp;window.pp=''}
$('ipf').onchange=function(){var v=$('pv');v.innerHTML='';[].slice.call(this.files,0,3).forEach(function(f){var m=document.createElement('img');m.src=URL.createObjectURL(f);m.style.cssText='width:72px;height:72px;object-fit:cover;border-radius:12px';v.appendChild(m)})};
$('g8').onclick=function(){var p=$('ip').value.trim();if(!p)return show($('r8'),'Describe the photo first');var g=this,fs=[].slice.call($('ipf').files,0,3);g.disabled=true;g.textContent='Creating... (up to 30s)';
Promise.all(fs.map(shrink)).then(function(bl){var fd=new FormData();fd.append('prompt',p);bl.forEach(function(x){if(x)fd.append('photos',x,'p.jpg')});return api('/api/image',{method:'POST',body:fd})}).then(function(j){$('r8').style.display='none';var m=$('im');m.src=j.image;m.style.display='block';$('d8').style.display='block';$('d8').onclick=function(){var a=document.createElement('a');a.href=j.image;a.download='photo.png';a.click()}}).catch(function(e){show($('r8'),'Error: '+e)}).then(function(){g.disabled=false;g.textContent='🎨 Generate photo'})}}
function thg(b){b.innerHTML='<label>Title</label><input id="tt1" value="Your Title Here"><label>Small text (optional)</label><input id="tt2"><label>Background photo (optional)</label><input type="file" id="tf" accept="image/*"><label>Colour theme</label><select id="tc">'+o(['Blue-Purple','Red-Orange','Green-Teal','Black-Gold'])+'</select><button class="go" id="g9">Create thumbnail</button><canvas id="cv" width="1280" height="720" style="width:100%;border-radius:14px;margin-top:12px;display:none"></canvas><button class="go" id="d9" style="display:none;background:#64748b">⬇ Download PNG</button>';
$('g9').onclick=function(){var cv=$('cv'),x=cv.getContext('2d'),th={'Blue-Purple':['#2563eb','#7c3aed'],'Red-Orange':['#ef4444','#f59e0b'],'Green-Teal':['#10b981','#06b6d4'],'Black-Gold':['#0f172a','#d97706']}[$('tc').value];
function fin(){var gr=x.createLinearGradient(0,720,0,150);gr.addColorStop(0,'rgba(0,0,0,.8)');gr.addColorStop(1,'rgba(0,0,0,0)');x.fillStyle=gr;x.fillRect(0,0,1280,720);
x.textAlign='center';x.lineJoin='round';x.font='900 100px "Noto Sans Telugu",Arial,sans-serif';var ls=[],c='';$('tt1').value.split(' ').forEach(function(w){var t=c?c+' '+w:w;if(x.measureText(t).width>1120&&c){ls.push(c);c=w}else c=t});ls.push(c);
ls.forEach(function(l,i){var y=570-(ls.length-1-i)*115;x.lineWidth=14;x.strokeStyle='#000';x.strokeText(l,640,y);x.fillStyle='#fff';x.fillText(l,640,y)});
if($('tt2').value){x.font='700 50px "Noto Sans Telugu",Arial,sans-serif';x.fillStyle='#fde047';x.fillText($('tt2').value,640,670)}
cv.style.display='block';$('d9').style.display='block';$('d9').onclick=function(){cv.toBlob(function(q){dl(q,'thumbnail.png')})}}
var f=$('tf').files[0];
if(f){var im=new Image();im.onload=function(){var k=Math.max(1280/im.width,720/im.height),w=im.width*k,h=im.height*k;x.drawImage(im,(1280-w)/2,(720-h)/2,w,h);fin()};im.src=URL.createObjectURL(f)}
else{var g=x.createLinearGradient(0,0,1280,720);g.addColorStop(0,th[0]);g.addColorStop(1,th[1]);x.fillStyle=g;x.fillRect(0,0,1280,720);fin()}}}
function o(a){return a.map(function(x){return'<option>'+x+'</option>'}).join('')}
function mk(b){var U={'JNTUH':.75,'JNTUK':.75,'JNTUA':.75,'Osmania (OU)':0,'Kakatiya (KU)':0,'Telangana Univ':0,'Andhra Univ':0,'Acharya Nagarjuna':0,'SVU':0,'Other':0};
b.innerHTML='<label>Student name (shown in PDF)</label><input id="sn" placeholder="Your name"><label>Level</label><select id="lv">'+o(['SSC (10th)','Inter (Intermediate)','Degree'])+'</select><div id="ex"></div><div id="sbw"><label>Subject-wise marks (optional)</label><div id="sb"></div><button class="go" id="as" style="background:#0ea5e9;margin-top:8px">➕ Add subject</button></div><label>Total marks (editable)</label><input id="tt" type="number" inputmode="decimal"><label id="ol">Marks obtained</label><input id="ob" type="number" inputmode="decimal"><button class="go" id="g4">Calculate</button><div class="res" id="r4"></div><button class="go" id="d4" style="display:none;background:#64748b">⬇ Download PDF report</button><p style="font-size:12px;color:var(--mu)" id="nt"></p>';
function set(){var l=$('lv').value[0],t=$('tt');if(l=='S')t.value=600;if(l=='I')t.value=$('yr').value.indexOf('Both')==0?1000:500;if(l=='D')t.value='';
var c=l=='D'&&$('md').value=='CGPA';$('ol').textContent=c?'CGPA (out of 10)':'Marks obtained';t.disabled=c;$('sbw').style.display=c?'none':'block';$('nt').textContent=c?'CGPA formula differs by university - confirm with your university circular.':'Default totals can differ by board/year - edit the total if yours is different. If you add subjects, totals are calculated from them.'}
function ui(){var l=$('lv').value[0],h='';
if(l=='I')h='<label>Group</label><select id="gp">'+o(['MPC','BiPC','CEC','HEC','MEC','Other'])+'</select><label>Year</label><select id="yr">'+o(['Both years (1000)','1st year (500)','2nd year (500)'])+'</select>';
if(l=='D')h='<label>University</label><select id="un">'+o(Object.keys(U))+'</select><label>Method</label><select id="md">'+o(['Marks','CGPA'])+'</select>';
$('ex').innerHTML=h;set();if($('yr'))$('yr').onchange=set;if($('md'))$('md').onchange=set}
$('lv').onchange=ui;ui();
$('as').onclick=function(){var d=document.createElement('div');d.className='r3';d.innerHTML='<input placeholder="Subject"><input type="number" inputmode="decimal" placeholder="Marks"><input type="number" inputmode="decimal" placeholder="Max" value="100">';$('sb').appendChild(d)};
$('g4').onclick=function(){var l=$('lv').value[0],r=$('r4'),cg=l=='D'&&$('md').value=='CGPA',x=+$('ob').value,t=+$('tt').value,R=[],m={name:$('sn').value.trim(),lv:$('lv').value};
if(l=='I')m.lv+=' - '+$('gp').value+' ('+$('yr').value.replace(/ \(.*/,'')+')';
if(l=='D')m.lv+=' - '+$('un').value;
if(cg){if(!(x>0&&x<=10))return show(r,'Enter CGPA 0-10');var u=$('un').value,p=Math.max(0,(x-U[u])*10);m.cg=x;m.pct=p;m.R=[];window.mkr=m;show(r,u+': '+p.toFixed(2)+'%');$('d4').style.display='block';return}
[].forEach.call(document.querySelectorAll('#sb .r3'),function(e){var v=e.children[1].value;if(v!=='')R.push({n:e.children[0].value.trim()||'Subject',m:+v,x:+e.children[2].value||100})});
if(R.length){if(R.some(function(q){return q.m<0||q.m>q.x}))return show(r,'A subject mark is more than its maximum');x=R.reduce(function(a,q){return a+q.m},0);t=R.reduce(function(a,q){return a+q.x},0);$('ob').value=x;$('tt').value=t}
if(!(t>0)||x<0||x>t)return show(r,'Check marks and total');
m.R=R;m.x=x;m.t=t;m.pct=x/t*100;window.mkr=m;show(r,'Percentage: '+m.pct.toFixed(2)+'%');$('d4').style.display='block'};
$('d4').onclick=function(){mkpdf(window.mkr)}}
function mkpdf(r){var c1=CF.c1,c2=CF.c2,p=r.pct,msg=p>=75?'Excellent work! 🌟':p>=60?'Good job! 👍':p>=40?'Keep improving! 💪':'Work harder - you can do it! 🔥',td='padding:12px 16px;',
rows=(r.R||[]).map(function(q,i){var z=Math.min(100,q.m/q.x*100);return'<tr style="background:'+(i%2?'#fff':'#f4f6fb')+'"><td style="'+td+'font-weight:700">'+esc(q.n)+'</td><td style="'+td+'text-align:center">'+q.m+' / '+q.x+'</td><td style="'+td+'width:190px"><div style="height:12px;border-radius:9px;background:#e5e9f2"><div style="height:12px;border-radius:9px;width:'+z.toFixed(0)+'%;background:linear-gradient(90deg,'+c1+','+c2+')"></div></div></td><td style="'+td+'text-align:right;font-weight:700;color:'+c1+'">'+z.toFixed(0)+'%</td></tr>'}).join(''),
tb=rows?'<table style="width:100%;border-collapse:collapse;border:1px solid #e5e9f2;margin-top:24px"><tr style="background:linear-gradient(90deg,'+c1+','+c2+');color:#fff"><th style="'+td+'text-align:left">Subject</th><th style="'+td+'">Marks</th><th style="'+td+'">Progress</th><th style="'+td+'text-align:right">%</th></tr>'+rows+'</table>':'',
sm=r.cg!=null?'CGPA: <b>'+r.cg+'</b>':'Total: <b>'+r.x+' / '+r.t+'</b>';
pout('<div style="padding:30px 40px"><div style="display:flex;align-items:center;justify-content:space-between;gap:20px"><div><div style="font-size:27px;font-weight:800;color:'+c1+'">📊 Marks Report</div>'+(r.name?'<div style="font-size:23px;font-weight:700;margin-top:6px">'+esc(r.name)+'</div>':'')+'<div style="display:inline-block;margin-top:8px;background:#eef2ff;color:'+c1+';padding:5px 14px;border-radius:99px;font-size:15px;font-weight:700">'+esc(r.lv)+'</div><div style="margin-top:10px;font-size:20px">'+sm+'</div></div><div style="flex:none;width:165px;height:165px;border-radius:50%;background:linear-gradient(135deg,'+c1+','+c2+');color:#fff;display:flex;flex-direction:column;align-items:center;justify-content:center;box-shadow:0 8px 24px rgba(0,0,0,.18)"><div style="font-size:42px;font-weight:800;line-height:1">'+p.toFixed(1)+'%</div><div style="font-size:13px;letter-spacing:1px;margin-top:4px">PERCENTAGE</div></div></div>'+tb+'<div style="margin-top:26px;text-align:center;font-size:23px;font-weight:800;color:'+c2+'">'+msg+'</div>'+cta()+'</div>','marks-report.pdf')}
function rbar(q,h){var t=q.correct+q.wrong+q.unattempted||1;return'<div style="display:flex;height:'+h+'px;border-radius:9px;overflow:hidden;background:#e5e9f2"><div style="width:'+q.correct/t*100+'%;background:#22c55e"></div><div style="width:'+q.wrong/t*100+'%;background:#ef4444"></div><div style="width:'+q.unattempted/t*100+'%;background:#94a3b8"></div></div>'}
function rsa(b){b.innerHTML='<p style="font-size:13px;color:var(--mu)">Paste your response sheet link. We read it automatically, find the subjects and the marking scheme (including negative marks) and work out your marks.</p><label>🔗 Response sheet link</label><input id="ru" type="url" placeholder="https://..."><label>🔑 Answer key link (optional - only if your sheet has no correct answers)</label><input id="rk" type="url" placeholder="https://..."><label>Or upload a screenshot / PDF (if the link does not open)</label><input type="file" id="rf" accept="image/*,application/pdf"><button class="go" id="g5">📈 Analyse</button><div class="res" id="r9"></div><div id="ao"></div><button class="go" id="d5" style="display:none;background:#64748b">⬇ Download PDF report</button><p style="font-size:12px;color:var(--mu)">AI reading can make mistakes - always check with your official sheet. Only public links work; pages that need a login cannot be opened.</p>';
$('g5').onclick=function(){var u=$('ru').value.trim(),f=$('rf').files[0];if(!u&&!f)return show($('r9'),'Paste your response sheet link first');var fd=new FormData(),g=this;if(u){fd.append('url',u);fd.append('key',$('rk').value.trim())}else fd.append('file',f);g.disabled=true;g.textContent='Reading your sheet... up to a minute';
api('/api/analyse',{method:'POST',body:fd}).then(function(R){$('r9').style.display='none';var C=0,W=0,U=0;R.subjects.forEach(function(q){C+=q.correct;W+=q.wrong;U+=q.unattempted});R.C=C;R.W=W;R.U=U;R.acc=C+W?C/(C+W)*100:0;
$('ao').innerHTML='<div class="res" style="display:block;margin-top:14px;text-align:center"><div style="font-size:13px">'+esc(R.exam||'Your exam')+(R.candidate?' · '+esc(R.candidate):'')+'</div><div style="margin:6px 0"><span class="big" id="n0" style="font-size:46px">0</span><span style="font-size:20px"> / '+R.max+'</span></div><div style="font-size:13px">Total marks · Scheme: +'+R.cm+' / −'+R.nm+' · Accuracy '+R.acc.toFixed(1)+'%</div></div><div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:12px"><div class="sc" style="background:#22c55e">✅ Correct<b id="n1">0</b></div><div class="sc" style="background:#ef4444">❌ Wrong<b id="n2">0</b></div><div class="sc" style="background:#64748b">⏭ Skipped<b id="n3">0</b></div></div>'+R.subjects.map(function(q){return'<div class="ar"><b>'+esc(q.name)+'</b>'+rbar(q,12)+'<small>✅ '+q.correct+' · ❌ '+q.wrong+' · ⏭ '+q.unattempted+' · Marks: '+q.marks+' / '+q.max+'</small></div>'}).join('')+(R.note?'<small>'+esc(R.note)+'</small>':'');
cnt($('n0'),R.total);cnt($('n1'),C);cnt($('n2'),W);cnt($('n3'),U);$('d5').style.display='block';$('d5').onclick=function(){rapdf(R)}}).catch(function(e){show($('r9'),'Error: '+e)}).then(function(){g.disabled=false;g.textContent='📈 Analyse'})}}
function rapdf(R){var c1=CF.c1,c2=CF.c2,td='padding:12px 12px;',p=R.max>0?R.total/R.max*100:0,msg=p>=75?'Excellent performance! 🌟':p>=60?'Good job! 👍':p>=40?'Keep improving! 💪':'Work harder - you can do it! 🔥',
cd=function(t,v,c){return'<div style="flex:1;background:'+c+';color:#fff;border-radius:18px;padding:14px;text-align:center"><div style="font-size:13px;letter-spacing:.5px">'+t+'</div><div style="font-size:34px;font-weight:800;line-height:1.3">'+v+'</div></div>'},
rows=R.subjects.map(function(q,i){return'<tr style="background:'+(i%2?'#fff':'#f4f6fb')+'"><td style="'+td+'font-weight:700">'+esc(q.name)+'</td><td style="'+td+'text-align:center;color:#16a34a;font-weight:700">'+q.correct+'</td><td style="'+td+'text-align:center;color:#dc2626;font-weight:700">'+q.wrong+'</td><td style="'+td+'text-align:center;color:#64748b;font-weight:700">'+q.unattempted+'</td><td style="'+td+'text-align:center;font-weight:800;color:'+c1+'">'+q.marks+'</td><td style="'+td+'width:130px">'+rbar(q,12)+'</td></tr>'}).join('');
pout('<div style="padding:30px 40px"><div style="display:flex;align-items:center;justify-content:space-between;gap:20px"><div><div style="font-size:27px;font-weight:800;color:'+c1+'">🏆 Response Sheet Report</div>'+(R.candidate?'<div style="font-size:22px;font-weight:700;margin-top:6px">'+esc(R.candidate)+'</div>':'')+'<div style="display:inline-block;margin-top:8px;background:#eef2ff;color:'+c1+';padding:5px 14px;border-radius:99px;font-size:15px;font-weight:700">'+esc(R.exam||'Exam')+'</div><div style="margin-top:10px;font-size:16px;color:#475569">Marking: +'+R.cm+' correct / −'+R.nm+' wrong</div></div><div style="flex:none;width:170px;height:170px;border-radius:50%;background:linear-gradient(135deg,'+c1+','+c2+');color:#fff;display:flex;flex-direction:column;align-items:center;justify-content:center;box-shadow:0 8px 24px rgba(0,0,0,.18)"><div style="font-size:40px;font-weight:800;line-height:1">'+R.total+'</div><div style="font-size:14px;margin-top:2px">out of '+R.max+'</div><div style="font-size:11px;letter-spacing:1px;margin-top:4px">TOTAL MARKS</div></div></div><div style="display:flex;gap:14px;margin-top:22px">'+cd('✅ CORRECT',R.C,'#22c55e')+cd('❌ WRONG',R.W,'#ef4444')+cd('⏭ SKIPPED',R.U,'#64748b')+cd('📊 ACCURACY',R.acc.toFixed(0)+'%',c2)+'</div><table style="width:100%;border-collapse:collapse;border:1px solid #e5e9f2;margin-top:24px"><tr style="background:linear-gradient(90deg,'+c1+','+c2+');color:#fff"><th style="'+td+'text-align:left">Subject</th><th style="'+td+'">✅</th><th style="'+td+'">❌</th><th style="'+td+'">⏭</th><th style="'+td+'">Marks</th><th style="'+td+'">Split</th></tr>'+rows+'</table><div style="margin-top:24px;text-align:center;font-size:23px;font-weight:800;color:'+c2+'">'+msg+'</div>'+cta()+'</div>','response-report.pdf')}
function imgc(b){b.innerHTML='<label>Choose image</label><input type="file" id="fi" accept="image/*"><label>Quality: <span id="qv">60</span>%</label><input type="range" id="qr" min="10" max="95" value="60"><button class="go" id="g1">Compress &amp; download</button><div class="res" id="r1"></div>';
$('qr').oninput=function(){$('qv').textContent=this.value};
$('g1').onclick=function(){var f=$('fi').files[0];if(!f)return show($('r1'),'Choose an image first');var im=new Image();
im.onload=function(){var c=document.createElement('canvas');c.width=im.width;c.height=im.height;var x=c.getContext('2d');x.fillStyle='#fff';x.fillRect(0,0,c.width,c.height);x.drawImage(im,0,0);
c.toBlob(function(q){show($('r1'),'Saved '+Math.max(0,Math.round((1-q.size/f.size)*100))+'% ('+kb(f.size)+' to '+kb(q.size)+')');dl(q,'compressed.jpg')},'image/jpeg',$('qr').value/100)};im.src=URL.createObjectURL(f)}}
function pdfl(cb){if(window.PDFLib)return cb();var s=document.createElement('script');s.src='https://cdnjs.cloudflare.com/ajax/libs/pdf-lib/1.17.1/pdf-lib.min.js';s.onload=cb;s.onerror=function(){alert('Could not load PDF engine - check internet')};document.head.appendChild(s)}
function pdfc(b){b.innerHTML='<label>Choose PDF</label><input type="file" id="fp" accept="application/pdf"><button class="go" id="g2">Optimize PDF</button><div class="res" id="r2"></div><p style="font-size:12px;color:var(--mu)">Lossless in-browser optimizer. Scanned or image-heavy PDFs need a server (Ghostscript) to shrink a lot.</p>';
$('g2').onclick=function(){var f=$('fp').files[0];if(!f)return show($('r2'),'Choose a PDF first');pdfl(function(){f.arrayBuffer().then(function(a){return PDFLib.PDFDocument.load(a,{ignoreEncryption:true})}).then(function(d){return d.save({useObjectStreams:true})}).then(function(u){var q=new Blob([u],{type:'application/pdf'});show($('r2'),'Saved '+Math.max(0,Math.round((1-q.size/f.size)*100))+'% ('+kb(f.size)+' to '+kb(q.size)+')');dl(q,'optimized.pdf')}).catch(function(){show($('r2'),'Could not read this PDF')})})}}
var LG={'te-IN':['Telugu','te'],'en-IN':['English','en'],'hi-IN':['Hindi','hi'],'ta-IN':['Tamil','ta'],'kn-IN':['Kannada','kn'],'ml-IN':['Malayalam','ml'],'ur-IN':['Urdu','ur']};
function lo(sel){return Object.keys(LG).map(function(k){return'<option value="'+k+'"'+(k==sel?' selected':'')+'>'+LG[k][0]+'</option>'}).join('')}
function lds(u,t,cb){if(window[t])return cb();var s=document.createElement('script');s.src=u;s.onload=cb;s.onerror=function(){alert('Could not load file - check internet')};document.head.appendChild(s)}
function trn(x,a,c){if(a==c||!x.trim())return Promise.resolve(x);var w=x.split(/\s+/),ch=[],cur='';
w.forEach(function(s){if((cur+' '+s).length>400){ch.push(cur);cur=s}else cur=cur?cur+' '+s:s});if(cur)ch.push(cur);
return Promise.all(ch.map(function(q){return fetch('https://api.mymemory.translated.net/get?q='+encodeURIComponent(q)+'&langpair='+encodeURIComponent(a+'|'+c)).then(function(r){return r.json()}).then(function(j){var t=j.responseData.translatedText;if(/MYMEMORY WARNING/.test(t))throw 0;return t})})).then(function(r){return r.join(' ')})}
function pban(){var u=location.origin,c1=CF.c1,c2=CF.c2;return'<div style="background:linear-gradient(135deg,'+c1+','+c2+');color:#fff;padding:36px 40px;text-align:center;border-radius:0 0 30px 30px"><div style="font-size:40px;font-weight:800;letter-spacing:.5px">⚡ '+esc(CF.name)+'</div><div style="font-size:15px;opacity:.92;margin-top:2px">All-in-one tools for everyone</div><a href="'+esc(u)+'" style="display:inline-block;margin-top:14px;background:rgba(255,255,255,.24);color:#fff;text-decoration:none;padding:8px 22px;border-radius:99px;font-size:15px">🔗 '+esc(u.replace(/^https?:\/\//,''))+'</a></div>'}
function cta(){var u=location.origin;return'<div style="margin-top:26px;text-align:center;background:#f4f6fb;border:2px dashed '+CF.c1+';border-radius:18px;padding:16px"><div style="font-size:16px">🚀 Try it free at</div><a href="'+esc(u)+'" style="font-size:22px;font-weight:800;color:'+CF.c1+';text-decoration:none">'+esc(u.replace(/^https?:\/\//,''))+'</a></div>'}
function pout(body,nm){var d=document.createElement('div'),w=document.createElement('div');
d.style.cssText='width:794px;background:#fff;color:#0f172a;font:18px/1.8 "Noto Sans Telugu","Noto Sans Devanagari","Noto Sans Tamil","Noto Sans Kannada","Noto Sans Malayalam","Noto Sans Arabic",sans-serif';d.innerHTML=pban()+body;
w.style.cssText='position:fixed;left:0;top:0;width:0;height:0;overflow:hidden;z-index:-1';w.appendChild(d);document.body.appendChild(w);
var fl=['Telugu','Devanagari','Tamil','Kannada','Malayalam','Arabic'].map(function(n){return document.fonts?document.fonts.load('18px "Noto Sans '+n+'"',d.textContent).catch(function(){}):0});
lds('https://cdnjs.cloudflare.com/ajax/libs/html2pdf.js/0.10.1/html2pdf.bundle.min.js','html2pdf',function(){Promise.all(fl).then(function(){setTimeout(function(){html2pdf().set({margin:0,filename:nm,enableLinks:true,html2canvas:{scale:2,scrollX:0,scrollY:0,windowWidth:794,useCORS:true},jsPDF:{unit:'mm',format:'a4'},pagebreak:{mode:['css','legacy']}}).from(d).save().then(function(){w.remove()}).catch(function(){w.remove();alert('PDF failed - please try again')})},300)})})}
function topdf(x,nm,tt){var c1=CF.c1,c2=CF.c2,sh=esc(location.origin.replace(/^https?:\/\//,''));
pout('<div style="padding:30px 40px 24px"><div style="display:flex;align-items:center;gap:12px;margin-bottom:16px"><div style="width:7px;height:36px;border-radius:5px;background:linear-gradient(180deg,'+c1+','+c2+')"></div><div style="font-size:27px;font-weight:800;color:'+c1+'">'+esc(tt||'Your Translation')+'</div></div><div style="background:#f4f6fb;border:1px solid #e5e9f2;border-left:6px solid '+c2+';border-radius:18px;padding:22px 26px;white-space:pre-wrap;word-break:break-word">'+esc(x)+'</div><div style="text-align:center;color:#64748b;font-size:13px;margin-top:26px">Created with '+esc(CF.name)+' · '+sh+'</div></div>',nm)}
function cnt(el,v){var t0=performance.now();(function f(n){var e=Math.min(1,(n-t0)/900);el.textContent=v%1?(v*e).toFixed(2):Math.round(v*e);if(e<1)requestAnimationFrame(f)})(t0)}
function mic(b,tr){var SR=window.SpeechRecognition||window.webkitSpeechRecognition;
b.innerHTML='<label>I will speak in</label><select id="lg">'+(tr?'<option value="te-IN">Telugu</option>':lo('te-IN'))+'</select><label>Get output in</label><select id="ol2">'+(tr?'<option value="en-IN">English</option>':lo('en-IN'))+'</select><button class="go" id="mc">🎤 Start</button><label>What you said</label><textarea id="tx" rows="5"></textarea>'+(tr?'<label>English</label><textarea id="en" rows="4"></textarea>':'<button class="go" id="d1">⬇ Download PDF</button><button class="go" id="d2" style="background:#64748b">⬇ Download Word</button><div class="res" id="r5"></div>');
if(!tr){var out=function(cb){var r=$('r5');r.style.display='block';if(!$('tx').value.trim()){r.textContent='Speak something first';return}r.textContent='Preparing...';
trn($('tx').value,LG[$('lg').value][1],LG[$('ol2').value][1]).then(function(t){r.textContent='Downloading...';cb(t)}).catch(function(){r.textContent='Translation failed - check internet or try later'})};
$('d1').onclick=function(){out(function(t){topdf(t,'speech.pdf',$('lg').value==$('ol2').value?'Your Speech Text':'Your Translation')})};
$('d2').onclick=function(){out(function(t){dl(new Blob(['<html><meta charset="utf-8"><body><p>'+esc(t).replace(/\n/g,'<br>')+'</p></body></html>'],{type:'application/msword'}),'speech.doc')})}}
if(!SR){$('tx').value='Voice input is not supported in this browser. Please use Chrome.';return}
var on=0;$('mc').onclick=function(){if(on){rec.stop();return}
window.rec=new SR();rec.lang=$('lg').value;rec.continuous=!tr;rec.interimResults=false;
rec.onstart=function(){on=1;$('mc').textContent='⏹ Stop (listening...)';$('mc').classList.add('lv')};
rec.onend=function(){on=0;$('mc').textContent='🎤 Start';$('mc').classList.remove('lv')};
rec.onresult=function(e){var s='';for(var i=e.resultIndex;i<e.results.length;i++)s+=e.results[i][0].transcript+' ';$('tx').value+=s;
if(tr)trn($('tx').value,'te','en').then(function(t){$('en').value=t}).catch(function(){$('en').value='Translation needs internet'})};
rec.start()}}
function qa(b){b.innerHTML='<p style="font-size:13px;color:var(--mu)">Upload a question paper (photo or PDF). Answers and explanations come in the same language as the paper.</p><input type="file" id="qf" accept="image/*,application/pdf"><button class="go" id="g6">Get answers</button><div class="res" id="r6"></div><div id="an" style="white-space:pre-wrap;margin-top:12px;line-height:1.7"></div><button class="go" id="d6" style="display:none;background:#64748b">⬇ Download answers as PDF</button>';
$('g6').onclick=function(){var f=$('qf').files[0];if(!f)return show($('r6'),'Upload a file first');var fd=new FormData(),g=this;fd.append('file',f);g.disabled=true;g.textContent='Solving... up to a minute';
fetch('/api/solve',{method:'POST',body:fd}).then(function(r){return r.json()}).then(function(j){if(j.error)throw j.error;$('r6').style.display='none';$('an').textContent=j.answer;$('d6').style.display='block';$('d6').onclick=function(){topdf(j.answer,'answers.pdf','Your Answers')}}).catch(function(e){show($('r6'),'Error: '+(typeof e=='string'?e:'cannot reach the server. Open the website from your Flask link (https://...), not the saved HTML file'))}).then(function(){g.disabled=false;g.textContent='Get answers'})}}
function ff(b){var P=['name','father','dob','mobile','email','address'],pf={};try{pf=JSON.parse(localStorage.getItem('pf'))||{}}catch(e){}
b.innerHTML='<p style="font-size:13px;color:var(--mu)">Save your details once, then upload a fillable PDF form.</p>'+P.map(function(k){return'<input id="p_'+k+'" placeholder="'+k+'" value="'+esc(pf[k]||'')+'" style="margin-bottom:6px">'}).join('')+'<label>Upload PDF form</label><input type="file" id="ffi" accept="application/pdf"><button class="go" id="g3">Auto-fill &amp; download</button><div class="res" id="r3"></div>';
var M={name:/name|applicant|candidate|student/i,father:/father|parent|guardian/i,dob:/dob|birth/i,mobile:/mobile|phone|contact/i,email:/mail/i,address:/address|village|town/i};
$('g3').onclick=function(){var f=$('ffi').files[0],v={};P.forEach(function(k){v[k]=$('p_'+k).value});try{localStorage.setItem('pf',JSON.stringify(v))}catch(e){}
if(!f)return show($('r3'),'Upload a PDF form first');
pdfl(function(){f.arrayBuffer().then(function(a){return PDFLib.PDFDocument.load(a)}).then(function(d){var n=0;d.getForm().getFields().forEach(function(fl){if(!(fl instanceof PDFLib.PDFTextField))return;var nm=fl.getName();for(var k in M){if(M[k].test(nm)&&v[k]&&!(k=='name'&&/father|parent/i.test(nm))){fl.setText(v[k]);n++;break}}});
return d.save().then(function(u){dl(new Blob([u],{type:'application/pdf'}),'filled.pdf');show($('r3'),n?'Filled '+n+' fields successfully':'No matching fields found - form may be scanned (needs OCR server)')})}).catch(function(){show($('r3'),'Could not read this PDF')})})}}
fetch('/api/visit',{method:'POST'}).then(function(r){return r.json()}).then(function(j){$('vcw').style.display='block';$('vt').textContent=j.total.toLocaleString('en-IN');var t0=performance.now();(function f(n){var e=Math.min(1,(n-t0)/1200);$('vc').textContent=Math.round(j.unique*e).toLocaleString('en-IN');if(e<1)requestAnimationFrame(f)})(t0)}).catch(function(){});
api('/api/config').then(function(j){CF=Object.assign(CF,j);applyCfg();chips();draw()}).catch(function(){});
chips();draw();applyCfg();
</script>
</body>
</html>
'''


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
