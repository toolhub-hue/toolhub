"""ToolHub - single-file Flask app (website + backend + admin panel).
Env vars: MONGO_URI, GEMINI_API_KEY, ADMIN_PASSWORD
Optional: SECRET_KEY, GEMINI_MODEL, GEMINI_IMAGE_MODEL, MONGO_DB
Install: pip install flask "pymongo[srv]" requests gunicorn
Run: python app.py   |   Production: gunicorn app:app --timeout 120
"""
import os, io, re, json, time, uuid, hmac, base64, hashlib, socket, ipaddress
from urllib.parse import urljoin, urlparse
import requests
from flask import Flask, request, jsonify, Response, session
from werkzeug.exceptions import HTTPException

from urllib3.util import connection as _conn

_orig_cc = _conn.create_connection


def _safe_cc(address, *a, **k):
    """Only allow outgoing connections to public internet addresses (blocks localhost/private networks)."""
    host, port = address[0], address[1]
    last = OSError("Blocked address")
    for _f, _t, _p, _c, sa in socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM):
        if not ipaddress.ip_address(sa[0].split("%")[0]).is_global:
            continue
        try:
            return _orig_cc((sa[0], port), *a, **k)
        except OSError as e:
            last = e
    raise last


_conn.create_connection = _safe_cc

try:
    from dotenv import load_dotenv

    load_dotenv()  # optional: reads a local .env file
except ImportError:
    pass

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


# ---------- Response Sheet Calculator (no AI: regex + PyPDF2 + BeautifulSoup) ----------
Q_RE = re.compile(r"Question\s*ID\s*[:\-]?\s*(\d+)", re.I)
_VAL = r"(--+|-?\d+(?:\.\d+)?(?:\s*,\s*\d+)*|[A-Da-d](?:\s*,\s*[A-Da-d])*(?![A-Za-z])|—|–|-)"
CH_RE = re.compile(r"(?:Chosen|Given|Marked)\s*(?:Option|Answer)\s*[:\-]?\s*" + _VAL, re.I)
CO_RE = re.compile(r"(?:Correct|Right)\s*(?:Option|Answer)\s*[:\-]?\s*" + _VAL, re.I)
NAME_RE = re.compile(r"(?:Candidate|Applicant|Student)\s*Name\s*[:\-]?[ \t]*\n?[ \t]*([A-Za-z][A-Za-z .'\-]{1,50})", re.I)
HDR1 = re.compile(r"^(?:Subject|Section|Part)\s*[:\-]\s*(.*)$", re.I)
HDR0 = re.compile(r"^(?:Subject|Section|Part)\s*[:\-]?$", re.I)
NOISE = set("""QUESTION QUESTIONS STATUS ANSWERED ANSWER ANSWERS OPTION OPTIONS CHOSEN CORRECT WRONG RESPONSE RESPONSES
SHEET CANDIDATE ROLL APPLICATION CENTRE CENTER INSTRUCTION INSTRUCTIONS NATIONAL TESTING AGENCY EXAM EXAMINATION TOTAL
SCORE MARK MARKS KEY PAGE DATE TIME SHIFT SESSION NAME NO NUMBER FORM TEST TYPE ID SECTION SUBJECT PART MCQ NOT ATTEMPTED
UNANSWERED PHOTO SIGNATURE SUMMARY RESULT DETAILS""".split())


def norm_text(t):
    t = t.replace("\xa0", " ").replace("\r", "\n")
    return re.sub(r"[ \t\f\v]+", " ", t)


def fetch(url):
    """Download a public link (HTML or PDF). Redirects are followed manually; private addresses are blocked."""
    sess = requests.Session()
    sess.trust_env = False
    for _ in range(4):
        p = urlparse(url)
        if p.scheme not in ("http", "https") or not p.hostname or p.port not in (None, 80, 443, 8080, 8443):
            raise Err("Please paste a valid http(s) link")
        try:
            r = sess.get(url, stream=True, timeout=(6, 15), allow_redirects=False,
                         headers={"User-Agent": "Mozilla/5.0 (compatible; ToolHub)"})
        except requests.RequestException:
            raise Err("Could not open the link. Make sure it is public (no login needed).", 422)
        if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("Location"):
            url = urljoin(url, r.headers["Location"])
            r.close()
            continue
        if r.status_code != 200:
            raise Err("The link returned error %d" % r.status_code, 422)
        buf = b""
        for ch in r.iter_content(65536):
            buf += ch
            if len(buf) > 8 * 1024 * 1024:
                raise Err("That page is too large (max 8 MB)", 422)
        return buf
    raise Err("Too many redirects", 422)


def pdf_text(data):
    try:
        from PyPDF2 import PdfReader
    except ImportError:
        raise Err("Server is missing PyPDF2 (add it to requirements)", 500)
    try:
        rd = PdfReader(io.BytesIO(data))
        if rd.is_encrypted:
            rd.decrypt("")
        return "\n".join((pg.extract_text() or "") for pg in rd.pages[:300])
    except Exception:
        raise Err("Could not read this PDF", 422)


def html_text(data):
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        raise Err("Server is missing beautifulsoup4 (add it to requirements)", 500)
    soup = BeautifulSoup(data, "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    return soup.get_text("\n")


def clean_name(n):
    n = re.sub(r"\s+", " ", n).strip(" :-–|")
    n = re.sub(r"\s*[-–(]?\s*Section\s*[A-Za-z0-9]+\)?$", "", n, flags=re.I).strip(" :-–|")
    return n[:40]


def find_candidate(text):
    m = NAME_RE.search(text)
    if not m:
        return ""
    n = re.split(r"\s+(?:Roll|Application|Registration|Reg|Test|Date|Exam|Father|Mother|Mobile|Email|Candidate)\b",
                 m.group(1), flags=re.I)[0].strip(" .'-")
    if not n or any(w.upper() in NOISE for w in re.findall(r"[A-Za-z]+", n)):
        return ""
    return n.title() if n.isupper() else n


def find_headers(text, cand):
    """Detect subject headings dynamically: 'Subject: X', 'Section: X', 'Part: X' or standalone heading lines."""
    lines, offs, pos, out = text.split("\n"), [], 0, []
    for ln in lines:
        offs.append(pos)
        pos += len(ln) + 1
    for i, raw in enumerate(lines):
        ln, name = raw.strip(), None
        if not ln or len(ln) > 60:
            continue
        m = HDR1.match(ln)
        if m and m.group(1).strip():
            name = clean_name(m.group(1))
        elif HDR0.match(ln):
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines):
                name = clean_name(lines[j])
        elif re.fullmatch(r"[A-Za-z][A-Za-z &/\-]{1,38}[A-Za-z]", ln):
            words = [w.upper() for w in re.findall(r"[A-Za-z]+", ln)]
            if not any(w in NOISE for w in words):
                if ln.isupper():
                    name = ln
                elif len(words) <= 4 and ln[0].isupper() and re.search(
                        r"Q\.?\s*\d+|Question\s*ID", " ".join(lines[i + 1:i + 6]), re.I):
                    name = ln
        if name and len(re.findall(r"[A-Za-z]", name)) >= 2 and name.lower() != cand.lower() \
                and not any(w.upper() in NOISE for w in re.findall(r"[A-Za-z]+", name)):
            out.append((offs[i], name.title() if name.isupper() else name))
    return out


def is_na(v):
    return v is None or re.fullmatch(r"-+|—|–", v.strip()) is not None


def norm_ans(v):
    out = []
    for t in re.split(r"\s*,\s*", v.strip().upper()):
        if t:
            try:
                out.append(repr(float(t)))
            except ValueError:
                out.append(t)
    return tuple(sorted(out))


def num(v, d):
    try:
        return max(0.0, min(100.0, float(v)))
    except (TypeError, ValueError):
        return d


def parse_sheet(text, pos, neg):
    cand = find_candidate(text)
    qs, hs = list(Q_RE.finditer(text)), find_headers(text, cand)
    subj, seen, ignored = {}, set(), 0
    for i, m in enumerate(qs):
        qid = m.group(1)
        if qid in seen:
            continue
        blk = text[m.end(): qs[i + 1].start() if i + 1 < len(qs) else len(text)]
        ch, co = CH_RE.search(blk), CO_RE.search(blk)
        if not ch and not co:
            ignored += 1
            continue
        seen.add(qid)
        h = [n for o, n in hs if o <= m.start()]
        name = h[-1] if h else "General"
        d = subj.setdefault(name, {"name": name, "total": 0, "correct": 0, "wrong": 0,
                                   "not_attempted": 0, "unverified": 0})
        d["total"] += 1
        c = ch.group(1) if ch else None
        if is_na(c):
            d["not_attempted"] += 1
        elif not co or is_na(co.group(1)):
            d["unverified"] += 1  # answered, but the correct option is not on the sheet
        elif norm_ans(c) == norm_ans(co.group(1)):
            d["correct"] += 1
        else:
            d["wrong"] += 1
    subs = list(subj.values())
    g = {"total": 0, "correct": 0, "wrong": 0, "not_attempted": 0}
    for d in subs:
        d["marks"] = round(d["correct"] * pos - d["wrong"] * neg, 2)
        for k in g:
            g[k] += d[k]
    g["marks"] = round(g["correct"] * pos - g["wrong"] * neg, 2)
    notes = []
    unv = sum(d["unverified"] for d in subs)
    if unv:
        notes.append("%d answered question(s) have no correct option on the sheet, so they are not counted in marks." % unv)
    if subs and list(subj) == ["General"]:
        notes.append("No subject headings were found, so all questions are shown together.")
    return {"candidate": cand, "pos": pos, "neg": neg, "subjects": subs, "grand_total": g, "note": " ".join(notes)}


@app.post("/response-calculator")
def response_calculator():
    limit("calc", 40)
    pos, neg = num(request.form.get("pos"), 4.0), num(request.form.get("neg"), 1.0)
    f, url = request.files.get("file"), s(request.form.get("url"), 600).strip()
    if f and f.filename:
        data = f.read()
        if not data.startswith(b"%PDF-"):
            raise Err("Please upload a PDF file")
        text = pdf_text(data)
    elif url:
        data = fetch(url)
        text = pdf_text(data) if data.startswith(b"%PDF-") else html_text(data)
    else:
        raise Err("Upload the PDF or paste the link first")
    text = norm_text(text)
    if len(text.strip()) < 20:
        raise Err("No readable text found. A scanned (image-only) PDF cannot be read without OCR.", 422)
    res = parse_sheet(text, pos, neg)
    if not res["subjects"]:
        raise Err("No questions found. The sheet must list 'Question ID', 'Chosen Option' and 'Correct Option'.", 422)
    return jsonify(res)


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


# ---------- Pro Animated PPT Maker (python-pptx only: no AI, no paid API) ----------
W, H = 13.333, 7.5
DESIGNS = {
    "neon": dict(name="Neon Tech", tags="Best for Tech, Startup, AI", bg=["#0F172A", "#1E1B4B"], ang=135, tc="#FFFFFF",
                 bc="#CBD5E1", acc="#6366F1", acc2="#22D3EE", card="#1E293B", calpha=70, cline="#6366F1", ct="#E2E8F0",
                 hl=["#6366F1", "#22D3EE"], hlt="#FFFFFF", font="Poppins", tfont="Poppins", style="glass"),
    "minimal": dict(name="Minimal Elite", tags="Best for Pitch, Education, Portfolio", bg=["#FFFFFF", "#FFFFFF"], ang=0,
                    tc="#000000", bc="#262626", acc="#000000", acc2="#6B7280", card="#F4F4F5", calpha=None, cline=None,
                    ct="#111111", hl="#000000", hlt="#FFFFFF", font="Inter", tfont="Inter", style="swiss"),
    "morph": dict(name="Gradient Morph", tags="Best for Creative, Marketing, Social", bg=["#7C3AED", "#EC4899"], ang=45,
                  tc="#FFFFFF", bc="#F5F3FF", acc="#FDE68A", acc2="#FFFFFF", card="#FFFFFF", calpha=18, cline="#FFFFFF",
                  ct="#FFFFFF", hl=["#FDE68A", "#FBCFE8"], hlt="#4C1D95", font="Montserrat", tfont="Montserrat", style="morph"),
    "corp": dict(name="Corporate Pro", tags="Best for Business, Reports, Finance", bg=["#FFFFFF", "#EFF6FF"], ang=270,
                 tc="#1E40AF", bc="#1F2937", acc="#1E40AF", acc2="#3B82F6", card="#FFFFFF", calpha=None, cline="#BFDBFE",
                 ct="#1F2937", hl=["#1E40AF", "#3B82F6"], hlt="#FFFFFF", font="Inter", tfont="Montserrat", style="corp"),
    "pop": dict(name="Creative Pop", tags="Best for Events, Youth, Kids", bg=["#FACC15", "#FACC15"], ang=0, tc="#000000",
                bc="#111111", acc="#000000", acc2="#EF4444", card="#FFFFFF", calpha=None, cline="#000000", ct="#000000",
                hl="#FFFFFF", hlt="#000000", font="Poppins", tfont="Montserrat", style="pop"),
}
TYPES = {"title", "bullets", "two", "stats", "timeline", "chart", "conclusion"}
LIM = {"bullets": 4, "two": 4, "stats": 3, "timeline": 4, "conclusion": 3, "title": 0, "chart": 0}
TOOLS = ["AI Photo Generator", "AI Prompt Generator", "Response Sheet Calculator", "Marks Percentage",
         "Image & PDF Compressor", "Voice to Text"]
GENERIC = [
    ("Key Concepts", ["Core ideas behind {t}", "Important terms to know", "How the pieces fit together"], None),
    ("Why It Matters", ["Growing importance of {t}", "Benefits for people and organisations", "Opportunities it creates"], None),
    ("How It Works", ["Understand the need", "Plan and prepare", "Apply and test", "Review and improve"], "timeline"),
    ("Applications", ["Everyday uses of {t}", "Examples from different fields", "Case studies worth exploring"], None),
    ("Key Benefits", ["Saves time and effort", "Improves quality and accuracy", "Supports better decisions"], "stats"),
    ("Challenges", ["Common obstacles to expect", "Risks to manage", "Skills and resources needed"], None),
    ("Best Practices", ["Start with clear goals", "Learn from proven examples", "Measure results regularly", "Keep improving"], None),
    ("Future Outlook", ["Where {t} is heading", "Emerging trends to watch", "How to stay prepared"], None),
    ("Getting Started", ["Define your objective", "Gather the right resources", "Take the first small step", "Review and adjust"], "timeline"),
    ("Key Features", ["What makes {t} different", "Main components", "Practical advantages"], None),
    ("Examples", ["A simple everyday example", "A real-world scenario", "Lessons learned"], None),
    ("Tips for Success", ["Stay curious and keep learning", "Practise regularly", "Ask for feedback"], None),
    ("Common Questions", ["What is {t}?", "Who can use it?", "How do I begin?"], None),
]
NAMES = ["Overview", "Key Points", "Highlights", "Details", "More Details", "Insights"]


def cut(t, n=150):
    t = re.sub(r"\s+", " ", str(t)).strip()
    return t if len(t) <= n else t[:n].rsplit(" ", 1)[0].rstrip(",;:") + "…"


def parse_matter(desc):
    lines = [re.sub(r"^\s*(?:[-*•▪●]+|\d+[.)])\s*", "", ln).strip() for ln in desc.splitlines()]
    lines = [x for x in lines if x]
    data, rest = [], []
    for ln in lines:
        m = re.match(r"^(.{2,28}?)\s*[:=\-–]\s*(-?\d+(?:\.\d+)?)\s*%?$", ln)
        (data if m else rest).append([m.group(1).strip(), float(m.group(2))] if m else ln)
    if len(data) < 3:
        data, rest = [], lines
    groups, cur = [], None
    for k, ln in enumerate(rest):
        nxt = rest[k + 1] if k + 1 < len(rest) else ""
        words = ln.split()
        if ln.endswith(":") or (2 <= len(words) <= 6 and ln.istitle() and not re.search(r"[.!?,;]$", ln)
                                and nxt and not nxt.istitle()):
            cur = {"title": cut(ln.rstrip(":"), 60), "items": []}
            groups.append(cur)
            continue
        if cur is None:
            cur = {"title": None, "items": []}
            groups.append(cur)
        for sent in (re.split(r"(?<=[.!?])\s+", ln) if len(ln) > 170 else [ln]):
            if sent.strip():
                cur["items"].append(cut(sent))
    return [g for g in groups if g["items"]], data[:8]


def pick(n_items, k):
    ks = {1: ["bullets"], 2: ["bullets", "two"], 3: ["bullets", "two", "stats"]}.get(n_items, ["bullets", "two", "timeline"])
    return ks[k % len(ks)]


def make_outline(topic, desc, n):
    notes, T = [], topic[:40]
    slides = [{"type": "title", "title": topic, "sub": "A presentation on " + topic, "bullets": []}]
    groups, data = parse_matter(desc) if desc.strip() else ([], [])
    mids, k = [], 0
    if groups:
        for g in groups:
            for c in range(0, len(g["items"]), 4):
                chunk = g["items"][c:c + 4]
                title = (g["title"] or NAMES[len(mids) % len(NAMES)]) + (" (cont.)" if c else "")
                mids.append({"type": pick(len(chunk), k), "title": title, "bullets": chunk})
                k += 1
        want = max(1, n - 2 - (1 if data else 0))
        if len(mids) > want:
            notes.append("Your content needs more than %d slides, so the last points were left out. Increase the slide count to include them." % n)
            mids = mids[:want]
        end = [(m["bullets"][0] if len(m["bullets"][0]) <= 70 else m["title"].replace(" (cont.)", "")) for m in mids[:3]]
    else:
        notes.append("No content was given, so a ready-made template outline was used (no AI). Please review and edit it for your topic.")
        mids = [{"type": "bullets", "title": "Introduction", "bullets": [
            "What %s is and why it matters today" % T, "Who benefits and how it is used", "What this presentation covers"]}]
        for j in range(max(0, n - 3)):
            ttl, bl, kind = GENERIC[j % len(GENERIC)]
            mids.append({"type": kind or pick(len(bl), k), "title": ttl + (" (%d)" % (j // len(GENERIC) + 1) if j >= len(GENERIC) else ""),
                         "bullets": [cut(b.replace("{t}", T)) for b in bl]})
            k += 1
        end = ["%s offers real opportunities" % T, "Start small and keep learning", "Thank you - questions are welcome"]
    slides += mids
    if data:
        slides.append({"type": "chart", "title": "Data at a Glance", "data": data, "bullets": []})
    slides.append({"type": "conclusion", "title": "Conclusion" if not groups else "Key Takeaways", "bullets": end})
    return clean_slides(slides), notes


def clean_slides(raw):
    out = []
    for x in lst(raw)[:20]:
        if not isinstance(x, dict) or x.get("type") not in TYPES:
            continue
        t = x["type"]
        sl = {"type": t, "title": s(x.get("title"), 80).strip() or "Untitled", "sub": s(x.get("sub"), 120).strip(), "bullets": []}
        items = [cut(b) for b in lst(x.get("bullets")) if str(b).strip()][:LIM[t]]
        if t == "chart":
            data = []
            for r in lst(x.get("data"))[:8]:
                try:
                    data.append([s(r[0], 24), float(r[1])])
                except (TypeError, ValueError, IndexError):
                    pass
            if len(data) >= 2:
                sl["data"] = data
            else:
                t, items = "bullets", ["Add your points here"]
        elif t != "title":
            if t in ("stats", "timeline") and len(items) < 3:
                t = "bullets"
            if not items:
                t, items = "bullets", ["Add your points here"]
            if t == "stats":
                sl["nums"] = [s(q, 8) for q in lst(x.get("nums"))[:3]]
        sl["type"], sl["bullets"] = t, items
        out.append(sl)
    return out


def S(k, x, y, w, h, **o):
    d = {"k": k, "x": x, "y": y, "w": w, "h": h}
    d.update(o)
    return d


def fit(text, w, h, size, lo=12):
    while size > lo:
        cpl = max(1, int((w - .3) * 72 / (size * .52)))
        if sum(max(1, -(-len(p) // cpl)) for p in text.split("\n")) * size * 1.3 / 72 <= h - .15:
            break
        size -= 1
    return size


def deco(D, i, cover=False):
    st, A, B = D["style"], D["acc"], D["acc2"]
    if st == "glass":
        return [S("oval", 8.8, -2.6, 6.4, 6.4, f=A, al=22), S("oval", -2.2, 4.8, 5, 5, f=B, al=14)]
    if st == "swiss":
        return [S("rect", 0, 0, .3, H, f="#000000"), S("rect", .3, H - .55, W - .3, .02, f="#000000"),
                S("text", W - 2.0, H - .5, 1.6, .4, t="%02d" % (i + 1), sz=14, c="#6B7280", b=True, ha="r", ft=D["tfont"])]
    if st == "morph":
        P = [(-1.6, -1.8, 5.2, 9.6, 4.6), (9.2, -2, 5.6, -1, 5.2), (3.2, 4.2, 5, 10.4, -1.4), (10.6, 3.6, 4.6, 1, -2.2)][i % 4]
        return [S("oval", P[0], P[1], P[2], P[2], f="#FFFFFF", al=14, n="!!blobA"),
                S("oval", P[3], P[4], 3.4, 3.4, f=A, al=28, n="!!blobB")]
    if st == "corp":
        if cover:
            return [S("rect", 0, 0, 7.6, H, f=A), S("rect", 7.6, 0, .08, H, f=B), S("oval", 9.3, 1.1, 3.4, 3.4, f=[A, B], ang=45)]
        return [S("rect", 0, 0, W, 1.35, f=A), S("rect", 0, 1.35, W, .07, f=B), S("rect", 0, H - .42, W, .42, f="#DBEAFE"),
                S("text", .7, H - .41, 7, .38, t=D["site"], sz=11, c=A, b=True, va="m", ft=D["font"]),
                S("text", W - 2.0, H - .41, 1.3, .38, t="%02d" % (i + 1), sz=11, c=A, b=True, ha="r", va="m", ft=D["font"])]
    return [S("oval", W - 2.7, -1.4, 3.8, 3.8, f="#000000"), S("star", .25, H - .92, .7, .7, f="#FFFFFF", l="#000000", lw=2.5),
            S("rect", 0, H - .2, W, .2, f="#000000")]


def head(D, text):
    st, tf = D["style"], D["tfont"]
    if st == "corp":
        return [S("text", .7, .17, W - 1.4, 1.05, t=text, sz=fit(text, W - 1.4, 1.0, 40, 24), c="#FFFFFF", b=True, va="m", ft=tf, a="fade")], 1.95
    x = .95 if st == "swiss" else .7
    o = [S("text", x, .5, W - x - .7, 1.15, t=text, sz=fit(text, W - x - .7, 1.1, 48 if st == "swiss" else 44, 26),
           c=D["tc"], b=True, va="m", ft=tf, a="fade")]
    if st == "pop":
        o.append(S("rect", x, 1.72, 3.0, .16, f="#000000", a="wipe"))
    else:
        o.append(S("rect", x, 1.72, 1.5 if st != "swiss" else 1.2, .09 if st != "swiss" else .1,
                   f=[D["acc"], D["acc2"]] if st == "glass" else D["acc"], a="wipe"))
    return o, 2.15


def badge_colors(D):
    st = D["style"]
    return ({"glass": D["acc"], "morph": D["acc"], "corp": D["acc"]}.get(st, "#000000"),
            {"morph": D["hlt"]}.get(st, "#FFFFFF"))


def cardbox(D, x, y, w, h, text=None, **o):
    st, out = D["style"], []
    if st == "pop":
        out.append(S("round", x + .1, y + .1, w, h, f="#000000", a=o.get("a")))
    kw = dict(f=D["card"], al=D.get("calpha"), l=D["cline"], lw=3 if st == "pop" else 1.25, a=o.get("a"))
    if text is not None:
        kw.update(t=text, sz=o["sz"], c=D["ct"], ft=D["font"], va="m", pl=o.get("pl", .15))
    out.append(S("rect" if st == "swiss" else "round", x, y, w, h, **kw))
    return out


def hlbox(D, x, y, w, h):
    st = D["style"]
    if st == "swiss":
        return [S("rect", x, y, w, h, f="#000000", a="fly")]
    if st == "pop":
        return [S("round", x + .1, y + .1, w, h, f="#000000", a="fly"), S("round", x, y, w, h, f="#FFFFFF", l="#000000", lw=4, a="fly")]
    return [S("round", x, y, w, h, f=D["hl"], ang=45, a="fly")]


def cover(D, sl):
    st, T, A, B, tf = D["style"], sl["title"], D["acc"], D["acc2"], D["tfont"]
    sub = sl.get("sub") or ""
    if st == "swiss":
        return [S("rect", .95, 1.9, 2.2, .14, f="#000000", a="wipe"),
                S("text", .95, 2.2, 11.4, 2.9, t=T, sz=fit(T, 11.4, 2.8, 72, 30), c="#000000", b=True, va="m", ft=tf, a="fade"),
                S("text", .95, 5.3, 11, .9, t=sub, sz=24, c="#4B5563", ft=D["font"], a="fade"),
                S("text", .95, 6.3, 8, .4, t="PRESENTATION  /  " + D["site"].upper(), sz=12, c="#6B7280", b=True, ft=D["font"], a="fade")]
    if st == "corp":
        return [S("text", .7, 2.0, 6.4, 3.0, t=T, sz=fit(T, 6.4, 2.9, 48, 26), c="#FFFFFF", b=True, va="m", ft=tf, a="fade"),
                S("rect", .8, 5.15, 1.5, .09, f=B, a="wipe"),
                S("text", .7, 5.35, 6.4, 1.2, t=sub, sz=20, c="#DBEAFE", ft=D["font"], a="fade"),
                S("text", 9.3, 2.0, 3.4, 1.6, t="◆", sz=72, c="#FFFFFF", ha="c", va="m", a="zoom"),
                S("text", 8.6, 5.0, 4.8, .6, t="PRESENTATION", sz=18, c=A, b=True, ha="c", ft=D["font"], a="fade")]
    if st == "pop":
        return [S("round", 1.12, 2.12, 9.4, 3.0, f="#000000", a="zoom"),
                S("round", 1.0, 2.0, 9.4, 3.0, f="#FFFFFF", l="#000000", lw=4, a="zoom"),
                S("text", 1.3, 2.15, 8.8, 2.7, t=T, sz=fit(T, 8.8, 2.6, 54, 26), c="#000000", b=True, va="m", ft=tf, a="pop"),
                S("text", 1.0, 5.4, 9.4, .9, t=sub, sz=22, c="#000000", b=True, ft=D["font"], a="fade"),
                S("star", 10.5, 3.7, 1.7, 1.7, f=B, l="#000000", lw=3, a="pop")]
    o = [S("round", .9, 1.45, 3.2, .5, f=A, al=28, l=A, lw=1, t="✦  PRESENTATION", sz=13, c=A if st == "glass" else "#FFFFFF", b=True,
           ha="c", va="m", pl=0, ft=D["font"], a="fade"),
         S("text", .9, 2.15, 8.4, 2.5, t=T, sz=fit(T, 8.4, 2.4, 56, 28), c=D["tc"], b=True, va="m", ft=tf, a="fade"),
         S("rect", .95, 4.8, 1.6, .09, f=[A, B] if st == "glass" else A, a="wipe"),
         S("text", .9, 5.05, 8.2, 1.0, t=sub, sz=22, c=D["bc"], ft=D["font"], a="fade")]
    if st == "glass":
        o += [S("oval", 9.5, 1.7, 3.4, 3.4, f=[A, B], ang=45, al=90, a="zoom"), S("text", 9.5, 2.4, 3.4, 2.0, t="◆", sz=84, c="#FFFFFF", ha="c", va="m", a="zoom")]
    return o


def layout(D, sl, i):
    t, st, items = sl["type"], D["style"], sl.get("bullets", [])
    if t == "title":
        return deco(D, i, True) + cover(D, sl)
    sh = deco(D, i)
    hd, top = head(D, sl["title"])
    sh += hd
    bf, bt = badge_colors(D)
    if t == "bullets":
        if st == "swiss":
            for k, b in enumerate(items):
                y = top + k * 1.2
                sh += [S("text", .95, y, 1.3, 1.0, t="%02d" % (k + 1), sz=28, c="#9CA3AF", b=True, va="m", ft=D["tfont"], a="fade"),
                       S("text", 2.4, y, 9.6, 1.0, t=b, sz=fit(b, 9.6, 1.0, 26, 14), c=D["bc"], va="m", ft=D["font"], a="wipe"),
                       S("rect", .95, y + 1.07, 11.4, .015, f="#D1D5DB", a="wipe")]
        else:
            n = len(items)
            if n <= 2:
                cells = [(.7, top + k * 2.15, W - 1.4, 1.9) for k in range(n)]
            else:
                cw = (W - 1.4 - .4) / 2
                cells = [(.7 + (k % 2) * (cw + .4), top + (k // 2) * 2.35, cw, 2.1) for k in range(n)]
            for k, (x, y, w, h) in enumerate(cells):
                sh += cardbox(D, x, y, w, h, items[k], sz=fit(items[k], w - 1.15, h, 22, 13), pl=1.05, a="fly")
                sh.append(S("oval", x + .25, y + h / 2 - .32, .64, .64, f=bf, t=str(k + 1), sz=20, c=bt, b=True, ha="c", va="m", pl=0, ft=D["tfont"], a="zoom"))
    elif t == "two":
        left = items[:-1] if len(items) > 1 else items
        call = items[-1] if len(items) > 1 else ""
        txt = "\n".join("▸ " + b for b in left)
        x0 = .95 if st == "swiss" else .7
        sh.append(S("text", x0, top, 7.1, 4.4, t=txt, sz=fit(txt, 7.1, 4.4, 24, 14), c=D["bc"], ft=D["font"], a="fade"))
        sh += hlbox(D, 8.2, top, 4.45, 4.4)
        sh += [S("text", 8.2, top + .25, 4.45, 1.5, t="✦", sz=64, c=D["hlt"], ha="c", va="m", a="zoom"),
               S("text", 8.5, top + 1.85, 3.85, 2.3, t=call, sz=fit(call, 3.85, 2.3, 24, 14), c=D["hlt"], b=True, ha="c", va="m", ft=D["font"], a="fade")]
    elif t in ("stats", "conclusion"):
        nums = sl.get("nums") or []
        cw, y0, ch = (W - 1.4 - .8) / 3, top + .1, (3.4 if t == "stats" else 2.9)
        for k in range(min(3, len(items))):
            x = .7 + k * (cw + .4)
            num = nums[k] if k < len(nums) and nums[k] else ("✓" if t == "conclusion" else "%02d" % (k + 1))
            sh += cardbox(D, x, y0, cw, ch, a="fly")
            sh += [S("text", x, y0 + .2, cw, 1.4, t=num, sz=fit(num, cw, 1.3, 54, 24), c=D["acc"] if st != "morph" else D["acc"], b=True, ha="c", va="m", ft=D["tfont"], a="zoom"),
                   S("text", x + .2, y0 + 1.65, cw - .4, ch - 1.85, t=items[k], sz=fit(items[k], cw - .4, ch - 1.85, 20, 13), c=D["ct"], ha="c", ft=D["font"], a="fade")]
        if t == "conclusion":
            yb = y0 + ch + .4
            sh += hlbox(D, .7, yb, W - 1.4, 1.0)
            sh.append(S("text", .7, yb, W - 1.4, 1.0, t="Thank you!  Questions are welcome", sz=28, c=D["hlt"], b=True, ha="c", va="m", ft=D["tfont"], a="fade"))
    elif t == "timeline":
        n, y0 = len(items), top + .9
        sh.append(S("rect", 1.1, y0 + .33, W - 2.2, .07, f=D["acc"] if st != "morph" else "#FFFFFF", a="wipe"))
        for k, b in enumerate(items):
            cx = 1.1 + .37 + k * ((W - 2.2 - .74) / max(1, n - 1))
            sh += [S("oval", cx - .37, y0, .74, .74, f=bf, t=str(k + 1), sz=22, c=bt, b=True, ha="c", va="m", pl=0, ft=D["tfont"], a="zoom"),
                   S("text", min(max(cx - 1.35, .35), W - .35 - 2.7), y0 + 1.1, 2.7, 2.6, t=b, sz=fit(b, 2.7, 2.6, 20, 13), c=D["bc"], ha="c", ft=D["font"], a="fade")]
    elif t == "chart":
        data = sl["data"]
        hi, lo = max(data, key=lambda r: r[1]), min(data, key=lambda r: r[1])
        txt = "Highest\n%s: %g\nLowest\n%s: %g" % (hi[0], hi[1], lo[0], lo[1])
        sh.append(S("chart", .7, top, 7.9, 4.6, data=data, cc=D["acc"] if st != "morph" else "#FDE68A", c=D["bc"], ft=D["font"], a="wipe"))
        sh += hlbox(D, 8.9, top, 3.75, 4.6)
        sh.append(S("text", 9.1, top + .2, 3.35, 4.2, t=txt, sz=22, c=D["hlt"], b=True, ha="c", va="m", ft=D["font"], a="fade"))
    return sh


def promo(D, i, url):
    st = D["style"]
    hd, top = head(D, "Made with " + D["site"])
    x0 = .95 if st == "swiss" else .7
    sh = deco(D, i) + hd
    sh.append(S("text", x0, top - .05, 9.4, .9, t="Free tools for students & creators: slides, marks, AI photos and more.",
                sz=22, c=D["bc"], ft=D["font"], a="fade"))
    for k, name in enumerate(TOOLS):
        x, y = x0 + (k % 3) * 4.0, 3.15 + (k // 3) * 1.0
        sh += cardbox(D, x, y, 3.75, .8, "★  " + name, sz=fit(name, 3.4, .75, 17, 12), pl=.15, a="fly")
    dom = re.sub(r"^https?://", "", url)
    sh += hlbox(D, 2.4, 5.25, 8.5, 1.05)
    sh.append(S("text", 2.4, 5.25, 8.5, 1.05, t="🔗  " + dom, sz=fit(dom, 8, 1.0, 30, 18), c=D["hlt"], b=True, ha="c", va="m", ft=D["tfont"], a="zoom", url=url))
    sh.append(S("star", W - 2.4, 4.75, 1.1, 1.1, f=D["acc2"] if st != "morph" else "#FDE68A", l="#000000" if st == "pop" else None, lw=3, a="pop"))
    sh.append(S("text", 2.4, 6.4, 8.5, .4, t="Open the link - it's free, no sign-up needed", sz=15, c=D["bc"], ha="c", ft=D["font"], a="fade"))
    return sh


def build_layouts(did, slides, site, url):
    D = dict(DESIGNS[did], site=site)
    out = []
    for i, sl in enumerate(list(slides) + [{"type": "promo"}]):
        shapes = promo(D, i, url) if sl["type"] == "promo" else layout(D, sl, i)
        k = 0
        for d in shapes:
            if d.get("a"):
                d["dl"] = round(min(k * .2, 3.0), 2)
                k += 1
        out.append({"bg": {"c": D["bg"], "ang": D["ang"]}, "shapes": shapes})
    return out


# ----- animation XML (entrance effects: fade / fly / zoom / wipe / pop) -----
PRESET = {"fade": (10, 0), "fly": (2, 4), "zoom": (53, 16), "wipe": (22, 8), "pop": (53, 16)}
TRANS = ('<mc:AlternateContent xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
         'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
         '<mc:Choice xmlns:p159="http://schemas.microsoft.com/office/powerpoint/2015/09/main" '
         'xmlns:p14="http://schemas.microsoft.com/office/powerpoint/2010/main" Requires="p159">'
         '<p:transition spd="slow" p14:dur="1500"><p159:morph option="byObject"/></p:transition></mc:Choice>'
         '<mc:Fallback><p:transition spd="med"><p:fade/></p:transition></mc:Fallback></mc:AlternateContent>')


def _tav(tm, v):
    val = '<p:fltVal val="0"/>' if v == "0" else '<p:strVal val="%s"/>' % v
    return '<p:tav tm="%d"><p:val>%s</p:val></p:tav>' % (tm, val)


def _anim(i, spid, attr, keys, add=""):
    return ('<p:anim calcmode="lin" valueType="num"><p:cBhvr%s><p:cTn id="%d" dur="500" fill="hold"/>'
            '<p:tgtEl><p:spTgt spid="%d"/></p:tgtEl><p:attrNameLst><p:attrName>%s</p:attrName></p:attrNameLst></p:cBhvr>'
            '<p:tavLst>%s</p:tavLst></p:anim>') % (add, i, spid, attr, "".join(_tav(t, v) for t, v in keys))


def _effect(nid, spid, kind, delay):
    pid, sub = PRESET[kind]
    eid = nid()
    inner = ('<p:set><p:cBhvr><p:cTn id="%d" dur="1" fill="hold"><p:stCondLst><p:cond delay="0"/></p:stCondLst></p:cTn>'
             '<p:tgtEl><p:spTgt spid="%d"/></p:tgtEl><p:attrNameLst><p:attrName>style.visibility</p:attrName></p:attrNameLst>'
             '</p:cBhvr><p:to><p:strVal val="visible"/></p:to></p:set>') % (nid(), spid)

    def fx(f):
        return ('<p:animEffect transition="in" filter="%s"><p:cBhvr><p:cTn id="%d" dur="500"/>'
                '<p:tgtEl><p:spTgt spid="%d"/></p:tgtEl></p:cBhvr></p:animEffect>') % (f, nid(), spid)
    if kind == "fade":
        inner += fx("fade")
    elif kind == "wipe":
        inner += fx("wipe(left)")
    elif kind == "fly":
        inner += _anim(nid(), spid, "ppt_x", [(0, "#ppt_x"), (100000, "#ppt_x")], ' additive="base"')
        inner += _anim(nid(), spid, "ppt_y", [(0, "1+#ppt_h/2"), (100000, "#ppt_y")], ' additive="base"')
    else:
        keys = [(0, "0"), (100000, "#ppt_%s")] if kind == "zoom" else [(0, "0"), (70000, "#ppt_%s*1.15"), (100000, "#ppt_%s")]
        for ax in ("w", "h"):
            inner += _anim(nid(), spid, "ppt_" + ax, [(t, v.replace("%s", ax)) for t, v in keys])
        inner += fx("fade")
    return ('<p:par><p:cTn id="%d" presetID="%d" presetClass="entr" presetSubtype="%d" fill="hold" grpId="0" nodeType="withEffect">'
            '<p:stCondLst><p:cond delay="%d"/></p:stCondLst><p:childTnLst>%s</p:childTnLst></p:cTn></p:par>') % (eid, pid, sub, int(delay * 1000), inner)


def add_animation(anims, shape, kind="fade", delay=0.0, mode="n"):
    """Queue an entrance animation (fade, fly, zoom, wipe, pop) for a shape; timing XML is written per slide."""
    anims.append((shape.shape_id, kind if kind in PRESET else "fade", delay, mode))


def timing_xml(anims):
    c = [4]

    def nid():
        c[0] += 1
        return c[0]
    fx = "".join(_effect(nid, sp, k, d) for sp, k, d, _m in anims)
    bld = "".join('<p:bldGraphic spid="%d" grpId="0"><p:bldAsOne/></p:bldGraphic>' % sp if m == "c" else
                  '<p:bldP spid="%d" grpId="0"%s/>' % (sp, ' animBg="1"' if m == "sh" else "")
                  for sp, _k, _d, m in anims if m != "n")
    return ('<p:timing xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"><p:tnLst><p:par>'
            '<p:cTn id="1" dur="indefinite" restart="never" nodeType="tmRoot"><p:childTnLst>'
            '<p:seq concurrent="1" nextAc="seek"><p:cTn id="2" dur="indefinite" nodeType="mainSeq"><p:childTnLst>'
            '<p:par><p:cTn id="3" fill="hold"><p:stCondLst><p:cond delay="indefinite"/>'
            '<p:cond evt="onBegin" delay="0"><p:tn val="2"/></p:cond></p:stCondLst><p:childTnLst>'
            '<p:par><p:cTn id="4" fill="hold"><p:stCondLst><p:cond delay="0"/></p:stCondLst><p:childTnLst>' + fx +
            '</p:childTnLst></p:cTn></p:par></p:childTnLst></p:cTn></p:par></p:childTnLst></p:cTn>'
            '<p:prevCondLst><p:cond evt="onPrev" delay="0"><p:tgtEl><p:sldTgt/></p:tgtEl></p:cond></p:prevCondLst>'
            '<p:nextCondLst><p:cond evt="onNext" delay="0"><p:tgtEl><p:sldTgt/></p:tgtEl></p:cond></p:nextCondLst>'
            '</p:seq></p:childTnLst></p:cTn></p:par></p:tnLst>' + ("<p:bldLst>%s</p:bldLst>" % bld if bld else "") + '</p:timing>')


def build_pptx(layouts):
    from pptx import Presentation
    from pptx.util import Inches, Pt
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.enum.text import PP_ALIGN, MSO_ANCHOR, MSO_AUTO_SIZE
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE
    from pptx.oxml import parse_xml
    from pptx.oxml.ns import qn
    from lxml import etree
    rgb = lambda h: RGBColor.from_string(h.lstrip("#").upper())
    kinds = {"rect": MSO_SHAPE.RECTANGLE, "round": MSO_SHAPE.ROUNDED_RECTANGLE, "oval": MSO_SHAPE.OVAL, "star": MSO_SHAPE.STAR_5_POINT}
    al = {"l": PP_ALIGN.LEFT, "c": PP_ALIGN.CENTER, "r": PP_ALIGN.RIGHT}
    va = {"t": MSO_ANCHOR.TOP, "m": MSO_ANCHOR.MIDDLE, "b": MSO_ANCHOR.BOTTOM}

    def paint(shp, d):
        f = d.get("f")
        if f is None:
            shp.fill.background()
        elif isinstance(f, list):
            shp.fill.gradient()
            shp.fill.gradient_angle = d.get("ang", 45)
            for stp, col in zip(shp.fill.gradient_stops, f):
                stp.color.rgb = rgb(col)
        else:
            shp.fill.solid()
            shp.fill.fore_color.rgb = rgb(f)
        if d.get("al") is not None and f is not None:
            for e in shp._element.spPr.iter(qn("a:srgbClr")):
                etree.SubElement(e, qn("a:alpha")).set("val", str(int(d["al"] * 1000)))
        if d.get("l"):
            shp.line.color.rgb = rgb(d["l"])
            shp.line.width = Pt(d.get("lw", 1))
        else:
            shp.line.fill.background()

    def put_text(shp, d):
        tf = shp.text_frame
        tf.word_wrap = True
        tf.auto_size = MSO_AUTO_SIZE.NONE
        tf.margin_left, tf.margin_right = Inches(d.get("pl", .12)), Inches(.12)
        tf.margin_top = tf.margin_bottom = Inches(.06)
        tf.vertical_anchor = va[d.get("va", "t")]
        for j, line in enumerate(d["t"].split("\n")):
            p = tf.paragraphs[0] if j == 0 else tf.add_paragraph()
            p.alignment = al[d.get("ha", "l")]
            p.space_after = Pt(8)
            r = p.add_run()
            r.text = line
            r.font.size, r.font.bold = Pt(d.get("sz", 18)), bool(d.get("b"))
            r.font.name = d.get("ft") or "Calibri"
            r.font.color.rgb = rgb(d.get("c", "#000000"))

    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(W), Inches(H)
    for lay in layouts:
        sl = prs.slides.add_slide(prs.slide_layouts[6])
        c, fl = lay["bg"]["c"], sl.background.fill
        if c[0] == c[1]:
            fl.solid()
            fl.fore_color.rgb = rgb(c[0])
        else:
            fl.gradient()
            fl.gradient_angle = lay["bg"]["ang"]
            for stp, col in zip(fl.gradient_stops, c):
                stp.color.rgb = rgb(col)
        anims = []
        for d in lay["shapes"]:
            k, mode = d["k"], "n"
            x, y, w, h = (Inches(d[q]) for q in ("x", "y", "w", "h"))
            if k == "chart":
                cd = CategoryChartData()
                cd.categories = [r[0] for r in d["data"]]
                cd.add_series("Data", [r[1] for r in d["data"]])
                shp = sl.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, x, y, w, h, cd)
                ch = shp.chart
                ch.has_legend = ch.has_title = False
                ch.font.size, ch.font.name = Pt(14), d.get("ft") or "Calibri"
                ch.font.color.rgb = rgb(d["c"])
                pl = ch.plots[0]
                pl.gap_width, pl.has_data_labels = 60, True
                pl.data_labels.font.size, pl.data_labels.font.bold = Pt(14), True
                pl.data_labels.font.color.rgb = rgb(d["c"])
                pl.series[0].format.fill.solid()
                pl.series[0].format.fill.fore_color.rgb = rgb(d["cc"])
                ch.value_axis.has_major_gridlines = False
                ch.value_axis.visible = False
                mode = "c"
            elif k == "text":
                shp, mode = sl.shapes.add_textbox(x, y, w, h), "tb"
            else:
                shp = sl.shapes.add_shape(kinds[k], x, y, w, h)
                shp.shadow.inherit = False
                if k == "round":
                    shp.adjustments[0] = .12
                paint(shp, d)
                mode = "sh" if d.get("t") else "n"
            if d.get("t") and k != "chart":
                put_text(shp, d)
            if d.get("url"):
                shp.click_action.hyperlink.address = d["url"]
            if d.get("n"):
                shp.name = d["n"]
            if d.get("a"):
                add_animation(anims, shp, d["a"], d.get("dl", 0), mode)
        sld = sl._element
        sld.append(parse_xml(TRANS))
        if anims:
            sld.append(parse_xml(timing_xml(anims)))
    bio = io.BytesIO()
    prs.save(bio)
    return bio.getvalue()


def site_info():
    name = DEFAULT["name"]
    try:
        db = get_db()
        doc = db.config.find_one({"_id": "site"}) if db is not None else None
        name = (doc or {}).get("name") or name
    except Exception:
        pass
    proto = (request.headers.get("X-Forwarded-Proto") or request.scheme).split(",")[0].strip()
    return name, "%s://%s" % (proto, request.host)


@app.post("/api/ppt/outline")
def ppt_outline():
    limit("pptA", 60)
    j = request.get_json(silent=True) or {}
    topic = s(j.get("topic"), 80).strip()
    if not topic:
        raise Err("Enter a topic first")
    n = max(5, min(15, int(num(j.get("n"), 8))))
    slides, notes = make_outline(topic, s(j.get("desc"), 6000), n)
    site, url = site_info()
    return jsonify(slides=slides, note=" ".join(notes), designs=[
        {"id": k, "name": v["name"], "tags": v["tags"], "thumb": build_layouts(k, slides[:2], site, url)[:2]}
        for k, v in DESIGNS.items()])


def _deck(j):
    did = j.get("design")
    if did not in DESIGNS:
        raise Err("Unknown design")
    slides = clean_slides(j.get("slides"))
    if not slides:
        raise Err("There are no slides to build")
    site, url = site_info()
    return did, build_layouts(did, slides, site, url)


@app.post("/api/ppt/preview")
def ppt_preview():
    limit("pptB", 600)
    return jsonify(layouts=_deck(request.get_json(silent=True) or {})[1])


@app.post("/api/ppt/build")
def ppt_build():
    limit("pptC", 30)
    j = request.get_json(silent=True) or {}
    did, lays = _deck(j)
    fn = re.sub(r"[^A-Za-z0-9]+", "_", s(j.get("topic"), 60)).strip("_")[:40] or "Presentation"
    return Response(build_pptx(lays), mimetype="application/vnd.openxmlformats-officedocument.presentationml.presentation",
                    headers={"Content-Disposition": 'attachment; filename="%s_%s_animated.pptx"' % (fn, DESIGNS[did]["name"].replace(" ", ""))})


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
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;700&family=Poppins:wght@400;700&family=Montserrat:wght@400;700&display=swap" rel="stylesheet">
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
.gt{margin-top:16px;border-radius:22px;padding:20px;color:#fff;text-align:center;background:linear-gradient(135deg,var(--ac),var(--ac2));box-shadow:var(--sh);animation:pp .5s}.gt .m{font-size:54px;font-weight:800;line-height:1.1;margin:4px 0}.gt .st{display:flex;justify-content:space-around;margin-top:14px;font-size:13px}.gt .st b{display:block;font-size:24px}
.sg{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;margin-top:12px}.sk{background:var(--card);border:1px solid var(--bd);border-radius:18px;padding:14px;animation:up .5s both}.sk h4{margin:0 0 4px;font-size:15px}.sk .mk{font-size:30px;font-weight:800;color:var(--ac);margin-bottom:6px}.sk small{display:block;color:var(--mu);font-size:12px;margin-top:6px}
.card.wide{grid-column:1/-1;display:flex;align-items:center;gap:18px;padding:22px 24px;color:#fff;border:0;overflow:hidden;position:relative;background:linear-gradient(120deg,#6366f1,#ec4899,#f59e0b,#6366f1);background-size:300% 300%;animation:up .5s both,bgm 8s ease infinite}
.card.wide h3{font-size:22px;color:#fff;margin:0 0 4px}.card.wide p{color:rgba(255,255,255,.92);font-size:14px;margin:0}.card.wide .ic{width:68px;height:68px;font-size:34px;background:rgba(255,255,255,.22);flex:none;margin:0}
.card.wide .bt{margin-left:auto;background:#fff;color:#6366f1;font-weight:800;padding:12px 22px;border-radius:99px;flex:none;box-shadow:0 6px 18px rgba(0,0,0,.2)}.card.wide .fl{position:absolute;right:190px;top:14px;font-size:30px;opacity:.4;animation:fy 4s ease-in-out infinite}
.card.wide::before{content:'🔥 NEW';position:absolute;top:0;left:0;background:#ef4444;color:#fff;font-size:11px;font-weight:800;padding:4px 12px;border-radius:0 0 14px 0}
@keyframes bgm{50%{background-position:100% 50%}}@keyframes fy{50%{transform:translateY(-12px) rotate(10deg)}}
@media(max-width:600px){.card.wide{flex-wrap:wrap}.card.wide .bt{width:100%;text-align:center;margin:0}.card.wide .fl{display:none}}
.dg{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:12px;margin-top:12px}.dc{border:1px solid var(--bd);border-radius:18px;padding:10px;background:var(--card);box-shadow:var(--sh);transition:transform .2s}.dc:hover{transform:translateY(-4px)}.dc .tr2{display:flex;gap:6px}.dc canvas{flex:1;min-width:0;width:50%;border-radius:8px}.dc h4{margin:10px 0 2px}.dc small{color:var(--mu)}
.pv{width:100%;border-radius:14px;box-shadow:var(--sh);display:block}.th{display:flex;gap:8px;overflow-x:auto;margin-top:10px;padding-bottom:6px}.th canvas{width:110px;flex:none;border-radius:6px;cursor:pointer;border:2px solid transparent}.th canvas.on{border-color:var(--ac)}
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
{id:'ppt',n:'Pro Animated PPT Maker',d:'Animated PowerPoint with 5 pro designs - free, no AI',i:'📊',c:'Creator',g:'#c4b5fd,#6366f1',u:ppt},
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
{id:'rsa',n:'Response Sheet Calculator',d:'Auto marks by subject - no AI',i:'📈',c:'Education',g:'#bae6fd,#0ea5e9',u:rsa}
];
var cur='All';
var $=function(i){return document.getElementById(i)};
function draw(){
var q=$('q').value.toLowerCase(),L=tiles().filter(function(t){return(cur=='All'||t.c==cur)&&(t.n+t.d).toLowerCase().indexOf(q)>-1});
$('grid').innerHTML=L.map(function(t,i){var g=t.g.split(',');if(t.id=='ppt')return'<div class="card wide" style="animation-delay:'+i*60+'ms" onclick="openT(\'ppt\')"><span class="fl">📊</span><span class="fl" style="right:120px;animation-delay:1s">✨</span><div class="ic">'+esc(t.i)+'</div><div><h3>'+esc(t.n)+'</h3><p>'+esc(t.d)+'</p><span class="tag" style="background:rgba(255,255,255,.24);color:#fff">'+esc(t.c)+' · 5 designs · Animated · Free</span></div><div class="bt">Create PPT →</div></div>';return'<div class="card'+(/^(pg|img|rsa)$/.test(t.id)?' hot':'')+'" style="animation-delay:'+i*60+'ms" onclick="openT(\''+t.id+'\')"><div class="ic" style="background:linear-gradient(135deg,'+g[0]+','+g[1]+')">'+esc(t.i)+'</div><h3>'+esc(t.n)+'</h3><p>'+esc(t.d)+'</p><span class="tag">'+esc(t.c)+'</span></div>'}).join('');
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
function closeT(){$('sh').style.maxWidth='';if(window.rec)try{rec.stop()}catch(e){}$('ov').classList.remove('on')}
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
function rbar(q,h){var t=q.total||1;return'<div style="display:flex;height:'+h+'px;border-radius:9px;overflow:hidden;background:#e5e9f2"><div style="width:'+q.correct/t*100+'%;background:#22c55e"></div><div style="width:'+q.wrong/t*100+'%;background:#ef4444"></div><div style="width:'+q.not_attempted/t*100+'%;background:#94a3b8"></div></div>'}
function rsa(b){var F=null;window.rsr=null;
b.innerHTML='<div id="dz" style="border:2px dashed var(--ac);border-radius:18px;padding:22px;text-align:center;cursor:pointer;background:rgba(37,99,235,.06)"><div style="font-size:34px">📄</div><div id="dzt"><b>Drag &amp; drop your response sheet PDF</b><br><small style="color:var(--mu)">or tap to choose a file</small></div></div><input type="file" id="rf" accept="application/pdf" style="display:none"><div style="text-align:center;color:var(--mu);margin:10px 0;font-size:13px">— OR —</div><input id="ru" type="url" placeholder="🔗 Paste response sheet link (https://...)"><div class="r2" style="grid-template-columns:1fr 1fr"><div><label>Marks per correct (+)</label><input id="pos" type="number" value="4" step="any"></div><div><label>Negative per wrong (−)</label><input id="neg" type="number" value="1" step="any"></div></div><label>Your name (shown in the PDF)</label><input id="rn" placeholder="Auto-filled from the sheet if found"><button class="go" id="g5">🧮 Calculate my marks</button><div class="res" id="r9"></div><div id="ao"></div><button class="go" id="d5" style="display:none;background:#64748b">⬇ Download PDF report</button><p style="font-size:12px;color:var(--mu)">Works with sheets that list Question ID, Chosen Option and Correct Option. Links must be public. Scanned (image-only) PDFs cannot be read.</p>';
var dz=$('dz'),pk=function(f){if(f&&/\.pdf$/i.test(f.name)){F=f;$('dzt').innerHTML='✅ <b>'+esc(f.name)+'</b>'}else show($('r9'),'Please choose a PDF file')};
dz.onclick=function(){$('rf').click()};$('rf').onchange=function(){pk(this.files[0])};
dz.ondragover=function(e){e.preventDefault();dz.style.background='rgba(37,99,235,.2)'};dz.ondragleave=function(){dz.style.background=''};
dz.ondrop=function(e){e.preventDefault();dz.style.background='';pk(e.dataTransfer.files[0])};
function rr(an){var R=window.rsr;if(!R)return;var p=+$('pos').value||0,n=+$('neg').value||0,G=R.grand_total,rd=function(v){return Math.round(v*100)/100};R.pos=p;R.neg=n;R.subjects.forEach(function(q){q.marks=rd(q.correct*p-q.wrong*n)});G.marks=rd(G.correct*p-G.wrong*n);
$('ao').innerHTML='<div class="gt"><div style="font-size:13px;letter-spacing:1px">🏆 YOUR TOTAL MARKS</div><div class="m"><span id="gm">0</span><span style="font-size:20px;opacity:.85"> / '+rd(G.total*p)+'</span></div><div style="font-size:12px;opacity:.9">Marking: +'+p+' / −'+n+'</div><div class="st"><div><b id="gc">0</b>✅ Correct</div><div><b id="gw">0</b>❌ Wrong</div><div><b id="gn">0</b>⏭ Skipped</div></div></div><div class="sg">'+R.subjects.map(function(q,i){return'<div class="sk" style="animation-delay:'+(an?i*80:0)+'ms"><h4>'+esc(q.name)+'</h4><div class="mk">'+q.marks+'</div>'+rbar(q,10)+'<small>'+q.total+' questions</small><small>✅ '+q.correct+' · ❌ '+q.wrong+' · ⏭ '+q.not_attempted+'</small></div>'}).join('')+'</div>'+(R.note?'<small style="display:block;margin-top:10px;color:var(--mu)">'+esc(R.note)+'</small>':'');
[['gm',G.marks],['gc',G.correct],['gw',G.wrong],['gn',G.not_attempted]].forEach(function(x){if(an)cnt($(x[0]),x[1]);else $(x[0]).textContent=x[1]})}
$('pos').oninput=$('neg').oninput=function(){rr(0)};
$('d5').onclick=function(){rapdf(window.rsr)};
$('g5').onclick=function(){var u=$('ru').value.trim();if(!F&&!u)return show($('r9'),'Upload the PDF or paste the link first');var fd=new FormData(),g=this;if(F)fd.append('file',F);else fd.append('url',u);fd.append('pos',$('pos').value);fd.append('neg',$('neg').value);g.disabled=true;g.textContent='Calculating...';
api('/response-calculator',{method:'POST',body:fd}).then(function(R){$('r9').style.display='none';if(!$('rn').value&&R.candidate)$('rn').value=R.candidate;window.rsr=R;rr(1);$('d5').style.display='block'}).catch(function(e){show($('r9'),'Error: '+e)}).then(function(){g.disabled=false;g.textContent='🧮 Calculate my marks'})}}
function rapdf(R){var c1=CF.c1,c2=CF.c2,G=R.grand_total,nm=($('rn').value||'').trim(),mx=G.total*R.pos,p=mx>0?Math.max(0,G.marks)/mx*100:0,msg=p>=75?'Excellent performance! 🌟':p>=60?'Good job! 👍':p>=40?'Keep improving! 💪':'Work harder - you can do it! 🔥',
cd=function(t,v,c){return'<div style="flex:1;background:'+c+';color:#fff;border-radius:18px;padding:14px;text-align:center"><div style="font-size:13px;letter-spacing:.5px">'+t+'</div><div style="font-size:34px;font-weight:800;line-height:1.3">'+v+'</div></div>'},
sc=R.subjects.map(function(q){var a=q.correct+q.wrong,ac=a?q.correct/a*100:0,cl=ac>=75?'#22c55e':ac>=50?'#f59e0b':'#ef4444';return'<div style="width:48.5%;box-sizing:border-box;background:#fff;border:1px solid #e5e9f2;border-top:7px solid '+cl+';border-radius:18px;padding:16px;margin-bottom:14px"><div style="font-size:19px;font-weight:800">'+esc(q.name)+'</div><div style="font-size:38px;font-weight:800;color:'+c1+';line-height:1.25">'+q.marks+'<span style="font-size:15px;color:#64748b;font-weight:600"> / '+Math.round(q.total*R.pos*100)/100+'</span></div>'+rbar(q,10)+'<div style="display:flex;justify-content:space-between;font-size:15px;margin-top:9px"><span style="color:#16a34a;font-weight:700">✅ '+q.correct+'</span><span style="color:#dc2626;font-weight:700">❌ '+q.wrong+'</span><span style="color:#64748b;font-weight:700">⏭ '+q.not_attempted+'</span></div><div style="font-size:13px;color:#64748b;margin-top:4px">'+q.total+' questions · '+ac.toFixed(0)+'% accuracy</div></div>'}).join('');
pout('<div style="padding:30px 40px"><div style="display:flex;align-items:center;justify-content:space-between;gap:20px"><div><div style="font-size:27px;font-weight:800;color:'+c1+'">🏆 Response Sheet Result</div>'+(nm?'<div style="font-size:26px;font-weight:800;margin-top:6px">🎓 '+esc(nm)+'</div>':'')+'<div style="display:inline-block;margin-top:10px;background:#eef2ff;color:'+c1+';padding:5px 14px;border-radius:99px;font-size:15px;font-weight:700">Marking: +'+R.pos+' / −'+R.neg+'</div><div style="margin-top:10px;font-size:16px;color:#475569">'+G.total+' questions · '+p.toFixed(1)+'% score</div></div><div style="flex:none;width:172px;height:172px;border-radius:50%;background:linear-gradient(135deg,'+c1+','+c2+');color:#fff;display:flex;flex-direction:column;align-items:center;justify-content:center;box-shadow:0 8px 24px rgba(0,0,0,.18)"><div style="font-size:40px;font-weight:800;line-height:1">'+G.marks+'</div><div style="font-size:14px;margin-top:2px">out of '+Math.round(mx*100)/100+'</div><div style="font-size:11px;letter-spacing:1px;margin-top:4px">TOTAL MARKS</div></div></div><div style="display:flex;gap:14px;margin-top:22px">'+cd('✅ CORRECT',G.correct,'#22c55e')+cd('❌ WRONG',G.wrong,'#ef4444')+cd('⏭ SKIPPED',G.not_attempted,'#64748b')+'</div><div style="font-size:20px;font-weight:800;color:'+c1+';margin:26px 0 12px">📚 Subject-wise Performance</div><div style="display:flex;flex-wrap:wrap;justify-content:space-between">'+sc+'</div><div style="margin-top:14px;text-align:center;font-size:23px;font-weight:800;color:'+c2+'">'+msg+'</div>'+cta()+'</div>','response-result.pdf')}
function gf(x,c,ang,X,Y,w,h){if(!Array.isArray(c))return c;var a=(ang||45)*Math.PI/180,dx=Math.cos(a),dy=-Math.sin(a),L=Math.abs(w*dx)+Math.abs(h*dy),cx=X+w/2,cy=Y+h/2,g=x.createLinearGradient(cx-dx*L/2,cy-dy*L/2,cx+dx*L/2,cy+dy*L/2);g.addColorStop(0,c[0]);g.addColorStop(1,c[1]);return g}
function rr2(x,X,Y,w,h,r){x.beginPath();x.moveTo(X+r,Y);x.arcTo(X+w,Y,X+w,Y+h,r);x.arcTo(X+w,Y+h,X,Y+h,r);x.arcTo(X,Y+h,X,Y,r);x.arcTo(X,Y,X+w,Y,r);x.closePath()}
function pa(x,d,X,Y,w,h){x.beginPath();if(d.k=='oval')x.ellipse(X+w/2,Y+h/2,w/2,h/2,0,0,7);else if(d.k=='round')rr2(x,X,Y,w,h,Math.min(w,h)*.12);else if(d.k=='star'){var cx=X+w/2,cy=Y+h/2,R=w/2;for(var i=0;i<10;i++){var a=-Math.PI/2+i*Math.PI/5,q=i%2?R*.4:R;x[i?'lineTo':'moveTo'](cx+q*Math.cos(a),cy+q*Math.sin(a))}x.closePath()}else x.rect(X,Y,w,h)}
function tx(x,d,X,Y,w,h,k,W){var px=(d.sz||18)*W/960,lh=px*1.2,pl=(d.pl==null?.12:d.pl)*k,pr=.12*k,mw=w-pl-pr;x.font=(d.b?'700 ':'400 ')+px+'px "'+(d.ft||'Poppins')+'",sans-serif';x.fillStyle=d.c||'#000';x.textBaseline='top';var L=[];
String(d.t).split('\n').forEach(function(p,j){var ln='';p.split(' ').forEach(function(wr){var tt=ln?ln+' '+wr:wr;if(x.measureText(tt).width>mw&&ln){L.push([ln,j]);ln=wr}else ln=tt});L.push([ln,j])});
var gap=8*W/960,np=L[L.length-1][1]+1,tot=L.length*lh+(np-1)*gap,yy=d.va=='m'?Y+(h-tot)/2:d.va=='b'?Y+h-tot-.06*k:Y+.06*k,pj=-1;
x.textAlign=d.ha=='c'?'center':d.ha=='r'?'right':'left';L.forEach(function(q){if(pj>=0&&q[1]!=pj)yy+=gap;pj=q[1];x.fillText(q[0],d.ha=='c'?X+pl+mw/2:d.ha=='r'?X+w-pr:X+pl,yy);yy+=lh})}
function ch2(x,d,X,Y,w,h,k){var D=d.data,mx=Math.max.apply(null,D.map(function(r){return r[1]}))||1,bw=w/D.length,f=14*k/72;x.font='700 '+f+'px "'+(d.ft||'Poppins')+'",sans-serif';x.textAlign='center';x.textBaseline='alphabetic';
D.forEach(function(r,i){var bh=(h-f*3.4)*Math.max(0,r[1])/mx,bx=X+i*bw+bw*.16,by=Y+h-f*1.9-bh;x.fillStyle=d.cc;x.fillRect(bx,by,bw*.68,bh);x.fillStyle=d.c;x.fillText(r[1],X+i*bw+bw/2,by-f*.4);x.fillText(String(r[0]).slice(0,12),X+i*bw+bw/2,Y+h-f*.5)})}
function rd(cv,L,t){var W=cv.width,Hh=cv.height,k=W/13.333,x=cv.getContext('2d');x.globalAlpha=1;x.fillStyle=gf(x,L.bg.c,L.bg.ang,0,0,W,Hh);x.fillRect(0,0,W,Hh);
L.shapes.forEach(function(d){var X=d.x*k,Y=d.y*k,w=d.w*k,h=d.h*k,e=1;if(t!=null&&d.a){e=Math.max(0,Math.min(1,(t-d.dl)/.5));if(e<=0)return}
x.save();x.globalAlpha=e;if(d.a=='fly')x.translate(0,(1-e)*Hh*.35);
if(d.a=='zoom'||d.a=='pop'){var z=Math.max(.01,d.a=='pop'&&e<1?e*1.15:e);x.translate(X+w/2,Y+h/2);x.scale(z,z);x.translate(-X-w/2,-Y-h/2)}
if(d.a=='wipe'){x.beginPath();x.rect(X,Y,w*e,h+4);x.clip()}
if(d.k=='chart')ch2(x,d,X,Y,w,h,k);else{if(d.k!='text'){pa(x,d,X,Y,w,h);if(d.f!=null){x.globalAlpha=e*(d.al==null?100:d.al)/100;x.fillStyle=gf(x,d.f,d.ang,X,Y,w,h);x.fill();x.globalAlpha=e}if(d.l){x.lineWidth=(d.lw||1)*W/960;x.strokeStyle=d.l;x.stroke()}}
if(d.t!=null&&d.t!=='')tx(x,d,X,Y,w,h,k,W)}x.restore()})}
function ppt(b){$('sh').style.maxWidth='920px';var P={i:0};
['Inter','Poppins','Montserrat'].forEach(function(f){if(document.fonts)document.fonts.load('700 20px "'+f+'"').then(function(){if(P.fr)P.fr()})});
b.innerHTML='<p style="font-size:13px;color:var(--mu)">Make an animated PowerPoint in 3 steps. Built with python-pptx - no AI. It uses your own text, or a clean template outline if you leave the content empty.</p><label>Topic title</label><input id="pt" placeholder="e.g. AI in Healthcare"><label>Your content (optional)</label><textarea id="pd" rows="5" placeholder="Paste your points here. Tip: end a line with : to start a new slide, e.g. Benefits:"></textarea><label>Number of slides: <b id="pnv">8</b></label><input type="range" id="pn" min="5" max="15" value="8"><p style="font-size:12px;color:var(--mu);margin:4px 0">One free promo slide for this website is added at the end.</p><button class="go" id="pg1">✨ Generate Designs</button><div class="res" id="pr"></div><div id="pst"></div>';
$('pn').oninput=function(){$('pnv').textContent=this.value};
$('pg1').onclick=function(){var g=this,tp=$('pt').value.trim();if(!tp)return show($('pr'),'Enter a topic first');g.disabled=true;g.textContent='Designing...';
api('/api/ppt/outline',{method:'POST',headers:J,body:JSON.stringify({topic:tp,desc:$('pd').value,n:+$('pn').value})}).then(function(j){$('pr').style.display='none';P.topic=tp;P.slides=j.slides;P.designs=j.designs;P.note=j.note;pds()}).catch(function(e){show($('pr'),'Error: '+e)}).then(function(){g.disabled=false;g.textContent='✨ Generate Designs'})};
function pds(){$('pst').innerHTML='<h3 style="margin:18px 0 0">Choose your design</h3><div class="dg">'+P.designs.map(function(d,i){return'<div class="dc"><div class="tr2"><canvas width="320" height="180" data-i="'+i+'" data-s="0"></canvas><canvas width="320" height="180" data-i="'+i+'" data-s="1"></canvas></div><h4>'+esc(d.name)+'</h4><small>'+esc(d.tags)+'</small><button class="go" style="margin-top:10px;padding:10px" data-pick="'+i+'">Select this design</button></div>'}).join('')+'</div>'+(P.note?'<small style="display:block;margin-top:10px;color:var(--mu)">'+esc(P.note)+'</small>':'');
P.fr=function(){[].forEach.call($('pst').querySelectorAll('canvas'),function(c){rd(c,P.designs[+c.dataset.i].thumb[+c.dataset.s],null)})};P.fr();
[].forEach.call($('pst').querySelectorAll('[data-pick]'),function(bn){bn.onclick=function(){P.d=P.designs[+bn.dataset.pick];pedit()}})}
function pcol(){return P.slides.map(function(s,i){var r=$('pe').querySelector('[data-i="'+i+'"]'),ln=r.querySelector('textarea').value.split('\n').map(function(z){return z.trim()}).filter(Boolean),o=Object.assign({},s,{title:r.querySelector('input').value});if(s.type=='chart')o.data=ln.map(function(z){var m=z.match(/^(.*?)[:=\-–]\s*(-?\d+(\.\d+)?)\s*%?$/);return m?[m[1].trim(),+m[2]]:null}).filter(Boolean);else o.bullets=ln;return o})}
function pedit(){$('pst').innerHTML='<div style="display:flex;justify-content:space-between;align-items:center;margin-top:18px"><h3 style="margin:0">'+esc(P.d.name)+'</h3><button class="chip" id="pback">← Change design</button></div><canvas id="pc" width="960" height="540" class="pv"></canvas><div style="display:flex;gap:8px;margin-top:10px;align-items:center"><button class="chip" id="pp">◀</button><span id="pi" style="flex:1;text-align:center;font-size:13px;color:var(--mu)"></span><button class="chip" id="pnx">▶</button><button class="chip on" id="ppl">▶ Play animation</button></div><div class="th" id="pth"></div><details style="margin-top:12px" open><summary style="cursor:pointer;font-weight:700">✏️ Edit content</summary><div id="pe"></div></details><button class="go" id="pdl">⬇ Download PPTX</button><div class="res" id="pr2"></div><p style="font-size:12px;color:var(--mu)">The preview is a close approximation. Animations and the Morph transition play in PowerPoint slideshow mode; other apps may simplify them.</p>';
$('pe').innerHTML=P.slides.map(function(s,i){var v=s.type=='chart'?s.data.map(function(r){return r[0]+': '+r[1]}).join('\n'):s.bullets.join('\n');return'<div class="ar" data-i="'+i+'"><b>Slide '+(i+1)+' · '+s.type+'</b><input value="'+esc(s.title)+'"'+(s.type=='title'?'':'')+'>'+(s.type=='title'?'':'<textarea rows="3">'+esc(v)+'</textarea>')+'</div>'}).join('')+'<small style="color:var(--mu)">The last slide (website promo) is added automatically.</small>';
[].forEach.call($('pe').querySelectorAll('.ar'),function(r){if(!r.querySelector('textarea')){var t=document.createElement('textarea');t.style.display='none';r.appendChild(t)}});
function main(){var L=P.L[P.i];rd($('pc'),L,null);$('pi').textContent='Slide '+(P.i+1)+' of '+P.L.length+(P.i==P.L.length-1?' (website promo)':'');[].forEach.call($('pth').children,function(c,j){c.className=j==P.i?'on':''})}
function thumbs(){$('pth').innerHTML='';P.L.forEach(function(L,j){var c=document.createElement('canvas');c.width=220;c.height=124;rd(c,L,null);c.onclick=function(){P.i=j;main()};$('pth').appendChild(c)});main()}
P.fr=function(){if(P.L)thumbs()};
function ref(){api('/api/ppt/preview',{method:'POST',headers:J,body:JSON.stringify({design:P.d.id,slides:pcol()})}).then(function(j){P.L=j.layouts;if(P.i>=P.L.length)P.i=P.L.length-1;thumbs()}).catch(function(e){show($('pr2'),'Error: '+e)})}
var tm;$('pe').oninput=function(){clearTimeout(tm);tm=setTimeout(ref,500)};
$('pback').onclick=function(){P.fr=null;pds()};$('pp').onclick=function(){if(P.L&&P.i>0){P.i--;main()}};$('pnx').onclick=function(){if(P.L&&P.i<P.L.length-1){P.i++;main()}};
$('ppl').onclick=function(){if(!P.L)return;var L=P.L[P.i],t0=performance.now(),mx=Math.max.apply(null,L.shapes.map(function(d){return d.a?d.dl+.6:0}));P.pl=L;(function f(n){var t=(n-t0)/1000;rd($('pc'),L,t);if(t<mx&&P.pl==L)requestAnimationFrame(f);else rd($('pc'),L,null)})(t0)};
$('pdl').onclick=function(){var g=this;g.disabled=true;g.textContent='Building PPTX...';fetch('/api/ppt/build',{method:'POST',headers:J,body:JSON.stringify({topic:P.topic,design:P.d.id,slides:pcol()})}).then(function(r){if(!r.ok)return r.json().then(function(j){throw j.error||'Error'});return r.blob()}).then(function(bl){dl(bl,(P.topic.replace(/[^\w\- ]+/g,'').trim().replace(/\s+/g,'_')||'Presentation')+'_'+P.d.name.replace(/\s+/g,'')+'_animated.pptx')}).catch(function(e){show($('pr2'),'Error: '+e)}).then(function(){g.disabled=false;g.textContent='⬇ Download PPTX'})};
ref()}}
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
