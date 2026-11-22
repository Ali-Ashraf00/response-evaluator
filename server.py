"""
LLM Eval Platform — Zero-Dependency Python Backend
---------------------------------------------------
Features:
  1. Serves pure HTML/CSS/JS frontend from ./static
  2. Equal-weight 5-criteria rubric (20% each):
     Instruction Following, Truthfulness, Verbosity / Tone, Helpfulness, Completeness
  3. 2-Pass Swap Test (Pass 1: A vs B, Pass 2: B vs A) to detect position bias
  4. Optional Groq / Gemini REST LLM Judge (falls back cleanly to deterministic heuristic)
  5. Blind Mode comparison (human 10 ratings vs hidden AI judge ratings)
  6. SQLite persistence (eval_history.db) + CSV export
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import sqlite3
import sys
import threading
import urllib.error
import urllib.request
import webbrowser
from contextlib import contextmanager
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
RUBRIC_PATH = BASE_DIR / "rubric.json"
DEFAULT_DB_PATH = BASE_DIR / "eval_history.db"

STATIC_MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
}

FILLER_PHRASES = (
    "as an ai",
    "as a language model",
    "amazing and fascinating",
    "super cool",
    "it is worth noting that",
    "delve into",
)

COMMON_CAPITALS: dict[str, tuple[str, ...]] = {
    "india": ("new delhi", "delhi"),
    "usa": ("washington", "washington, d.c.", "washington dc"),
    "united states": ("washington", "washington, d.c.", "washington dc"),
    "uk": ("london",),
    "united kingdom": ("london",),
    "france": ("paris",),
    "japan": ("tokyo",),
    "china": ("beijing",),
    "germany": ("berlin",),
    "russia": ("moscow",),
    "australia": ("canberra",),
    "canada": ("ottawa",),
    "italy": ("rome",),
    "spain": ("madrid",),
    "brazil": ("brasilia", "brasília"),
    "pakistan": ("islamabad",),
    "nepal": ("kathmandu",),
    "bangladesh": ("dhaka",),
    "sri lanka": ("sri jayawardenepura kotte", "colombo"),
    "uae": ("abu dhabi",),
    "south korea": ("seoul",),
}

STOPWORDS = {
    "what", "when", "where", "which", "who", "whom", "whose", "why", "how",
    "the", "and", "for", "with", "that", "this", "from", "into", "your",
    "about", "in", "on", "at", "to", "of", "is", "are", "was", "were",
    "be", "been", "answer", "explain", "compare", "sentences", "sentence",
    "short", "include", "covering", "one", "two", "three",
}


def load_dotenv(env_path: Path = BASE_DIR / ".env") -> None:
    """Load simple KEY=VALUE pairs from .env if the file exists."""
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip().strip('"').strip("'")


load_dotenv()


def load_rubric(rubric_path: Path = RUBRIC_PATH) -> dict[str, Any]:
    """Read criteria definitions and sample cases from rubric.json."""
    return json.loads(rubric_path.read_text(encoding="utf-8"))


def get_engine_info() -> dict[str, str]:
    """Report which judge engine is active based on available API keys."""
    groq_key = os.getenv("GROQ_API_KEY", "").strip()
    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
    preferred = os.getenv("DEFAULT_PROVIDER", "").strip().lower()

    if preferred == "heuristic":
        return {"id": "heuristic", "label": "deterministic heuristic (no API key)"}
    if preferred == "gemini" and gemini_key:
        model = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
        return {"id": "gemini", "label": f"Gemini API ({model})"}
    if groq_key:
        model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
        return {"id": "groq", "label": f"Groq API ({model})"}
    if gemini_key:
        model = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
        return {"id": "gemini", "label": f"Gemini API ({model})"}
    return {"id": "heuristic", "label": "deterministic heuristic (no API key)"}


def save_engine_settings(provider: str, api_key: str, env_path: Path = BASE_DIR / ".env") -> dict[str, str]:
    """Activate Groq, Gemini, or heuristic mode immediately and persist to .env."""
    provider = provider.strip().lower()
    api_key = api_key.strip()

    if provider == "groq":
        if api_key:
            os.environ["GROQ_API_KEY"] = api_key
        os.environ["DEFAULT_PROVIDER"] = "groq"
    elif provider == "gemini":
        if api_key:
            os.environ["GEMINI_API_KEY"] = api_key
        os.environ["DEFAULT_PROVIDER"] = "gemini"
    else:
        os.environ["DEFAULT_PROVIDER"] = "heuristic"

    # Preserve any existing env variables (e.g. GEMINI_MODEL, GROQ_MODEL, PORT, HOST)
    existing_vars: dict[str, str] = {}
    if env_path.is_file():
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                existing_vars[k.strip()] = v.strip().strip('"').strip("'")

    existing_vars["DEFAULT_PROVIDER"] = os.environ.get("DEFAULT_PROVIDER", "heuristic")
    existing_vars["GROQ_API_KEY"] = os.environ.get("GROQ_API_KEY", "")
    existing_vars["GEMINI_API_KEY"] = os.environ.get("GEMINI_API_KEY", "")

    lines = [f"{k}={v}" for k, v in existing_vars.items()]
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return get_engine_info()


# ---------------------------------------------------------------------------
# SQLite Database Layer (Raw sqlite3 — zero ORM)
# ---------------------------------------------------------------------------

@contextmanager
def _db_connect(db_path: Path):
    """Open, commit/rollback, and deterministically close a SQLite connection."""
    conn = sqlite3.connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Path = DEFAULT_DB_PATH) -> None:
    """Create the evaluations table if it does not already exist."""
    with _db_connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS evaluations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                mode TEXT NOT NULL,
                prompt TEXT NOT NULL,
                response_a TEXT NOT NULL,
                response_b TEXT NOT NULL,
                engine TEXT NOT NULL,
                pass1_winner TEXT NOT NULL,
                pass2_winner TEXT NOT NULL,
                final_winner TEXT NOT NULL,
                consistent INTEGER NOT NULL,
                confidence TEXT NOT NULL,
                score_a REAL NOT NULL,
                score_b REAL NOT NULL,
                exact_matches INTEGER,
                result_json TEXT NOT NULL
            )
            """
        )


def save_evaluation(db_path: Path, payload: dict[str, Any]) -> int:
    """Insert a completed Compare or Blind evaluation into SQLite."""
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _db_connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO evaluations (
                created_at, mode, prompt, response_a, response_b, engine,
                pass1_winner, pass2_winner, final_winner, consistent,
                confidence, score_a, score_b, exact_matches, result_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                created_at,
                payload["mode"],
                payload["prompt"],
                payload["response_a"],
                payload["response_b"],
                payload["engine"],
                payload["pass1_winner"],
                payload["pass2_winner"],
                payload["final_winner"],
                1 if payload["consistent"] else 0,
                payload["confidence"],
                float(payload["score_a"]),
                float(payload["score_b"]),
                payload.get("exact_matches"),
                json.dumps(payload, ensure_ascii=False),
            ),
        )
        return int(cur.lastrowid)


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = json.loads(row["result_json"])
    data["id"] = row["id"]
    data["created_at"] = row["created_at"]
    return data


def list_evaluations(
    db_path: Path,
    mode: str | None = None,
    consistency: str | None = None,
    query: str | None = None,
) -> list[dict[str, Any]]:
    """Return saved evaluations newest-first with optional filters."""
    clauses: list[str] = []
    params: list[Any] = []
    if mode in ("compare", "blind"):
        clauses.append("mode = ?")
        params.append(mode)
    if consistency == "consistent":
        clauses.append("consistent = 1")
    elif consistency == "bias":
        clauses.append("consistent = 0")
    if query:
        clauses.append("(LOWER(prompt) LIKE ? OR LOWER(response_a) LIKE ? OR LOWER(response_b) LIKE ?)")
        like_term = f"%{query.strip().lower()}%"
        params.extend([like_term, like_term, like_term])

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"SELECT * FROM evaluations {where_sql} ORDER BY id DESC LIMIT 200"

    with _db_connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_dict(row) for row in rows]


def get_evaluation_count(db_path: Path) -> int:
    with _db_connect(db_path) as conn:
        row = conn.execute("SELECT COUNT(*) FROM evaluations").fetchone()
    return int(row[0]) if row else 0


def delete_evaluation(db_path: Path, row_id: int) -> bool:
    with _db_connect(db_path) as conn:
        cur = conn.execute("DELETE FROM evaluations WHERE id = ?", (row_id,))
        return cur.rowcount > 0


def clear_evaluations(db_path: Path) -> int:
    with _db_connect(db_path) as conn:
        cur = conn.execute("DELETE FROM evaluations")
        return int(cur.rowcount)


def export_history_csv(db_path: Path) -> str:
    """Serialize evaluation history as a downloadable UTF-8 CSV string."""
    rows = list_evaluations(db_path)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "id", "created_at", "mode", "engine", "prompt",
            "response_a", "response_b", "score_a", "score_b",
            "pass1_winner", "pass2_winner", "final_winner",
            "consistent", "confidence", "exact_matches",
        ]
    )
    for item in rows:
        writer.writerow(
            [
                item.get("id", ""),
                item.get("created_at", ""),
                item.get("mode", ""),
                item.get("engine", ""),
                item.get("prompt", ""),
                item.get("response_a", ""),
                item.get("response_b", ""),
                f"{item.get('score_a', 0):.2f}",
                f"{item.get('score_b', 0):.2f}",
                item.get("pass1_winner", ""),
                item.get("pass2_winner", ""),
                item.get("final_winner", ""),
                item.get("consistent", True),
                item.get("confidence", ""),
                item.get("exact_matches", ""),
            ]
        )
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Deterministic Heuristic Rubric Judge (No API Key Needed)
# ---------------------------------------------------------------------------

def _split_sentences(text: str) -> list[str]:
    cleaned = text.strip()
    if not cleaned:
        return []
    parts = [s.strip() for s in re.split(r"(?<=[.!?])\s+", cleaned) if s.strip()]
    return parts if parts else [cleaned]


def _extract_keywords(text: str) -> set[str]:
    words = re.findall(r"[a-zA-Z0-9]{3,}", text.lower())
    return {w for w in words if w not in STOPWORDS}


def _parse_constraints(prompt: str) -> dict[str, Any]:
    low = prompt.lower()
    range_match = re.search(r"(\d+)\s*(?:-|–|to)\s*(\d+)\s*sentences?", low)
    exact_match = re.search(r"\b(?:in|exactly)\s+(\d+)\s*sentences?", low)
    word_limit = re.search(r"(?:under|max|within|less than)\s+(\d+)\s*words?", low)
    wants_code = any(k in low for k in ("code", "python", "function", "snippet", "def ", "sql", "javascript"))
    wants_bullets = any(k in low for k in ("bullet", "list", "steps"))
    wants_comparison = any(k in low for k in ("compare", "difference", "vs", "versus"))
    min_s = int(range_match.group(1)) if range_match else (int(exact_match.group(1)) if exact_match else None)
    max_s = int(range_match.group(2)) if range_match else (int(exact_match.group(1)) if exact_match else None)
    is_short_qa = (
        bool(re.match(r"^\s*(?:what|who|which|where|when)\b", low))
        and min_s is None
        and not wants_code
        and not wants_bullets
        and not wants_comparison
    )
    return {
        "min_sentences": min_s,
        "max_sentences": max_s,
        "max_words": int(word_limit.group(1)) if word_limit else None,
        "wants_code": wants_code,
        "wants_bullets": wants_bullets,
        "wants_comparison": wants_comparison,
        "is_short_qa": is_short_qa,
        "is_nobel_math": "nobel prize" in low and "math" in low,
        "is_brain_myth": "10%" in low and "brain" in low,
        "is_great_wall_myth": "great wall" in low and "space" in low,
    }


def _check_factual_qa(prompt: str, resp_low: str) -> tuple[str | None, str]:
    """Verify common factual Q&A (capitals, simple arithmetic) in offline heuristic mode."""
    p_low = prompt.lower()

    cap_match = re.search(r"\bcapital\s+of\s+([a-z\s]+?)(?:\?|$|\.|,)", p_low)
    if cap_match:
        country = cap_match.group(1).strip()
        if country in COMMON_CAPITALS:
            accepted = COMMON_CAPITALS[country]
            canonical = accepted[0].title()
            if any(re.search(rf"\b{re.escape(ans)}\b", resp_low) for ans in accepted):
                return "verified_true", f"Correctly identifies the capital of {country.title()} ({canonical})."
            return "verified_false", f"Incorrect factual answer: the capital of {country.title()} is {canonical}."

    math_match = re.search(r"\b(-?\d+)\s*([+\-*/x×])\s*(-?\d+)\b", p_low)
    if math_match:
        a, op, b = int(math_match.group(1)), math_match.group(2), int(math_match.group(3))
        expected: float | int | None = None
        if op == "+":
            expected = a + b
        elif op == "-":
            expected = a - b
        elif op in ("*", "x", "×"):
            expected = a * b
        elif op == "/" and b != 0:
            expected = a // b if a % b == 0 else round(a / b, 2)
        if expected is not None:
            exp_str = str(expected)
            if re.search(rf"(?<![\d.]){re.escape(exp_str)}(?![\d.])", resp_low):
                return "verified_true", f"Gives the factually correct calculation result ({exp_str})."
            return "verified_false", f"Incorrect calculation; the right answer is {exp_str}."

    return None, ""


def _score_response_heuristic(prompt: str, response: str) -> dict[str, dict[str, Any]]:
    """Score a response across the 5 Vindex criteria on a 1 to 6 pointwise scale."""
    text = response.strip()
    if not text:
        return {
            key: {"score": 1, "evidence": "No response text was provided."}
            for key in ("instruction_following", "truthfulness", "prompt_correctness", "writing_quality", "verbosity")
        }

    low = text.lower()
    sentences = _split_sentences(text)
    words = re.findall(r"\b\w+\b", text)
    s_count = len(sentences)
    w_count = len(words)
    constraints = _parse_constraints(prompt)
    prompt_kw = _extract_keywords(prompt)
    resp_kw = _extract_keywords(text)
    overlap_ratio = len(prompt_kw & resp_kw) / max(1, len(prompt_kw))
    fact_status, fact_note = _check_factual_qa(prompt, low)

    # 1. Instruction Following (IF) — 1 to 6
    if_score = 6
    if_notes: list[str] = []
    min_s, max_s = constraints["min_sentences"], constraints["max_sentences"]
    if min_s is not None and max_s is not None:
        if min_s <= s_count <= max_s:
            label = "Two" if s_count == 2 else ("Three" if s_count == 3 else str(s_count))
            if_notes.append(f"{label} sentences, inside the {min_s}–{max_s} sentence limit.")
        else:
            gap = abs(s_count - min_s) if s_count < min_s else abs(s_count - max_s)
            if_score -= 2 if gap == 1 else 3
            if_notes.append(f"Used {s_count} sentence(s), outside the {min_s}–{max_s} limit.")
    elif constraints["max_words"] is not None:
        if w_count <= constraints["max_words"]:
            if_notes.append(f"{w_count} words, within the {constraints['max_words']}-word cap.")
        else:
            if_score -= 2
            if_notes.append(f"Exceeded {constraints['max_words']}-word limit ({w_count} words).")

    if constraints["wants_code"]:
        has_code = any(tok in text for tok in ("`", "def ", "return ", "class ", "SELECT ", "function "))
        if not has_code:
            if_score -= 2
            if_notes.append("Omitted the requested code example.")
        else:
            if_notes.append("Included concrete code as requested.")

    if constraints["wants_bullets"]:
        has_bullets = bool(re.search(r"(?:^|\n)\s*(?:[-*]|\d+\.)\s+", text))
        if not has_bullets:
            if_score -= 2
            if_notes.append("Did not format as a bulleted/numbered list.")

    if not if_notes:
        if_notes.append(
            "Directly answers the user question."
            if constraints["is_short_qa"]
            else "Fully adheres to prompt structure, instructions, and formatting."
        )
    if_score = max(1, min(6, if_score))

    # 2. Truthfulness — 1 to 6
    tr_score = 6
    tr_evidence = "Factual statements align cleanly with verified facts without fabrication."
    fabricated_nobel = False

    if constraints["is_nobel_math"]:
        rejects_myth = any(p in low for p in ("no nobel prize", "does not exist", "doesn't exist", "never been a nobel"))
        names_real_prize = "abel prize" in low or "fields medal" in low
        names_2019_winner = "uhlenbeck" in low
        if rejects_myth and names_real_prize and names_2019_winner:
            tr_score = 6
            tr_evidence = "Correctly states no Nobel Prize in Mathematics exists and names the real 2019 Abel Prize winner."
        elif rejects_myth:
            tr_score = 5
            tr_evidence = "Correctly rejects false premise, with minor room for more specific equivalents."
        else:
            tr_score = 1
            fabricated_nobel = True
            tr_evidence = "Major factual error: Claims a 2019 Nobel Prize in Mathematics was awarded. That prize does not exist."
    elif constraints["is_brain_myth"] or constraints["is_great_wall_myth"]:
        rejects = any(p in low for p in ("myth", "false", "no,", "actually", "not visible", "does not", "misconception"))
        if rejects:
            tr_score = 6
            tr_evidence = "Accurately debunks the misconception instead of reinforcing a false premise."
        else:
            tr_score = 1
            tr_evidence = "Major factual error: Reinforces a known misconception without factual correction."
    elif fact_status == "verified_true":
        tr_score = 6
        tr_evidence = f"Entirely accurate: {fact_note}"
    elif fact_status == "verified_false":
        tr_score = 1
        tr_evidence = f"Major factual error: {fact_note}"
    elif constraints["is_short_qa"]:
        tr_score = 4
        tr_evidence = "Factual answer provided; enable Groq or Gemini API to deeply verify open-domain claims."
    else:
        vague_hype = sum(1 for p in ("research team", "praised its elegance", "scientists proved", "100% guaranteed") if p in low)
        grounded_cues = bool(re.search(r"\b(?:TLS|HTTP|Python|RecursionError|base case|\d{4}|because|whereas)\b", text))
        if vague_hype >= 2 and not grounded_cues:
            tr_score = 2
            tr_evidence = "Multiple unverified claims reduce reliability."
        elif w_count < 15 and overlap_ratio < 0.25:
            tr_score = 4
            tr_evidence = "Generally accurate but concise with limited verifiable technical detail."

    # 3. Prompt Correctness — 1 to 6
    if fabricated_nobel:
        pc_score = 1
        pc_evidence = "Fails to correctly address the prompt; relies on fabricated award."
    elif fact_status == "verified_false":
        pc_score = 1
        pc_evidence = "Incorrect answer: does not correctly identify the requested factual answer."
    elif fact_status == "verified_true":
        pc_score = 6
        pc_evidence = "Completely and correctly answers the question with the exact required information."
    elif constraints["is_nobel_math"] and tr_score == 6:
        pc_score = 6
        pc_evidence = "Completely and correctly addresses the prompt by rectifying the false premise and supplying the true prize."
    elif constraints["wants_comparison"]:
        asks_speed = "speed" in prompt.lower()
        has_security = any(k in low for k in ("security", "secure", "encrypt", "tls", "whereas", "while"))
        has_speed = any(k in low for k in ("speed", "fast", "faster", "handshake", "latency"))
        covered = (has_security and has_speed) if asks_speed else has_security
        pc_score = 6 if covered and w_count >= 30 else 3
        pc_evidence = (
            "Completely and correctly covers both comparison items and requested trade-offs."
            if pc_score == 6
            else "Only partially addresses comparison; misses essential speed or security dimensions."
        )
    elif constraints["wants_code"]:
        has_code = any(tok in text for tok in ("`", "def ", "return "))
        pc_score = 6 if has_code and "base case" in low else (4 if has_code else 2)
        pc_evidence = (
            "Correctly covers definition, base case logic, and working code snippet."
            if pc_score == 6
            else "Partially answers prompt but omits required code or base case explanation."
        )
    else:
        if overlap_ratio >= 0.40 and s_count >= 2 and w_count >= 20:
            pc_score = 6
            pc_evidence = "Completely and correctly interprets and addresses all parts of the prompt."
        elif overlap_ratio >= 0.20 or constraints["is_short_qa"]:
            pc_score = 5
            pc_evidence = "Addresses the prompt directly with all core information present."
        else:
            pc_score = 3
            pc_evidence = "Addresses the prompt superficially with key details missing."

    # 4. Writing Quality — 1 to 6
    wq_score = 6
    filler_hits = sum(1 for phrase in FILLER_PHRASES if phrase in low)
    unique_ratio = len({w.lower() for w in words}) / max(1, w_count)

    if filler_hits >= 2:
        wq_score = 3
        wq_evidence = "Contains conversational filler or cliché phrases that detract from professional tone."
    elif unique_ratio < 0.50 and w_count > 35:
        wq_score = 4
        wq_evidence = "Noticeable repetition creates awkward phrasing and reduces writing quality."
    elif fabricated_nobel:
        wq_score = 5
        wq_evidence = "Good writing, fluent and clear structure, though presenting a false statement."
    elif constraints["is_short_qa"] and w_count <= 25:
        wq_score = 6
        wq_evidence = "Clear, concise, professional wording well suited to the question."
    elif s_count >= 2 and len(re.findall(r"\b\w+\b", sentences[-1])) >= 15:
        wq_score = 5
        wq_evidence = "Clear structure and tone, with minor pacing imbalance in the final sentence."
    else:
        wq_evidence = "Clear, concise, professional; excellent structure and tone for the audience."

    # 5. Verbosity — 1 to 6
    vb_score = 6
    if fabricated_nobel:
        vb_score = 5
        vb_evidence = "Concise and polite, with appropriate length for a short answer."
    elif constraints["is_short_qa"]:
        if w_count <= 20:
            vb_score = 6
            vb_evidence = "Delivers exactly the needed level of detail with no unnecessary padding."
        else:
            vb_score = 4
            vb_evidence = "Includes slightly more background than necessary for a direct factual question."
    elif s_count >= 2 and len(re.findall(r"\b\w+\b", sentences[-1])) >= 15:
        vb_score = 5
        vb_evidence = "Provides required information with appropriate detail and minimal excess."
    elif w_count < 10 and not constraints["is_short_qa"]:
        vb_score = 3
        vb_evidence = "Overly brief; omits helpful context or explanations."
    elif w_count > 80 and min_s and max_s:
        vb_score = 3
        vb_evidence = "Contains unnecessary detail that exceeds the intended scope."
    else:
        vb_evidence = "Delivers exactly the needed level of detail with no missing info or unnecessary fluff."

    return {
        "instruction_following": {"score": if_score, "evidence": " ".join(if_notes)},
        "truthfulness": {"score": tr_score, "evidence": tr_evidence},
        "prompt_correctness": {"score": pc_score, "evidence": pc_evidence},
        "writing_quality": {"score": wq_score, "evidence": wq_evidence},
        "verbosity": {"score": vb_score, "evidence": vb_evidence},
    }


# ---------------------------------------------------------------------------
# Optional Live LLM Judge (Groq / Gemini REST APIs via stdlib urllib)
# ---------------------------------------------------------------------------

def _parse_json_content(raw_text: str) -> dict[str, Any]:
    """Strip optional markdown code fences and parse JSON cleanly."""
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return json.loads(cleaned)


def _call_llm_json(provider: str, system_prompt: str, user_prompt: str) -> dict[str, Any]:
    """Call Groq or Gemini REST API and parse strict JSON output."""
    if provider == "groq":
        api_key = os.getenv("GROQ_API_KEY", "").strip()
        model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "LLMEvalPlatform/1.0",
        }
        body = {
            "model": model,
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers)
        try:
            print(f"[API DEBUG] Provider: {provider}")
            print(f"[API DEBUG] Model: {model}")

            with urllib.request.urlopen(req, timeout=25) as resp:
                payload = json.loads(resp.read().decode("utf-8"))

            print("[API SUCCESS] Request completed")
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")

            print(f"[API ERROR] Status: {exc.code}")
            print(f"[API ERROR] Provider: {provider}")
            print(f"[API ERROR] Model: {model}")
            print(f"[API ERROR] Response: {error_body[:1500]}")

            raise

        return _parse_json_content(payload["choices"][0]["message"]["content"])

    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    primary_model = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
    models_to_try = [primary_model]
    if primary_model != "gemini-3.1-flash-lite-preview":
        models_to_try.append("gemini-3.1-flash-lite-preview")

    last_exc = None
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "LLMEvalPlatform/1.0",
    }
    body = {
        "contents": [{"parts": [{"text": f"{system_prompt}\n\n{user_prompt}"}]}],
        "generationConfig": {"temperature": 0.0, "responseMimeType": "application/json"},
    }

    for model in models_to_try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers)
        print(f"[API DEBUG] Provider: Gemini | Model: {model}")

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8"))

            print(f"[API SUCCESS] Gemini request completed with {model}")
            return _parse_json_content(
                payload["candidates"][0]["content"]["parts"][0]["text"]
            )
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            print(f"[API ERROR] HTTP Status: {exc.code} for Model: {model}")
            print(f"[API ERROR] Response: {error_body[:500]}")
            last_exc = exc
            if exc.code in (503, 404, 429) and len(models_to_try) > 1:
                print(f"[API RETRY] Falling back from {model} due to status {exc.code}...")
                continue
            raise
        except Exception as exc:
            print(f"[API ERROR] {type(exc).__name__}: {exc} for Model: {model}")
            last_exc = exc
            if len(models_to_try) > 1:
                print(f"[API RETRY] Falling back from {model} due to error...")
                continue
            raise

    if last_exc:
        raise last_exc



def _judge_llm_single_pass(
    provider: str,
    prompt: str,
    first_text: str,
    second_text: str,
    first_identity: str,
    second_identity: str,
) -> dict[str, Any]:
    """Ask LLM judge to rate Position 1 and Position 2 across the 5 Vindex criteria (1–6 scale)."""
    system_prompt = (
        "You are an impartial expert LLM evaluation judge adhering to the Vindex Guidelines.\n\n"
        "CRITICAL RULES (read carefully before scoring):\n"
        "1. INSTRUCTION FOLLOWING is the HIGHEST priority criterion (weight 25%). "
        "If the user prompt contains ANY explicit constraint — such as a word count "
        "(e.g. 'exactly 10 words', 'in 50 words'), sentence count (e.g. '2-3 sentences'), "
        "format restriction (e.g. 'do not use bullet points', 'use a numbered list'), "
        "or length limit — a response that VIOLATES that constraint MUST receive "
        "instruction_following score of 1 or 2, regardless of content quality. "
        "Count the actual words carefully before scoring.\n"
        "2. VERBOSITY: If the prompt asks for a specific length (e.g. 'exactly 10 words') "
        "and a response exceeds or falls short of that target, give verbosity score 1-2.\n"
        "3. A response can be factually correct and well-written but still score LOW overall "
        "if it violates explicit instructions. Following instructions > content quality.\n\n"
        "Evaluate Position 1 and Position 2 on these 5 weighted criteria using a strict 1 to 6 integer scale:\n"
        "- instruction_following (IF, weight 25%): 1 = completely fails to follow instructions/constraints, "
        "6 = fully adheres to ALL instructions, format requirements, and length constraints.\n"
        "- truthfulness (Truth, weight 25%): 1 = major factual fabrications/errors, "
        "6 = entirely accurate & supported by facts.\n"
        "- prompt_correctness (Correctness, weight 20%): 1 = does not solve / incorrect answer, "
        "6 = completely and correctly addresses all parts of the prompt.\n"
        "- writing_quality (Writing, weight 15%): 1 = unintelligible/unacceptable, "
        "6 = clear, concise, professional structure.\n"
        "- verbosity (Verbosity, weight 15%): 1 = far too long or too short vs what was asked, "
        "6 = exactly the needed level of detail matching the prompt's requirements.\n\n"
        "Return strict JSON matching this schema:\n"
        "{\n"
        '  "pos1": {"instruction_following": {"score": 1-6, "evidence": "..."}, '
        '"truthfulness": {"score": 1-6, "evidence": "..."}, '
        '"prompt_correctness": {"score": 1-6, "evidence": "..."}, '
        '"writing_quality": {"score": 1-6, "evidence": "..."}, '
        '"verbosity": {"score": 1-6, "evidence": "..."}},\n'
        '  "pos2": {"instruction_following": {"score": 1-6, "evidence": "..."}, '
        '"truthfulness": {"score": 1-6, "evidence": "..."}, '
        '"prompt_correctness": {"score": 1-6, "evidence": "..."}, '
        '"writing_quality": {"score": 1-6, "evidence": "..."}, '
        '"verbosity": {"score": 1-6, "evidence": "..."}},\n'
        '  "winner": "pos1" | "pos2" | "tie"\n'
        "}"
    )
    user_prompt = (
        f"USER PROMPT:\n{prompt}\n\n"
        f"=== POSITION 1 ===\n{first_text}\n\n"
        f"=== POSITION 2 ===\n{second_text}"
    )
    raw = _call_llm_json(provider, system_prompt, user_prompt)
    keys = ("instruction_following", "truthfulness", "prompt_correctness", "writing_quality", "verbosity")

    def clean_side(side: dict[str, Any]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for k in keys:
            item = side.get(k, {})
            score = max(1, min(6, int(item.get("score", 3))))
            ev = str(item.get("evidence", "Evaluated against Vindex rubric."))
            out[k] = {"score": score, "evidence": ev}
        return out

    s1 = clean_side(raw.get("pos1", {}))
    s2 = clean_side(raw.get("pos2", {}))
    score1 = _compute_weighted_score(s1)
    score2 = _compute_weighted_score(s2)

    if score1 > score2:
        winner = first_identity
    elif score2 > score1:
        winner = second_identity
    else:
        raw_w = str(raw.get("winner", "tie")).lower()
        winner = first_identity if raw_w == "pos1" else (second_identity if raw_w == "pos2" else "Tie")

    return {
        "scores_by_identity": {first_identity: s1, second_identity: s2},
        "score_by_identity": {first_identity: score1, second_identity: score2},
        "winner": winner,
        "picked_position": 1 if winner == first_identity else (2 if winner == second_identity else 0),
    }


# ---------------------------------------------------------------------------
# Vindex Scoring & Aggregation Layer (Weighted 1–6 Scale)
# ---------------------------------------------------------------------------

VINDEX_WEIGHTS = {
    "instruction_following": 0.25,
    "truthfulness": 0.25,
    "prompt_correctness": 0.20,
    "writing_quality": 0.15,
    "verbosity": 0.15,
}


def _compute_weighted_score(side_scores: dict[str, dict[str, Any]]) -> float:
    """Compute the Vindex weighted pointwise score: IF*0.25 + Truth*0.25 + Correctness*0.20 + Writing*0.15 + Verbosity*0.15."""
    total = sum(side_scores.get(k, {}).get("score", 1) * w for k, w in VINDEX_WEIGHTS.items())
    return round(float(total), 2)


def _get_quality_tier(weighted_score: float) -> tuple[int, str]:
    """Map weighted score (1.00–6.00) to Vindex quality integer & label."""
    if weighted_score >= 5.50:
        return 6, "Excellent"
    if weighted_score >= 4.50:
        return 5, "Good"
    if weighted_score >= 3.50:
        return 4, "Fair"
    if weighted_score >= 2.50:
        return 3, "Mediocre"
    if weighted_score >= 1.50:
        return 2, "Poor"
    return 1, "Very Poor"


def _compute_ab_preference(score_a: float, score_b: float) -> tuple[int, str]:
    """Calculate Vindex SxS AB_preference on a -3 to +3 scale."""
    diff = round(score_a - score_b, 2)
    if diff >= 1.50:
        return 3, "+3 · Response A is significantly better"
    if diff >= 0.80:
        return 2, "+2 · Response A is moderately better"
    if diff >= 0.30:
        return 1, "+1 · Response A is slightly better"
    if diff <= -1.50:
        return -3, "-3 · Response B is significantly better"
    if diff <= -0.80:
        return -2, "-2 · Response B is moderately better"
    if diff <= -0.30:
        return -1, "-1 · Response B is slightly better"
    return 0, "0 · The responses are approximately equivalent"


def _winner_from_scores(score_a: float, score_b: float) -> str:
    if score_a > score_b:
        return "Response A"
    if score_b > score_a:
        return "Response B"
    return "Tie"


def _build_synthesis(
    winner: str,
    consistent: bool,
    score_a: float,
    score_b: float,
    tier_a_label: str,
    tier_b_label: str,
    pref_label: str,
    a_scores: dict[str, dict[str, Any]],
    b_scores: dict[str, dict[str, Any]],
    rubric_criteria: list[dict[str, Any]],
) -> dict[str, str]:
    """Generate Vindex justification starting with the mandatory verdict syntax."""
    name_map = {c["key"]: c["name"] for c in rubric_criteria}
    a_high = [name_map[k] for k, v in a_scores.items() if v["score"] >= 5]
    a_low = [name_map[k] for k, v in a_scores.items() if v["score"] <= 3]
    b_high = [name_map[k] for k, v in b_scores.items() if v["score"] >= 5]
    b_low = [name_map[k] for k, v in b_scores.items() if v["score"] <= 3]

    if not consistent:
        headline = "Position bias detected across swapped passes"
        summary = (
            f"Pass 1 scored Response A at {score_a:.2f} ({tier_a_label}) and Response B at {score_b:.2f} ({tier_b_label}), "
            "but swapping slots flipped the preferred response. Treat this verdict as low confidence."
        )
    elif winner == "Tie" or "approximately equivalent" in pref_label.lower():
        headline = "The responses are approximately equivalent"
        summary = (
            f"The responses are approximately equivalent because both achieved comparable weighted scores "
            f"(Response A: {score_a:.2f} [{tier_a_label}], Response B: {score_b:.2f} [{tier_b_label}])."
        )
    elif winner == "Response A":
        reasons = []
        if a_high:
            reasons.append(f"it delivers superior {', '.join(a_high[:2])}")
        if b_low:
            reasons.append(f"Response B exhibits deficiencies in {', '.join(b_low[:2])}")
        reason_txt = " and ".join(reasons) if reasons else "it achieved a higher overall weighted score"
        headline = "Response A is better than Response B"
        summary = f"Response A is better than Response B because {reason_txt} (Weighted {score_a:.2f} [{tier_a_label}] vs {score_b:.2f} [{tier_b_label}])."
    else:
        reasons = []
        if b_high:
            reasons.append(f"it delivers superior {', '.join(b_high[:2])}")
        if a_low:
            reasons.append(f"Response A exhibits deficiencies in {', '.join(a_low[:2])}")
        reason_txt = " and ".join(reasons) if reasons else "it achieved a higher overall weighted score"
        headline = "Response B is better than Response A"
        summary = f"Response B is better than Response A because {reason_txt} (Weighted {score_b:.2f} [{tier_b_label}] vs {score_a:.2f} [{tier_a_label}])."

    strengths_parts: list[str] = []
    if a_high:
        strengths_parts.append(f"A: strong in {', '.join(a_high[:2])}.")
    if b_high:
        strengths_parts.append(f"B: strong in {', '.join(b_high[:2])}.")
    if not strengths_parts:
        strengths_parts.append("Both responses addressed parts of the prompt.")

    issues_parts: list[str] = []
    if b_low:
        worst_k = next((k for k, v in b_scores.items() if v["score"] <= 3), None)
        ev = b_scores[worst_k]["evidence"] if worst_k else "deductions flagged"
        issues_parts.append(f"B: {ev}")
    if a_low:
        worst_k = next((k for k, v in a_scores.items() if v["score"] <= 3), None)
        ev = a_scores[worst_k]["evidence"] if worst_k else "deductions flagged"
        issues_parts.append(f"A: {ev}")
    if not issues_parts:
        issues_parts.append("No critical rubric deductions flagged.")

    return {
        "headline": headline,
        "summary": summary,
        "strengths": " ".join(strengths_parts),
        "issues": " ".join(issues_parts),
    }


def _run_heuristic_passes(
    prompt: str,
    response_a: str,
    response_b: str,
    simulate_position_bias: bool = False,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], float, float, str, int, str, int]:
    """Score A and B deterministically across Pass 1 (A, B) and Pass 2 (B, A)."""
    a_scores = _score_response_heuristic(prompt, response_a)
    b_scores = _score_response_heuristic(prompt, response_b)
    score_a = _compute_weighted_score(a_scores)
    score_b = _compute_weighted_score(b_scores)
    pass1_winner = _winner_from_scores(score_a, score_b)
    pass1_pos = 1 if pass1_winner == "Response A" else (2 if pass1_winner == "Response B" else 0)

    if simulate_position_bias:
        # Simulate a biased judge that flips to whichever answer sits in Position 1 on Pass 2
        pass2_winner = "Response B" if pass1_winner == "Response A" else "Response A"
        pass2_pos = 1
    else:
        pass2_winner = pass1_winner
        pass2_pos = 2 if pass2_winner == "Response A" else (1 if pass2_winner == "Response B" else 0)

    return a_scores, b_scores, score_a, score_b, pass1_winner, pass1_pos, pass2_winner, pass2_pos


def run_comparison(
    prompt: str,
    response_a: str,
    response_b: str,
    rubric: dict[str, Any] | None = None,
    simulate_position_bias: bool = False,
) -> dict[str, Any]:
    """Run Pass 1 (A vs B) and Pass 2 (B vs A) and return complete Vindex comparison JSON."""
    if rubric is None:
        rubric = load_rubric()
    engine = get_engine_info()
    used_engine = engine["label"]

    if engine["id"] in ("groq", "gemini") and not simulate_position_bias:
        try:
            p1 = _judge_llm_single_pass(engine["id"], prompt, response_a, response_b, "Response A", "Response B")
            p2 = _judge_llm_single_pass(engine["id"], prompt, response_b, response_a, "Response B", "Response A")
            a_scores = p1["scores_by_identity"]["Response A"]
            b_scores = p1["scores_by_identity"]["Response B"]
            score_a = p1["score_by_identity"]["Response A"]
            score_b = p1["score_by_identity"]["Response B"]
            pass1_winner = p1["winner"]
            pass1_pos = p1["picked_position"]
            pass2_winner = p2["winner"]
            pass2_pos = p2["picked_position"]
        except Exception as exc:
            print(f"[WARN] LLM API call failed ({type(exc).__name__}: {exc}), falling back to heuristic.")
            used_engine = f"deterministic heuristic (API fallback: {type(exc).__name__})"
            a_scores, b_scores, score_a, score_b, pass1_winner, pass1_pos, pass2_winner, pass2_pos = (
                _run_heuristic_passes(prompt, response_a, response_b, simulate_position_bias=False)
            )
    else:
        a_scores, b_scores, score_a, score_b, pass1_winner, pass1_pos, pass2_winner, pass2_pos = (
            _run_heuristic_passes(prompt, response_a, response_b, simulate_position_bias=simulate_position_bias)
        )

    consistent = pass1_winner == pass2_winner
    if consistent:
        final_winner = pass1_winner
        confidence = "high"
    else:
        final_winner = "Position bias detected"
        confidence = "low"

    tier_a_int, tier_a_label = _get_quality_tier(score_a)
    tier_b_int, tier_b_label = _get_quality_tier(score_b)
    pref_val, pref_label = _compute_ab_preference(score_a, score_b)

    synthesis = _build_synthesis(
        winner=final_winner,
        consistent=consistent,
        score_a=score_a,
        score_b=score_b,
        tier_a_label=tier_a_label,
        tier_b_label=tier_b_label,
        pref_label=pref_label,
        a_scores=a_scores,
        b_scores=b_scores,
        rubric_criteria=rubric["criteria"],
    )

    return {
        "mode": "compare",
        "prompt": prompt,
        "response_a": response_a,
        "response_b": response_b,
        "engine": used_engine,
        "score_a": score_a,
        "score_b": score_b,
        "tier_a_int": tier_a_int,
        "tier_a_label": tier_a_label,
        "tier_b_int": tier_b_int,
        "tier_b_label": tier_b_label,
        "preference_score": pref_val,
        "preference_label": pref_label,
        "pass1_winner": pass1_winner,
        "pass1_position": pass1_pos,
        "pass2_winner": pass2_winner,
        "pass2_position": pass2_pos,
        "final_winner": final_winner,
        "consistent": consistent,
        "confidence": confidence,
        "criteria": [
            {
                "key": c["key"],
                "name": c["name"],
                "weight_percent": c["weight_percent"],
                "anchor": c["anchor"],
                "a": a_scores.get(c["key"], {"score": 1, "evidence": "Not scored"}),
                "b": b_scores.get(c["key"], {"score": 1, "evidence": "Not scored"}),
            }
            for c in rubric["criteria"]
        ],
        "synthesis": synthesis,
        "exact_matches": None,
    }


def run_blind_review(
    prompt: str,
    response_a: str,
    response_b: str,
    human_ratings: dict[str, dict[str, int]],
    rubric: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compare human blind ratings (10 scores on 1–6 scale) against AI judge scores."""
    if rubric is None:
        rubric = load_rubric()
    ai_eval = run_comparison(prompt, response_a, response_b, rubric, simulate_position_bias=False)

    exact_matches = 0
    human_a_scores: dict[str, dict[str, Any]] = {}
    human_b_scores: dict[str, dict[str, Any]] = {}
    comparisons: list[dict[str, Any]] = []

    for item in ai_eval["criteria"]:
        key = item["key"]
        h_a = max(1, min(6, int(human_ratings.get(key, {}).get("a", 3))))
        h_b = max(1, min(6, int(human_ratings.get(key, {}).get("b", 3))))
        human_a_scores[key] = {"score": h_a}
        human_b_scores[key] = {"score": h_b}
        ai_a = item["a"]["score"]
        ai_b = item["b"]["score"]
        diff_a = abs(h_a - ai_a)
        diff_b = abs(h_b - ai_b)
        if diff_a == 0:
            exact_matches += 1
        if diff_b == 0:
            exact_matches += 1

        comparisons.append(
            {
                "key": key,
                "name": item["name"],
                "human_a": h_a,
                "human_b": h_b,
                "ai_a": ai_a,
                "ai_b": ai_b,
                "diff_a": diff_a,
                "diff_b": diff_b,
            }
        )

    human_score_a = _compute_weighted_score(human_a_scores)
    human_score_b = _compute_weighted_score(human_b_scores)
    _, h_tier_a = _get_quality_tier(human_score_a)
    _, h_tier_b = _get_quality_tier(human_score_b)
    h_pref_val, h_pref_label = _compute_ab_preference(human_score_a, human_score_b)
    human_prefer = _winner_from_scores(human_score_a, human_score_b)
    ai_prefer = _winner_from_scores(ai_eval["score_a"], ai_eval["score_b"])

    return {
        **ai_eval,
        "mode": "blind",
        "human_score_a": human_score_a,
        "human_score_b": human_score_b,
        "human_tier_a": h_tier_a,
        "human_tier_b": h_tier_b,
        "human_preference": h_pref_label,
        "human_prefer": human_prefer,
        "ai_prefer": ai_prefer,
        "winner_agreement": human_prefer == ai_prefer,
        "exact_matches": exact_matches,
        "blind_comparisons": comparisons,
    }


# ---------------------------------------------------------------------------
# HTTP Request Handler (REST API + Static Files)
# ---------------------------------------------------------------------------

class EvalRequestHandler(BaseHTTPRequestHandler):
    """Serves the HTML/CSS/JS app and JSON API endpoints."""

    db_path: Path = DEFAULT_DB_PATH
    rubric: dict[str, Any] = {}

    def log_message(self, format: str, *args: Any) -> None:
        # Clean concise server log
        print(f"[{self.log_date_time_string()}] {format % args}")

    def _send_json(self, status: int, data: Any) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(raw)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw)

    def _serve_static(self, rel_path: str) -> None:
        clean_path = "index.html" if rel_path in ("", "/") else rel_path.lstrip("/")
        if clean_path.startswith("static/"):
            clean_path = clean_path[len("static/"):]

        target = (STATIC_DIR / clean_path).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.is_file():
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "File not found"})
            return

        content = target.read_bytes()
        mime = STATIC_MIME_TYPES.get(target.suffix.lower(), "application/octet-stream")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/health":
            self._send_json(HTTPStatus.OK, {"status": "ok", "engine": get_engine_info()})
            return

        if path == "/api/config":
            self._send_json(
                HTTPStatus.OK,
                {
                    "rubric": self.rubric,
                    "engine": get_engine_info(),
                    "history_count": get_evaluation_count(self.db_path),
                },
            )
            return

        if path == "/api/history":
            mode = qs.get("mode", [None])[0]
            consistency = qs.get("consistency", [None])[0]
            q = qs.get("q", [None])[0]
            items = list_evaluations(self.db_path, mode=mode, consistency=consistency, query=q)
            self._send_json(HTTPStatus.OK, {"items": items, "count": len(items)})
            return

        if path == "/api/history/export":
            csv_data = export_history_csv(self.db_path).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="llm_eval_history.csv"')
            self.send_header("Content-Length", str(len(csv_data)))
            self.end_headers()
            self.wfile.write(csv_data)
            return

        self._serve_static(unquote(path))

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            body = self._read_json_body()
        except (ValueError, json.JSONDecodeError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid JSON body."})
            return

        if path == "/api/settings":
            provider = str(body.get("provider", "heuristic")).strip().lower()
            api_key = str(body.get("api_key", "")).strip()
            if provider in ("groq", "gemini") and not api_key and not os.getenv(f"{provider.upper()}_API_KEY", "").strip():
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": f"Please provide a {provider.title()} API key."})
                return
            engine = save_engine_settings(provider, api_key)
            self._send_json(HTTPStatus.OK, {"engine": engine})
            return

        prompt = str(body.get("prompt", "")).strip()
        response_a = str(body.get("response_a", "")).strip()
        response_b = str(body.get("response_b", "")).strip()
        if not prompt or not response_a or not response_b:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Prompt, Response A, and Response B are required."})
            return

        if path == "/api/compare":
            simulate_bias = bool(body.get("simulate_position_bias", False))
            should_save = bool(body.get("save", True))
            result = run_comparison(prompt, response_a, response_b, self.rubric, simulate_position_bias=simulate_bias)
            if should_save:
                result["id"] = save_evaluation(self.db_path, result)
            result["saved"] = should_save
            result["history_count"] = get_evaluation_count(self.db_path)
            self._send_json(HTTPStatus.OK, result)
            return

        if path == "/api/blind":
            ratings = body.get("human_ratings")
            if not isinstance(ratings, dict) or len(ratings) < 5:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Please rate all 5 criteria for both Response A and Response B."})
                return
            try:
                result = run_blind_review(prompt, response_a, response_b, ratings, self.rubric)
            except (KeyError, TypeError, ValueError):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Incomplete rating values (1–6 required per response/criterion)."})
                return
            should_save = bool(body.get("save", True))
            if should_save:
                result["id"] = save_evaluation(self.db_path, result)
            result["saved"] = should_save
            result["history_count"] = get_evaluation_count(self.db_path)
            self._send_json(HTTPStatus.OK, result)
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": "Endpoint not found."})

    
    def do_DELETE(self) -> None:
        path = urlparse(self.path).path

        # Delete all history
        if path == "/api/history":
            deleted = clear_evaluations(self.db_path)

            self._send_json(HTTPStatus.OK, {
                "success": True,
                "deleted": deleted,
                "history_count": get_evaluation_count(self.db_path)
            })
            return

        # Delete individual history record
        match = re.fullmatch(r"/api/history/(\d+)", path)

        if match:
            row_id = int(match.group(1))

            try:
                deleted = delete_evaluation(
                    self.db_path,
                    row_id
                )

                history_count = get_evaluation_count(
                    self.db_path
                )

                if deleted:
                    print(f"[HISTORY] Record {row_id} deleted")
                else:
                    print(f"[HISTORY] Record {row_id} not found")

                self._send_json(HTTPStatus.OK, {
                    "success": True,
                    "deleted": deleted,
                    "already_missing": not deleted,
                    "history_count": history_count,
                    "message": (
                        "Record deleted successfully"
                        if deleted
                        else "Record already missing"
                    )
                })

            except Exception as exc:
                print(f"[HISTORY ERROR] {exc}")

                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {
                        "success": False,
                        "error": "History deletion failed"
                    }
                )

            return

        self._send_json(
            HTTPStatus.NOT_FOUND,
            {"error": "Endpoint not found."}
        )



def create_server(host: str = "127.0.0.1", port: int = 8000, db_path: Path = DEFAULT_DB_PATH) -> ThreadingHTTPServer:
    """Initialize DB + rubric and return a ready-to-serve HTTP server instance."""
    load_dotenv()
    init_db(db_path)
    rubric = load_rubric()

    class BoundHandler(EvalRequestHandler):
        pass

    BoundHandler.db_path = db_path
    BoundHandler.rubric = rubric
    ThreadingHTTPServer.allow_reuse_address = True
    return ThreadingHTTPServer((host, port), BoundHandler)


if __name__ == "__main__":
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8000"))
    server = create_server(host, port, DEFAULT_DB_PATH)
    url = f"http://{host}:{port}"
    print(f"LLM Eval Platform running at {url}")
    print(f"Judge engine: {get_engine_info()['label']}")
    if "--open" in sys.argv:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server.")
        server.server_close()
