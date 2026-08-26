"""
HarvesterAI - Processing & parsing module
"""
import io
import json
import random
import re
import requests
import threading
import time
import traceback
import concurrent.futures
from datetime import datetime
from math import ceil

import pandas as pd

from database import (
    run_append_log, run_update_progress, run_finish,
    ai_results_insert_batch
)


# ── JobContext ────────────────────────────────────────────────────────────────

class JobContext:
    def __init__(self, run_id, project_id, publish_fn):
        self.run_id = run_id
        self.project_id = project_id
        self._publish = publish_fn
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.completed = 0
        self.total = 0
        self.failed = []

    def log(self, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        run_append_log(self.run_id, line)
        self._publish(self.run_id, "log", line)

    def progress(self, completed, total):
        run_update_progress(self.run_id, completed)
        self._publish(self.run_id, "progress", {"completed": completed, "total": total})


# ── Question file parsing ─────────────────────────────────────────────────────

def parse_questions_excel(data: bytes):
    """Parse Questions.xlsx. Returns (questions_list, context_dict, names_dict)."""
    df = pd.read_excel(io.BytesIO(data))
    questions, context, names = [], {}, {}

    for idx, row in df.iterrows():
        q_num = int(row.iloc[0]) if pd.notna(row.iloc[0]) else idx + 1
        q_text = str(row.get("Question","")).strip() if pd.notna(row.get("Question","")) else ""
        if not q_text:
            continue
        questions.append(q_text)

        if "Qname" in df.columns and pd.notna(row.get("Qname","")):
            qn = str(row["Qname"]).strip()
            if qn: names[q_num] = qn

        ctx_parts = []
        for col in ["Recommended Answer Options", "Additional Instructions"]:
            if col in df.columns and pd.notna(row.get(col,"")):
                v = str(row[col]).strip()
                if v: ctx_parts.append(v)

        examples = [str(row[c]).strip() for c in
                    [f"Example Answer {i}" for i in range(1,6)]
                    if c in df.columns and pd.notna(row.get(c,""))]
        if examples:
            ctx_parts.append(f"Example answers: {'; '.join(examples)}")
        context[q_num] = "\n".join(ctx_parts)

    return questions, context, names


# ── Extractor form parsing ─────────────────────────────────────────────────────

def parse_extractor_excel(data: bytes) -> list:
    """
    Parse ExtracterExample.xlsx into a list of field definitions.
    Returns list of dicts with: tab, question, info, show_with (list of ints),
    qtype, answer_options (list), table_rows, table_cols, field_index
    """
    df = pd.read_excel(io.BytesIO(data))
    fields = []

    def clean(v):
        return str(v).strip() if pd.notna(v) else ""

    for idx, row in df.iterrows():
        tab = clean(row.get("Tab",""))
        question = clean(row.get("Question",""))
        info = clean(row.get("Info",""))
        qtype = clean(row.get("Qtype",""))
        ao_raw = clean(row.get("AO",""))

        # ShowWith: semicolon-separated question numbers
        sw_raw = clean(row.get("ShowWith",""))
        show_with = []
        if sw_raw:
            for p in sw_raw.split(";"):
                p = p.strip()
                if p.isdigit():
                    show_with.append(int(p))

        answer_options = [o.strip() for o in ao_raw.split(";") if o.strip()] if ao_raw else []

        table_rows = clean(row.get("TableRows",""))
        table_cols = clean(row.get("TableCols",""))
        try: table_rows = int(float(table_rows))
        except (TypeError, ValueError): table_rows = None
        try: table_cols = int(float(table_cols))
        except (TypeError, ValueError): table_cols = None

        fields.append({
            "field_index": idx,
            "tab": tab,
            "question": question,
            "info": info,
            "show_with": show_with,
            "qtype": qtype,
            "answer_options": answer_options,
            "table_rows": table_rows,
            "table_cols": table_cols,
        })

    return fields


# ── RIS parsing ───────────────────────────────────────────────────────────────

def _ris_synthetic_filename(entry, authors, index):
    """Generate a filename slug when RIS has no file attachment."""
    first_author = authors[0].split(",")[0].strip() if authors else "Unknown"
    # remove non-alphanumeric
    first_author = re.sub(r"[^A-Za-z0-9]", "", first_author)
    year = entry.get("year", "")
    return f"{first_author}{year}_{index+1}.pdf"


def parse_ris(content: str) -> dict:
    """
    Returns {pdf_filename: {title, authors, journal, year, doi, abstract}}
    If a record has no file attachment, a synthetic filename is generated from
    author+year so the entry still appears in the bibliography.
    """
    ris_entries = {}
    current, authors, pdf_files = {}, [], []
    entry_index = [0]

    def flush():
        if not current and not authors:
            return
        if authors:
            current["authors"] = "; ".join(authors)
        if pdf_files:
            for pf in pdf_files:
                ris_entries[pf] = current.copy()
        else:
            fname = _ris_synthetic_filename(current, authors, entry_index[0])
            ris_entries[fname] = current.copy()
        entry_index[0] += 1

    for line in content.splitlines():
        line = line.rstrip()
        if len(line) < 5:
            continue
        tag = line[:5]          # e.g. "TY  -", "TI  -", "AU  -"
        val = line[6:].strip() if len(line) > 6 else ""

        if tag == "TY  -":
            flush()
            current, authors, pdf_files = {}, [], []
        elif tag == "TI  -":
            current["title"] = val
        elif tag == "AU  -":
            authors.append(val)
        elif tag in ("T2  -", "JO  -", "JF  -"):
            current.setdefault("journal", val)
        elif tag in ("PY  -", "Y1  -"):
            m = re.search(r"\d{4}", val)
            if m: current["year"] = m.group()
        elif tag == "DO  -":
            current["doi"] = val
        elif tag in ("AB  -", "N2  -"):
            current["abstract"] = val
        elif tag in ("L1  -", "L2  -", "L4  -"):
            if val and not pdf_files:
                # Only take the first PDF attachment per record (1 entry per paper)
                clean = val.replace("file:///", "").replace("\\", "/")
                basename = clean.split("/")[-1]
                if basename.lower().endswith(".pdf"):
                    pdf_files.append(basename)
                    current["pdf_rel_path"] = clean  # full relative path for local PDF lookup
        elif tag == "ER  -":
            flush()
            current, authors, pdf_files = {}, [], []

    flush()
    return ris_entries


def ris_to_bib_entries(project_id: str, ris_entries: dict) -> list:
    """Convert parsed RIS dict to list suitable for bib_upsert_batch."""
    rows = []
    for pdf_filename, entry in ris_entries.items():
        rows.append({
            "pdf_filename": pdf_filename,
            "title": entry.get("title",""),
            "authors": entry.get("authors",""),
            "journal": entry.get("journal",""),
            "year": entry.get("year",""),
            "doi": entry.get("doi",""),
            "abstract": entry.get("abstract",""),
            "pdf_rel_path": entry.get("pdf_rel_path",""),
        })
    return rows


# ── API call ──────────────────────────────────────────────────────────────────

def _render_raw_template(template_text, subs):
    """Parse a raw request-template JSON and substitute {{PLACEHOLDER}} tokens
    inside every string value. Returns (url, headers, body, response_path)."""
    spec = json.loads(template_text)

    def walk(obj):
        if isinstance(obj, str):
            for k, v in subs.items():
                if k in obj:
                    obj = obj.replace(k, v)
            return obj
        if isinstance(obj, list):
            return [walk(x) for x in obj]
        if isinstance(obj, dict):
            return {k: walk(v) for k, v in obj.items()}
        return obj

    spec = walk(spec)
    url = spec.get("url")
    if not url:
        raise ValueError("template must include a top-level \"url\"")
    headers = spec.get("headers", {"Content-Type": "application/json"})
    body = spec.get("body", {})
    response_path = spec.get("response_path", "choices.0.message.content")
    return url, headers, body, response_path


def _extract_by_path(data, path):
    """Walk a dotted path (e.g. 'choices.0.message.content') into a response dict."""
    cur = data
    for part in path.split("."):
        if part == "":
            continue
        if isinstance(cur, list):
            cur = cur[int(part)]
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return ""
        if cur is None:
            return ""
    return cur


class _Heartbeat:
    """Log that a request is still in flight, once a minute until it returns.

    A slow model is silent otherwise: the provider holds the connection open and
    trickles keepalive bytes, so requests' timeout never fires and nothing is
    logged until the call finishes. A run that is merely slow then looks dead.
    """

    def __init__(self, ctx, label, interval=60):
        self.ctx, self.label, self.interval = ctx, label, interval
        self._stop = threading.Event()
        self._thread = None
        self.started = 0.0

    def __enter__(self):
        self.started = time.time()
        if self.ctx:
            self._thread = threading.Thread(target=self._tick, daemon=True)
            self._thread.start()
        return self

    def _tick(self):
        while not self._stop.wait(self.interval):
            if self.ctx.stop.is_set():
                return
            mins = (time.time() - self.started) / 60
            self.ctx.log(f"{self.label}still waiting on the model ({mins:.0f}m elapsed)")

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
        return False

    def elapsed(self):
        return time.time() - self.started


def _fmt_secs(s):
    return f"{s:.0f}s" if s < 60 else f"{int(s // 60)}m{int(s % 60):02d}s"


def call_api(api_key, pdf_text, questions, question_context, model, temperature,
             top_p, system_context, provider, max_output_tokens=8000,
             max_retries=10, ctx=None, extra_context="",
             raw_api_mode=False, raw_api_template="", label="", reasoning_effort=""):

    q_parts = []
    for i, q in enumerate(questions, 1):
        t = f"{i}. {q}"
        if i in question_context and question_context[i]:
            t += f"\n   {question_context[i].replace(chr(10),' ')}"
        q_parts.append(t)

    extra_block = f"Additional Context/Codebook:\n{extra_context[:50000]}\n\n" if extra_context else ""

    user_content = (
        f"{system_context}\n\n"
        f"{extra_block}"
        f"PDF Document:\n{pdf_text[:200000]}{'...' if len(pdf_text)>200000 else ''}\n\n"
        f"Questions:\n{chr(10).join(q_parts)}\n\n"
        "Provide answers as a numbered list. Start each answer with [[N]] where N is the question number."
    )

    response_path = None  # set only in raw mode

    if raw_api_mode and raw_api_template.strip():
        subs = {
            "{{API_KEY}}": api_key or "",
            "{{MODEL}}": model or "",
            "{{PROMPT}}": user_content,
            "{{PDF_TEXT}}": pdf_text[:200000],
            "{{QUESTIONS}}": chr(10).join(q_parts),
            "{{SYSTEM_CONTEXT}}": system_context or "",
        }
        url, headers, payload, response_path = _render_raw_template(raw_api_template, subs)
    elif provider == "anthropic":
        url = "https://api.anthropic.com/v1/messages"
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01",
                   "Content-Type": "application/json"}
        payload = {"model": model, "max_tokens": min(max_output_tokens, 4096),
                   "messages": [{"role": "user", "content": user_content}]}
    else:
        if provider in ("openai", "openai_legacy"):
            url = "https://api.openai.com/v1/chat/completions"
        elif provider == "openrouter":
            url = "https://openrouter.ai/api/v1/chat/completions"
        elif provider == "kimi":
            url = "https://api.moonshot.ai/v1/chat/completions"
        else:
            url = "https://api.deepseek.com/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        _tokens_key = "max_completion_tokens" if provider == "openai" else "max_tokens"
        payload = {"model": model, "messages": [{"role":"user","content":user_content}],
                   "temperature": temperature, "top_p": top_p, _tokens_key: max_output_tokens}
        # Only OpenRouter takes this object. The other providers spell it differently
        # (or not at all) and would reject an unknown field, so never send it there.
        if provider == "openrouter" and reasoning_effort:
            payload["reasoning"] = {"effort": reasoning_effort}

    for attempt in range(1, max_retries + 1):
        if ctx and ctx.stop.is_set():
            raise RuntimeError("stopped by user")
        try:
            with _Heartbeat(ctx, label) as hb:
                resp = requests.post(url, headers=headers, json=payload, timeout=300)
            if resp.ok and ctx and hb.elapsed() >= 60:
                # Worth saying out loud — it is the only signal of how slow a model is
                ctx.log(f"{label}model replied after {_fmt_secs(hb.elapsed())}")
            if resp.status_code == 429 or "rate limit" in resp.text.lower():
                wait = 60 + random.uniform(0, 5)
                if ctx: ctx.log(f"Rate limit — waiting {wait:.0f}s...")
                time.sleep(wait)
                continue
            if not resp.ok:
                body = resp.text[:500]
                if ctx: ctx.log(f"API error {resp.status_code}: {body}")
                resp.raise_for_status()
            data = resp.json()
            if response_path is not None:
                # Raw mode: pull the answer text from the user-specified path and
                # normalize to the shape parse_answers expects. Parsing is unchanged.
                text = _extract_by_path(data, response_path)
                data = {"choices": [{"message": {"content": str(text or "")}}]}
            elif provider == "anthropic":
                text = data.get("content",[{}])[0].get("text","")
                data = {"choices":[{"message":{"content":text}}]}
            return data
        except Exception as e:
            wait = 15 + random.uniform(0,5)
            if ctx: ctx.log(f"Request error attempt {attempt}/{max_retries}: {e}. Waiting {wait:.0f}s...")
            time.sleep(wait)

    raise RuntimeError(f"Max retries ({max_retries}) exceeded.")


def parse_answers(response_text, num_questions):
    answers = [""] * num_questions
    response_text = response_text or ""
    if not response_text:
        return answers
    for i in range(1, num_questions + 1):
        m = re.search(rf"\[\[{i}\]\]\s*(.*?)(?=\[\[\d+\]\]|$)", response_text, re.DOTALL)
        if m:
            answers[i-1] = re.sub(r"\s+", " ", m.group(1).strip())
    if all(a == "" for a in answers):
        splits = re.compile(r"(?:^|\n)\s*\d+\.\s*").split(response_text)
        for i, s in enumerate(splits[1:num_questions+1]):
            answers[i] = re.sub(r"\s+", " ", s.strip())
    return answers


def _usage_summary(data):
    """"in 41,234 / out 8,000 tok (7,410 reasoning) · $0.0123" — "" if the provider
    reported nothing. Providers vary in which fields they fill, so every part is
    optional; reasoning tokens are what make an output budget disappear.
    """
    usage = (data or {}).get("usage") or {}
    if not usage:
        return ""
    parts = []
    prompt = usage.get("prompt_tokens")
    out = usage.get("completion_tokens")
    if prompt is not None or out is not None:
        parts.append(f"in {prompt or 0:,} / out {out or 0:,} tok")
    reasoning = ((usage.get("completion_tokens_details") or {}).get("reasoning_tokens"))
    if reasoning:
        parts.append(f"({reasoning:,} reasoning)")
    cost = usage.get("cost")
    if isinstance(cost, (int, float)) and cost > 0:
        parts.append(f"· ${cost:.4f}")
    return " ".join(parts)


def _completion_text(data, max_output_tokens=None):
    """(answer_text, why_empty) from a chat completion.

    Reasoning models can spend the whole output budget thinking and return
    content: null. That used to reach parse_answers as None and surface as
    "expected string or bytes-like object, got 'NoneType'", which says nothing
    about the cause — so when there is no answer, say why.
    """
    choice = ((data or {}).get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    for holder, key in ((msg, "content"), (msg, "text"), (choice, "text")):
        val = holder.get(key)
        if isinstance(val, str) and val.strip():
            return val, ""

    finish = choice.get("finish_reason") or "unknown"
    usage = (data or {}).get("usage") or {}
    out_tokens = usage.get("completion_tokens")
    reasoning = msg.get("reasoning") or msg.get("reasoning_content") or ""
    why = f"the model returned no answer text (finish_reason={finish}"
    if out_tokens is not None:
        why += f", {out_tokens} output tokens"
    why += ")"
    if reasoning:
        why += f"; it produced {len(reasoning)} chars of reasoning instead"
    if finish == "length" or (out_tokens and max_output_tokens and out_tokens >= max_output_tokens):
        why += (f" — it used the whole {max_output_tokens} token output budget before answering."
                " Raise Max Output Tokens in the project settings, or pick a model that"
                " reasons less.")
    return "", why


# ── Batch processing ──────────────────────────────────────────────────────────

def process_batch(batch_id, pdf_batch, questions, question_context, question_names,
                  api_key, model, temperature, top_p, system_context, provider,
                  max_output_tokens, run_id, project_id, ctx: JobContext,
                  extra_context="", raw_api_mode=False, raw_api_template="",
                  reasoning_effort=""):
    """Process one batch. Each pdf: {filename, text}"""
    for pdf in pdf_batch:
        if ctx.stop.is_set():
            break
        filename = pdf["filename"]
        ctx.log(f"Batch {batch_id+1}: {filename}")
        try:
            if not pdf["text"].strip():
                ctx.log(f"  Skipped (empty text): {filename}")
                with ctx.lock:
                    ctx.failed.append(f"{filename} (empty)")
                    ctx.completed += 1
                    ctx.progress(ctx.completed, ctx.total)
                continue

            resp = call_api(api_key, pdf["text"], questions, question_context,
                            model, temperature, top_p, system_context, provider,
                            max_output_tokens, ctx=ctx, extra_context=extra_context,
                            raw_api_mode=raw_api_mode, raw_api_template=raw_api_template,
                            label=f"Batch {batch_id+1}: ", reasoning_effort=reasoning_effort)
            answer_text, why_empty = _completion_text(resp, max_output_tokens)
            if not answer_text:
                ctx.log(f"  ERROR {filename}: {why_empty}")
                with ctx.lock:
                    ctx.failed.append(f"{filename} (no answer text)")
                    ctx.completed += 1
                    ctx.progress(ctx.completed, ctx.total)
                continue
            answers = parse_answers(answer_text, len(questions))

            if all(a == "" for a in answers):
                ctx.log(f"  WARNING no answers parsed: {filename}")
                with ctx.lock:
                    ctx.failed.append(f"{filename} (no answers)")

            # Write to DB row-by-row
            rows = []
            for i, (q, a) in enumerate(zip(questions, answers), 1):
                rows.append({
                    "run_id": run_id,
                    "project_id": project_id,
                    "pdf_filename": filename,
                    "question_num": i,
                    "question_text": q,
                    "qname": question_names.get(i, ""),
                    "answer": a,
                })
            ai_results_insert_batch(rows)

            with ctx.lock:
                ctx.completed += 1
                ctx.progress(ctx.completed, ctx.total)
            usage = _usage_summary(resp)
            ctx.log(f"  Done: {filename}" + (f" — {usage}" if usage else ""))

        except Exception as e:
            if "stopped by user" not in str(e):
                ctx.log(f"  ERROR {filename}: {e}")
            with ctx.lock:
                ctx.failed.append(f"{filename} (error)")
                ctx.completed += 1
                ctx.progress(ctx.completed, ctx.total)


def run_ai_job(run_id, project_id, config, pdfs, ctx: JobContext):
    """
    Main AI extraction entry point.
    pdfs = [{filename, text}]  — text extracted client-side
    config keys: api_key, provider, model, temperature, top_p, max_output_tokens,
                 max_workers, system_context,
                 questions_json, question_context_json, question_names_json
    """
    ctx.log("=== AI Extraction Starting ===")
    ctx.log(f"Provider: {config['provider']}  Model: {config['model']}")
    ctx.log(f"PDFs: {len(pdfs)}")
    if config.get("extra_context"):
        ctx.log(f"Extra context: {len(config['extra_context'])} chars")
    if config.get("raw_api_mode") and config.get("raw_api_template","").strip():
        ctx.log("Raw API template: ENABLED (Basic model settings overridden)")
    if config.get("reasoning_effort"):
        ctx.log(f"Reasoning effort: {config['reasoning_effort']}")

    try:
        questions = json.loads(config.get("questions_json","[]"))
        question_context = {int(k): v for k,v in json.loads(config.get("question_context_json","{}")).items()}
        question_names = {int(k): v for k,v in json.loads(config.get("question_names_json","{}")).items()}
    except Exception as e:
        ctx.log(f"ERROR loading questions: {e}")
        run_finish(run_id, "failed")
        ctx._publish(run_id, "done", {"status":"failed"})
        return

    if not questions:
        ctx.log("ERROR: No questions configured on this project.")
        run_finish(run_id, "failed")
        ctx._publish(run_id, "done", {"status":"failed"})
        return

    ctx.log(f"Questions loaded: {len(questions)}")

    ctx.total = len(pdfs)
    ctx.completed = 0
    ctx.progress(0, ctx.total)

    num_workers = min(config.get("max_workers", 5), len(pdfs))
    batch_size = ceil(len(pdfs) / num_workers)
    batches = [pdfs[i*batch_size:(i+1)*batch_size] for i in range(num_workers)]
    ctx.log(f"Workers: {num_workers}  Batches: {len(batches)}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
        futures = [
            executor.submit(process_batch, i, batch,
                            questions, question_context, question_names,
                            config["api_key"], config["model"],
                            config.get("temperature", 0.2), config.get("top_p", 0.95),
                            config.get("system_context",""), config["provider"],
                            config.get("max_output_tokens", 8000),
                            run_id, project_id, ctx,
                            extra_context=config.get("extra_context", ""),
                            raw_api_mode=config.get("raw_api_mode", False),
                            raw_api_template=config.get("raw_api_template", ""),
                            reasoning_effort=config.get("reasoning_effort", ""))
            for i, batch in enumerate(batches)
        ]
        for f in concurrent.futures.as_completed(futures):
            if ctx.stop.is_set():
                executor.shutdown(wait=False, cancel_futures=True)
                break

    if ctx.stop.is_set():
        ctx.log("Extraction cancelled by user.")
        run_finish(run_id, "cancelled", len(ctx.failed))
        ctx._publish(run_id, "done", {"status":"cancelled"})
        return

    ctx.log("="*50)
    ctx.log(f"Complete: {ctx.completed}/{ctx.total}  Failed: {len(ctx.failed)}")
    for f in ctx.failed:
        ctx.log(f"  • {f}")
    ctx.log("="*50)

    final_status = "failed" if len(ctx.failed) == ctx.total else "complete"
    run_finish(run_id, final_status, len(ctx.failed))
    ctx._publish(run_id, "done", {"status": final_status})
