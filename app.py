"""
HarvesterAI Web - FastAPI backend
"""
import asyncio
import base64
import hashlib
import hmac
import io
import json
import logging
import os
import queue
import re as _re
import threading
import time as _time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional, List

import pandas as pd
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from database import (
    init_db,
    project_create, project_update, project_get, project_list, project_delete,
    project_backup, project_restore,
    project_member_add, project_member_remove, project_member_set_role,
    project_members_list, get_project_role, project_list_for_user,
    run_create, run_get, run_list, run_delete, run_finish, run_update_ai_ref, run_append_log,
    run_update_settings, human_answers_for_run,
    ai_results_for_pdf, ai_results_for_pdf_by_bib, ai_results_for_export,
    human_result_save, human_results_for_pdf, human_results_progress,
    human_paper_statuses, human_paper_statuses_by_coder, get_paper_status, human_results_for_export,
    human_last_coders, human_decision_coders,
    paper_lock_acquire, paper_lock_release, paper_lock_get,
    bib_clear, bib_upsert_batch, bib_list, bib_get, bib_get_by_file,
    bib_sub_entries, bib_apply_split, paper_parent,
    match_filename_by_bib,
    user_list, user_add, user_add_with_password, user_get_by_name,
    user_set_role, user_set_password, user_list_with_roles, user_delete,
    audit_log_append, audit_log_for_export, audit_log_for_paper,
)
from processing import (
    parse_questions_excel, parse_extractor_excel, parse_ris,
    ris_to_bib_entries, JobContext, run_ai_job
)

import sys as _sys
BASE_DIR = Path(_sys._MEIPASS) if getattr(_sys, 'frozen', False) else Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"
logger = logging.getLogger(__name__)

# ── Auth config ───────────────────────────────────────────────────────────────
AUTH_MODE    = os.environ.get("AUTH_MODE", "name_selector")   # "name_selector" | "login"
AUTH_SECRET  = os.environ.get("AUTH_SECRET_KEY", "dev-secret-please-change-in-production")
TOKEN_TTL    = 60 * 60 * 12  # 12 hours
SIGNUP_MODE  = os.environ.get("SIGNUP_MODE", "closed")   # "open" | "code" | "closed"
SIGNUP_CODE  = os.environ.get("SIGNUP_CODE", "")
PDF_LOCATION = os.environ.get("PDF_LOCATION", "local")   # "local" | "server" | "ignore"

# PDF storage directory (only used when PDF_LOCATION=server)
_data_dir_for_pdfs = Path(os.environ["HARVESTERAI_DATA_DIR"]) if os.environ.get("HARVESTERAI_DATA_DIR") else Path(__file__).parent
PDF_DIR = _data_dir_for_pdfs / "pdfs"
PDF_DIR.mkdir(parents=True, exist_ok=True)

# Global roles (stored in users.role)
G_ADMIN = "admin"   # site-wide admin: full access to everything
G_USER  = "user"    # regular user: access only to projects they're a member of

# Project roles (stored in project_members.project_role)
P_OWNER  = "owner"   # full control, manages project members, can delete project
P_ADMIN  = "admin"   # Review Administrator: configure/AI/export/add members
P_MEMBER = "member"  # Team Member: human coding only

def _hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100_000)
    return base64.b64encode(salt + dk).decode()

def _check_password(password: str, stored: str) -> bool:
    try:
        raw = base64.b64decode(stored.encode())
        salt, dk = raw[:16], raw[16:]
        dk2 = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100_000)
        return hmac.compare_digest(dk, dk2)
    except Exception:
        return False

def _make_token(username: str, global_role: str = G_USER) -> str:
    payload = json.dumps({"u": username, "g": global_role, "exp": int(_time.time()) + TOKEN_TTL})
    payload_b64 = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    sig = hmac.new(AUTH_SECRET.encode(), payload_b64.encode(), hashlib.sha256).hexdigest()
    return f"{payload_b64}.{sig}"

def _verify_token(token: str) -> Optional[dict]:
    """Returns {"u": username, "g": global_role} or None if invalid/expired."""
    try:
        payload_b64, sig = token.rsplit(".", 1)
        expected = hmac.new(AUTH_SECRET.encode(), payload_b64.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            return None
        padded = payload_b64 + "=" * (4 - len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode())
        if payload["exp"] < _time.time():
            return None
        return {"u": payload["u"], "g": payload.get("g", G_USER)}
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        logger.warning("Token verification failed: %s", exc)
        return None

app = FastAPI(title="HarvesterAI")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

init_db()

if AUTH_MODE == "login" and AUTH_SECRET == "dev-secret-please-change-in-production":
    raise RuntimeError("AUTH_SECRET_KEY must be set when AUTH_MODE=login")

# Create initial admin in login mode if no users exist yet
if AUTH_MODE == "login":
    _initial_user = os.environ.get("INITIAL_ADMIN_USER", "admin")
    _initial_pass = os.environ.get("INITIAL_ADMIN_PASSWORD", "")
    if _initial_pass and not user_list():
        user_add_with_password(_initial_user, _hash_password(_initial_pass), role=G_ADMIN)

# ── Project-role permission check ─────────────────────────────────────────────
def _project_allowed(proj_role: str, method: str, path: str) -> bool:
    if proj_role == P_OWNER:
        return True
    if proj_role == P_ADMIN:
        # Admin cannot delete the project itself
        if method == "DELETE" and _re.match(r"^/api/projects/[^/]+$", path):
            return False
        return True
    if proj_role == P_MEMBER:
        # Members: read-only project data + full human coding
        if method == "GET":
            if _re.match(r"^/api/projects/[^/]+(/bibliography|/runs)?$", path):
                return True
            if _re.match(r"^/api/runs/[^/]+(/human/.+|/status_summary(_by_reviewer)?)?$", path):
                return True
        if method in ("POST", "PATCH"):
            if _re.match(r"^/api/runs/[^/]+/human/paper/", path):
                return True
        return False
    return False

# ── Auth middleware (login mode only) ─────────────────────────────────────────
_PUBLIC_PATHS = {"/api/auth/mode", "/api/auth/login", "/api/auth/signup-mode", "/api/auth/register", "/api/config/pdf-location"}
_SSE_PATH_RE = _re.compile(r"^/api/runs/[^/]+/stream$")

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if AUTH_MODE != "login":
        return await call_next(request)
    path = request.url.path
    method = request.method
    if method == "OPTIONS" or not path.startswith("/api/") or path in _PUBLIC_PATHS or _SSE_PATH_RE.match(path):
        return await call_next(request)
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    token_data = _verify_token(auth[7:])
    if not token_data:
        return JSONResponse({"detail": "Invalid or expired token"}, status_code=401)
    username = token_data["u"]
    global_role = token_data["g"]
    request.state.current_user = username
    request.state.global_role = global_role
    request.state.project_role = None
    # Global admin: full access everywhere
    if global_role == G_ADMIN:
        return await call_next(request)
    # Regular user: deny user-management routes
    if path.startswith("/api/users"):
        return JSONResponse({"detail": "Insufficient permissions"}, status_code=403)
    # Any authenticated user can change their own password
    if method == "POST" and path == "/api/auth/change-password":
        return await call_next(request)
    # Reads OpenRouter's public model list — no project, no key, nothing to gate
    if method == "GET" and path == "/api/openrouter/model":
        return await call_next(request)
    # Any authenticated user can list or create a project, or import
    if path == "/api/projects" and method in ("GET", "POST"):
        return await call_next(request)
    if method == "POST" and path == "/api/projects/import":
        return await call_next(request)
    # Resolve which project this request targets
    proj_id = None
    m = _re.match(r"^/api/projects/([^/]+)", path)
    if m:
        proj_id = m.group(1)
    else:
        m = _re.match(r"^/api/runs/([^/]+)", path)
        if m:
            run = run_get(m.group(1))
            if run:
                proj_id = run["project_id"]
    if not proj_id:
        return JSONResponse({"detail": "Insufficient permissions"}, status_code=403)
    # Check project membership
    proj_role = get_project_role(proj_id, username)
    if not proj_role:
        return JSONResponse({"detail": "Not a member of this project"}, status_code=403)
    request.state.project_role = proj_role
    if not _project_allowed(proj_role, method, path):
        return JSONResponse({"detail": "Insufficient permissions"}, status_code=403)
    return await call_next(request)

# ── SSE fan-out ───────────────────────────────────────────────────────────────
_subscribers: dict[str, list[queue.Queue]] = {}
_sub_lock = threading.Lock()
_active_jobs: dict[str, JobContext] = {}

def _publish(run_id, event_type, data):
    with _sub_lock:
        for q in _subscribers.get(run_id, []):
            q.put((event_type, data))

def _subscribe(run_id) -> queue.Queue:
    q: queue.Queue = queue.Queue()
    with _sub_lock:
        _subscribers.setdefault(run_id, []).append(q)
    return q

def _unsubscribe(run_id, q):
    with _sub_lock:
        try: _subscribers.get(run_id, []).remove(q)
        except ValueError: pass


# ══════════════════════════════════════════════════════════════════════════════
# Auth endpoints
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/auth/mode")
def get_auth_mode():
    return {"mode": AUTH_MODE}

class LoginRequest(BaseModel):
    username: str
    password: str

@app.post("/api/auth/login")
def login(body: LoginRequest):
    if AUTH_MODE != "login":
        raise HTTPException(400, "Login not required in this deployment")
    user = user_get_by_name(body.username.strip())
    if not user or not user.get("password_hash"):
        raise HTTPException(401, "Invalid credentials")
    if not _check_password(body.password, user["password_hash"]):
        raise HTTPException(401, "Invalid credentials")
    global_role = user.get("role") or G_USER
    token = _make_token(user["name"], global_role)
    return {"token": token, "username": user["name"], "global_role": global_role}


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str

@app.get("/api/auth/signup-mode")
def get_signup_mode():
    return {"signup_mode": SIGNUP_MODE if AUTH_MODE == "login" else "closed"}


_OR_MODEL_CACHE = {"at": 0.0, "models": {}}


@app.get("/api/openrouter/model")
def openrouter_model_info(model: str):
    """What OpenRouter reports about one model's reasoning support.

    Lets the settings UI offer effort levels only where they exist, rather than
    sending a parameter the model would ignore — or reject.
    """
    now = _time.time()
    if now - _OR_MODEL_CACHE["at"] > 900 or not _OR_MODEL_CACHE["models"]:
        try:
            import requests
            data = requests.get("https://openrouter.ai/api/v1/models", timeout=20).json()
            _OR_MODEL_CACHE["models"] = {m["id"]: m for m in data.get("data", [])}
            _OR_MODEL_CACHE["at"] = now
        except Exception:
            logger.exception("Could not reach OpenRouter's model list")
            return {"known": False}

    m = _OR_MODEL_CACHE["models"].get((model or "").strip())
    if not m:
        return {"known": False}
    r = m.get("reasoning") or {}
    return {
        "known": True,
        "supports_reasoning": bool(r) or ("reasoning" in (m.get("supported_parameters") or [])),
        "mandatory": bool(r.get("mandatory")),
        "default_effort": r.get("default_effort"),
        "supported_efforts": r.get("supported_efforts") or [],
    }


@app.get("/api/config/pdf-location")
def get_pdf_location():
    return {"pdf_location": PDF_LOCATION}


class RegisterRequest(BaseModel):
    username: str
    password: str
    code: Optional[str] = None

@app.post("/api/auth/register", status_code=201)
def register(body: RegisterRequest):
    if AUTH_MODE != "login":
        raise HTTPException(400, "Not in login mode")
    if SIGNUP_MODE == "closed":
        raise HTTPException(403, "Registration is closed")
    if SIGNUP_MODE == "code":
        if not SIGNUP_CODE or body.code != SIGNUP_CODE:
            raise HTTPException(403, "Invalid access code")
    name = body.username.strip()
    if not name:
        raise HTTPException(400, "Username required")
    if not body.password:
        raise HTTPException(400, "Password required")
    if user_get_by_name(name):
        raise HTTPException(409, "Username already taken")
    user_add_with_password(name, _hash_password(body.password), role=G_USER)
    token = _make_token(name, G_USER)
    return {"token": token, "username": name, "global_role": G_USER}


@app.post("/api/auth/change-password")
def change_password(body: ChangePasswordRequest, request: Request):
    if AUTH_MODE != "login":
        raise HTTPException(400, "Not in login mode")
    username = getattr(request.state, "current_user", None)
    if not username:
        raise HTTPException(401, "Not authenticated")
    if not body.new_password:
        raise HTTPException(400, "New password required")
    user = user_get_by_name(username)
    if not user:
        raise HTTPException(404, "User not found")
    if not _check_password(body.current_password, user.get("password_hash", "")):
        raise HTTPException(401, "Current password is incorrect")
    user_set_password(username, _hash_password(body.new_password))
    return {"changed": True}


# ══════════════════════════════════════════════════════════════════════════════
# Projects
# ══════════════════════════════════════════════════════════════════════════════

class ProjectCreate(BaseModel):
    name: str
    description: str = ""

@app.get("/api/projects")
def list_projects(request: Request):
    if AUTH_MODE == "login":
        if getattr(request.state, "global_role", G_USER) == G_ADMIN:
            return project_list()
        username = getattr(request.state, "current_user", "")
        return project_list_for_user(username)
    return project_list()

@app.post("/api/projects", status_code=201)
def create_project(body: ProjectCreate, request: Request):
    proj_id = str(uuid.uuid4())
    project_create(proj_id, body.name, body.description)
    if AUTH_MODE == "login":
        username = getattr(request.state, "current_user", "")
        if username:
            project_member_add(proj_id, username, P_OWNER)
    return project_get(proj_id)

@app.get("/api/projects/{proj_id}")
def get_project(proj_id: str, request: Request):
    p = project_get(proj_id)
    if not p: raise HTTPException(404, "Project not found")
    if AUTH_MODE == "login":
        global_role = getattr(request.state, "global_role", G_USER)
        project_role = getattr(request.state, "project_role", None)
        # Team members can view project metadata, but should not receive the shared API key.
        if global_role != G_ADMIN and project_role == P_MEMBER:
            p["api_key"] = ""
    return p

@app.delete("/api/projects/{proj_id}")
def delete_project(proj_id: str):
    if not project_get(proj_id): raise HTTPException(404)
    project_delete(proj_id)
    return {"deleted": proj_id}


class ConfigUpdate(BaseModel):
    provider: Optional[str] = None
    model: Optional[str] = None
    api_key: Optional[str] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    max_output_tokens: Optional[int] = None
    max_workers: Optional[int] = None
    system_context: Optional[str] = None
    audit_log_enabled: Optional[int] = None
    show_reviewer_breakdown: Optional[int] = None
    raw_api_mode: Optional[int] = None
    raw_api_template: Optional[str] = None
    reasoning_effort: Optional[str] = None

@app.patch("/api/projects/{proj_id}/config")
def update_config(proj_id: str, body: ConfigUpdate):
    if not project_get(proj_id): raise HTTPException(404)
    fields = {k: v for k, v in body.dict().items() if v is not None}
    project_update(proj_id, fields)
    return project_get(proj_id)


# ── File uploads ──────────────────────────────────────────────────────────────

class FileUpload(BaseModel):
    filename: str
    data_b64: str   # base64-encoded file bytes

@app.post("/api/projects/{proj_id}/questions")
def upload_questions(proj_id: str, body: FileUpload):
    if not project_get(proj_id): raise HTTPException(404)
    try:
        data = base64.b64decode(body.data_b64)
        questions, context, names = parse_questions_excel(data)
    except Exception as e:
        raise HTTPException(400, f"Failed to parse questions file: {e}")

    project_update(proj_id, {
        "questions_blob": data,
        "questions_filename": body.filename,
        "questions_json": json.dumps(questions),
        "question_context_json": json.dumps(context),
        "question_names_json": json.dumps(names),
    })
    return {"questions": len(questions), "filename": body.filename}

@app.post("/api/projects/{proj_id}/extractor")
def upload_extractor(proj_id: str, body: FileUpload):
    if not project_get(proj_id): raise HTTPException(404)
    try:
        data = base64.b64decode(body.data_b64)
        fields = parse_extractor_excel(data)
    except Exception as e:
        raise HTTPException(400, f"Failed to parse extractor file: {e}")

    project_update(proj_id, {
        "extractor_blob": data,
        "extractor_filename": body.filename,
        "extractor_json": json.dumps(fields),
    })
    return {"fields": len(fields), "filename": body.filename}

@app.post("/api/projects/{proj_id}/ris")
def upload_ris(proj_id: str, body: FileUpload):
    if not project_get(proj_id): raise HTTPException(404)
    try:
        data = base64.b64decode(body.data_b64)
        content = data.decode("utf-8", errors="replace")
        ris_entries = parse_ris(content)
        bib_rows = ris_to_bib_entries(proj_id, ris_entries)
        bib_clear(proj_id)
        if bib_rows:
            bib_upsert_batch(proj_id, bib_rows)
    except Exception as e:
        raise HTTPException(400, f"Failed to parse RIS file: {e}")

    project_update(proj_id, {
        "ris_raw": content,
        "ris_filename": body.filename,
    })
    return {"entries": len(ris_entries), "filename": body.filename}

@app.post("/api/projects/{proj_id}/extra_context")
def upload_extra_context(proj_id: str, body: FileUpload):
    if not project_get(proj_id): raise HTTPException(404)
    try:
        data = base64.b64decode(body.data_b64)
        name_lower = body.filename.lower()
        if name_lower.endswith(".pdf"):
            import pdfplumber, io as _io
            text_parts = []
            with pdfplumber.open(_io.BytesIO(data)) as pdf:
                for page in pdf.pages:
                    t = page.extract_text()
                    if t:
                        text_parts.append(t)
            text = "\n\n".join(text_parts)
        else:
            text = data.decode("utf-8", errors="replace")
    except Exception as e:
        raise HTTPException(400, f"Failed to read extra context file: {e}")
    project_update(proj_id, {
        "extra_context_text": text,
        "extra_context_filename": body.filename,
    })
    return {"filename": body.filename, "chars": len(text)}

@app.get("/api/projects/{proj_id}/bibliography")
def get_bibliography(proj_id: str):
    if not project_get(proj_id): raise HTTPException(404)
    return bib_list(proj_id)

@app.get("/api/projects/{proj_id}/ai_filenames")
def get_ai_filenames(proj_id: str):
    """Return distinct PDF filenames that already have AI results for this project."""
    rows = ai_results_for_export(proj_id)
    filenames = list({r["pdf_filename"] for r in rows})
    return {"filenames": filenames}


class PdfUploadItem(BaseModel):
    filename: str
    data_b64: str

@app.post("/api/projects/{proj_id}/pdfs", status_code=201)
def upload_project_pdfs(proj_id: str, body: List[PdfUploadItem]):
    if PDF_LOCATION != "server":
        raise HTTPException(400, "PDF server storage is not enabled")
    if not project_get(proj_id):
        raise HTTPException(404)
    proj_pdf_dir = PDF_DIR / proj_id
    proj_pdf_dir.mkdir(parents=True, exist_ok=True)
    saved, failed = [], []
    for item in body:
        safe_name = os.path.basename(item.filename)
        if not safe_name:
            continue
        try:
            data = base64.b64decode(item.data_b64)
            (proj_pdf_dir / safe_name).write_bytes(data)
            saved.append(safe_name)
        except OSError as e:
            # ENAMETOOLONG, disk full, illegal characters — one unwritable file
            # must not take down the rest of the batch
            logger.warning("PDF upload failed for %s in project %s: %s", safe_name, proj_id, e)
            failed.append({"filename": safe_name, "reason": e.strerror or str(e)})
        except Exception as e:
            logger.exception("PDF upload failed for %s in project %s", safe_name, proj_id)
            failed.append({"filename": safe_name, "reason": str(e)})
    return {"saved": len(saved), "failed": failed}

@app.get("/api/projects/{proj_id}/pdfs")
def list_project_pdfs(proj_id: str):
    if PDF_LOCATION != "server":
        return {"filenames": []}
    proj_pdf_dir = PDF_DIR / proj_id
    if not proj_pdf_dir.exists():
        return {"filenames": []}
    return {"filenames": [f.name for f in proj_pdf_dir.iterdir() if f.is_file()]}

@app.delete("/api/projects/{proj_id}/pdfs")
def clear_project_pdfs(proj_id: str):
    if PDF_LOCATION != "server":
        raise HTTPException(400, "PDF server storage is not enabled")
    if not project_get(proj_id):
        raise HTTPException(404)
    proj_pdf_dir = PDF_DIR / proj_id
    if proj_pdf_dir.exists():
        import shutil
        shutil.rmtree(proj_pdf_dir)
    return {"cleared": True}

def _resolve_pdf_by_bib(proj_id: str, safe_name: str):
    """Files renamed by Zotfile are stored under a different name than the one the
    RIS recorded, so an exact lookup misses even though the PDF is present. Fall
    back to the same author+year/DOI matching used for AI results."""
    proj_pdf_dir = PDF_DIR / proj_id
    if not proj_pdf_dir.is_dir():
        return None
    bib = bib_get_by_file(proj_id, safe_name)
    if not bib:
        return None
    candidates = [f.name for f in proj_pdf_dir.iterdir() if f.is_file()]
    match = match_filename_by_bib(bib, candidates)
    return proj_pdf_dir / match if match else None

@app.get("/pdfs/{proj_id}/{filename}")
def serve_project_pdf(proj_id: str, filename: str):
    # Sub-entries of a split paper share the paper's PDF.
    safe_name = os.path.basename(paper_parent(filename))
    path = PDF_DIR / proj_id / safe_name
    try:
        exists = path.exists()
    except OSError:
        # A name the filesystem can't even represent (ENAMETOOLONG) raises here
        # rather than returning False. It can't be on disk, so try the fallback.
        exists = False
    if not exists:
        path = _resolve_pdf_by_bib(proj_id, safe_name)
        if path is None:
            raise HTTPException(404, "PDF not found")
    return FileResponse(str(path), media_type="application/pdf")


# ══════════════════════════════════════════════════════════════════════════════
# AI Runs
# ══════════════════════════════════════════════════════════════════════════════

class PdfPayload(BaseModel):
    filename: str
    text: str

class AIRunCreate(BaseModel):
    name: str
    pdfs: List[PdfPayload]
    test_mode: bool = False
    sample_size: int = 5

@app.get("/api/projects/{proj_id}/runs")
def list_runs(proj_id: str):
    return run_list(proj_id)

@app.post("/api/projects/{proj_id}/runs/ai", status_code=201)
def create_ai_run(proj_id: str, body: AIRunCreate):
    proj = project_get(proj_id)
    if not proj: raise HTTPException(404)

    if not proj.get("questions_json") or proj["questions_json"] == "[]":
        raise HTTPException(400, "Upload a questions file first")
    if not proj.get("raw_api_mode") and not proj.get("api_key"):
        raise HTTPException(400, "Set an API key in project config first")

    import random as rnd
    pdfs = [p.dict() for p in body.pdfs]
    if body.test_mode and len(pdfs) > body.sample_size:
        pdfs = rnd.sample(pdfs, body.sample_size)
    if not pdfs:
        raise HTTPException(400, "Select at least one PDF")

    run_id = str(uuid.uuid4())
    run_create(run_id, proj_id, body.name, "ai", len(pdfs))

    config = {
        "api_key": proj["api_key"],
        "provider": proj["provider"],
        "model": proj["model"],
        "temperature": proj["temperature"],
        "top_p": proj["top_p"],
        "max_output_tokens": proj["max_output_tokens"],
        "max_workers": proj["max_workers"],
        "system_context": proj["system_context"],
        "questions_json": proj["questions_json"],
        "question_context_json": proj["question_context_json"],
        "question_names_json": proj["question_names_json"],
        "extra_context": proj.get("extra_context_text") or "",
        "raw_api_mode": bool(proj.get("raw_api_mode")),
        "raw_api_template": proj.get("raw_api_template") or "",
        "reasoning_effort": proj.get("reasoning_effort") or "",
    }

    ctx = JobContext(run_id, proj_id, _publish)
    _active_jobs[run_id] = ctx

    def _thread():
        try:
            run_ai_job(run_id, proj_id, config, pdfs, ctx)
        except Exception as e:
            ctx.log(f"Unhandled error: {e}")
            run_finish(run_id, "failed")
            _publish(run_id, "done", {"status": "failed"})
        _active_jobs.pop(run_id, None)

    threading.Thread(target=_thread, daemon=True).start()
    return {"run_id": run_id}

@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    run = run_get(run_id)
    if not run: raise HTTPException(404, "Run not found")
    return run

@app.post("/api/runs/{run_id}/cancel")
def cancel_run(run_id: str):
    ctx = _active_jobs.get(run_id)
    if not ctx: raise HTTPException(400, "Run not active")
    ctx.stop.set()
    return {"cancelled": run_id}

@app.delete("/api/runs/{run_id}")
def delete_run(run_id: str):
    if not run_get(run_id): raise HTTPException(404)
    ctx = _active_jobs.get(run_id)
    if ctx: ctx.stop.set()
    run_delete(run_id)
    return {"deleted": run_id}

@app.get("/api/runs/{run_id}/stream")
async def stream_run(run_id: str, token: Optional[str] = None):
    # EventSource cannot send headers, so we accept token as a query param
    if AUTH_MODE == "login" and token:
        token_data = _verify_token(token)
        if not token_data:
            raise HTTPException(401, "Invalid or expired token")
    run = run_get(run_id)
    if not run: raise HTTPException(404)

    async def generator():
        existing = run.get("log_text","")
        if existing:
            yield f"data: {json.dumps({'type':'history','log':existing})}\n\n"
        completed = run.get("completed_count", 0)
        total = run.get("pdf_count", 0)
        if total:
            yield f"data: {json.dumps({'type':'progress','completed':completed,'total':total})}\n\n"

        if run["status"] in ("complete","failed","cancelled"):
            yield f"data: {json.dumps({'type':'done','status':run['status']})}\n\n"
            return

        q = _subscribe(run_id)
        loop = asyncio.get_event_loop()
        try:
            while True:
                try:
                    etype, data = await loop.run_in_executor(None, lambda: q.get(timeout=25))
                    payload = {"type": etype}
                    if isinstance(data, dict): payload.update(data)
                    else: payload["data"] = data
                    yield f"data: {json.dumps(payload)}\n\n"
                    if etype == "done": break
                except queue.Empty:
                    yield f"data: {json.dumps({'type':'ping'})}\n\n"
        finally:
            _unsubscribe(run_id, q)

    return StreamingResponse(generator(), media_type="text/event-stream",
                             headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})


# ══════════════════════════════════════════════════════════════════════════════
# Human Runs
# ══════════════════════════════════════════════════════════════════════════════

def _deciders(run_id: str) -> dict:
    """{pdf_filename: coder} for who settled each paper in a run.

    The reviewer who set the status owns the decision; if nobody did, credit whoever
    edited last so the card still says something useful.
    """
    coders = human_last_coders(run_id)
    coders.update({k: v for k, v in human_decision_coders(run_id).items() if v})
    return coders


def _is_blank_answer(raw) -> bool:
    """True for the several shapes an unanswered field takes in storage."""
    return raw is None or str(raw).strip() in ("", "null", '""', "[]", "{}")


def _carry_over_answers(run: dict, pdf_filename: str) -> int:
    """Copy the carried-over fields from the source run into this one for one paper.

    Runs when a paper is opened rather than only at creation, because a verification
    run follows its source live — papers that qualify later must be seeded too. Only
    fills fields that are still empty here, so it never overwrites the verifier, and
    keeps the original reviewer as the author of the value it copied.
    """
    if (run.get("run_mode") or "clean") != "verification" or not run.get("source_run_id"):
        return 0
    try:
        fields = {int(f) for f in json.loads(run.get("carry_fields_json") or "[]")}
    except (TypeError, ValueError):
        fields = set()
    if not fields:
        return 0

    mine = human_results_for_pdf(run["id"], pdf_filename)          # keyed by int
    source = human_answers_for_run(run["source_run_id"], pdf_filename)
    copied = 0
    for fi in sorted(fields):
        if not _is_blank_answer(mine.get(fi)):   # the verifier already has something here
            continue
        src = source.get(str(fi))
        if not src or _is_blank_answer(src.get("answer")):
            continue
        human_result_save(run["id"], run["project_id"], pdf_filename, fi,
                          src.get("qname", ""), src.get("question_text", ""),
                          src["answer"], coder=src.get("coder", ""))
        copied += 1
    return copied


def _verification_papers(run: dict) -> Optional[set]:
    """Papers a verification run covers: those in its source run that match its filter.

    Returns None for a clean run, so callers fall back to the whole bibliography.
    """
    if (run.get("run_mode") or "clean") != "verification":
        return None
    source = run.get("source_run_id") or ""
    if not source:
        return None
    rows = human_results_for_export(run["project_id"], source)
    try:
        values = json.loads(run.get("filter_values_json") or "[]")
    except (TypeError, ValueError):
        values = []
    field = run.get("filter_field")
    field = None if field is None or int(field) < 0 else int(field)
    question = run.get("filter_question") or ""
    if not values or (field is None and not question):
        # No answer filter: every paper the source run touched
        return {r["pdf_filename"] for r in rows}
    return _papers_passing(rows, None, field, question, values) or set()


class HumanRunCreate(BaseModel):
    name: str
    pdf_filenames: List[str]   # just the filenames — matched against bibliography
    ai_run_ref: Optional[str] = None
    # Verification runs: a second pass over an earlier extraction
    run_mode: str = "clean"                       # clean | verification
    source_run_id: Optional[str] = None
    filter_field: Optional[int] = None
    filter_question: Optional[str] = None
    filter_values: List[str] = []
    show_ai: bool = True
    show_original: bool = False
    carry_fields: List[int] = []   # answers copied from the source run, e.g. group names

@app.post("/api/projects/{proj_id}/runs/human", status_code=201)
def create_human_run(proj_id: str, body: HumanRunCreate):
    proj = project_get(proj_id)
    if not proj: raise HTTPException(404)
    if not proj.get("extractor_json") or proj["extractor_json"] == "[]":
        raise HTTPException(400, "Upload an extractor form file first")

    # Re-parse stored RIS if bibliography is empty (handles projects uploaded
    # before the no-attachment fix, or where RIS had no file path tags)
    existing_bib = bib_list(proj_id)
    if not existing_bib and proj.get("ris_raw"):
        try:
            ris_entries = parse_ris(proj["ris_raw"])
            bib_rows = ris_to_bib_entries(proj_id, ris_entries)
            if bib_rows:
                bib_upsert_batch(proj_id, bib_rows)
                existing_bib = bib_list(proj_id)
        except Exception:
            logger.exception("Failed to rebuild bibliography from stored RIS for project %s", proj_id)

    if not existing_bib:
        raise HTTPException(400, "No bibliography found — upload a RIS file in the Configure tab first")

    run_id = str(uuid.uuid4())
    codeable = [b for b in existing_bib if not b.get("is_container")]
    settings = {}
    total = len(codeable)
    if body.run_mode == "verification":
        if not body.source_run_id:
            raise HTTPException(400, "Pick the extraction run to verify")
        src = run_get(body.source_run_id)
        if not src or src["project_id"] != proj_id:
            raise HTTPException(400, "That extraction run is not part of this project")
        settings = {
            "run_mode": "verification",
            "source_run_id": body.source_run_id,
            "filter_field": -1 if body.filter_field is None else body.filter_field,
            "filter_question": body.filter_question or "",
            "filter_values_json": json.dumps(body.filter_values or []),
            "show_ai": 1 if body.show_ai else 0,
            "show_original": 1 if body.show_original else 0,
            "carry_fields_json": json.dumps(sorted(set(body.carry_fields or []))),
        }
        papers = _verification_papers({**settings, "project_id": proj_id})
        total = len(papers or [])
        if not total:
            raise HTTPException(400, "No papers in that run match the filter")
    else:
        settings = {"show_ai": 1 if body.show_ai else 0, "show_original": 0}

    run_create(run_id, proj_id, body.name, "human", total, ai_run_ref=body.ai_run_ref)
    run_update_settings(run_id, settings)
    return {"run_id": run_id, "pdf_count": total}


class VerificationPreview(BaseModel):
    source_run_id: str
    filter_field: Optional[int] = None
    filter_question: Optional[str] = None
    filter_values: List[str] = []


@app.post("/api/projects/{proj_id}/runs/human/preview")
def preview_verification_run(proj_id: str, body: VerificationPreview):
    """How many papers a verification run would cover, before creating it."""
    if not project_get(proj_id): raise HTTPException(404)
    src = run_get(body.source_run_id)
    if not src or src["project_id"] != proj_id:
        raise HTTPException(400, "That extraction run is not part of this project")
    papers = _verification_papers({
        "run_mode": "verification", "project_id": proj_id,
        "source_run_id": body.source_run_id,
        "filter_field": -1 if body.filter_field is None else body.filter_field,
        "filter_question": body.filter_question or "",
        "filter_values_json": json.dumps(body.filter_values or []),
    }) or set()
    coders = _deciders(body.source_run_id)
    by_coder = {}
    for pdf in papers:
        by_coder[coders.get(pdf) or "(unknown)"] = by_coder.get(coders.get(pdf) or "(unknown)", 0) + 1
    return {"count": len(papers), "by_coder": by_coder}

@app.patch("/api/runs/{run_id}/ai_run_ref")
def update_run_ai_ref(run_id: str, body: dict):
    run = run_get(run_id)
    if not run: raise HTTPException(404)
    run_update_ai_ref(run_id, body.get("ai_run_ref"))
    return {}

@app.get("/api/runs/{run_id}/human/papers")
def get_human_run_papers(run_id: str):
    """Return list of papers in this human run with per-paper progress."""
    run = run_get(run_id)
    if not run: raise HTTPException(404)
    proj_id = run["project_id"]

    # Get papers from DB — stored as a list in a side table? No — we derive from
    # all filenames that have any results, plus the initial list stored at run creation.
    # Simpler: fetch bib entries and cross-reference with run's pdf count.
    # Actually we need the paper list — store it in run log on creation? 
    # Better: store it in a dedicated table. For now, fetch from bib and mark progress.
    bib = bib_list(proj_id)
    progress = human_results_progress(run_id)

    # Also get papers explicitly added that may not be in bib
    from database import get_db
    with get_db() as conn:
        known = conn.execute(
            "SELECT DISTINCT pdf_filename FROM human_results WHERE run_id=?", (run_id,)
        ).fetchall()
        known_names = {r["pdf_filename"] for r in known}

    statuses = human_paper_statuses(run_id)
    last_coders = human_last_coders(run_id)

    # A verification run only covers the papers its source run matched, and each
    # card names whoever made the final call there so verifiers can avoid their own.
    covered = _verification_papers(run)
    dropped: set = set()
    if covered is not None:
        # The list follows the source run live, so a paper can stop matching after
        # someone has verified it. Keep anything already worked on — losing sight of
        # a reviewer's answers is worse than showing a paper that no longer qualifies.
        worked = set(progress) | {k for k, v in statuses.items() if v}
        dropped = worked - covered
        covered = covered | worked
    source_coders = _deciders(run["source_run_id"]) if covered is not None else {}
    source_statuses = human_paper_statuses(run["source_run_id"]) if covered is not None else {}

    papers = []
    for b in bib:
        fn = b["pdf_filename"]
        if covered is not None and fn not in covered:
            known_names.discard(fn)
            continue
        papers.append({**b, "answered": progress.get(fn, 0), "paper_status": statuses.get(fn, ""),
                       "last_coder": last_coders.get(fn, ""),
                       "original_coder": source_coders.get(fn, ""),
                       "original_status": source_statuses.get(fn, ""),
                       "filter_dropped": fn in dropped})
        known_names.discard(fn)
    # Sub-entries of a split paper follow their container in the list
    papers.sort(key=lambda p: (p.get("parent_pdf") or p["pdf_filename"],
                               1 if p.get("parent_pdf") else 0,
                               p.get("entry_order") or 0))

    # Any extra filenames (human results that aren't in bib)
    for fn in known_names:
        if covered is not None and fn not in covered:
            continue
        papers.append({"pdf_filename": fn, "title":"", "authors":"", "doi":"",
                        "answered": progress.get(fn, 0), "paper_status": statuses.get(fn, ""),
                        "last_coder": last_coders.get(fn, ""),
                        "original_coder": source_coders.get(fn, ""),
                        "original_status": source_statuses.get(fn, ""),
                        "filter_dropped": fn in dropped})

    return papers

class SplitEntry(BaseModel):
    key: Optional[str] = None      # existing sub-entry, or None to create one
    label: str = ""


class SplitRequest(BaseModel):
    entries: List[SplitEntry] = []  # wanted state, in order; empty undoes the split


# NOTE: like /log below, this must be declared BEFORE get_paper_form so the greedy
# {pdf_filename:path} converter does not swallow the trailing "/split".
@app.post("/api/runs/{run_id}/human/paper/{pdf_filename:path}/split")
def split_paper_entry(run_id: str, pdf_filename: str, body: SplitRequest):
    """Split a paper that reports several studies into separately coded sub-entries."""
    run = run_get(run_id)
    if not run: raise HTTPException(404)
    proj_id = run["project_id"]
    parent = paper_parent(pdf_filename)
    try:
        subs = bib_apply_split(proj_id, parent,
                               [{"key": e.key, "label": e.label} for e in body.entries])
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"parent": parent, "entries": subs}


@app.get("/api/runs/{run_id}/human/paper/{pdf_filename:path}/split")
def get_paper_entries(run_id: str, pdf_filename: str):
    run = run_get(run_id)
    if not run: raise HTTPException(404)
    return {"parent": paper_parent(pdf_filename),
            "entries": bib_sub_entries(run["project_id"], paper_parent(pdf_filename))}


# NOTE: this must be declared BEFORE get_paper_form — that route's greedy
# {pdf_filename:path} converter would otherwise swallow the trailing "/log"
# and shadow this endpoint (both are GET).
@app.get("/api/runs/{run_id}/human/paper/{pdf_filename:path}/log")
def get_paper_log(run_id: str, pdf_filename: str):
    run = run_get(run_id)
    if not run: raise HTTPException(404)
    proj = project_get(run["project_id"])
    if not proj: raise HTTPException(404)
    if not proj.get("audit_log_enabled"):
        return []
    return audit_log_for_paper(run_id, pdf_filename)

@app.get("/api/runs/{run_id}/human/paper/{pdf_filename:path}")
def get_paper_form(run_id: str, pdf_filename: str):
    """Return form fields, existing human answers, and relevant AI answers for one paper."""
    run = run_get(run_id)
    if not run: raise HTTPException(404)
    proj_id = run["project_id"]

    proj = project_get(proj_id)
    extractor = json.loads(proj.get("extractor_json","[]"))
    bib = bib_get(proj_id, pdf_filename)

    # Seed anything this run carries over from the run it verifies, then read back
    _carry_over_answers(run, pdf_filename)

    # Human answers already saved
    human_answers = human_results_for_pdf(run_id, pdf_filename)

    # AI answers for ShowWith — from referenced run, then any run as fallback.
    # A sub-entry has no AI run of its own; it reads the paper's.
    ai_key = paper_parent(pdf_filename)
    ai_run_ref = run.get("ai_run_ref") or None
    ai_answers = ai_results_for_pdf(proj_id, ai_key, run_id=ai_run_ref)
    if not ai_answers and bib:
        ai_answers = ai_results_for_pdf_by_bib(proj_id, bib, run_id=ai_run_ref)
    # If still nothing, search across all AI runs (handles papers only in test run)
    if not ai_answers:
        ai_answers = ai_results_for_pdf(proj_id, ai_key, run_id=None)
    if not ai_answers and bib:
        ai_answers = ai_results_for_pdf_by_bib(proj_id, bib, run_id=None)

    return {
        "pdf_filename": pdf_filename,
        "bib": bib,
        "extractor_fields": extractor,
        "human_answers": human_answers,   # {field_index_str: answer_json}
        "ai_answers": ai_answers,          # {question_num: answer_text}
        "paper_status": get_paper_status(run_id, pdf_filename),
        # A verification run shows the source run's answers beside each field.
        "original_answers": (human_answers_for_run(run["source_run_id"], pdf_filename)
                             if (run.get("run_mode") == "verification" and run.get("source_run_id")
                                 and run.get("show_original")) else {}),
        "original_coder": (_deciders(run["source_run_id"]).get(pdf_filename, "")
                           if run.get("source_run_id") else ""),
        "entry_label": (bib or {}).get("entry_label", ""),
        "parent_pdf": (bib or {}).get("parent_pdf", ""),
        "is_container": bool((bib or {}).get("is_container")),
        "entries": bib_sub_entries(proj_id, paper_parent(pdf_filename)),
    }


class AnswerSave(BaseModel):
    field_index: int
    qname: str = ""
    question_text: str = ""
    answer: str  # JSON-encoded value
    coder: str = ""


def _shorten_for_log(value, limit: int = 240) -> str:
    text = "" if value is None else str(value).replace("\n", "\\n")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _log_human_save_failure(run_id: str, pdf_filename: str, save_kind: str, payload: dict, reason: str):
    field_index = payload.get("field_index", -1)
    coder = payload.get("coder", "") or "(unknown)"
    qname = payload.get("qname", "") or payload.get("question_text", "") or save_kind
    answer_preview = _shorten_for_log(payload.get("answer", payload.get("status", "")))
    line = (
        f"ERROR: Human {save_kind} save failed | paper={pdf_filename} | field={field_index} | "
        f"coder={coder} | qname={_shorten_for_log(qname, 120)} | answer={answer_preview} | reason={reason}"
    )
    try:
        run_append_log(run_id, line)
    except Exception:
        logger.exception("Failed to append human save failure log for run %s", run_id)

@app.post("/api/runs/{run_id}/human/paper/{pdf_filename:path}/answer")
def save_answer(run_id: str, pdf_filename: str, body: AnswerSave):
    run = run_get(run_id)
    if not run: raise HTTPException(404)
    payload = body.model_dump()
    try:
        proj = project_get(run["project_id"])
        if proj and proj.get("audit_log_enabled"):
            old = human_results_for_pdf(run_id, pdf_filename).get(body.field_index, "")
            audit_log_append(run["project_id"], run_id, pdf_filename,
                             body.field_index, body.qname, old, body.answer, body.coder)
        human_result_save(run_id, run["project_id"], pdf_filename,
                          body.field_index, body.qname, body.question_text, body.answer, body.coder)
        return {"saved": True}
    except HTTPException as exc:
        _log_human_save_failure(run_id, pdf_filename, "answer", payload, str(exc.detail))
        raise
    except Exception:
        logger.exception("Failed to save human answer for run=%s paper=%s field=%s", run_id, pdf_filename, body.field_index)
        _log_human_save_failure(run_id, pdf_filename, "answer", payload, "server error")
        raise HTTPException(500, "Server error while saving answer")

class StatusSave(BaseModel):
    status: str  # 'done' | 'needs_review' | 'incomplete' | ''
    coder: str = ""

@app.patch("/api/runs/{run_id}/human/paper/{pdf_filename:path}/status")
def save_paper_status(run_id: str, pdf_filename: str, body: StatusSave):
    run = run_get(run_id)
    if not run: raise HTTPException(404)
    payload = body.model_dump()
    try:
        proj = project_get(run["project_id"])
        if proj and proj.get("audit_log_enabled"):
            old = get_paper_status(run_id, pdf_filename)
            audit_log_append(run["project_id"], run_id, pdf_filename,
                             -1, "_status", old, body.status, body.coder)
        human_result_save(run_id, run["project_id"], pdf_filename,
                          -1, "_status", "", body.status, body.coder)
        return {"saved": True}
    except HTTPException as exc:
        _log_human_save_failure(run_id, pdf_filename, "status", payload, str(exc.detail))
        raise
    except Exception:
        logger.exception("Failed to save human paper status for run=%s paper=%s", run_id, pdf_filename)
        _log_human_save_failure(run_id, pdf_filename, "status", payload, "server error")
        raise HTTPException(500, "Server error while saving paper status")

class LockAcquire(BaseModel):
    coder: str = ""
    force: bool = False

@app.post("/api/runs/{run_id}/human/paper/{pdf_filename:path}/lock")
def acquire_lock(run_id: str, pdf_filename: str, body: LockAcquire):
    run = run_get(run_id)
    if not run: raise HTTPException(404)
    if body.force:
        paper_lock_release(run_id, pdf_filename, paper_lock_get(run_id, pdf_filename) or "")
    ok, locked_by = paper_lock_acquire(run_id, pdf_filename, body.coder)
    return {"ok": ok, "locked_by": locked_by}

@app.delete("/api/runs/{run_id}/human/paper/{pdf_filename:path}/lock")
def release_lock(run_id: str, pdf_filename: str, coder: str = Query("")):
    run = run_get(run_id)
    if not run: raise HTTPException(404)
    paper_lock_release(run_id, pdf_filename, coder)
    return {}

@app.get("/api/runs/{run_id}/status_summary")
def get_status_summary(run_id: str):
    run = run_get(run_id)
    if not run: raise HTTPException(404)
    statuses = human_paper_statuses(run_id)
    covered = _verification_papers(run)
    if covered is not None:
        # A verification run covers a moving subset, so count it now rather than
        # trusting the number stored when the run was created.
        covered = covered | set(human_results_progress(run_id)) | {k for k, v in statuses.items() if v}
        total = len(covered)
    else:
        # Splitting a paper changes how many entries there are to code, so count the
        # bibliography rather than the number of PDFs the run started with.
        codeable = [b for b in bib_list(run["project_id"]) if not b.get("is_container")]
        total = len(codeable) or run["pdf_count"]
    done         = sum(1 for s in statuses.values() if s == "done")
    needs_review = sum(1 for s in statuses.values() if s == "needs_review")
    incomplete   = sum(1 for s in statuses.values() if s == "incomplete")
    not_started  = total - sum(1 for s in statuses.values() if s)
    return {"total": total, "done": done, "needs_review": needs_review,
            "incomplete": incomplete, "not_started": not_started}

@app.get("/api/runs/{run_id}/status_summary_by_reviewer")
def get_status_summary_by_reviewer(run_id: str):
    run = run_get(run_id)
    if not run: raise HTTPException(404)
    return human_paper_statuses_by_coder(run_id)

@app.get("/api/projects/{proj_id}/audit_log")
def get_audit_log(proj_id: str, run_id: Optional[str] = None):
    if not project_get(proj_id): raise HTTPException(404)
    return audit_log_for_export(proj_id, run_id or None)

@app.post("/api/runs/{run_id}/finish")
def finish_human_run(run_id: str):
    run = run_get(run_id)
    if not run: raise HTTPException(404)
    run_finish(run_id, "complete")
    return {"status": "complete"}


# ══════════════════════════════════════════════════════════════════════════════
# Export
# ══════════════════════════════════════════════════════════════════════════════

_ILLEGAL_CHARS = _re.compile(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]')

def _sanitize_df(df: "pd.DataFrame") -> "pd.DataFrame":
    """Strip characters that openpyxl/Excel rejects from all string columns."""
    for col in df.select_dtypes(include='object').columns:
        df[col] = df[col].apply(
            lambda x: _ILLEGAL_CHARS.sub('', x) if isinstance(x, str) else x
        )
    return df


# Outcome blocks: every field of a block carries the block in its qtype
# ("xPO3_GMeansTable" → PO3), and the block's specifier is "POutcomeSpecifier3".
_X_BLOCK_RE    = _re.compile(r"^x(P|S)O(\d*)_")
_SPEC_BLOCK_RE = _re.compile(r"^(P|S)OutcomeSpecifier(\d*)$")
# The suffix _wide_col_keys appends to a repeated question: " [PO3]", " [#45]", " [PO3 #45]"
_DISAMBIG_RE   = _re.compile(r" \[(?:[PS]O\d*(?: #\d+)?|#\d+)\]$")

def _outcome_block(qtype: str) -> Optional[str]:
    """Return the outcome block a field belongs to ('PO', 'PO3', 'SO2'), or None."""
    m = _X_BLOCK_RE.match(qtype or "") or _SPEC_BLOCK_RE.match(qtype or "")
    return f"{m.group(1)}O{m.group(2)}" if m else None


def _insert_unique(df: "pd.DataFrame", pos: int, name, vals) -> str:
    """Insert a column, suffixing the name if it is taken, so we never raise."""
    final, n = name, 2
    while final in df.columns:
        final = f"{name}_{n}"; n += 1
    df.insert(pos, final, vals)
    return final


def _explode_group_cols(df: "pd.DataFrame") -> "pd.DataFrame":
    """Expand GroupSpecifier and GMeansTable JSON columns into per-group flat columns."""
    def _try_json(v):
        if not isinstance(v, str) or not v.strip(): return None
        try: return json.loads(v)
        except (TypeError, ValueError): return None

    def _is_groupspec(v):
        p = _try_json(v)
        return isinstance(p, list) and bool(p) and isinstance(p[0], dict) and "role" in p[0]

    def _is_gmeans(v):
        p = _try_json(v)
        return isinstance(p, dict) and "timepoints" in p and "data" in p

    gs_col = None
    gmt_cols = []
    for col in df.columns:
        non_null = df[col].dropna()
        if non_null.empty: continue
        sample = non_null.iloc[0]
        if _is_groupspec(sample): gs_col = col
        elif _is_gmeans(sample): gmt_cols.append(col)

    if gs_col is None:
        return df

    def parse_groups(val):
        p = _try_json(val)
        if isinstance(p, list): return {str(g.get("id","")): g for g in p if isinstance(g, dict)}
        return {}

    def main_g(grps, role):
        return next((g for g in grps.values() if g.get("role") == role), None)

    gs_raw = df[gs_col].to_dict()
    gmt_raw = {c: df[c].to_dict() for c in gmt_cols}
    paper_groups = {idx: parse_groups(gs_raw.get(idx,"")) for idx in df.index}

    # Explode GroupSpecifier → IG_Name, IG_N, CG_Name, CG_N
    gs_pos = df.columns.get_loc(gs_col)
    df = df.drop(columns=[gs_col])
    for i, (col_name, role, field) in enumerate([
        ("IG_Name","intervention","name"), ("IG_N","intervention","n"),
        ("CG_Name","control","name"),      ("CG_N","control","n"),
    ]):
        vals = [main_g(paper_groups.get(idx,{}), role) for idx in df.index]
        _insert_unique(df, gs_pos + i, col_name, [g.get(field,"") if g else "" for g in vals])

    # Explode GMeansTable columns
    for gmt_col in gmt_cols:
        orig = gmt_raw[gmt_col]
        all_tps = []
        for v in orig.values():
            p = _try_json(v)
            if isinstance(p, dict):
                for tp in p.get("timepoints", []):
                    if tp not in all_tps: all_tps.append(tp)
        if not all_tps: continue

        gmt_pos = df.columns.get_loc(gmt_col)
        df = df.drop(columns=[gmt_col])

        new_cols = []
        for prefix, role in [("IG","intervention"), ("CG","control")]:
            for tp in all_tps:
                new_cols.append((f"{prefix}_M__{tp}_{gmt_col}",  role, tp, "M"))
                new_cols.append((f"{prefix}_SD__{tp}_{gmt_col}", role, tp, "SD"))

        for insert_i, (col_name, role, tp, stat) in enumerate(new_cols):
            vals = []
            for idx in df.index:
                g = main_g(paper_groups.get(idx,{}), role)
                gid = str(g["id"]) if g else None
                p = _try_json(orig.get(idx,""))
                cell = ""
                if gid and isinstance(p, dict):
                    cell = p.get("data",{}).get(gid,{}).get(f"{tp}_{stat}","")
                vals.append(cell)
            _insert_unique(df, gmt_pos + insert_i, col_name, vals)

    return df


def _explode_cortable_cols(df: "pd.DataFrame") -> "pd.DataFrame":
    """Expand CorTable JSON columns into one column per construct-pair.

    Single value in a cell → bare number.
    Multiple values for the same pair (e.g. two Depression measures) →
    'Label A x Label B: 0.45; Label C x Label B: 0.32'.
    """
    def _try_json(v):
        if not isinstance(v, str) or not v.strip(): return None
        try: return json.loads(v)
        except (TypeError, ValueError): return None

    def _is_cortable(v):
        p = _try_json(v)
        return isinstance(p, dict) and "present" in p and "correlations" in p

    cortable_cols = [
        col for col in df.columns
        if not df[col].dropna().empty and _is_cortable(df[col].dropna().iloc[0])
    ]
    if not cortable_cols:
        return df

    for ct_col in cortable_cols:
        ct_pos = df.columns.get_loc(ct_col)

        # First pass: resolve each row to {construct_pair: [(label1, label2, value)]}
        # and collect the ordered set of all construct pairs seen.
        parsed_rows = {}
        pair_order = []
        seen_pairs: set = set()

        for idx, raw in df[ct_col].items():
            p = _try_json(raw)
            if not isinstance(p, dict):
                parsed_rows[idx] = {}
                continue
            present = p.get("present", [])
            correlations = p.get("correlations", {})
            id_map = {
                item["id"]: item for item in present
                if isinstance(item, dict) and "id" in item
            }
            row_data: dict = {}
            for pair_key, value in correlations.items():
                parts = pair_key.split("|", 1)
                if len(parts) != 2:
                    continue
                item1 = id_map.get(parts[0])
                item2 = id_map.get(parts[1])
                if not item1 or not item2:
                    continue
                cpair = f"{item1['construct']} x {item2['construct']}"
                if cpair not in seen_pairs:
                    seen_pairs.add(cpair)
                    pair_order.append(cpair)
                row_data.setdefault(cpair, []).append(
                    (item1["label"], item2["label"], value)
                )
            parsed_rows[idx] = row_data

        # Second pass: build new columns
        prefix = f"{ct_col}: " if ct_col else ""
        new_cols: dict = {}
        for cpair in pair_order:
            col_name = prefix + cpair
            cells = []
            for idx in df.index:
                entries = parsed_rows.get(idx, {}).get(cpair, [])
                if not entries:
                    cells.append("")
                elif len(entries) == 1:
                    cells.append(str(entries[0][2]))
                else:
                    cells.append("; ".join(
                        f"{l1} x {l2}: {v}" for l1, l2, v in entries
                    ))
            new_cols[col_name] = cells

        df = df.drop(columns=[ct_col])
        for i, (col_name, col_vals) in enumerate(new_cols.items()):
            _insert_unique(df, ct_pos + i, col_name, col_vals)

    return df


def _order_columns(df: "pd.DataFrame", lead, trail=()) -> "pd.DataFrame":
    """Put identity and bibliography columns first and admin columns last.

    Anything not named keeps its order in between, so the answers stay together.
    """
    lead = [c for c in lead if c in df.columns]
    trail = [c for c in trail if c in df.columns]
    named = set(lead) | set(trail)
    middle = [c for c in df.columns if c not in named]
    return df[lead + middle + trail]


def _wide_col_keys(df_h: "pd.DataFrame", proj_id: str):
    """Column keys for the human wide pivot, unique per field.

    Question text is not unique — an extractor repeats the same questions in every
    outcome block. Names used by a single field are left as-is (so existing exports
    keep their headers); a name shared by several fields gets its outcome block
    appended ("Means [PO3]"), falling back to the field index.

    Returns (column name → (outcome block, field index), key per row of df_h).
    """
    proj = project_get(proj_id) or {}
    try:
        extractor = json.loads(proj.get("extractor_json") or "[]")
    except (TypeError, ValueError):
        extractor = []
    # Field indices shift when an extractor is edited, so an answer saved by an older
    # run can sit at an index that now holds a different question. Only trust the
    # block when the stored question still matches the one defined at that index.
    block_by_fi = {}
    for f in extractor:
        if not isinstance(f, dict) or f.get("field_index") is None: continue
        blk = _outcome_block(f.get("qtype") or "")
        if blk: block_by_fi[int(f["field_index"])] = (blk, f.get("question") or "")

    base = df_h["qname"].where(df_h["qname"] != "", "Field_" + df_h["field_index"].astype(str))
    fields = df_h["field_index"].astype(int)
    shared = {b for b, n in fields.groupby(base).nunique().items() if n > 1}

    def block_of(b, fi):
        blk, question = block_by_fi.get(fi, (None, None))
        return blk if blk and question == b else None

    counts = {}
    for b, fi in zip(base, fields):
        counts.setdefault(b, set()).add(fi)

    def key(b, fi):
        if b not in shared: return b
        blk = block_of(b, fi)
        if not blk: return f"{b} [#{fi}]"
        # Two fields of the same block can carry the same question; keep the index too.
        same_block = [i for i in counts[b] if block_of(b, i) == blk]
        return f"{b} [{blk}]" if len(same_block) == 1 else f"{b} [{blk} #{fi}]"

    keys = [key(b, fi) for b, fi in zip(base, fields)]
    block_by_col = {k: (block_of(b, fi), fi) for k, b, fi in zip(keys, base, fields)}
    return block_by_col, keys


_ALL_STATUSES = ("done", "needs_review", "incomplete", "none")


def _answer_choices(raw) -> list:
    """The scalar values a stored answer represents, for matching a filter choice."""
    if raw is None or raw == "":
        return []
    try:
        val = json.loads(raw)
    except (TypeError, ValueError):
        return [str(raw)]
    if isinstance(val, list):
        out = []
        for v in val:
            # Outcome specifiers store a list of {id, name, type} — match on those too,
            # so "outcomes of type X" is a usable filter.
            if isinstance(v, dict):
                out += [str(x) for x in v.values() if not isinstance(x, (dict, list))]
            elif not isinstance(v, list):
                out.append(str(v))
        return out
    if isinstance(val, dict):
        return [str(v) for v in val.values() if not isinstance(v, (dict, list))]
    return [str(val)]


def _papers_passing(hr_rows, statuses, filter_field, filter_question, filter_values):
    """Papers matching the status / answer filters, or None when neither is active."""
    keep = None
    if statuses and set(statuses) != set(_ALL_STATUSES):
        wanted = set(statuses)
        # Status is per run, so a paper counts if any exported run gives it a wanted
        # one; "none" covers a run where the paper was never given a status.
        status_by_pair = {}
        for r in hr_rows:
            pair = (r["pdf_filename"], r.get("run_id"))
            if r["field_index"] == -1 and (r.get("answer") or ""):
                status_by_pair[pair] = r["answer"]
            else:
                status_by_pair.setdefault(pair, "none")
        keep = {pdf for (pdf, _), st in status_by_pair.items() if st in wanted}

    if filter_values and (filter_field is not None or filter_question):
        wanted_vals = set(filter_values)
        matched = set()
        for r in hr_rows:
            # Match on the field index and on the question text, so answers saved
            # before the extractor was last edited are still found.
            hit = (filter_field is not None and r["field_index"] == filter_field) or \
                  (bool(filter_question) and r.get("qname") == filter_question)
            if hit and wanted_vals & set(_answer_choices(r.get("answer"))):
                matched.add(r["pdf_filename"])
        keep = matched if keep is None else (keep & matched)
    return keep


def _run_labels(proj_id: str) -> dict:
    """{run id: display label}, disambiguated when two runs share a name."""
    runs = run_list(proj_id)
    times = {}
    for r in runs:
        times[r["name"]] = times.get(r["name"], 0) + 1
    return {r["id"]: (r["name"] if times[r["name"]] == 1 else f"{r['name']} ({r['id'][:8]})")
            for r in runs}


@app.get("/api/projects/{proj_id}/export")
def export_project(proj_id: str,
                   run_id: Optional[str] = None,
                   run_ids: Optional[List[str]] = Query(None),
                   source: str = "both",
                   fmt: str = "both",
                   include_bib: bool = True,
                   statuses: Optional[List[str]] = Query(None),
                   filter_field: Optional[int] = None,
                   filter_question: Optional[str] = None,
                   filter_values: Optional[List[str]] = Query(None)):
    """Generate and stream an Excel export on demand from the database.

    `run_ids` selects any number of runs (`run_id` stays for older callers) and the wide
    sheets carry one row per paper per run, so no run overwrites another. Papers can be
    narrowed to a set of human statuses and to the answers given to one question.
    """
    proj = project_get(proj_id)
    if not proj: raise HTTPException(404)
    runs = [r for r in (run_ids or ([run_id] if run_id else [])) if r]
    labels = _run_labels(proj_id)
    def run_label(rid): return labels.get(rid, rid or "")

    filters_on = bool(filter_values and (filter_field is not None or filter_question)) \
                 or bool(statuses and set(statuses) != set(_ALL_STATUSES))
    hr_rows = None
    if source in ("human", "both") or filters_on:
        hr_rows = human_results_for_export(proj_id, runs)
    allowed = _papers_passing(hr_rows, statuses, filter_field, filter_question,
                              filter_values) if filters_on else None

    AI_BIB  = ["title","authors","journal","year","doi","abstract"]
    HUM_BIB = ["title","authors","journal","year","doi"]

    output = io.BytesIO()
    sheets_written = 0
    with pd.ExcelWriter(output, engine="openpyxl") as writer:

        if source in ("human", "both"):
            if allowed is not None:
                hr_rows = [r for r in hr_rows if r["pdf_filename"] in allowed]
            if hr_rows:
                df_all = pd.DataFrame(hr_rows)
                df_all.insert(1, "run", df_all["run_id"].map(run_label))
                # A split paper's studies are stored under their own keys; report the
                # paper and the study in separate columns.
                entry = (df_all.pop("entry_label") if "entry_label" in df_all.columns
                         else pd.Series("", index=df_all.index))
                df_all.insert(2, "entry", entry.fillna(""))

                def decode_ans(v):
                    try:
                        val = json.loads(v)
                        # Preserve raw JSON for complex types (GroupSpecifier, GMeansTable) so they can be exploded later
                        if isinstance(val, (dict, list)) and any(isinstance(x, dict) for x in (val if isinstance(val, list) else [val])):
                            return v
                        if isinstance(val, list): return "; ".join(str(x) for x in val)
                        return str(val)
                    except (TypeError, ValueError):
                        return str(v)

                # Build per-paper log and status_log from full data (before filtering sentinels)
                def build_log(grp):
                    edits = grp[grp["field_index"] >= 0]
                    if edits.empty: return ""
                    parts = []
                    for coder, cg in edits.groupby("coder"):
                        if not coder: continue
                        edited = cg["qname"].where(cg["qname"] != "", "Field_" + cg["field_index"].astype(str)).tolist()
                        ts = cg["updated_at"].max()
                        parts.append(f"{coder} edited {', '.join(edited)} on {ts}")
                    return "; ".join(parts)

                def build_status_log(grp):
                    st = grp[grp["field_index"] == -1]
                    if st.empty: return ""
                    row = st.iloc[0]
                    return " — ".join(p for p in [row.get("answer",""), row.get("coder",""), row.get("updated_at","")] if p)

                # Grouped by run as well as paper, so each run keeps its own log.
                log_series = df_all.groupby(["pdf_filename","run_id"]).apply(build_log).rename("log")
                sl_series  = df_all.groupby(["pdf_filename","run_id"]).apply(build_status_log).rename("status_log")

                # Now filter to answer rows only
                df_h = df_all[df_all["field_index"] >= 0].copy()
                df_h["answer"] = df_h["answer"].apply(decode_ans)

                if fmt in ("long", "both"):
                    drop = [] if include_bib else [c for c in HUM_BIB + ["abstract"] if c in df_h.columns]
                    df_long_h = df_h.drop(columns=drop).copy()
                    df_long_h["pdf_filename"] = df_long_h["pdf_filename"].map(paper_parent)
                    df_long_h = _order_columns(
                        df_long_h,
                        ["pdf_filename", "entry", "run"] + [c for c in HUM_BIB if include_bib],
                        ["run_id"])
                    _sanitize_df(df_long_h).to_excel(writer, sheet_name="Human_Long", index=False)
                    sheets_written += 1

                if fmt in ("wide", "both"):
                    # Question names repeat across outcome blocks (every block has a
                    # "Means", a "Describe the measure", ...), so key the pivot on a
                    # name made unique per field — otherwise the blocks collapse into
                    # one column and all but the first are dropped.
                    _blocks, col_keys = _wide_col_keys(df_h, proj_id)
                    df_h["col"] = col_keys
                    df_wide_h = df_h.pivot_table(
                        index=["pdf_filename","run_id"], columns="col", values="answer", aggfunc="first"
                    ).reset_index()
                    pairs = pd.MultiIndex.from_frame(df_wide_h[["pdf_filename","run_id"]])
                    for col in (HUM_BIB if include_bib else []):
                        if col in df_h.columns:
                            df_wide_h[col] = df_h.groupby(["pdf_filename","run_id"])[col].first().reindex(pairs).values
                    df_wide_h["log"]        = log_series.reindex(pairs).values
                    df_wide_h["status_log"] = sl_series.reindex(pairs).values
                    df_wide_h.insert(1, "run", df_wide_h.pop("run_id").map(run_label))
                    entry_by_key = df_h.groupby("pdf_filename")["entry"].first()
                    df_wide_h.insert(1, "entry", df_wide_h["pdf_filename"].map(entry_by_key).fillna(""))
                    df_wide_h["pdf_filename"] = df_wide_h["pdf_filename"].map(paper_parent)
                    df_wide_h = _order_columns(
                        df_wide_h,
                        ["pdf_filename", "entry", "run"] + [c for c in HUM_BIB if include_bib],
                        ["log", "status_log"])
                    df_wide_h = _explode_group_cols(df_wide_h)
                    # Outcome specifiers and xPO_/xSO_ means tables stay as raw JSON:
                    # the exploder gave every control column the intervention values.
                    # See OUTCOME_EXPLODE_NOTES.md before bringing it back.
                    df_wide_h = _explode_cortable_cols(df_wide_h)
                    _sanitize_df(df_wide_h).to_excel(writer, sheet_name="Human_Wide", index=False)
                    sheets_written += 1

        if source in ("ai", "both"):
            ai_rows = ai_results_for_export(proj_id, runs)
            if allowed is not None:
                ai_rows = [r for r in ai_rows if r["pdf_filename"] in allowed]
            if ai_rows:
                df_long = pd.DataFrame(ai_rows)
                df_long.insert(1, "run", df_long["run_id"].map(run_label))
                bib_cols = AI_BIB if include_bib else []
                if fmt in ("long", "both"):
                    drop = [] if include_bib else [c for c in AI_BIB if c in df_long.columns]
                    df_long_ai = _order_columns(df_long.drop(columns=drop),
                                                ["pdf_filename", "run"] + bib_cols, ["run_id"])
                    _sanitize_df(df_long_ai).to_excel(writer, sheet_name="AI_Long", index=False)
                    sheets_written += 1

                if fmt in ("wide", "both"):
                    names = {int(k):v for k,v in json.loads(proj.get("question_names_json","{}")).items()}
                    df_long["Q_num"] = df_long.groupby(["pdf_filename","run_id"]).cumcount() + 1
                    df_wide = df_long.pivot(index=["pdf_filename","run_id"], columns="Q_num",
                                            values="answer").reset_index()
                    pairs = pd.MultiIndex.from_frame(df_wide[["pdf_filename","run_id"]])
                    for col in bib_cols:
                        df_wide[col] = df_long.groupby(["pdf_filename","run_id"])[col].first().reindex(pairs).values
                    df_wide["run"] = df_wide["run_id"].map(run_label)

                    q_cols = sorted(c for c in df_wide.columns if isinstance(c, int))
                    for q in q_cols:
                        df_wide.rename(columns={q: names.get(q, f"Q{q}")}, inplace=True)
                    col_order = ["pdf_filename","run"] + bib_cols + [names.get(q, f"Q{q}") for q in q_cols]
                    df_wide = df_wide[[c for c in col_order if c in df_wide.columns]]
                    _sanitize_df(df_wide).to_excel(writer, sheet_name="AI_Wide", index=False)
                    sheets_written += 1

        if proj.get("audit_log_enabled"):
            audit_rows = audit_log_for_export(proj_id, runs)
            if allowed is not None:
                audit_rows = [r for r in audit_rows if r.get("pdf_filename") in allowed]
            if audit_rows:
                _sanitize_df(pd.DataFrame(audit_rows)).to_excel(writer, sheet_name="Audit_Log", index=False)
                sheets_written += 1

        if sheets_written == 0:
            # Prevent openpyxl crash on empty workbook; error returned below
            pd.DataFrame({"message": ["No data found for the selected filters."]}).to_excel(writer, sheet_name="No Data", index=False)

    if sheets_written == 0:
        raise HTTPException(404, "No data found for the selected runs, sources and filters")

    output.seek(0)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"HarvesterAI_Export_{ts}.xlsx"
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'}
    )


# ══════════════════════════════════════════════════════════════════════════════
# Backup / Restore
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/projects/{proj_id}/backup")
def backup_project(proj_id: str):
    """Download a full project backup as a JSON file."""
    if not project_get(proj_id): raise HTTPException(404)
    data = project_backup(proj_id)
    proj_name = data["project"]["name"].replace(" ", "_")[:30]
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"HarvesterAI_Backup_{proj_name}_{ts}.json"
    content = json.dumps(data, ensure_ascii=False, indent=2).encode()
    return StreamingResponse(
        iter([content]),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )

# ══════════════════════════════════════════════════════════════════════════════
# Users
# ══════════════════════════════════════════════════════════════════════════════

class UserCreate(BaseModel):
    name: str
    password: Optional[str] = None
    role: Optional[str] = None

class RoleUpdate(BaseModel):
    role: str

@app.get("/api/users")
def list_users():
    if AUTH_MODE == "login":
        return user_list_with_roles()
    return user_list()

@app.post("/api/users", status_code=201)
def create_user(body: UserCreate):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Name required")
    if AUTH_MODE == "login":
        # Middleware already ensures only G_ADMIN reaches here
        if not body.password:
            raise HTTPException(400, "Password required")
        target_role = (body.role or G_USER).lower()
        if target_role not in (G_ADMIN, G_USER):
            raise HTTPException(400, "Invalid role — must be 'admin' or 'user'")
        user_add_with_password(name, _hash_password(body.password), role=target_role)
    else:
        user_add(name)
    return {"name": name}

@app.patch("/api/users/{username}/role")
def update_user_role(username: str, body: RoleUpdate):
    if AUTH_MODE != "login":
        raise HTTPException(400, "Not in login mode")
    # Middleware already ensures only G_ADMIN reaches here
    target_role = body.role.lower()
    if target_role not in (G_ADMIN, G_USER):
        raise HTTPException(400, "Invalid role — must be 'admin' or 'user'")
    if not user_get_by_name(username):
        raise HTTPException(404, "User not found")
    user_set_role(username, target_role)
    return {"name": username, "role": target_role}

class PasswordReset(BaseModel):
    password: str

@app.patch("/api/users/{username}/password")
def reset_user_password(username: str, body: PasswordReset):
    if AUTH_MODE != "login":
        raise HTTPException(400, "Not in login mode")
    if not body.password:
        raise HTTPException(400, "Password required")
    if not user_get_by_name(username):
        raise HTTPException(404, "User not found")
    user_set_password(username, _hash_password(body.password))
    return {"name": username, "password_reset": True}

@app.delete("/api/users/{username}")
def delete_user_account(username: str, request: Request):
    if AUTH_MODE != "login":
        raise HTTPException(400, "Not in login mode")
    # Middleware already ensures only G_ADMIN reaches here
    requester_name = getattr(request.state, "current_user", "")
    if requester_name == username:
        raise HTTPException(400, "Cannot delete your own account")
    if not user_get_by_name(username):
        raise HTTPException(404, "User not found")
    user_delete(username)
    return {"deleted": username}


# ══════════════════════════════════════════════════════════════════════════════
# Project Members
# ══════════════════════════════════════════════════════════════════════════════

class ProjectMemberAdd(BaseModel):
    username: str
    project_role: str = P_MEMBER

class ProjectMemberRoleUpdate(BaseModel):
    project_role: str

@app.get("/api/projects/{proj_id}/members")
def list_project_members(proj_id: str):
    if not project_get(proj_id): raise HTTPException(404)
    return project_members_list(proj_id)

@app.post("/api/projects/{proj_id}/members", status_code=201)
def add_project_member(proj_id: str, body: ProjectMemberAdd, request: Request):
    if not project_get(proj_id): raise HTTPException(404)
    target_role = body.project_role.lower()
    if target_role not in (P_OWNER, P_ADMIN, P_MEMBER):
        raise HTTPException(400, "Invalid project role")
    proj_role = getattr(request.state, "project_role", None)
    global_role = getattr(request.state, "global_role", G_USER)
    # Admin (not owner) cannot elevate someone to owner
    if proj_role == P_ADMIN and target_role == P_OWNER and global_role != G_ADMIN:
        raise HTTPException(403, "Only project owners can assign the owner role")
    if not user_get_by_name(body.username):
        raise HTTPException(404, "User not found")
    project_member_add(proj_id, body.username, target_role)
    return {"project_id": proj_id, "username": body.username, "project_role": target_role}

@app.patch("/api/projects/{proj_id}/members/{username}/role")
def update_project_member_role(proj_id: str, username: str, body: ProjectMemberRoleUpdate, request: Request):
    if not project_get(proj_id): raise HTTPException(404)
    target_role = body.project_role.lower()
    if target_role not in (P_OWNER, P_ADMIN, P_MEMBER):
        raise HTTPException(400, "Invalid project role")
    proj_role = getattr(request.state, "project_role", None)
    global_role = getattr(request.state, "global_role", G_USER)
    current_member_role = get_project_role(proj_id, username)
    if not current_member_role:
        raise HTTPException(404, "Member not found")
    # Admin cannot touch owners (unless global admin)
    if proj_role == P_ADMIN and global_role != G_ADMIN:
        if current_member_role == P_OWNER or target_role == P_OWNER:
            raise HTTPException(403, "Only project owners can manage owner roles")
    owner_count = sum(1 for m in project_members_list(proj_id) if m["project_role"] == P_OWNER)
    if current_member_role == P_OWNER and target_role != P_OWNER and owner_count <= 1:
        raise HTTPException(400, "Project must keep at least one owner")
    project_member_set_role(proj_id, username, target_role)
    return {"project_id": proj_id, "username": username, "project_role": target_role}

@app.delete("/api/projects/{proj_id}/members/{username}")
def remove_project_member(proj_id: str, username: str, request: Request):
    if not project_get(proj_id): raise HTTPException(404)
    proj_role = getattr(request.state, "project_role", None)
    global_role = getattr(request.state, "global_role", G_USER)
    current_member_role = get_project_role(proj_id, username)
    if not current_member_role:
        raise HTTPException(404, "Member not found")
    # Admin cannot remove owners (unless global admin)
    if proj_role == P_ADMIN and global_role != G_ADMIN and current_member_role == P_OWNER:
        raise HTTPException(403, "Only project owners can remove other owners")
    owner_count = sum(1 for m in project_members_list(proj_id) if m["project_role"] == P_OWNER)
    if current_member_role == P_OWNER and owner_count <= 1:
        raise HTTPException(400, "Project must keep at least one owner")
    project_member_remove(proj_id, username)
    return {"removed": username}


@app.post("/api/projects/import", status_code=201)
def import_project(body: dict, request: Request):
    """Create a new project from a backup JSON."""
    if body.get("version") != 1 or "project" not in body:
        raise HTTPException(400, "Invalid backup file")
    new_id = str(uuid.uuid4())
    try:
        project_restore(body, new_id)
        if AUTH_MODE == "login":
            username = getattr(request.state, "current_user", "")
            if username:
                project_member_add(new_id, username, P_OWNER)
    except Exception as e:
        raise HTTPException(400, f"Import failed: {e}")
    return project_get(new_id)


# ── Serve frontend ────────────────────────────────────────────────────────────
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
