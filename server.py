import base64
import binascii
import io
import os
import re
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta
from functools import wraps
from itertools import combinations
from pathlib import Path

from bson import Binary, ObjectId

from flask import Flask, Response, jsonify, request, send_from_directory, session
from werkzeug.exceptions import HTTPException

# python-docx reads Word (.docx) files; pypdf reads PDF files.
try:
    from docx import Document
except ImportError:  
    Document = None
try:
    from pypdf import PdfReader
except ImportError:  
    PdfReader = None

BASE_DIR = Path(__file__).resolve().parent
MAX_FILE_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_QUESTIONS = 50  # Maximum questions allowed in a working list or saved quiz
MAX_GRAPH_TOPICS = 40  # Keep the concept graph small and fast to traverse
MAX_GRAPH_NEIGHBORS = 8  # Strongest related topics kept per node
MAX_AVATAR_BYTES = 5 * 1024 * 1024  # 5 MB


def load_local_env():
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key in {"MONGO_URI", "MONGO_DB", "MONGO_TIMEOUT_MS"}:
            os.environ.setdefault(key, value.strip().strip('"').strip("'"))


load_local_env()

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017/")
MONGO_DB_NAME = os.environ.get("MONGO_DB", "studyious")
MONGO_TIMEOUT_MS = int(os.environ.get("MONGO_TIMEOUT_MS", "20000"))

try:
    from bson import ObjectId
    from pymongo import ASCENDING, MongoClient
    from pymongo.errors import DuplicateKeyError
except ImportError:  
    ObjectId = None
    ASCENDING = 1
    MongoClient = None
    DuplicateKeyError = Exception
    mongo_client = None
    CLIENT_ERROR = "pymongo is not installed. Run: pip install pymongo"
else:
    try:
        mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=MONGO_TIMEOUT_MS)
        CLIENT_ERROR = None
    except Exception as exc:  
        mongo_client = None
        CLIENT_ERROR = str(exc)

app = Flask(__name__)


ON_VERCEL = os.environ.get("VERCEL") == "1" or bool(os.environ.get("VERCEL_ENV"))

SECRET_KEY = os.environ.get("STUDYIOUS_SECRET_KEY")
if ON_VERCEL and not SECRET_KEY:
    raise RuntimeError("STUDYIOUS_SECRET_KEY must be set in the Vercel environment.")
app.config["SECRET_KEY"] = SECRET_KEY or "dev-only-change-me"

app.config["SESSION_PERMANENT"] = True
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=7)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SECURE"] = ON_VERCEL
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"


# Database Setup
@contextmanager
def get_db():
    # Yield the MongoDB database for this app
    if mongo_client is None:
        raise RuntimeError(CLIENT_ERROR or "MongoDB client is not available.")
    yield mongo_client[MONGO_DB_NAME]


def object_id(value):
    # Convert a string id to ObjectId, or None when it is invalid
    try:
        return ObjectId(str(value))
    except Exception:
        return None


def current_user_id():
    return object_id(session.get("user_id"))


def public_doc(document):
    # Return a JSON-safe copy with 'id' instead of MongoDB's '_id'
    result = dict(document)
    result["id"] = str(result.pop("_id"))
    if "user_id" in result:
        result["user_id"] = str(result["user_id"])
    return result


def now_string():
    return datetime.now().isoformat(timespec="seconds")

# Profile avatar setup when users want to customize their profile
def decode_avatar(data_url):
    match = re.match(
        r"^data:image/(png|jpeg|jpg|webp);base64,(.+)$",
        data_url,
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return None, None

    image_type = match.group(1).lower()
    extension = "jpg" if image_type in ("jpeg", "jpg") else image_type
    try:
        raw = base64.b64decode(match.group(2), validate=True)
    except (binascii.Error, ValueError):
        return None, None

    if not raw or len(raw) > MAX_AVATAR_BYTES:
        return None, None
    signatures = {
        "png": raw.startswith(b"\x89PNG\r\n\x1a\n"),
        "jpg": raw.startswith(b"\xff\xd8\xff"),
        "webp": raw.startswith(b"RIFF") and raw[8:12] == b"WEBP",
    }
    if not signatures.get(extension):
        return None, None
    return raw, extension


def avatar_url(filename):
    return f"/avatars/{filename}" if filename else None


AVATAR_MIME_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "webp": "image/webp",
}


def init_db():
    # This creates the index of users, files, and quizzes that are stored within MongoDb
    with get_db() as db:
        existing = set(db.list_collection_names())
        for name in ("users", "files", "quizzes", "avatars"):
            if name not in existing:
                db.create_collection(name)
        
        db.users.create_index([("email", ASCENDING)], unique=True)
        db.files.create_index([("user_id", ASCENDING)])
        db.quizzes.create_index([("user_id", ASCENDING)])
        db.avatars.create_index([("user_id", ASCENDING)], unique=True)
        db.avatars.create_index([("filename", ASCENDING)], unique=True)


try:
    init_db()
    DB_STARTUP_ERROR = None
except Exception as exc:  
    DB_STARTUP_ERROR = str(exc)
    print(f"Warning: could not initialize MongoDB: {DB_STARTUP_ERROR}")


@app.errorhandler(Exception)
def handle_unexpected_error(error):
    if isinstance(error, HTTPException):
        if request.path.startswith("/api/"):
            return jsonify(error=error.description), error.code
        return error
    if request.path.startswith("/api/"):
        message = DB_STARTUP_ERROR or str(error)
        return jsonify(error=f"Server error: {message}"), 500
    raise error


def valid_email(email):
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email))

# Werkzeug is used for the password hashing
def hash_password(password):
    try:
        from werkzeug.security import generate_password_hash

        return generate_password_hash(password)
    except ImportError:  
        import hashlib

        return hashlib.sha256(password.encode()).hexdigest()


def verify_password(stored, password):
    try:
        from werkzeug.security import check_password_hash

        return check_password_hash(stored, password)
    except (ImportError, ValueError):  
        import hashlib

        return hashlib.sha256(password.encode()).hexdigest() == stored


# Routing for Registration and Login
@app.route("/")
@app.route("/login.html")
def login_page():
    return send_from_directory(BASE_DIR, "login.html")


@app.route("/studyious.html")
def app_page():
    return send_from_directory(BASE_DIR, "studyious.html")


@app.route("/avatars/<filename>")
def avatar_file(filename):
    """Serve a profile picture from MongoDB (no local disk needed)."""
    if Path(filename).name != filename:
        return "Not found", 404
    try:
        with get_db() as db:
            row = db.avatars.find_one(
                {"filename": filename}, {"data": 1, "content_type": 1}
            )
    except Exception:
        return "Not found", 404
    if not row or not row.get("data"):
        return "Not found", 404
    return Response(
        bytes(row["data"]),
        mimetype=row.get("content_type") or "image/png",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.route("/<path:filename>")
def static_files(filename):
    """Serve the existing CSS/JS files next to this script."""
    return send_from_directory(BASE_DIR, filename)


# Authentication API
@app.route("/api/register", methods=["POST"])
def register():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not name or not valid_email(email) or len(password) < 6:
        return jsonify(error="Enter a name, a valid email, and a password of at least 6 characters."), 400

    with get_db() as db:
        if db.users.find_one({"email": email}):
            return jsonify(error="An account with that email already exists."), 409
        try:
            db.users.insert_one({
                "name": name,
                "email": email,
                "password_hash": hash_password(password),
                "created_at": now_string(),
            })
        except DuplicateKeyError:
            return jsonify(error="An account with that email already exists."), 409

    return jsonify(message="Account created. You can log in now.")


@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    with get_db() as db:
        user = db.users.find_one({"email": email})

    if not user or not verify_password(user["password_hash"], password):
        return jsonify(error="Invalid email or password."), 401

    session["user_id"] = str(user["_id"])
    session["user_name"] = user["name"]
    session["user_email"] = user["email"]
    return jsonify(message="Logged in.", name=user["name"])


@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify(message="Logged out.")


def require_login(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return jsonify(error="Please log in first."), 401
        return view(*args, **kwargs)

    return wrapper


@app.route("/api/me")
def me():
    if "user_id" not in session:
        return jsonify(authenticated=False), 401

    avatar = None
    try:
        with get_db() as db:
            user = db.users.find_one({"_id": current_user_id()}, {"avatar": 1})
            if user:
                avatar = avatar_url(user.get("avatar"))
    except Exception: 
        pass

    return jsonify(
        authenticated=True,
        name=session.get("user_name"),
        email=session.get("user_email"),
        avatar=avatar,
    )

# Profile avatar save function: image bytes are kept in MongoDB.
@app.route("/api/avatar", methods=["POST"])
@require_login
def save_avatar():
    data = request.get_json(silent=True) or {}
    raw, extension = decode_avatar(str(data.get("image") or ""))
    if raw is None:
        return jsonify(error="Choose a valid PNG, JPEG, or WebP image."), 400
    if len(raw) > MAX_AVATAR_BYTES:
        return jsonify(error="Profile picture must be smaller than 5 MB."), 400

    user_id = current_user_id()
    filename = f"{user_id}_{int(datetime.now().timestamp())}.{extension}"
    with get_db() as db:
        db.avatars.update_one(
            {"user_id": user_id},
            {"$set": {
                "filename": filename,
                "data": Binary(raw),
                "content_type": AVATAR_MIME_TYPES[extension],
                "updated_at": now_string(),
            }},
            upsert=True,
        )
        db.users.update_one({"_id": user_id}, {"$set": {"avatar": filename}})
    return jsonify(avatar=avatar_url(filename))

# Profile avatar deletion
@app.route("/api/avatar", methods=["DELETE"])
@require_login
def delete_avatar():
    user_id = current_user_id()
    with get_db() as db:
        db.users.update_one({"_id": user_id}, {"$unset": {"avatar": ""}})
        db.avatars.delete_one({"user_id": user_id})
    return jsonify(message="Profile picture removed.")


# File upload and reading API
ALLOWED_EXTENSIONS = {".docx", ".pdf"}
ALLOWED_MIME_TYPES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/pdf",
}
MIME_FOR_EXTENSION = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pdf": "application/pdf",
}


# Accepts only .docx / .pdf, and reject a mismatched MIME type when given
def allowed_file(filename, content_type):
    extension = Path(filename).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        return False
    mime = (content_type or "").split(";")[0].strip().lower()
    if not mime or mime == "application/octet-stream":
        return True
    return mime in ALLOWED_MIME_TYPES and mime == MIME_FOR_EXTENSION[extension]


def read_docx(stream):
    if Document is None:
        raise RuntimeError("python-docx is not installed. Run: pip install python-docx")
    document = Document(stream)
    return "\n".join(p.text for p in document.paragraphs if p.text.strip())


def read_pdf(stream):
    if PdfReader is None:
        raise RuntimeError("pypdf is not installed. Run: pip install pypdf")
    reader = PdfReader(stream)
    text = "\n".join((page.extract_text() or "") for page in reader.pages)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


# Key-points remind when user does file upload for setting up quiz

MAX_KEY_POINTS = 12
STOP_WORDS = set(
    """the a an and or but if then than that this these those of to in on at by for with from
    as is are was were be been being it its into about over under between during before after
    above below up down out off again further once here there when where why how all any both
    each few more most other some such no nor not only own same so too very can will just don
    should now also may might must shall would could does did doing have has had having do
    we you they he she i my our your their his her its which who whom what""".split()
)
SIGNAL_WORDS = (
    "important", "key", "main", "primary", "major", "significant", "essential", "critical",
    "summary", "in conclusion", "overall", "therefore", "thus", "result", "cause", "effect",
    "purpose", "function", "advantage", "disadvantage", "benefit", "limitation", "example",
    "must", "should", "remember", "note that", "defined as", "refers to", "means that",
)
NOISE_PATTERNS = (
    r"all rights reserved", r"copyright", r"table of contents", r"page\s+\d+",
    r"https?://", r"www\.", r"\S+@\S+", r"^\d+(?:\.\d+)?$", r"^[ivxlcdm]+\.$",
    r"^chapter\s+\d+", r"^figure\s+\d+", r"^section\s+\d+", r"^\s*[-=_*]{3,}\s*$",
)
LIST_MARKER = re.compile(r"^(?:[-•*–—]|\d+[.)]|[a-z][.)])\s+")


def split_into_candidates(text):
    
    candidates = []
    buffer = ""

    def flush():
        nonlocal buffer
        if buffer:
            candidates.append(buffer.strip())
            buffer = ""

    for raw_line in text.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line:
            flush()
            continue
        if buffer and len(buffer) > 40 and not re.search(r"[.!?;:]$", buffer) and not LIST_MARKER.match(line):
            buffer += " " + line
        else:
            flush()
            buffer = line
    flush()
    return candidates


def is_noise(line):
    lower = line.lower()
    if len(line) < 25 or len(line) > 400:
        return True
    words = re.findall(r"[A-Za-z]+", line)
    if len(words) < 5 or line.isupper():
        return True
    return any(re.search(pattern, lower) for pattern in NOISE_PATTERNS)


def topic_words(candidates):
    counts = Counter()
    for line in candidates:
        words = {
            word for word in re.findall(r"[a-z]{4,}", line.lower())
            if word not in STOP_WORDS
        }
        counts.update(words)
    return {word for word, count in counts.items() if count >= 2}

# Build an adjacency list of topics that occur together in source lines
def build_concept_graph(text):
    candidates = split_into_candidates(text)
    topics = topic_words(candidates)
    if not topics:
        return {}

    if len(topics) > MAX_GRAPH_TOPICS:
        counts = Counter()
        for line in candidates:
            counts.update(
                {word for word in re.findall(r"[a-z]{4,}", line.lower()) if word in topics}
            )
        topics = {word for word, _ in counts.most_common(MAX_GRAPH_TOPICS)}

    pair_counts = Counter()
    for line in candidates:
        terms = sorted({word for word in re.findall(r"[a-z]{4,}", line.lower()) if word in topics})
        for pair in combinations(terms, 2):
            pair_counts[pair] += 1

    neighbors = {topic: Counter() for topic in topics}
    for (first, second), count in pair_counts.items():
        neighbors[first][second] += count
        neighbors[second][first] += count

    return {
        topic: [other for other, _ in counter.most_common(MAX_GRAPH_NEIGHBORS)]
        for topic, counter in sorted(neighbors.items())
        if counter
    }


def score_line(line, topics):
    lower = line.lower()
    score = 0

    if LIST_MARKER.match(line):
        score += 4
    if "?" in line:
        score += 3
    if re.search(r"\b(is|are|was|were|means|refers to|defined as|consists of|includes|involves)\b", lower):
        score += 2
    # Linear Search when system scans for signal words
    if any(signal in lower for signal in SIGNAL_WORDS):
        score += 3
    if re.search(r"\b\d+(?:\.\d+)?\s*%?\b|\b(percent|rate|ratio)\b", lower):
        score += 2
    if re.search(r"\b(first|second|third|next|then|finally|in conclusion|in summary|overall)\b", lower):
        score += 2
    if topics & set(re.findall(r"[a-z]{4,}", lower)):
        score += 2
    if 40 <= len(line) <= 220:
        score += 2
    elif len(line) > 300:
        score -= 3
    if lower.startswith(("and ", "but ", "or ", "also ", "however ", "for example ")):
        score -= 2
    return score


# Selection sort
def selection_sort(items, key=None):
    result = list(items)
    key = key or (lambda item: item)
    for start in range(len(result) - 1):
        best = start
        for index in range(start + 1, len(result)):
            if key(result[index]) < key(result[best]):
                best = index
        if best != start:
            result[start], result[best] = result[best], result[start]
    return result


def selection_top(items, count, key=None):
    result = list(items)
    key = key or (lambda item: item)
    for target in range(min(max(count, 0), len(result))):
        best = target
        for index in range(target + 1, len(result)):
            if key(result[index]) < key(result[best]):
                best = index
        if best != target:
            result[target], result[best] = result[best], result[target]
    return result[:count]


def extract_key_points(text, limit=MAX_KEY_POINTS):
    candidates = split_into_candidates(text)
    topics = topic_words(candidates)
    seen = set()
    scored = []

    for index, line in enumerate(candidates):
        line = line.strip(" •–—-*")
        if is_noise(line):
            continue
        key = re.sub(r"[^a-z0-9]+", " ", line.lower()).strip()
        if key in seen:
            continue
        seen.add(key)
        scored.append((score_line(line, topics), index, line))

    if not scored:
        return [line for line in candidates if len(line) >= 15][:limit]

    
    selected = selection_top(scored, limit, key=lambda item: (-item[0], item[1]))
    selected = selection_sort(selected, key=lambda item: item[1])
    return [line for _, _, line in selected]


# File Upload Question Generator
def clean_subject(value):
    return re.sub(r"^(the|a|an)\s+", "", value.strip(" .,:;"), flags=re.IGNORECASE)


def make_definition_question(point):
    if re.match(r"^(?:there|here)\s+(?:is|are|was|were)\b", point, re.IGNORECASE):
        return None 
    match = re.match(
        r"^(.{3,70}?)\s+(is|are|was|were|means|refers to|defined as|consists of|includes|involves)\s+(.+)$",
        point,
        re.IGNORECASE,
    )
    if not match:
        return None

    subject = clean_subject(match.group(1))
    verb = match.group(2).lower()
    answer = match.group(3).strip(" .;")
    if len(subject) < 3 or not 5 <= len(answer) <= 220:
        return None

    templates = {
        "is": f"What is {subject}?",
        "are": f"What are {subject}?",
        "was": f"What was {subject}?",
        "were": f"What were {subject}?",
        "means": f"What does {subject} mean?",
        "refers to": f"What does {subject} refer to?",
        "defined as": f"How is {subject} defined?",
        "consists of": f"What does {subject} consist of?",
        "includes": f"What does {subject} include?",
        "involves": f"What does {subject} involve?",
    }
    question = templates[verb]
    return {"question": question[0].upper() + question[1:], "answer": answer}


def make_list_question(point):
    match = re.match(
        r"^(?:There are|There were)\s+([\w-]+)\s+(.+?)[:：]\s*(.+)$",
        point,
        re.IGNORECASE,
    )
    if not match:
        return None

    count = match.group(1).lower()
    label = match.group(2).strip(" .;")
    answer = match.group(3).strip(" .;")
    if len(label) < 3 or not 3 <= len(answer) <= 220:
        return None
    return {"question": f"What are the {count} {label}?", "answer": answer}


def make_cloze_question(point, topics):
    words = re.findall(r"(?<![A-Za-z-])[A-Za-z]{4,}(?![A-Za-z-])", point)
    candidates = [word for word in words if word.lower() not in STOP_WORDS and len(word) <= 30]
    if not candidates:
        return None

    preferred = [word for word in candidates if word.lower() in topics and len(word) >= 6]
    pick = sorted(preferred or candidates, key=len, reverse=True)[0]
    cloze = re.sub(r"\b" + re.escape(pick) + r"\b", "_____", point, count=1, flags=re.IGNORECASE)
    if cloze == point:
        return None
    return {"question": f"Fill in the blank: {cloze}", "answer": pick}


def generate_questions(text, limit=10, topics=None):
    points = extract_key_points(text, limit=max(limit * 2, 20))
    topics = topics if topics is not None else topic_words(points)
    questions = []
    seen = set()

    for index, point in enumerate(points):
        qa = None
        if point.rstrip().endswith("?") and index + 1 < len(points):
            next_point = points[index + 1]
            if not next_point.rstrip().endswith("?"):
                qa = {"question": point.strip(), "answer": next_point.strip()}
        qa = qa or make_definition_question(point)
        qa = qa or make_list_question(point)
        qa = qa or make_cloze_question(point, topics)
        if not qa:
            continue

        key = re.sub(r"[^a-z0-9]+", " ", qa["question"].lower()).strip()
        if key in seen:
            continue
        seen.add(key)
        qa["topics"] = [
            word for word in re.findall(r"[a-z]{4,}", qa["question"].lower()) if word in topics
        ][:MAX_GRAPH_NEIGHBORS]
        questions.append(qa)
        if len(questions) >= limit:
            break

    return questions


# Verifies what file type is uploaded
@app.route("/api/upload", methods=["POST"])
@require_login
def upload():
    if "file" not in request.files:
        return jsonify(error="No file was uploaded."), 400
    uploaded = request.files["file"]
    if not uploaded or not uploaded.filename:
        return jsonify(error="Please choose a file."), 400
    if not allowed_file(uploaded.filename, uploaded.content_type):
        return jsonify(error="Only .docx and .pdf files are accepted."), 400

    raw = uploaded.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        return jsonify(error="File is too large (maximum 10 MB)."), 413
    if not raw:
        return jsonify(error="The uploaded file is empty."), 400

    extension = Path(uploaded.filename).suffix.lower()
    try:
        if extension == ".docx":
            if not raw.startswith(b"PK"):
                return jsonify(error="This file is not a valid .docx document."), 400
            text = read_docx(io.BytesIO(raw))
        else:
            if b"%PDF" not in raw[:1024]:
                return jsonify(error="This file is not a valid PDF document."), 400
            text = read_pdf(io.BytesIO(raw))
    except Exception as exc:
        return jsonify(error=f"Could not read the file: {exc}"), 422

    concept_graph = build_concept_graph(text)
    questions = generate_questions(
        text, limit=min(10, MAX_QUESTIONS), topics=set(concept_graph)
    )

    file_oid = ObjectId()
    file_id = str(file_oid)
    for question in questions:
        question["file_id"] = file_id

    with get_db() as db:
        db.files.insert_one({
            "_id": file_oid,
            "user_id": current_user_id(),
            "filename": uploaded.filename,
            "questions": questions,
            "question_count": len(questions),
            "concept_graph": concept_graph,
            "uploaded_at": now_string(),
        })

    return jsonify(
        id=file_id,
        filename=uploaded.filename,
        text="\n".join(f"Q: {q['question']}\nA: {q['answer']}" for q in questions),
        questions=questions,
        concept_graph=concept_graph,
        extracted_count=len(questions),
    )


# Storing files in database
@app.route("/api/files")
@require_login
def list_files():
    with get_db() as db:
        rows = db.files.find(
            {"user_id": current_user_id()},
            {"filename": 1, "question_count": 1, "uploaded_at": 1},
        ).sort([("uploaded_at", -1), ("_id", -1)])
    return jsonify(files=[public_doc(row) for row in rows])


@app.route("/api/files/<file_id>")
@require_login
def get_file(file_id):
    with get_db() as db:
        row = db.files.find_one({"_id": object_id(file_id), "user_id": current_user_id()})
    if not row:
        return jsonify(error="File not found."), 404
    return jsonify(file=public_doc(row))


@app.route("/api/files/<file_id>", methods=["DELETE"])
@require_login
def delete_file(file_id):
    with get_db() as db:
        result = db.files.delete_one({"_id": object_id(file_id), "user_id": current_user_id()})
    return (jsonify(message="File deleted.") if result.deleted_count
            else (jsonify(error="File not found."), 404))


# Storing quizzes in database
@app.route("/api/quizzes")
@require_login
def list_quizzes():
    with get_db() as db:
        rows = db.quizzes.find(
            {"user_id": current_user_id()},
            {"title": 1, "question_count": 1, "created_at": 1},
        ).sort([("created_at", -1), ("_id", -1)])
    return jsonify(quizzes=[public_doc(row) for row in rows])


def normalize_question(item):
    if isinstance(item, dict):
        question = str(item.get("question") or "").strip()
        if not question:
            return None
        result = {"question": question, "answer": str(item.get("answer") or "").strip()}
        topics = item.get("topics")
        if isinstance(topics, list):
            result["topics"] = [
                str(topic).strip().lower() for topic in topics if str(topic).strip()
            ][:MAX_GRAPH_NEIGHBORS]
        file_id = item.get("file_id")
        if isinstance(file_id, str) and file_id:
            result["file_id"] = file_id
        return result
    question = str(item).strip()
    return question or None


# Merge file graphs and link topics that occur together in quiz questions
def build_quiz_concept_graph(db, questions, user_id):
    graph = {}
    file_ids = list(dict.fromkeys(
        question["file_id"] for question in questions if question.get("file_id")
    ))
    for file_id in file_ids:
        row = db.files.find_one(
            {"_id": object_id(file_id), "user_id": user_id}, {"concept_graph": 1}
        )
        if not row:
            continue
        for topic, neighbors in (row.get("concept_graph") or {}).items():
            graph.setdefault(topic, set()).update(neighbors)

    for question in questions:
        terms = sorted(set(question.get("topics") or []))
        for first, second in combinations(terms, 2):
            graph.setdefault(first, set()).add(second)
            graph.setdefault(second, set()).add(first)

    return {
        topic: sorted(neighbors)[:MAX_GRAPH_NEIGHBORS]
        for topic, neighbors in graph.items()
    }

# Quiz creation
@app.route("/api/quizzes", methods=["POST"])
@require_login
def create_quiz():
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip() or "Untitled quiz"
    raw_questions = data.get("questions") or []
    if not isinstance(raw_questions, list) or not raw_questions:
        return jsonify(error="Add at least one question first."), 400
    questions = [item for item in (normalize_question(q) for q in raw_questions) if item]
    if not questions:
        return jsonify(error="Add at least one question first."), 400
    if len(questions) > MAX_QUESTIONS:
        return jsonify(error=f"A quiz can contain at most {MAX_QUESTIONS} questions."), 400

    user_id = current_user_id()
    with get_db() as db:
        concept_graph = build_quiz_concept_graph(db, questions, user_id)
        # Concept graph of how quiz information is stored
        result = db.quizzes.insert_one({
            "user_id": user_id,
            "title": title,
            "questions": questions,
            "question_count": len(questions),
            "concept_graph": concept_graph,
            "created_at": now_string(),
        })
        quiz_id = str(result.inserted_id)

    return jsonify(id=quiz_id, title=title, question_count=len(questions)), 201

# Checks for the list of quizzes
@app.route("/api/quizzes/<quiz_id>")
@require_login
def get_quiz(quiz_id):
    with get_db() as db:
        row = db.quizzes.find_one({"_id": object_id(quiz_id), "user_id": current_user_id()})
    if not row:
        return jsonify(error="Quiz not found."), 404
    return jsonify(quiz=public_doc(row))

# Quiz deletion
@app.route("/api/quizzes/<quiz_id>", methods=["DELETE"])
@require_login
def delete_quiz(quiz_id):
    with get_db() as db:
        result = db.quizzes.delete_one({"_id": object_id(quiz_id), "user_id": current_user_id()})
    return (jsonify(message="Quiz deleted.") if result.deleted_count
            else (jsonify(error="Quiz not found."), 404))


if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG", "1") == "1")
