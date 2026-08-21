"""
HarvesterAI - Database layer (SQLite + WAL mode)
"""
import os
import sqlite3
import sys
import logging
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

# Resolve a writable data directory:
#   1. HARVESTERAI_DATA_DIR env var (Docker / custom server installs)
#   2. sys.frozen (PyInstaller EXE) → AppData\Local\HarvesterAI
#   3. Dev / bare-metal → same directory as this file
if os.environ.get("HARVESTERAI_DATA_DIR"):
    _data_dir = Path(os.environ["HARVESTERAI_DATA_DIR"])
elif getattr(sys, "frozen", False):
    _data_dir = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "HarvesterAI"
else:
    _data_dir = Path(__file__).parent

_data_dir.mkdir(parents=True, exist_ok=True)
DB_PATH = _data_dir / "harvesterai.db"
logger = logging.getLogger(__name__)

@contextmanager
def get_db():
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS projects (
            id                    TEXT PRIMARY KEY,
            name                  TEXT NOT NULL,
            description           TEXT DEFAULT '',
            created_at            TEXT NOT NULL,
            updated_at            TEXT,
            questions_blob        BLOB,
            questions_filename    TEXT DEFAULT '',
            questions_json        TEXT DEFAULT '[]',
            question_context_json TEXT DEFAULT '{}',
            question_names_json   TEXT DEFAULT '{}',
            extractor_blob        BLOB,
            extractor_filename    TEXT DEFAULT '',
            extractor_json        TEXT DEFAULT '[]',
            ris_raw               TEXT DEFAULT '',
            ris_filename          TEXT DEFAULT '',
            provider              TEXT DEFAULT 'deepseek',
            model                 TEXT DEFAULT 'deepseek-chat',
            api_key               TEXT DEFAULT '',
            temperature           REAL DEFAULT 0.2,
            top_p                 REAL DEFAULT 0.95,
            max_output_tokens     INTEGER DEFAULT 8000,
            max_workers           INTEGER DEFAULT 5,
            system_context        TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS runs (
            id              TEXT PRIMARY KEY,
            project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            name            TEXT NOT NULL,
            type            TEXT DEFAULT 'ai',
            status          TEXT DEFAULT 'pending',
            created_at      TEXT NOT NULL,
            updated_at      TEXT,
            pdf_count       INTEGER DEFAULT 0,
            completed_count INTEGER DEFAULT 0,
            failed_count    INTEGER DEFAULT 0,
            log_text        TEXT DEFAULT '',
            ai_run_ref      TEXT DEFAULT NULL
        );

        CREATE TABLE IF NOT EXISTS ai_results (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id        TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            project_id    TEXT NOT NULL,
            pdf_filename  TEXT NOT NULL,
            question_num  INTEGER NOT NULL,
            question_text TEXT DEFAULT '',
            qname         TEXT DEFAULT '',
            answer        TEXT DEFAULT '',
            created_at    TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_ai_run  ON ai_results(run_id);
        CREATE INDEX IF NOT EXISTS idx_ai_pdf  ON ai_results(project_id, pdf_filename);

        CREATE TABLE IF NOT EXISTS human_results (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id        TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            project_id    TEXT NOT NULL,
            pdf_filename  TEXT NOT NULL,
            field_index   INTEGER NOT NULL,
            qname         TEXT DEFAULT '',
            question_text TEXT DEFAULT '',
            answer        TEXT DEFAULT '',
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL,
            UNIQUE(run_id, pdf_filename, field_index) ON CONFLICT REPLACE
        );
        CREATE INDEX IF NOT EXISTS idx_hr_run ON human_results(run_id);
        CREATE INDEX IF NOT EXISTS idx_hr_pdf ON human_results(project_id, pdf_filename);

        CREATE TABLE IF NOT EXISTS bibliography (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id   TEXT NOT NULL,
            pdf_filename TEXT NOT NULL,
            title        TEXT DEFAULT '',
            authors      TEXT DEFAULT '',
            journal      TEXT DEFAULT '',
            year         TEXT DEFAULT '',
            doi          TEXT DEFAULT '',
            abstract     TEXT DEFAULT '',
            pdf_rel_path TEXT DEFAULT '',
            UNIQUE(project_id, pdf_filename) ON CONFLICT REPLACE
        );
        CREATE INDEX IF NOT EXISTS idx_bib ON bibliography(project_id, pdf_filename);

        CREATE TABLE IF NOT EXISTS users (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS project_members (
            project_id   TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            username     TEXT NOT NULL,
            project_role TEXT NOT NULL DEFAULT 'member',
            added_at     TEXT NOT NULL,
            PRIMARY KEY (project_id, username)
        );
        CREATE INDEX IF NOT EXISTS idx_pm ON project_members(project_id);
        CREATE INDEX IF NOT EXISTS idx_pm_user ON project_members(username);

        CREATE TABLE IF NOT EXISTS audit_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id   TEXT NOT NULL,
            run_id       TEXT NOT NULL,
            pdf_filename TEXT NOT NULL,
            field_index  INTEGER NOT NULL,
            qname        TEXT DEFAULT '',
            old_answer   TEXT DEFAULT '',
            new_answer   TEXT DEFAULT '',
            coder        TEXT DEFAULT '',
            changed_at   TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_audit ON audit_log(project_id, run_id);
        """)
        conn.commit()

    def _run_migration(sql: str, desc: str, ignore_duplicate_column: bool = False):
        with get_db() as conn:
            try:
                conn.execute(sql)
                conn.commit()
            except sqlite3.OperationalError as exc:
                msg = str(exc).lower()
                if ignore_duplicate_column and "duplicate column name" in msg:
                    logger.debug("Skipping migration %s: %s", desc, exc)
                    return
                logger.exception("Database migration failed: %s", desc)
                raise
            except Exception:
                logger.exception("Database migration failed: %s", desc)
                raise

    _run_migration(
        "ALTER TABLE projects ADD COLUMN audit_log_enabled INTEGER DEFAULT 0",
        "add projects.audit_log_enabled",
        ignore_duplicate_column=True,
    )
    _run_migration(
        "ALTER TABLE human_results ADD COLUMN coder TEXT DEFAULT ''",
        "add human_results.coder",
        ignore_duplicate_column=True,
    )
    _run_migration(
        "ALTER TABLE bibliography ADD COLUMN pdf_rel_path TEXT DEFAULT ''",
        "add bibliography.pdf_rel_path",
        ignore_duplicate_column=True,
    )
    _run_migration(
        "ALTER TABLE users ADD COLUMN password_hash TEXT DEFAULT NULL",
        "add users.password_hash",
        ignore_duplicate_column=True,
    )
    _run_migration(
        "ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'user'",
        "add users.role",
        ignore_duplicate_column=True,
    )
    _run_migration(
        "UPDATE users SET role='user' WHERE role NOT IN ('admin', 'user')",
        "normalize users.role values",
    )
    _run_migration(
        "ALTER TABLE projects ADD COLUMN show_reviewer_breakdown INTEGER DEFAULT 0",
        "add projects.show_reviewer_breakdown",
        ignore_duplicate_column=True,
    )
    _run_migration(
        "ALTER TABLE projects ADD COLUMN extra_context_text TEXT DEFAULT ''",
        "add projects.extra_context_text",
        ignore_duplicate_column=True,
    )
    _run_migration(
        "ALTER TABLE projects ADD COLUMN extra_context_filename TEXT DEFAULT ''",
        "add projects.extra_context_filename",
        ignore_duplicate_column=True,
    )
    _run_migration(
        "ALTER TABLE projects ADD COLUMN raw_api_mode INTEGER DEFAULT 0",
        "add projects.raw_api_mode",
        ignore_duplicate_column=True,
    )
    _run_migration(
        "ALTER TABLE projects ADD COLUMN raw_api_template TEXT DEFAULT ''",
        "add projects.raw_api_template",
        ignore_duplicate_column=True,
    )

    # Migration: add paper_locks table if not present
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS paper_locks (
                run_id       TEXT NOT NULL,
                pdf_filename TEXT NOT NULL,
                coder        TEXT NOT NULL,
                locked_at    TEXT NOT NULL,
                PRIMARY KEY (run_id, pdf_filename)
            )
        """)
        conn.commit()


DEFAULT_SYSTEM_CONTEXT = (
    "You are an expert assistant helping extract data for a systematic review. "
    "Prioritise accuracy and report data exactly as it appears in the text. "
    "Only use the provided PDF as your source. If information is absent, say so — "
    "never provide illustrative or fabricated data."
)

# ── Projects ──────────────────────────────────────────────────────────────────

def project_create(proj_id, name, description=""):
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO projects (id, name, description, created_at, system_context) VALUES (?,?,?,?,?)",
            (proj_id, name, description, now, DEFAULT_SYSTEM_CONTEXT)
        )
        conn.commit()

def project_update(proj_id, fields: dict):
    if not fields:
        return
    fields["updated_at"] = datetime.utcnow().isoformat()
    cols = ", ".join(f"{k}=?" for k in fields)
    with get_db() as conn:
        conn.execute(f"UPDATE projects SET {cols} WHERE id=?", list(fields.values()) + [proj_id])
        conn.commit()

def project_get(proj_id, include_blobs=False):
    with get_db() as conn:
        if include_blobs:
            row = conn.execute("SELECT * FROM projects WHERE id=?", (proj_id,)).fetchone()
        else:
            row = conn.execute("""
                SELECT id, name, description, created_at, updated_at,
                       questions_filename, extractor_filename, ris_filename,
                       provider, model, api_key, temperature, top_p,
                       max_output_tokens, max_workers, system_context,
                       questions_json, question_context_json, question_names_json,
                       extractor_json, ris_raw, audit_log_enabled, show_reviewer_breakdown,
                       extra_context_text, extra_context_filename,
                       raw_api_mode, raw_api_template
                FROM projects WHERE id=?""", (proj_id,)).fetchone()
        return dict(row) if row else None

def project_list():
    with get_db() as conn:
        rows = conn.execute("""
            SELECT p.id, p.name, p.description, p.created_at, p.updated_at,
                   p.provider, p.model, p.questions_filename, p.extractor_filename, p.ris_filename,
                   (SELECT COUNT(*) FROM runs r WHERE r.project_id=p.id) as run_count,
                   (SELECT COUNT(DISTINCT pdf_filename) FROM ai_results a WHERE a.project_id=p.id) as ai_pdfs,
                   (SELECT COUNT(DISTINCT pdf_filename) FROM human_results h WHERE h.project_id=p.id) as human_pdfs
            FROM projects p ORDER BY p.created_at DESC
        """).fetchall()
        return [dict(r) for r in rows]

def project_delete(proj_id):
    with get_db() as conn:
        conn.execute("DELETE FROM projects WHERE id=?", (proj_id,))
        conn.commit()


# ── Runs ──────────────────────────────────────────────────────────────────────

def run_create(run_id, project_id, name, run_type, pdf_count, ai_run_ref=None):
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO runs (id, project_id, name, type, status, created_at, pdf_count, ai_run_ref) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (run_id, project_id, name, run_type, "running", now, pdf_count, ai_run_ref)
        )
        conn.commit()

def run_append_log(run_id, line):
    with get_db() as conn:
        conn.execute("UPDATE runs SET log_text = log_text || ?, updated_at=? WHERE id=?",
                     (line + "\n", datetime.utcnow().isoformat(), run_id))
        conn.commit()

def run_update_ai_ref(run_id, ai_run_ref):
    with get_db() as conn:
        conn.execute("UPDATE runs SET ai_run_ref=? WHERE id=?", (ai_run_ref, run_id))
        conn.commit()

def run_update_progress(run_id, completed):
    with get_db() as conn:
        conn.execute("UPDATE runs SET completed_count=?, updated_at=? WHERE id=?",
                     (completed, datetime.utcnow().isoformat(), run_id))
        conn.commit()

def run_finish(run_id, status, failed_count=0):
    with get_db() as conn:
        conn.execute("UPDATE runs SET status=?, failed_count=?, updated_at=? WHERE id=?",
                     (status, failed_count, datetime.utcnow().isoformat(), run_id))
        conn.commit()

def run_get(run_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return dict(row) if row else None

def run_list(project_id):
    with get_db() as conn:
        rows = conn.execute("""
            SELECT *,
              (SELECT COUNT(*) FROM human_results h
               WHERE h.run_id=runs.id AND h.field_index=-1 AND h.answer='done') as done_count
            FROM runs WHERE project_id=? ORDER BY created_at DESC
        """, (project_id,)).fetchall()
        return [dict(r) for r in rows]

def run_delete(run_id):
    with get_db() as conn:
        conn.execute("DELETE FROM runs WHERE id=?", (run_id,))
        conn.commit()


# ── AI Results ────────────────────────────────────────────────────────────────

def ai_results_insert_batch(rows: list):
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        conn.executemany(
            "INSERT INTO ai_results (run_id, project_id, pdf_filename, question_num, "
            "question_text, qname, answer, created_at) VALUES (?,?,?,?,?,?,?,?)",
            [(r["run_id"], r["project_id"], r["pdf_filename"], r["question_num"],
              r.get("question_text",""), r.get("qname",""), r.get("answer",""), now)
             for r in rows]
        )
        conn.commit()

def match_filename_by_bib(bib, filenames):
    """Pick the one filename in `filenames` that belongs to bibliography entry `bib`.
    Handles Zotfile-renamed files (e.g. 'Smith-2023-Title.pdf') vs RIS storage names.
    Returns None unless exactly one candidate matches, so an ambiguous guess is
    never served in place of the real paper."""
    import re
    # Strategy 1: first author last name + year (Zotfile: "Smith-2023-Title.pdf")
    authors = bib.get("authors", "")
    year = bib.get("year", "")
    if authors and year:
        raw_lastname = authors.split(';')[0].split(',')[0].strip()
        lastname = re.sub(r'[^a-z]', '', raw_lastname.lower())
        if lastname:
            matches = [fn for fn in filenames
                       if lastname in re.sub(r'[^a-z]', '', fn.lower()) and year in fn]
            if len(matches) == 1:
                return matches[0]

    # Strategy 2: DOI suffix in filename (e.g. filename IS the DOI article number)
    doi = bib.get("doi", "")
    if doi:
        doi_suffix = doi.split('/')[-1].lower()
        if len(doi_suffix) > 4:
            matches = [fn for fn in filenames if doi_suffix in fn.lower()]
            if len(matches) == 1:
                return matches[0]

    return None

def ai_results_for_pdf_by_bib(project_id, bib, run_id=None):
    """Fallback: find AI results using author+year or DOI when filenames don't match."""
    with get_db() as conn:
        if run_id:
            rows = conn.execute(
                "SELECT DISTINCT pdf_filename FROM ai_results WHERE run_id=?", (run_id,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT DISTINCT pdf_filename FROM ai_results WHERE project_id=?", (project_id,)
            ).fetchall()
    filenames = [r["pdf_filename"] for r in rows]

    match = match_filename_by_bib(bib, filenames)
    return ai_results_for_pdf(project_id, match, run_id=run_id) if match else {}

def ai_results_for_pdf(project_id, pdf_filename, run_id=None):
    """Return {question_num: answer} for one PDF."""
    with get_db() as conn:
        if run_id:
            rows = conn.execute(
                "SELECT question_num, answer FROM ai_results WHERE run_id=? AND pdf_filename=?",
                (run_id, pdf_filename)
            ).fetchall()
        else:
            # Most recent AI run that has this pdf
            rows = conn.execute("""
                SELECT a.question_num, a.answer FROM ai_results a
                WHERE a.project_id=? AND a.pdf_filename=?
                  AND a.run_id=(
                      SELECT r.id FROM runs r
                      JOIN ai_results a2 ON a2.run_id=r.id
                      WHERE r.project_id=? AND a2.pdf_filename=?
                      ORDER BY r.created_at DESC LIMIT 1)
            """, (project_id, pdf_filename, project_id, pdf_filename)).fetchall()
        return {r["question_num"]: r["answer"] for r in rows}

def _run_filter(col, run_id):
    """SQL fragment + params restricting `col` to one run id or a list of them."""
    if not run_id:
        return "", []
    ids = [run_id] if isinstance(run_id, str) else [r for r in run_id if r]
    if not ids:
        return "", []
    if len(ids) == 1:
        return f" AND {col}=?", [ids[0]]
    return f" AND {col} IN ({','.join('?' * len(ids))})", list(ids)


def ai_results_for_export(project_id, run_id=None):
    cond = "a.project_id=?"
    params = [project_id]
    frag, fp = _run_filter("a.run_id", run_id)
    cond += frag; params += fp
    with get_db() as conn:
        rows = conn.execute(f"""
            SELECT a.run_id, a.pdf_filename, a.question_num, a.question_text, a.qname, a.answer,
                   b.title, b.authors, b.journal, b.year, b.doi, b.abstract
            FROM ai_results a
            LEFT JOIN bibliography b ON b.project_id=a.project_id AND b.pdf_filename=a.pdf_filename
            WHERE {cond} ORDER BY a.pdf_filename, a.question_num
        """, params).fetchall()
    return [dict(r) for r in rows]


# ── Human Results ─────────────────────────────────────────────────────────────

def human_result_save(run_id, project_id, pdf_filename, field_index, qname, question_text, answer_json, coder=""):
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        conn.execute("""
            INSERT INTO human_results
              (run_id, project_id, pdf_filename, field_index, qname, question_text, answer, coder, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(run_id, pdf_filename, field_index) DO UPDATE SET
              answer=excluded.answer, coder=excluded.coder, updated_at=excluded.updated_at
        """, (run_id, project_id, pdf_filename, field_index, qname, question_text, answer_json, coder, now, now))
        conn.commit()

def human_results_for_pdf(run_id, pdf_filename):
    """Return {field_index: answer_json} for one paper (excludes status sentinel)."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT field_index, answer FROM human_results WHERE run_id=? AND pdf_filename=? AND field_index >= 0",
            (run_id, pdf_filename)
        ).fetchall()
        return {r["field_index"]: r["answer"] for r in rows}

def human_results_progress(run_id):
    """Return {pdf_filename: answered_count} (excludes status sentinel row)."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT pdf_filename, COUNT(*) as cnt FROM human_results "
            "WHERE run_id=? AND field_index >= 0 AND answer != '' AND answer NOT IN ('null','\"\"','[]') "
            "GROUP BY pdf_filename",
            (run_id,)
        ).fetchall()
        return {r["pdf_filename"]: r["cnt"] for r in rows}

def human_last_coders(run_id):
    """Return {pdf_filename: coder} for the most recent edit on each paper."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT pdf_filename, coder FROM human_results "
            "WHERE run_id=? AND field_index >= 0 AND answer != '' "
            "  AND updated_at = ("
            "    SELECT MAX(hr2.updated_at) FROM human_results hr2 "
            "    WHERE hr2.run_id = human_results.run_id AND hr2.pdf_filename = human_results.pdf_filename "
            "    AND hr2.field_index >= 0 AND hr2.answer != '') "
            "GROUP BY pdf_filename",
            (run_id,)
        ).fetchall()
    return {r["pdf_filename"]: r["coder"] for r in rows}

def human_paper_statuses(run_id):
    """Return {pdf_filename: status_str} for papers that have a status set."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT pdf_filename, answer FROM human_results WHERE run_id=? AND field_index=-1",
            (run_id,)
        ).fetchall()
    return {r["pdf_filename"]: r["answer"] for r in rows}

def human_paper_statuses_by_coder(run_id):
    """Return {coder: {done, needs_review, incomplete}} for a run."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT coder, answer FROM human_results WHERE run_id=? AND field_index=-1 AND answer != ''",
            (run_id,)
        ).fetchall()
    result = {}
    for r in rows:
        coder = r["coder"] or "(unassigned)"
        if coder not in result:
            result[coder] = {"done": 0, "needs_review": 0, "incomplete": 0}
        if r["answer"] in result[coder]:
            result[coder][r["answer"]] += 1
    return result

def get_paper_status(run_id, pdf_filename):
    """Return the status string for one paper, or '' if not set."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT answer FROM human_results WHERE run_id=? AND pdf_filename=? AND field_index=-1",
            (run_id, pdf_filename)
        ).fetchone()
    return row["answer"] if row else ""

LOCK_TTL = 90  # seconds

def paper_lock_acquire(run_id, pdf_filename, coder):
    """Try to acquire or refresh a lock. Returns (ok, locked_by)."""
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        row = conn.execute(
            "SELECT coder, locked_at FROM paper_locks WHERE run_id=? AND pdf_filename=?",
            (run_id, pdf_filename)
        ).fetchone()
        if row:
            age = (datetime.utcnow() - datetime.fromisoformat(row["locked_at"])).total_seconds()
            if row["coder"] == coder or age > LOCK_TTL:
                conn.execute(
                    "UPDATE paper_locks SET coder=?, locked_at=? WHERE run_id=? AND pdf_filename=?",
                    (coder, now, run_id, pdf_filename)
                )
                conn.commit()
                return (True, coder)
            else:
                return (False, row["coder"])
        conn.execute(
            "INSERT INTO paper_locks(run_id, pdf_filename, coder, locked_at) VALUES(?,?,?,?)",
            (run_id, pdf_filename, coder, now)
        )
        conn.commit()
        return (True, coder)

def paper_lock_release(run_id, pdf_filename, coder):
    """Release a lock if held by coder."""
    with get_db() as conn:
        conn.execute(
            "DELETE FROM paper_locks WHERE run_id=? AND pdf_filename=? AND coder=?",
            (run_id, pdf_filename, coder)
        )
        conn.commit()

def paper_lock_get(run_id, pdf_filename):
    """Return current lock holder, or None if unlocked/expired."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT coder, locked_at FROM paper_locks WHERE run_id=? AND pdf_filename=?",
            (run_id, pdf_filename)
        ).fetchone()
        if not row:
            return None
        age = (datetime.utcnow() - datetime.fromisoformat(row["locked_at"])).total_seconds()
        return None if age > LOCK_TTL else row["coder"]


def human_results_for_export(project_id, run_id=None):
    cond = "h.project_id=?"
    params = [project_id]
    frag, fp = _run_filter("h.run_id", run_id)
    cond += frag; params += fp
    with get_db() as conn:
        rows = conn.execute(f"""
            SELECT h.run_id, h.pdf_filename, h.field_index, h.qname, h.question_text, h.answer,
                   h.coder, h.updated_at,
                   b.title, b.authors, b.journal, b.year, b.doi, b.abstract
            FROM human_results h
            LEFT JOIN bibliography b ON b.project_id=h.project_id AND b.pdf_filename=h.pdf_filename
            WHERE {cond} ORDER BY h.pdf_filename, h.field_index
        """, params).fetchall()
    return [dict(r) for r in rows]


# ── Bibliography ──────────────────────────────────────────────────────────────

def bib_clear(project_id):
    with get_db() as conn:
        conn.execute("DELETE FROM bibliography WHERE project_id=?", (project_id,))
        conn.commit()

def bib_upsert_batch(project_id, entries: list):
    with get_db() as conn:
        conn.executemany(
            "INSERT INTO bibliography (project_id, pdf_filename, title, authors, journal, year, doi, abstract, pdf_rel_path) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            [(project_id, e["pdf_filename"], e.get("title",""), e.get("authors",""),
              e.get("journal",""), e.get("year",""), e.get("doi",""), e.get("abstract",""),
              e.get("pdf_rel_path",""))
             for e in entries]
        )
        conn.commit()

def bib_list(project_id):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM bibliography WHERE project_id=? ORDER BY authors, year", (project_id,)
        ).fetchall()
        return [dict(r) for r in rows]

def bib_get(project_id, pdf_filename):
    base = pdf_filename.replace("\\","/").split("/")[-1]
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM bibliography WHERE project_id=? AND (pdf_filename=? OR pdf_filename LIKE ?)",
            (project_id, pdf_filename, f"%{base}")
        ).fetchone()
        return dict(row) if row else {}

def bib_get_by_file(project_id, filename):
    """Bibliography entry for a requested PDF file, matched against the stored
    filename or the RIS path's basename. Exact matches are tried first: storage
    names routinely contain '%' (e.g. '10.1007%2Fs10389-004-0091-9.pdf'), which
    is a LIKE wildcard and would otherwise match the wrong paper."""
    base = filename.replace("\\","/").split("/")[-1]
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM bibliography WHERE project_id=? AND (pdf_filename=? OR pdf_rel_path=?)",
            (project_id, base, base)
        ).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT * FROM bibliography WHERE project_id=? AND "
                "(pdf_filename LIKE ? ESCAPE '\\' OR pdf_rel_path LIKE ? ESCAPE '\\')",
                (project_id, f"%{_like_escape(base)}", f"%{_like_escape(base)}")
            ).fetchone()
        return dict(row) if row else {}

def _like_escape(s):
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# ── Backup / Restore ──────────────────────────────────────────────────────────

def project_backup(proj_id: str) -> dict:
    """Return a complete serialisable backup of a project."""
    import base64 as _b64
    with get_db() as conn:
        proj_row = conn.execute("SELECT * FROM projects WHERE id=?", (proj_id,)).fetchone()
        if not proj_row:
            return None
        proj = dict(proj_row)
        # Encode binary blobs as base64 strings
        for col in ("questions_blob", "extractor_blob"):
            if proj.get(col):
                proj[col] = _b64.b64encode(proj[col]).decode()

        runs      = [dict(r) for r in conn.execute("SELECT * FROM runs WHERE project_id=?",         (proj_id,)).fetchall()]
        ai        = [dict(r) for r in conn.execute("SELECT * FROM ai_results WHERE project_id=?",   (proj_id,)).fetchall()]
        human     = [dict(r) for r in conn.execute("SELECT * FROM human_results WHERE project_id=?",(proj_id,)).fetchall()]
        bib       = [dict(r) for r in conn.execute("SELECT * FROM bibliography WHERE project_id=?", (proj_id,)).fetchall()]

    return {"version": 1, "project": proj, "runs": runs,
            "ai_results": ai, "human_results": human, "bibliography": bib}


def project_restore(backup: dict, new_proj_id: str) -> str:
    """Insert a project (and all related data) from a backup dict with a fresh ID."""
    import base64 as _b64
    import uuid as _uuid

    proj = dict(backup["project"])
    old_id = proj["id"]
    proj["id"] = new_proj_id
    proj["created_at"] = datetime.utcnow().isoformat()
    proj["updated_at"] = datetime.utcnow().isoformat()

    # Decode blobs
    for col in ("questions_blob", "extractor_blob"):
        if proj.get(col):
            proj[col] = _b64.b64decode(proj[col])

    # Build old→new run ID map
    run_id_map = {r["id"]: str(_uuid.uuid4()) for r in backup.get("runs", [])}

    with get_db() as conn:
        cols = list(proj.keys())
        conn.execute(
            f"INSERT INTO projects ({','.join(cols)}) VALUES ({','.join('?'*len(cols))})",
            list(proj.values())
        )

        for run in backup.get("runs", []):
            r = dict(run)
            r["id"] = run_id_map[r["id"]]
            r["project_id"] = new_proj_id
            if r.get("ai_run_ref") and r["ai_run_ref"] in run_id_map:
                r["ai_run_ref"] = run_id_map[r["ai_run_ref"]]
            c = list(r.keys())
            conn.execute(f"INSERT INTO runs ({','.join(c)}) VALUES ({','.join('?'*len(c))})", list(r.values()))

        for b in backup.get("bibliography", []):
            b_row = {k: v for k, v in b.items() if k != "id"}
            b_row["project_id"] = new_proj_id
            c = list(b_row.keys())
            conn.execute(f"INSERT INTO bibliography ({','.join(c)}) VALUES ({','.join('?'*len(c))})", list(b_row.values()))

        for a in backup.get("ai_results", []):
            a_row = {k: v for k, v in a.items() if k != "id"}
            a_row["project_id"] = new_proj_id
            a_row["run_id"] = run_id_map.get(a_row["run_id"], a_row["run_id"])
            c = list(a_row.keys())
            conn.execute(f"INSERT INTO ai_results ({','.join(c)}) VALUES ({','.join('?'*len(c))})", list(a_row.values()))

        for h in backup.get("human_results", []):
            h_row = {k: v for k, v in h.items() if k != "id"}
            h_row["project_id"] = new_proj_id
            h_row["run_id"] = run_id_map.get(h_row["run_id"], h_row["run_id"])
            c = list(h_row.keys())
            conn.execute(f"INSERT INTO human_results ({','.join(c)}) VALUES ({','.join('?'*len(c))})", list(h_row.values()))

        conn.commit()
    return new_proj_id


# ── Users ──────────────────────────────────────────────────────────────────────

def user_list():
    with get_db() as conn:
        rows = conn.execute("SELECT name FROM users ORDER BY name").fetchall()
        return [r["name"] for r in rows]

def user_add(name: str):
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO users (name, created_at) VALUES (?,?)", (name, now))
        conn.commit()

def user_add_with_password(name: str, password_hash: str, role: str = "user"):
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO users (name, password_hash, role, created_at) VALUES (?,?,?,?)",
            (name, password_hash, role, now)
        )
        conn.commit()

def user_get_by_name(name: str):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE name=?", (name,)).fetchone()
        return dict(row) if row else None

def user_set_role(name: str, role: str):
    with get_db() as conn:
        conn.execute("UPDATE users SET role=? WHERE name=?", (role, name))
        conn.commit()

def user_list_with_roles():
    with get_db() as conn:
        rows = conn.execute("SELECT name, role FROM users ORDER BY name").fetchall()
        return [{"name": r["name"], "role": r["role"] or "member"} for r in rows]

def user_set_password(name: str, password_hash: str):
    with get_db() as conn:
        conn.execute("UPDATE users SET password_hash=? WHERE name=?", (password_hash, name))
        conn.commit()

def user_delete(name: str):
    with get_db() as conn:
        conn.execute("DELETE FROM users WHERE name=?", (name,))
        conn.commit()


# ── Project members ────────────────────────────────────────────────────────────

def project_member_add(project_id: str, username: str, project_role: str = "member"):
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO project_members (project_id, username, project_role, added_at) VALUES (?,?,?,?)",
            (project_id, username, project_role, now)
        )
        conn.commit()

def project_member_remove(project_id: str, username: str):
    with get_db() as conn:
        conn.execute("DELETE FROM project_members WHERE project_id=? AND username=?", (project_id, username))
        conn.commit()

def project_member_set_role(project_id: str, username: str, project_role: str):
    with get_db() as conn:
        conn.execute(
            "UPDATE project_members SET project_role=? WHERE project_id=? AND username=?",
            (project_role, project_id, username)
        )
        conn.commit()

def project_members_list(project_id: str):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT username, project_role, added_at FROM project_members WHERE project_id=? ORDER BY added_at",
            (project_id,)
        ).fetchall()
        return [dict(r) for r in rows]

def get_project_role(project_id: str, username: str):
    """Returns the project_role for the user, or None if not a member."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT project_role FROM project_members WHERE project_id=? AND username=?",
            (project_id, username)
        ).fetchone()
        return row["project_role"] if row else None

def project_list_for_user(username: str):
    """Projects the user is a member of, with their project_role included."""
    with get_db() as conn:
        rows = conn.execute("""
            SELECT p.id, p.name, p.description, p.created_at, p.updated_at,
                   p.provider, p.model, p.questions_filename, p.extractor_filename, p.ris_filename,
                   (SELECT COUNT(*) FROM runs r WHERE r.project_id=p.id) as run_count,
                   (SELECT COUNT(DISTINCT pdf_filename) FROM ai_results a WHERE a.project_id=p.id) as ai_pdfs,
                   (SELECT COUNT(DISTINCT pdf_filename) FROM human_results h WHERE h.project_id=p.id) as human_pdfs,
                   pm.project_role
            FROM projects p
            JOIN project_members pm ON pm.project_id=p.id AND pm.username=?
            ORDER BY p.created_at DESC
        """, (username,)).fetchall()
        return [dict(r) for r in rows]


# ── Audit Log ──────────────────────────────────────────────────────────────────

def audit_log_append(project_id, run_id, pdf_filename, field_index, qname, old_answer, new_answer, coder):
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO audit_log (project_id, run_id, pdf_filename, field_index, qname, "
            "old_answer, new_answer, coder, changed_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (project_id, run_id, pdf_filename, field_index, qname, old_answer, new_answer, coder, now)
        )
        conn.commit()

def audit_log_for_export(project_id, run_id=None):
    cond = "project_id=?"
    params = [project_id]
    frag, fp = _run_filter("run_id", run_id)
    cond += frag; params += fp
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM audit_log WHERE {cond} ORDER BY changed_at", params
        ).fetchall()
    return [dict(r) for r in rows]

def audit_log_for_paper(run_id, pdf_filename):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_log WHERE run_id=? AND pdf_filename=? ORDER BY changed_at DESC",
            (run_id, pdf_filename)
        ).fetchall()
    return [dict(r) for r in rows]
