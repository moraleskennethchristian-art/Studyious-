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

LIGATURES = str.maketrans({
    "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st",
})
WEAK_ANSWER_STARTS = {
    "it", "this", "that", "these", "those", "they", "them", "there", "here",
    "which", "who", "what", "also", "above", "below", "following", "same",
    "not", "never", "both", "each", "all", "some", "many",
}


BOILERPLATE_LINE_PATTERNS = (
    re.compile(r"^CSP06\b", re.IGNORECASE),
    re.compile(r"^CHAPTER\s+\d+\b", re.IGNORECASE),
    re.compile(r"^(?:FINAL PERIOD|LEARNING OUTCOMES|DISCUSSION BASIS|COURSE APPROACH)\b", re.IGNORECASE),
    re.compile(r"^(?:REVIEW QUESTIONS|TRUSTED REFERENCES)\b", re.IGNORECASE),
    re.compile(r"^BASIS\s*:", re.IGNORECASE),
    re.compile(r"^(?:KEY LESSON|REMEMBER|RULE|EXAMPLE|SCENARIO|CLASSROOM EXAMPLE|FOR THIS COURSE)\s*:?", re.IGNORECASE),
    re.compile(r"^(?:PREPARED FOR|PRIMARY/OFFICIAL|CONCEPTS ALIGN|INTRODUCTORY|EXAMPLES ARE|“LIKELY FIT”)", re.IGNORECASE),
)

QUESTION_STARTS = ("what", "why", "how", "which", "when", "where", "who", "whom")
IMPERATIVE_STARTS = (
    "use", "avoid", "give", "choose", "start", "stop", "connect", "run",
    "check", "inspect", "plug", "select", "keep", "identify", "compare",
    "distinguish", "explain", "differentiate", "do not", "don't",
)
BAD_FACT_WORDS = (
    "example", "scenario", "recap", "clues", "classification", "sequence",
    "coverage", "worksheet", "review", "reference", "basis", "footer",
    "slide", "chapter", "csp06", "universidad", "dagupan", "best when",
    "possible causes", "checks to perform", "initialize the device",
    "most important connection",
)

RELATION_VERBS = {
    "provides": "provide", "offers": "offer", "gives": "give",
    "uses": "use", "requires": "require", "contains": "contain",
    "supports": "support", "causes": "cause", "produces": "produce",
    "leads to": "lead to", "results in": "result in", "follows": "follow",
    "separates": "separate", "identifies": "identify", "controls": "control",
    "manages": "manage", "evaluates": "evaluate", "represents": "represent",
    "connects": "connect", "creates": "create", "reduces": "reduce",
    "exchanges": "exchange", "communicates": "communicate", "proves": "prove",
    "decides": "decide", "depends on": "depend on", "allows": "allow",
    "lets": "let", "enables": "enable", "protects": "protect",
    "prevents": "prevent", "can change": "change", "changes": "change",
}
RELATION_VERB_PATTERN = "|".join(
    re.escape(verb) for verb in sorted(RELATION_VERBS, key=len, reverse=True)
)


def normalize_extracted_text(text):
    """Repair common PDF/DOCX extraction artefacts before candidate selection."""
    text = text.replace("\u00ad", "").translate(LIGATURES)
    text = text.replace("：", ":").replace("–", "-").replace("—", "-")
    text = re.sub(r"([A-Za-z])-\s*\n\s*([A-Za-z])", r"\1\2", text)
    text = re.sub(r"[→←↓↑]", " ", text)
    return "\n".join(" ".join(line.split()) for line in text.splitlines())


def _footer_key(line):
    """Ignore a trailing page number so repeated footers can be detected."""
    return re.sub(r"\s+\d{1,3}$", "", line.strip()).lower()


def remove_repeated_lines(text, minimum=3, max_length=100):
    """Drop repeated headers/footers and standalone page labels."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    counts = Counter(lines)
    footer_counts = Counter(_footer_key(line) for line in lines)
    kept = []

    for line in lines:
        if len(line) > max_length:
            kept.append(line)
            continue
        if counts[line] >= minimum:
            continue
        if (
            footer_counts[_footer_key(line)] >= 2
            and not re.search(r"[.!?]$", line)
        ):
            continue
        kept.append(line)

    return "\n".join(kept)


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
    """Read paragraphs, headings, and tables in document order."""
    if Document is None:
        raise RuntimeError("python-docx is not installed. Run: pip install python-docx")
    document = Document(stream)
    lines = []

    try:
        from docx.table import Table
        from docx.text.paragraph import Paragraph
    except ImportError:
        lines.extend(p.text for p in document.paragraphs if p.text.strip())
        for table in document.tables:
            for row in table.rows:
                cells = [" ".join(cell.text.split()) for cell in row.cells]
                if any(cells):
                    lines.append(" | ".join(cells))
        return remove_repeated_lines(normalize_extracted_text("\n".join(lines)))

    for child in document.element.body.iterchildren():
        if child.tag.endswith("}p"):
            paragraph = Paragraph(child, document)
            text = paragraph.text.strip()
            if text:
                lines.append(text)
        elif child.tag.endswith("}tbl"):
            table = Table(child, document)
            for row in table.rows:
                cells = [" ".join(cell.text.split()) for cell in row.cells if cell.text.strip()]
                if cells:
                    lines.append(" | ".join(cells))

    return remove_repeated_lines(normalize_extracted_text("\n".join(lines)))


def read_pdf(stream):
    if PdfReader is None:
        raise RuntimeError("pypdf is not installed. Run: pip install pypdf")
    reader = PdfReader(stream)
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text(extraction_mode="layout") or "")
        except TypeError:
            pages.append(page.extract_text() or "")
    text = normalize_extracted_text("\n".join(pages))
    return remove_repeated_lines(text)


# Key-points remind when user does file upload for setting up quiz

MAX_KEY_POINTS = 12
STOP_WORDS = set(
    """the a an and or but if then than that this these those of to in on at by for with from
    as is are was were be been being it its into about over under between during before after
    above below up down out off again further once here there when where why how all any both
    each few more most other some such no nor not only own same so too very can will just don
    should now also may might must shall would could does did doing have has had having do
    we you they he she i my our your their his her its which who whom what used uses using""".split()
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
EXTRA_TOPIC_STOP_WORDS = {
    "able", "account", "answer", "approach", "basis", "chapter", "classroom",
    "course", "csp", "dagupan", "detail", "example", "examples", "explain",
    "final", "first", "identify", "lesson", "main", "note", "overall", "period",
    "question", "questions", "recap", "reference", "references", "remember",
    "review", "rule", "scenario", "second", "short", "signal", "slide",
    "student", "students", "summary", "third", "topic", "topics", "universidad",
}

LIST_MARKER = re.compile(r"^(?:[-•*–—]|\d+[.)]|[a-z][.)])\s+")
SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")
DEFINITION_VERB_PATTERN = re.compile(
    r"\b(is|are|was|were|means|refers to|defined as|consists of|includes|involves)\b",
    re.IGNORECASE,
)
QUIZ_FACT_PATTERN = re.compile(
    r"\b(is|are|was|were|means|refers to|defined as|consists of|includes|involves|"
    r"provides|offers|gives|uses|requires|contains|supports|causes|produces|"
    r"leads to|results in|follows|separates|identifies|controls|manages|"
    r"evaluates|represents|connects|creates|reduces|exchanges|communicates|"
    r"proves|decides|depends on|allows|lets|enables|protects|prevents|can|"
    r"changes|there are|there were)\b",
    re.IGNORECASE,
)


def strip_leading_markers(line):
    """Remove repeated bullet/number markers such as '- 1. '."""
    previous = None
    while previous != line:
        previous = line
        line = LIST_MARKER.sub("", line).strip()
    return line


def is_boilerplate(line):
    return any(pattern.match(line.strip()) for pattern in BOILERPLATE_LINE_PATTERNS)


def is_noise(line):
    line = line.strip()
    if not line or is_boilerplate(line):
        return True
    if len(line) < 20 or len(line) > 260:
        return True
    if re.search(r"[•▪◦∙|→]", line) or line.count(";") >= 2:
        return True
    words = re.findall(r"[A-Za-z]+", line)
    if len(words) < 5 or line.isupper():
        return True
    if len(re.findall(r"\b\d+[.)]\b", line)) >= 2:
        return True
    lower = line.lower()
    if any(re.search(pattern, lower) for pattern in NOISE_PATTERNS):
        return True
    if re.search(r"\b(?:chapter|csp06|universidad|dagupan|final period)\b", lower):
        return True
    return False


def is_quiz_fact(line):
    if is_noise(line) or "?" in line:
        return False
    if not line.rstrip().endswith(".") or not line[0].isupper():
        return False
    if len(line.split()) < 6 or line.count(":") > 1:
        return False
    if re.search(r"[✓✔✗□■]", line):
        return False
    lower = line.lower()
    if lower.startswith(QUESTION_STARTS + IMPERATIVE_STARTS):
        return False
    if "do not" in lower or "don't" in lower or lower.startswith("not "):
        return False
    if any(word in lower for word in BAD_FACT_WORDS):
        return False
    if re.search(r"\bch\s*\d+\b", lower):
        return False
    return bool(QUIZ_FACT_PATTERN.search(lower))


def split_sentences(line):
    return [part.strip() for part in SENTENCE_SPLIT.split(line) if part.strip()]


def split_into_candidates(text):
    candidates = []
    buffer = ""

    def add(line):
        line = strip_leading_markers(line.strip())
        if line and not is_noise(line):
            candidates.append(line)

    def flush():
        nonlocal buffer
        if buffer:
            add(buffer)
            buffer = ""

    for raw_line in text.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line:
            flush()
            continue

        line = strip_leading_markers(line)
        if is_noise(line):
            flush()
            continue

        sentences = split_sentences(line)
        if len(sentences) > 1:
            flush()
            for sentence in sentences:
                add(sentence)
            continue

        if buffer and len(buffer) > 40 and not re.search(r"[.!?;:]$", buffer):
            buffer += " " + line
        else:
            flush()
            buffer = line

    flush()
    return candidates


def is_noise(line):
    lower = line.lower()
    if len(line) < 12 or len(line) > 400:
        return True
    words = re.findall(r"[A-Za-z]+", line)
    if len(words) < 3 or (line.isupper() and len(line) > 80):
        return True
    if len(line) < 25 and not (
        LIST_MARKER.match(line)
        or line.endswith(":")
        or re.search(r"\b(is|are|was|were|means|refers to|defined as|uses|follows)\b", lower)
    ):
        return True
    return any(re.search(pattern, lower) for pattern in NOISE_PATTERNS)


def stem_word(word):
    if len(word) <= 4:
        return word
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith("ing") and len(word) > 6:
        return word[:-3]
    if word.endswith("ed") and len(word) > 5:
        return word[:-2]
    if word.endswith("es") and len(word) > 5:
        return word[:-2]
    if word.endswith("s") and len(word) > 4:
        return word[:-1]
    return word


def word_tokens(text):
    return [
        stem_word(word)
        for word in re.findall(r"[a-z]{4,}", text.lower())
        if word not in STOP_WORDS
    ]


def line_topic_terms(line, topics):
    tokens = word_tokens(line)
    terms = set(tokens)
    terms.update(f"{first} {second}" for first, second in zip(tokens, tokens[1:]))
    return {term for term in topics if term in terms}


def topic_words(candidates):
    counts = Counter()
    pair_counts = Counter()
    for line in candidates:
        if not is_quiz_fact(line):
            continue
        tokens = [
            token for token in word_tokens(line)
            if token not in EXTRA_TOPIC_STOP_WORDS
        ]
        counts.update(set(tokens))
        pair_counts.update({f"{a} {b}" for a, b in zip(tokens, tokens[1:])})

    topics = {word for word, count in counts.items() if count >= 2}
    topics.update(pair for pair, count in pair_counts.items() if count >= 2)
    return topics

# Build an adjacency list of topics that occur together in source lines
def build_concept_graph(text):
    candidates = split_into_candidates(text)
    topics = topic_words(candidates)
    if not topics:
        return {}

    if len(topics) > MAX_GRAPH_TOPICS:
        counts = Counter()
        for line in candidates:
            counts.update(line_topic_terms(line, topics))
        topics = {word for word, _ in counts.most_common(MAX_GRAPH_TOPICS)}

    pair_counts = Counter()
    for line in candidates:
        terms = sorted(line_topic_terms(line, topics))
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
    if line_topic_terms(line, topics):
        score += 2
    if 40 <= len(line) <= 220:
        score += 2
    elif len(line) > 300:
        score -= 3
    if lower.startswith(("and ", "but ", "or ", "also ", "however ", "for example ")):
        score -= 2
    if "?" in line or lower.startswith(QUESTION_STARTS):
        score -= 4
    if not is_quiz_fact(line):
        score -= 6
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
        line = strip_leading_markers(line.strip())
        if not is_quiz_fact(line):
            continue
        key = re.sub(r"[^a-z0-9]+", " ", line.lower()).strip()
        if key in seen:
            continue
        seen.add(key)
        scored.append((score_line(line, topics), index, line))

    if not scored:
        return [line for line in candidates if is_quiz_fact(line)][:limit]

    
    sections = min(4, max(1, limit))
    width = max(1, (len(scored) + sections - 1) // sections)
    buckets = [scored[start:start + width] for start in range(0, len(scored), width)]
    ranked_buckets = [
        selection_sort(bucket, key=lambda item: (-item[0], item[1]))
        for bucket in buckets
    ]

    selected = []
    for position in range(width):
        for bucket in ranked_buckets:
            if position < len(bucket):
                selected.append(bucket[position])
                if len(selected) >= limit:
                    break
        if len(selected) >= limit:
            break

    selected = selection_sort(selected, key=lambda item: item[1])
    return [line for _, _, line in selected]


# File Upload Question Generator
def clean_answer(value):
    answer = " ".join(str(value or "").split()).strip(" .;:")
    answer = re.split(r"\s+(?:Basis|Reference|Example|Rule)\s*:", answer, flags=re.IGNORECASE)[0]
    answer = re.split(r",\s+(?:so|because|while|whereas|but)\b", answer, flags=re.IGNORECASE)[0]
    return re.sub(r"\s+\d{1,3}$", "", answer).strip(" .;:")


def lower_starts(value, starts):
    return value.lower().startswith(tuple(starts))


def clean_subject(value):
    subject = re.sub(r"^[\d•.\s]+", "", str(value or "").strip(" .,:;"))
    subject = re.sub(r"^(?:the|a|an)\s+", "", subject, flags=re.IGNORECASE)
    subject = subject.strip(" .,:;")
    if lower_starts(subject, QUESTION_STARTS + ("this", "that", "these", "those", "it", "they", "there", "here")):
        return ""
    if subject.lower() in WEAK_ANSWER_STARTS or ";" in subject:
        return ""
    return subject


def make_definition_question(point):
    if "?" in point or not is_quiz_fact(point):
        return None
    if re.match(r"^(?:there|here)\s+(?:is|are|was|were)\b", point, re.IGNORECASE):
        return None
    match = DEFINITION_VERB_PATTERN.search(point)
    if not match or match.start() < 3:
        return None
    subject = clean_subject(point[:match.start()])
    verb = match.group(1).lower()
    answer = clean_answer(point[match.end():])
    if len(subject) < 3 or len(subject) > 70 or ";" in subject:
        return None
    if subject.lower() in WEAK_ANSWER_STARTS or subject.lower().endswith(
        ("that", "which", "who", "where", "when")
    ):
        return None
    if not 5 <= len(answer) <= 220 or "?" in answer:
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
    if not is_quiz_fact(point) or "?" in point or len(point) > 200:
        return None
    first_word = point.split()[0].lower().strip(" .,:;")
    if first_word in WEAK_ANSWER_STARTS or first_word in IMPERATIVE_STARTS:
        return None
    words = re.findall(r"(?<![A-Za-z-])[A-Za-z]{6,}(?![A-Za-z-])", point)
    preferred = [
        word for word in words
        if stem_word(word.lower()) in topics
        and word.lower() not in EXTRA_TOPIC_STOP_WORDS
        and not word.lower().endswith("ing")
    ]
    if not preferred:
        return None
    pick = sorted(preferred, key=lambda word: (-len(word), words.index(word)))[0]
    cloze = re.sub(r"\b" + re.escape(pick) + r"\b", "_____", point, count=1, flags=re.IGNORECASE)
    if cloze == point:
        return None
    return {"question": f"Fill in the blank: {cloze}", "answer": pick}


def make_complexity_question(point):
    match = re.match(
        r"^(.{3,70}?)\s+(?:runs|operates|has|takes|is)\s+in\s+(O\([^)]+\)|Θ\([^)]+\)|Ω\([^)]+\))",
        point,
        re.IGNORECASE,
    )
    if not match:
        return None
    subject = clean_subject(match.group(1))
    if len(subject) < 3:
        return None
    question = f"What is the time complexity of {subject}?"
    return {"question": question[0].upper() + question[1:], "answer": match.group(2)}


def make_relation_question(point):
    if "?" in point or not is_quiz_fact(point):
        return None

    important = re.match(r"^(.{3,70}?)\s+is\s+important\s+because\s+(.+)$", point, re.IGNORECASE)
    if important:
        subject = clean_subject(important.group(1))
        answer = clean_answer(important.group(2))
        if 3 <= len(subject) <= 70 and 5 <= len(answer) <= 220:
            question = f"Why is {subject} important?"
            return {"question": question[0].upper() + question[1:], "answer": answer}

    used_to = re.match(r"^(.{3,80}?)\s+(?:is|are)\s+used\s+to\s+(.+)$", point, re.IGNORECASE)
    if used_to:
        subject = clean_subject(used_to.group(1))
        answer = clean_answer(used_to.group(2))
        if 3 <= len(subject) <= 80 and 5 <= len(answer) <= 220:
            question = f"What is {subject} used for?"
            return {"question": question[0].upper() + question[1:], "answer": f"To {answer}"}

    evaluated = re.match(r"^(.{3,80}?)\s+are\s+evaluated\s+using\s+(.+)$", point, re.IGNORECASE)
    if evaluated:
        subject = clean_subject(evaluated.group(1))
        answer = clean_answer(evaluated.group(2))
        if 3 <= len(subject) <= 80 and 5 <= len(answer) <= 220:
            question = f"What are {subject} evaluated using?"
            return {"question": question[0].upper() + question[1:], "answer": answer}

    match = re.match(
        rf"^(.{{3,80}}?)\s+({RELATION_VERB_PATTERN})\s+(.+)$",
        point,
        re.IGNORECASE,
    )
    if match:
        if DEFINITION_VERB_PATTERN.search(match.group(1)):
            return None
        subject = clean_subject(match.group(1))
        if " and " in subject.lower():
            return None
        answer = clean_answer(match.group(3))
        answer = re.split(r",\s+(?:while|whereas|but)\b", answer)[0].strip(" .;:")
        verb = RELATION_VERBS[match.group(2).lower()]
        if 3 <= len(subject) <= 80 and 5 <= len(answer) <= 220:
            question = f"What does {subject} {verb}?"
            return {"question": question[0].upper() + question[1:], "answer": answer}
    return None


def is_valid_qa(qa):
    question = str(qa.get("question") or "").strip()
    answer = str(qa.get("answer") or "").strip()
    if not question or not 3 <= len(answer) <= 180:
        return False
    if len(answer.split()) > 30 or question.count("?") > 1 or "?" in answer:
        return False
    if re.search(r"\b(?:CSP06|CHAPTER\s+\d+|UNIVERSIDAD|DAGUPAN)\b", f"{question} {answer}", re.IGNORECASE):
        return False
    if re.match(r"^(?:what|why|how|which|when|where|who)\s+\d", question, re.IGNORECASE):
        return False
    first = answer.split()[0].lower().strip(".,;:")
    if first in {"not", "no", "never"}:
        return False
    if first in WEAK_ANSWER_STARTS and len(answer.split()) <= 5:
        return False
    if answer.lower().strip(" .;:") == question.lower().strip(" .;:"):
        return False
    return True


def answer_signature(text):
    return frozenset(word_tokens(text))


def answers_similar(first, second):
    left = answer_signature(first)
    right = answer_signature(second)
    if not left or not right:
        return False
    return len(left & right) / len(left | right) >= 0.72


def generate_questions(text, limit=10, topics=None):
    points = extract_key_points(text, limit=max(limit * 3, 30))
    topics = topics if topics is not None else topic_words(points)
    candidates = []
    seen_questions = set()
    seen_answers = []

    for point in points:
        qa = make_definition_question(point)
        qa = qa or make_relation_question(point)
        qa = qa or make_complexity_question(point)
        qa = qa or make_list_question(point)
        qa = qa or make_cloze_question(point, topics)

        if not qa or not is_valid_qa(qa):
            continue

        question_key = re.sub(r"[^a-z0-9]+", " ", qa["question"].lower()).strip()
        if question_key in seen_questions:
            continue
        if any(answers_similar(qa["answer"], old) for old in seen_answers):
            continue

        seen_questions.add(question_key)
        seen_answers.append(qa["answer"])
        qa["topics"] = sorted(
            line_topic_terms(f"{qa['question']} {qa['answer']}", topics)
        )[:MAX_GRAPH_NEIGHBORS]
        candidates.append(qa)

    if len(candidates) <= limit:
        return candidates

    def primary_topic(qa):
        return qa["topics"][0] if qa["topics"] else ""

    counts = Counter(primary_topic(qa) for qa in candidates if primary_topic(qa))
    ranked = selection_sort(
        list(enumerate(candidates)),
        key=lambda item: (
            counts.get(primary_topic(item[1]), 10 ** 6) if primary_topic(item[1]) else 10 ** 6,
            item[0],
        ),
    )
    return [qa for _, qa in ranked[:limit]]


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
