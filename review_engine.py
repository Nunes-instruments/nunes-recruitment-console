from __future__ import annotations

import hashlib
import html
import json
import re
from pathlib import Path
from typing import Iterable

from database import (
    conn,
    get_by_id,
    get_by_source_key,
    list_applications,
    log,
    now,
    get_state,
    set_state,
)
from automation import extract_text_from_resume
from config import load_settings
from recruitment_pipeline import role_accepts_ranking, canonical_active_role_title
from local_rag import (
    CATEGORY_WEIGHTS,
    EMBEDDING_MODEL,
    SCORING_MODEL_VERSION,
    cosine_similarity,
    embed_texts,
    extract_structured_candidate,
    parse_jd_requirements,
    split_text_chunks,
)

GENERIC_ROLES = {
    "",
    "the position",
    "position",
    "job",
    "the job",
    "unknown",
    "day",
    "days",
    "with",
    "in a",
    "role",
    "all jobs",
    "messages",
    "view billing history",
    "update payment method",
    "view performance report",
    "cookies, privacy and terms",
    "security",
    "billing",
    "upgrade to premium",
}

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "has", "have", "in", "is", "it", "of", "on", "or", "our", "the",
    "their", "this", "to", "with", "will", "you", "your", "candidate",
    "candidates", "requirement", "requirements", "ability", "strong",
    "good", "excellent", "knowledge", "skills", "skill", "work", "working",
    "required", "preferred", "must", "mandatory", "minimum", "essential",
    "desirable",
}

EDUCATION_HINTS = (
    "bachelor", "master", "degree", "diploma", "b.e", "b.tech", "m.tech",
    "bcom", "b.com", "mcom", "m.com", "mba", "bba", "b.sc", "m.sc",
    "engineering", "university", "college",
)

EXPERIENCE_HINTS = (
    "experience", "worked as", "working as", "years", "year", "employment",
    "professional experience", "work history", "career",
)

AUTO_SHORTLIST_THRESHOLD = 85.0

TERMINAL_INDEED_STATUSES = {
    "hired", "selected", "not selected", "rejected", "withdrawn", "archived",
}


def _normalized_indeed_status(value):
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def terminal_ranking_reason(application):
    status = _normalized_indeed_status((application or {}).get("indeed_status"))
    if status not in TERMINAL_INDEED_STATUSES:
        return None
    label = str((application or {}).get("indeed_status") or status).strip()
    return f"Removed from active ranking: Indeed status is {label}."



PROTECTED_REQUIREMENT_RE = re.compile(
    r"(?i)\b("
    r"gender|male|female|woman|women|man|men|sex|sexual|"
    r"age|years\s+old|date\s+of\s+birth|dob|"
    r"religion|religious|caste|race|racial|ethnic|ethnicity|"
    r"marital|married|unmarried|pregnan|"
    r"disability|disabled|medical\s+condition|health\s+condition|"
    r"sexual\s+orientation|gay|lesbian|bisexual|transgender|"
    r"political|party\s+affiliation|trade\s+union|union\s+member|"
    r"nationality|citizenship"
    r")\b"
)


def init_role_review_db():
    """
    Create/migrate role-review tables safely across V11.9 -> V11.10+.

    Important: indexes that reference new V11.10 columns are created only
    AFTER ALTER TABLE migrations. Older V11.9 databases do not yet contain
    rank_position / auto_shortlisted, so creating those indexes first causes
    SQLite `no such column` and breaks the whole Role Review API.
    """
    with conn() as c:
        # Tables first. Keep this schema compatible with both fresh and older DBs.
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS role_profiles (
                job_title TEXT PRIMARY KEY COLLATE NOCASE,
                job_description TEXT NOT NULL DEFAULT '',
                description_source TEXT NOT NULL DEFAULT '',
                indeed_job_url TEXT,
                description_status TEXT NOT NULL DEFAULT 'WAITING',
                description_checked_at TEXT,
                knockout_requirements_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS candidate_reviews (
                application_id INTEGER PRIMARY KEY,
                job_title TEXT,
                analysis_status TEXT NOT NULL DEFAULT 'PENDING',
                resume_fingerprint TEXT,
                role_fingerprint TEXT,
                requirements_json TEXT NOT NULL DEFAULT '[]',
                evidence_json TEXT NOT NULL DEFAULT '[]',
                summary_json TEXT NOT NULL DEFAULT '{}',
                requirements_evidenced INTEGER NOT NULL DEFAULT 0,
                requirements_total INTEGER NOT NULL DEFAULT 0,
                match_score REAL NOT NULL DEFAULT 0,
                confidence_score REAL NOT NULL DEFAULT 0,
                match_classification TEXT NOT NULL DEFAULT 'NOT_ANALYZED',
                jd_version TEXT,
                scoring_model_version TEXT,
                rank_position INTEGER,
                auto_shortlisted INTEGER NOT NULL DEFAULT 0,
                auto_bucket TEXT NOT NULL DEFAULT 'WAITING',
                score_breakdown_json TEXT NOT NULL DEFAULT '{}',
                completed_at TEXT,
                manual_status TEXT NOT NULL DEFAULT 'UNREVIEWED',
                reviewer_note TEXT,
                analyzed_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS rag_vector_cache (
                cache_key TEXT NOT NULL,
                model_name TEXT NOT NULL,
                item_index INTEGER NOT NULL,
                text_hash TEXT NOT NULL,
                content TEXT NOT NULL,
                embedding_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(cache_key, model_name, item_index)
            );

            CREATE INDEX IF NOT EXISTS idx_rag_vector_cache_key
            ON rag_vector_cache(cache_key, model_name);
            """
        )

        # Migrate ROLE table before any query relies on the new fields.
        role_cols = {
            row["name"]
            for row in c.execute("PRAGMA table_info(role_profiles)").fetchall()
        }
        role_additions = {
            "description_source": "TEXT NOT NULL DEFAULT ''",
            "indeed_job_url": "TEXT",
            "description_status": "TEXT NOT NULL DEFAULT 'WAITING'",
            "description_checked_at": "TEXT",
            "knockout_requirements_json": "TEXT NOT NULL DEFAULT '[]'",
        }
        for name, definition in role_additions.items():
            if name not in role_cols:
                c.execute(
                    f"ALTER TABLE role_profiles ADD COLUMN {name} {definition}"
                )

        # Migrate CANDIDATE REVIEW table before creating V11.10 indexes.
        review_cols = {
            row["name"]
            for row in c.execute("PRAGMA table_info(candidate_reviews)").fetchall()
        }
        review_additions = {
            "match_score": "REAL NOT NULL DEFAULT 0",
            "rank_position": "INTEGER",
            "auto_shortlisted": "INTEGER NOT NULL DEFAULT 0",
            "auto_bucket": "TEXT NOT NULL DEFAULT 'WAITING'",
            "score_breakdown_json": "TEXT NOT NULL DEFAULT '{}'",
            "completed_at": "TEXT",
            "confidence_score": "REAL NOT NULL DEFAULT 0",
            "match_classification": "TEXT NOT NULL DEFAULT 'NOT_ANALYZED'",
            "jd_version": "TEXT",
            "scoring_model_version": "TEXT",
        }
        for name, definition in review_additions.items():
            if name not in review_cols:
                c.execute(
                    f"ALTER TABLE candidate_reviews ADD COLUMN {name} {definition}"
                )

        # Normalize old role rows so the frontend sees the correct state.
        c.execute(
            """
            UPDATE role_profiles
            SET description_status=CASE
                    WHEN trim(COALESCE(job_description,''))<>'' THEN 'READY'
                    ELSE 'WAITING'
                END
            WHERE description_status IS NULL
               OR trim(description_status)=''
               OR description_status='WAITING'
            """
        )

        # Indexes LAST: all referenced columns now definitely exist.
        c.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_candidate_reviews_job
            ON candidate_reviews(job_title);

            CREATE INDEX IF NOT EXISTS idx_candidate_reviews_rank
            ON candidate_reviews(job_title, rank_position);

            CREATE INDEX IF NOT EXISTS idx_candidate_reviews_auto_shortlist
            ON candidate_reviews(job_title, auto_shortlisted, rank_position);
            """
        )

        c.execute(
            """
            UPDATE candidate_reviews
            SET analysis_status='PENDING',
                rank_position=NULL,
                auto_shortlisted=0,
                auto_bucket='WAITING',
                updated_at=?
            WHERE analysis_status='READY'
              AND COALESCE(scoring_model_version,'')<>?
            """,
            (now(), SCORING_MODEL_VERSION),
        )


def meaningful_role(value: str | None) -> bool:
    return (value or "").strip().lower() not in GENERIC_ROLES


def _clean_role(value: str | None) -> str:
    cleaned = re.sub(r"\s+", " ", str(value or "")).strip()[:200]
    return canonical_active_role_title(cleaned) if cleaned else ""


def _clean_description(value: str | None) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(?:p|li|div|section|h\d)>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:40000]


def sync_roles_from_applications():
    init_role_review_db()
    timestamp = now()
    seen = set()
    for application in list_applications(10000):
        title = _clean_role(application.get("job_title"))
        if not meaningful_role(title) or not role_accepts_ranking(title):
            continue
        key = title.lower()
        if key in seen:
            continue
        seen.add(key)
        with conn() as connection:
            connection.execute(
                """
                INSERT INTO role_profiles(
                    job_title, job_description, description_source,
                    description_status, created_at, updated_at
                ) VALUES (?, '', '', 'WAITING', ?, ?)
                ON CONFLICT(job_title) DO NOTHING
                """,
                (title, timestamp, timestamp),
            )


def get_role_profile(job_title: str):
    init_role_review_db()
    with conn() as c:
        row = c.execute(
            "SELECT * FROM role_profiles WHERE lower(job_title)=lower(?) LIMIT 1",
            (_clean_role(job_title),),
        ).fetchone()
        return dict(row) if row else None


def save_role_knockout_requirements(job_title: str, requirements: list[str]):
    init_role_review_db()
    title = _clean_role(job_title)
    if not meaningful_role(title):
        raise ValueError("A real active job role is required.")
    clean = []
    seen = set()
    for requirement in requirements or []:
        text = re.sub(r"\s+", " ", str(requirement or "")).strip()[:280]
        key = _normalize_text(text)
        if not key or key in seen or PROTECTED_REQUIREMENT_RE.search(text):
            continue
        seen.add(key)
        clean.append(text)
    ts = now()
    with conn() as connection:
        connection.execute(
            """
            INSERT INTO role_profiles(
                job_title, job_description, description_source,
                description_status, knockout_requirements_json,
                created_at, updated_at
            ) VALUES (?, '', '', 'WAITING', ?, ?, ?)
            ON CONFLICT(job_title) DO UPDATE SET
                knockout_requirements_json=excluded.knockout_requirements_json,
                updated_at=excluded.updated_at
            """,
            (title, json.dumps(clean, ensure_ascii=False), ts, ts),
        )
        connection.execute(
            """
            UPDATE candidate_reviews
            SET analysis_status='PENDING', rank_position=NULL,
                auto_shortlisted=0, auto_bucket='WAITING', updated_at=?
            WHERE lower(trim(job_title))=lower(trim(?))
            """,
            (ts, title),
        )
    return get_role_profile(title)


def _description_hash(description: str) -> str:
    return hashlib.sha256(
        str(description or "").encode("utf-8", "ignore")
    ).hexdigest()


def upsert_role_description(
    job_title: str,
    job_description: str,
    source: str = "indeed",
    job_url: str | None = None,
):
    init_role_review_db()
    title = _clean_role(job_title)
    description = _clean_description(job_description)

    if not meaningful_role(title):
        return None
    if len(description) < 100:
        return None

    ts = now()
    previous = get_role_profile(title) or {}
    changed = (
        _clean_description(previous.get("job_description")) != description
    )

    with conn() as c:
        c.execute(
            """
            INSERT INTO role_profiles(
                job_title, job_description, description_source,
                indeed_job_url, description_status, description_checked_at,
                created_at, updated_at
            )
            VALUES (?,?,?,?, 'READY', ?, ?, ?)
            ON CONFLICT(job_title) DO UPDATE SET
                job_description=excluded.job_description,
                description_source=excluded.description_source,
                indeed_job_url=COALESCE(excluded.indeed_job_url, role_profiles.indeed_job_url),
                description_status='READY',
                description_checked_at=excluded.description_checked_at,
                updated_at=excluded.updated_at
            """,
            (
                title,
                description,
                str(source or "indeed")[:80],
                str(job_url or "")[:1200] or None,
                ts,
                ts,
                ts,
            ),
        )

        if changed:
            c.execute(
                """
                UPDATE candidate_reviews
                SET analysis_status='PENDING',
                    rank_position=NULL,
                    auto_shortlisted=0,
                    auto_bucket='WAITING',
                    updated_at=?
                WHERE lower(job_title)=lower(?)
                """,
                (ts, title),
            )

    return get_role_profile(title)


def save_role_description(job_title: str, job_description: str):
    # Backward-compatible API. V11.10 obtains descriptions automatically from
    # Indeed and the UI no longer requires manual setup.
    result = upsert_role_description(
        job_title,
        job_description,
        source="legacy_manual",
    )
    if not result:
        raise ValueError("A real role title and job description are required.")
    return result


def sync_job_descriptions_from_scan(results):
    updated = []
    for item in results or []:
        title = _clean_role(item.get("job_title"))
        description = item.get("job_description") or ""
        if not meaningful_role(title) or len(str(description).strip()) < 100:
            continue
        role = upsert_role_description(
            title,
            description,
            source=item.get("job_description_source") or "indeed_candidate_job",
            job_url=item.get("job_url"),
        )
        if role:
            updated.append(title)
    return list(dict.fromkeys(updated))


def ingest_discovered_job_descriptions(rows):
    """
    Persist every discovered Indeed role immediately.

    A listing-only role is visible with description_status=WAITING. As soon as
    the full Indeed job description is available, the same profile becomes
    READY and candidate ranking proceeds normally.
    """
    updated = []
    ts = now()

    for row in rows or []:
        title = _clean_role(row.get("job_title") or row.get("title"))
        description = row.get("job_description") or row.get("description") or ""
        job_url = row.get("job_url") or row.get("url")
        source = row.get("source") or "indeed_jobs"

        if not meaningful_role(title):
            continue

        raw_status = re.sub(
            r"\s+",
            " ",
            str(row.get("job_status") or row.get("status") or ""),
        ).strip().lower()

        if any(x in raw_status for x in ("closed", "expired", "filled")):
            # sync_discovered_jobs() still records the CLOSED lifecycle before
            # this function runs. We simply do not feed closed roles into the
            # active ranking workspace.
            continue

        with conn() as c:
            c.execute(
                """
                INSERT INTO role_profiles(
                    job_title, job_description, description_source,
                    indeed_job_url, description_status,
                    description_checked_at, created_at, updated_at
                )
                VALUES (?, '', ?, ?, 'WAITING', ?, ?, ?)
                ON CONFLICT(job_title) DO UPDATE SET
                    indeed_job_url=COALESCE(excluded.indeed_job_url, role_profiles.indeed_job_url),
                    description_source=CASE
                        WHEN trim(COALESCE(role_profiles.job_description,''))=''
                        THEN excluded.description_source
                        ELSE role_profiles.description_source
                    END,
                    description_status=CASE
                        WHEN trim(COALESCE(role_profiles.job_description,''))=''
                        THEN 'WAITING'
                        ELSE role_profiles.description_status
                    END,
                    description_checked_at=excluded.description_checked_at,
                    updated_at=excluded.updated_at
                """,
                (
                    title,
                    str(source or "indeed_jobs")[:80],
                    str(job_url or "")[:1200] or None,
                    ts,
                    ts,
                    ts,
                ),
            )

        if len(str(description).strip()) < 100:
            continue

        role = upsert_role_description(
            title,
            description,
            source=source,
            job_url=job_url,
        )
        if role:
            updated.append(title)

    return list(dict.fromkeys(updated))


def list_roles():
    sync_roles_from_applications()

    with conn() as c:
        rows = c.execute(
            """
            SELECT
                rrs.job_title,
                COALESCE(rrs.lifecycle_status, 'UNKNOWN') AS lifecycle_status,
                COALESCE(rrs.candidate_total_hint, 0) AS candidate_total_hint,
                COALESCE(rrs.candidate_new_hint, 0) AS candidate_new_hint,
                COALESCE(rp.job_description,'') AS job_description,
                COALESCE(rp.description_source,'') AS description_source,
                COALESCE(rp.indeed_job_url, rrs.indeed_job_url) AS indeed_job_url,
                COALESCE(rp.description_status,'WAITING') AS description_status,
                rp.description_checked_at,
                COALESCE(rp.updated_at, rrs.updated_at) AS updated_at,
                COUNT(a.id) AS applicant_count,
                SUM(CASE WHEN lower(trim(COALESCE(a.indeed_status,''))) IN (
                    'hired','selected','not selected','rejected','withdrawn','archived'
                ) THEN 0 ELSE 1 END) AS active_applicant_count,
                SUM(CASE WHEN cr.analysis_status='READY' THEN 1 ELSE 0 END)
                    AS analyzed_count,
                SUM(CASE WHEN cr.analysis_status IN ('READY','EXCLUDED_TERMINAL_STATUS')
                         THEN 0 ELSE 1 END) AS waiting_count,
                SUM(CASE WHEN cr.auto_shortlisted=1 THEN 1 ELSE 0 END)
                    AS auto_shortlisted_count,
                SUM(CASE WHEN cr.analysis_status='EXCLUDED_TERMINAL_STATUS'
                         THEN 1 ELSE 0 END) AS removed_count
            FROM recruitment_role_state rrs
            LEFT JOIN role_profiles rp
              ON lower(trim(rp.job_title)) = lower(trim(rrs.job_title))
            LEFT JOIN applications a
              ON lower(trim(a.job_title)) = lower(trim(rrs.job_title))
            LEFT JOIN candidate_reviews cr
              ON cr.application_id = a.id
            WHERE upper(trim(COALESCE(rrs.lifecycle_status,'UNKNOWN')))
                  IN ('OPEN','PAUSED','FLAGGED')
            GROUP BY
                rrs.job_title, rrs.lifecycle_status, rrs.candidate_total_hint, rrs.candidate_new_hint,
                rp.job_description, rp.description_source,
                rp.indeed_job_url, rrs.indeed_job_url, rp.description_status,
                rp.description_checked_at, rp.updated_at, rrs.updated_at
            ORDER BY
                CASE upper(trim(COALESCE(rrs.lifecycle_status,'UNKNOWN')))
                    WHEN 'OPEN' THEN 0
                    WHEN 'PAUSED' THEN 1
                    WHEN 'FLAGGED' THEN 2
                    ELSE 3
                END,
                COALESCE(rrs.candidate_total_hint, 0) DESC,
                lower(rrs.job_title)
            """
        ).fetchall()
        return [dict(r) for r in rows]


def _normalize_text(value: str) -> str:
    value = str(value or "").lower()
    value = re.sub(r"[^a-z0-9+#./&\- ]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _tokens(value: str):
    return [
        x
        for x in re.findall(r"[a-z0-9+#.]{2,}", _normalize_text(value))
        if x not in STOPWORDS
    ]


def parse_requirements(job_description: str):
    return parse_jd_requirements(job_description)


def _resume_chunks(text: str):
    return split_text_chunks(text)


def _requirement_weight(requirement: str) -> float:
    low = str(requirement or "").lower()
    if any(x in low for x in ["must", "mandatory", "required", "minimum", "essential"]):
        return 1.35
    if any(x in low for x in ["preferred", "advantage", "plus", "nice to have", "desirable"]):
        return 0.75
    return 1.0


def _requirement_evidence(requirement: str, chunks: list[str], normalized_resume: str):
    """Local retrieval + keyword evidence scoring (RAG-style, no API cost).

    Each job requirement acts as a query. Resume chunks are retrieved by token
    overlap, then the best evidence is scored. OpenAI, when enabled, is only a
    small refinement after this deterministic retrieval step.
    """
    req_norm = _normalize_text(requirement)
    req_tokens = list(dict.fromkeys(_tokens(requirement)))[:20]
    weight = _requirement_weight(requirement)

    if not req_tokens:
        return {
            "requirement": requirement,
            "status": "NOT_FOUND",
            "evidence": "",
            "retrieved_evidence": [],
            "matched_terms": [],
            "coverage": 0.0,
            "weight": weight,
        }

    # Exact normalized phrase is the strongest possible local evidence.
    if req_norm and len(req_norm) >= 8 and req_norm in normalized_resume:
        exact = next(
            (chunk for chunk in chunks if req_norm in _normalize_text(chunk)),
            "",
        )
        return {
            "requirement": requirement,
            "status": "EVIDENCE_FOUND",
            "evidence": exact[:420],
            "retrieved_evidence": [exact[:420]] if exact else [],
            "matched_terms": req_tokens,
            "coverage": 1.0,
            "weight": weight,
        }

    # Retrieve the top resume chunks for this requirement. Longer/specific
    # tokens contribute slightly more than short generic tokens.
    ranked_chunks = []
    for chunk in chunks:
        chunk_norm = _normalize_text(chunk)
        if not chunk_norm:
            continue
        matched = [token for token in req_tokens if token in chunk_norm]
        if not matched:
            continue
        weighted_hits = sum(1.0 + min(0.6, max(0, len(token)-4) * 0.06) for token in matched)
        weighted_total = sum(1.0 + min(0.6, max(0, len(token)-4) * 0.06) for token in req_tokens)
        coverage = min(1.0, weighted_hits / max(0.01, weighted_total))
        ranked_chunks.append((coverage, len(matched), chunk, matched))

    ranked_chunks.sort(key=lambda x: (x[0], x[1], len(x[2])), reverse=True)
    top = ranked_chunks[:3]
    if not top:
        return {
            "requirement": requirement,
            "status": "NOT_FOUND",
            "evidence": "",
            "retrieved_evidence": [],
            "matched_terms": [],
            "coverage": 0.0,
            "weight": weight,
        }

    best_coverage, _, best_chunk, best_terms = top[0]
    needed = 1 if len(req_tokens) == 1 else 2
    if len(req_tokens) >= 5:
        needed = 3
    if len(req_tokens) >= 9:
        needed = 4

    evidence_found = len(best_terms) >= needed
    score_coverage = best_coverage if evidence_found else min(best_coverage, 0.42)

    return {
        "requirement": requirement,
        "status": "EVIDENCE_FOUND" if evidence_found else "NOT_FOUND",
        "evidence": best_chunk[:420],
        "retrieved_evidence": [row[2][:420] for row in top],
        "matched_terms": best_terms[:18],
        "coverage": round(score_coverage, 4),
        "weight": weight,
    }


def _cached_vectors(cache_key: str, contents: list[str], model_name: str):
    if not contents:
        return [], model_name
    init_role_review_db()
    text_hashes = [
        hashlib.sha256(content.encode("utf-8", "ignore")).hexdigest()
        for content in contents
    ]
    with conn() as connection:
        rows = connection.execute(
            """
            SELECT model_name, item_index, text_hash, content, embedding_json
            FROM rag_vector_cache
            WHERE cache_key=? AND model_name IN (?, 'lexical-hash-fallback')
            ORDER BY model_name, item_index
            """,
            (cache_key, model_name),
        ).fetchall()

    cached = {}
    for row in rows:
        cached.setdefault(row["model_name"], []).append(row)
    for backend in (model_name, "lexical-hash-fallback"):
        candidates = cached.get(backend) or []
        candidates.sort(key=lambda row: int(row["item_index"]))
        if len(candidates) != len(contents):
            continue
        if not all(
            row["text_hash"] == text_hashes[index]
            and row["content"] == contents[index]
            for index, row in enumerate(candidates)
        ):
            continue
        try:
            return [json.loads(row["embedding_json"]) for row in candidates], backend
        except Exception:
            break

    vectors, backend = embed_texts(contents, model_name=model_name)
    with conn() as connection:
        connection.execute(
            "DELETE FROM rag_vector_cache WHERE cache_key=? AND model_name=?",
            (cache_key, backend),
        )
        connection.executemany(
            """
            INSERT INTO rag_vector_cache(
                cache_key, model_name, item_index, text_hash, content,
                embedding_json, created_at
            ) VALUES (?,?,?,?,?,?,?)
            """,
            [
                (
                    cache_key, backend, index, text_hashes[index], content,
                    json.dumps(vectors[index], separators=(",", ":")), now(),
                )
                for index, content in enumerate(contents)
            ],
        )
    return vectors, backend


def _semantic_evidence(requirements, requirement_vectors, chunks, chunk_vectors):
    evidence = []
    chunk_rows = [
        {"text": text, "vector": vector}
        for text, vector in zip(chunks, chunk_vectors)
    ]
    full_text = " ".join(chunks)
    norm_full = _normalize_text(full_text)

    for requirement, requirement_vector in zip(requirements, requirement_vectors):
        text = str(requirement.get("requirement") or "")
        tokens = list(dict.fromkeys(_tokens(text)))[:24]
        best = None
        for chunk in chunk_rows:
            chunk_text = chunk["text"]
            normalized_chunk = _normalize_text(chunk_text)
            matched = [token for token in tokens if token in normalized_chunk]
            lexical = len(matched) / max(1, len(tokens))
            similarity = max(0.0, cosine_similarity(requirement_vector, chunk["vector"]))
            exact = len(_normalize_text(text)) >= 8 and _normalize_text(text) in normalized_chunk
            current = (exact, similarity, lexical, chunk_text, matched)
            if best is None or current[:3] > best[:3]:
                best = current

        item = {
            "requirement": text,
            "category": requirement.get("category") or "required_skills",
            "must_have": bool(requirement.get("must_have")),
            "status": "NOT_FOUND",
            "evidence": "Not found in resume.",
            "retrieved_evidence": [],
            "matched_terms": [],
            "semantic_similarity": 0.0,
            "lexical_coverage": 0.0,
            "coverage": 0.0,
            "confidence": 0.0,
        }
        if best:
            exact, similarity, chunk_lexical, source_text, matched = best
            global_matched = [token for token in tokens if token in norm_full]
            
            # Special credit for degrees/education
            is_edu = any(d in text.lower() for d in ("degree", "graduate", "b.com", "bba", "b.e", "b.tech", "diploma"))
            if is_edu and any(d in norm_full for d in ("b com", "b.com", "bba", "b e", "b.e", "btech", "degree", "graduate", "diploma")):
                global_matched.extend(["degree", "graduate"])
                
            # Special credit for languages
            for lang in ("english", "tamil", "hindi", "telugu", "malayalam"):
                if lang in text.lower() and lang in norm_full:
                    global_matched.append(lang)
                    
            g_lex = len(set(global_matched)) / max(1, len(tokens))
            lexical = max(chunk_lexical, 0.70 * g_lex + 0.30 * chunk_lexical)
            semantic_coverage = max(0.0, min(1.0, (similarity - 0.25) / 0.40))
            
            if exact:
                status, coverage, confidence = "EXPLICIT", 1.0, 0.98
            elif lexical >= 0.38 or (similarity >= 0.45 and lexical >= 0.10):
                status = "SUPPORTED"
                cov = max(lexical, 0.45 + semantic_coverage * 0.48 if similarity >= 0.25 else lexical * 0.92)
                coverage = min(0.95, max(0.55, cov))
                confidence = min(0.90, 0.55 + lexical * 0.40)
            elif lexical >= 0.20 or similarity >= 0.32:
                status = "INFERRED"
                cov = max(lexical * 0.85, 0.32 + semantic_coverage * 0.40 if similarity >= 0.20 else lexical * 0.78)
                coverage = min(0.75, max(0.38, cov))
                confidence = min(0.75, 0.40 + lexical * 0.35)
            elif lexical >= 0.08 or similarity >= 0.20:
                status, coverage, confidence = "WEAK", min(0.45, 0.20 + lexical * 0.35), 0.40
            else:
                status, coverage, confidence = "NOT_FOUND", 0.0, 0.0

            if status != "NOT_FOUND":
                all_matched = list(dict.fromkeys(matched + global_matched))
                item.update({
                    "status": status,
                    "evidence": source_text[:520] if source_text else "Evidence verified in candidate resume.",
                    "retrieved_evidence": [source_text[:520]] if source_text else [],
                    "matched_terms": all_matched[:18],
                    "semantic_similarity": round(similarity, 4),
                    "lexical_coverage": round(lexical, 4),
                    "coverage": round(coverage, 4),
                    "confidence": round(confidence, 4),
                })
        evidence.append(item)
    return evidence


def _extract_lines(text: str, hints: Iterable[str], limit=5):
    found = []
    for chunk in _resume_chunks(text):
        low = chunk.lower()
        if any(hint in low for hint in hints):
            found.append(chunk[:340])
            if len(found) >= limit:
                break
    return found


def _fingerprint(path: str | None, description: str, resume_text: str = ""):
    h = hashlib.sha256()
    h.update(str(description or "").encode("utf-8", "ignore"))
    if resume_text:
        h.update(str(resume_text).encode("utf-8", "ignore"))
    if path:
        p = Path(path)
        h.update(str(p).encode("utf-8", "ignore"))
        try:
            stat = p.stat()
            h.update(str(stat.st_size).encode())
            h.update(str(stat.st_mtime_ns).encode())
        except Exception:
            pass
    return h.hexdigest()


def _existing_review(application_id: int):
    with conn() as c:
        row = c.execute(
            "SELECT * FROM candidate_reviews WHERE application_id=?",
            (int(application_id),),
        ).fetchone()
        return dict(row) if row else None


def _classification_for_score(score, settings=None):
    values = settings or load_settings()
    value = float(score or 0)
    if value >= float(values.get("ranking_strong_threshold", 85.0)):
        return "STRONG_MATCH"
    if value >= float(values.get("ranking_good_threshold", 70.0)):
        return "GOOD_MATCH"
    if value >= float(values.get("ranking_moderate_threshold", 50.0)):
        return "MODERATE_MATCH"
    return "LOW_MATCH"


def _bucket_for_score(score):
    return _classification_for_score(score)


def _score_evidence(evidence, settings=None):
    values = settings or load_settings()
    weights = dict(CATEGORY_WEIGHTS)
    weights.update(values.get("ranking_weights") or {})
    grouped = {}
    for item in evidence or []:
        grouped.setdefault(item.get("category") or "required_skills", []).append(item)

    active_weight = sum(max(0.0, float(weights.get(category, 0.0))) for category in grouped)
    category_scores = {}
    score = 0.0
    if active_weight:
        for category, items in grouped.items():
            category_weight = max(0.0, float(weights.get(category, 0.0)))
            average = sum(float(item.get("coverage") or 0.0) for item in items) / max(1, len(items))
            maximum_points = 100.0 * category_weight / active_weight
            points = maximum_points * average
            score += points
            category_scores[category] = {
                "weight": category_weight,
                "score": round(points, 2),
                "maximum": round(maximum_points, 2),
                "requirements": len(items),
            }

    score = round(max(0.0, min(100.0, score)), 1)
    classification = _classification_for_score(score, values)
    confidence = 0.0
    if evidence:
        confidence = round(100.0 * sum(
            float(item.get("confidence") or 0.0) for item in evidence
        ) / len(evidence), 1)
    matches = [item for item in evidence if float(item.get("coverage") or 0) >= 0.55]
    missing = [item for item in evidence if float(item.get("coverage") or 0) < 0.35]
    weak = [item for item in evidence if item.get("status") == "WEAK"]
    mandatory_missing = [
        item["requirement"] for item in evidence
        if item.get("must_have") and float(item.get("coverage") or 0) < 0.45
    ]
    breakdown = {
        "method": "sqlite_cached_local_embedding_rag_v1",
        "scoring_model_version": SCORING_MODEL_VERSION,
        "category_weights": weights,
        "category_scores": category_scores,
        "final_score": score,
        "classification": classification,
        "confidence_score": confidence,
        "matched_requirements": len(matches),
        "total_requirements": len(evidence),
        "top_matches": [item["requirement"] for item in sorted(evidence, key=lambda row: row.get("coverage", 0), reverse=True) if float(item.get("coverage") or 0) >= 0.55][:8],
        "top_missing": [item["requirement"] for item in missing[:8]],
        "weak_requirements": [item["requirement"] for item in weak[:8]],
        "mandatory_missing": mandatory_missing,
    }
    return score, classification, breakdown, confidence


def _save_review(
    application_id,
    job_title,
    analysis_status,
    requirements,
    evidence,
    summary,
    evidenced,
    total,
    resume_fingerprint,
    role_fingerprint,
    ts,
    match_score=0.0,
    auto_bucket="WAITING",
    score_breakdown=None,
    confidence_score=0.0,
    jd_version=None,
    scoring_model_version=SCORING_MODEL_VERSION,
):
    ready = analysis_status == "READY"
    thresholds = load_settings()
    breakdown = score_breakdown or {}
    auto_shortlisted = int(
        ready
        and float(match_score or 0) >= float(thresholds.get("ranking_strong_threshold", 85.0))
        and int(total or 0) > 0
        and not breakdown.get("knockout_missing")
    )

    with conn() as c:
        c.execute(
            """
            INSERT INTO candidate_reviews(
                application_id, job_title, analysis_status,
                resume_fingerprint, role_fingerprint,
                requirements_json, evidence_json, summary_json,
                requirements_evidenced, requirements_total,
                match_score, confidence_score, match_classification,
                jd_version, scoring_model_version,
                rank_position, auto_shortlisted,
                auto_bucket, score_breakdown_json, completed_at,
                manual_status, reviewer_note, analyzed_at,
                created_at, updated_at
            )
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(application_id) DO UPDATE SET
                job_title=excluded.job_title,
                analysis_status=excluded.analysis_status,
                resume_fingerprint=excluded.resume_fingerprint,
                role_fingerprint=excluded.role_fingerprint,
                requirements_json=excluded.requirements_json,
                evidence_json=excluded.evidence_json,
                summary_json=excluded.summary_json,
                requirements_evidenced=excluded.requirements_evidenced,
                requirements_total=excluded.requirements_total,
                match_score=excluded.match_score,
                confidence_score=excluded.confidence_score,
                match_classification=excluded.match_classification,
                jd_version=excluded.jd_version,
                scoring_model_version=excluded.scoring_model_version,
                rank_position=CASE
                    WHEN excluded.analysis_status='READY'
                    THEN candidate_reviews.rank_position
                    ELSE NULL
                END,
                auto_shortlisted=excluded.auto_shortlisted,
                auto_bucket=excluded.auto_bucket,
                score_breakdown_json=excluded.score_breakdown_json,
                completed_at=excluded.completed_at,
                analyzed_at=excluded.analyzed_at,
                updated_at=excluded.updated_at
            """,
            (
                int(application_id),
                job_title,
                analysis_status,
                resume_fingerprint,
                role_fingerprint,
                json.dumps(requirements, ensure_ascii=False),
                json.dumps(evidence, ensure_ascii=False),
                json.dumps(summary, ensure_ascii=False),
                int(evidenced),
                int(total),
                float(match_score or 0),
                float(confidence_score or 0),
                str(breakdown.get("classification") or "NOT_ANALYZED"),
                jd_version,
                scoring_model_version,
                None,
                auto_shortlisted,
                auto_bucket,
                json.dumps(breakdown, ensure_ascii=False),
                ts if ready else None,
                "UNREVIEWED",
                None,
                ts,
                ts,
                ts,
            ),
        )
    return get_review(application_id)


def _decode_review(row: dict):
    out = dict(row)
    for key, default in [
        ("requirements_json", []),
        ("evidence_json", []),
        ("summary_json", {}),
        ("score_breakdown_json", {}),
    ]:
        try:
            out[key.replace("_json", "")] = json.loads(
                out.get(key) or json.dumps(default)
            )
        except Exception:
            out[key.replace("_json", "")] = default
    return out


def get_review(application_id: int):
    row = _existing_review(application_id)
    return _decode_review(row) if row else None


def invalidate_application_review(application_id: int):
    """Queue one changed resume for recalculation without deleting its prior evidence."""
    with conn() as connection:
        connection.execute(
            """
            UPDATE candidate_reviews
            SET analysis_status='PENDING', rank_position=NULL,
                auto_shortlisted=0, auto_bucket='WAITING', updated_at=?
            WHERE application_id=?
              AND analysis_status='READY'
            """,
            (now(), int(application_id)),
        )


def _explain_with_local_model(evidence, score, confidence, classification, settings):
    supported = [
        {
            "requirement": item.get("requirement"),
            "status": item.get("status"),
            "resume_evidence": item.get("evidence"),
        }
        for item in evidence
        if item.get("status") in {"EXPLICIT", "SUPPORTED", "INFERRED", "WEAK"}
    ][:16]
    matched = [item["requirement"] for item in evidence if item.get("coverage", 0) >= 0.55]
    missing = [item["requirement"] for item in evidence if item.get("coverage", 0) < 0.35]
    fallback = (
        f"{classification.replace('_', ' ').title()} match ({score:.1f}%). "
        f"{len(matched)} of {len(evidence)} requirements have supporting resume evidence."
    )
    if missing:
        fallback += " Not found in resume: " + "; ".join(missing[:3]) + "."
    fallback += f" Evidence confidence: {confidence:.0f}%."

    if not settings.get("local_ai_enabled") or not supported:
        return fallback, "evidence_template"

    try:
        import urllib.request

        prompt = (
            "Write one short professional explanation using only the supplied resume evidence. "
            "Do not infer missing skills or qualifications. Do not mention protected traits. "
            "If evidence is absent, say it was not found in the resume. Return JSON only: "
            '{"explanation":"..."}. Evidence: '
            + json.dumps({"score": score, "classification": classification, "evidence": supported}, ensure_ascii=False)
        )
        request = urllib.request.Request(
            (settings.get("ollama_url") or "http://127.0.0.1:11434").rstrip("/") + "/api/generate",
            data=json.dumps({
                "model": settings.get("ollama_model") or "llama3.2:3b",
                "prompt": prompt,
                "stream": False,
                "format": "json",
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            result = json.loads(response.read().decode("utf-8", "replace"))
        generated = json.loads(result.get("response") or "{}")
        explanation = re.sub(r"\s+", " ", str(generated.get("explanation") or "")).strip()
        if explanation and len(explanation) <= 500:
            return explanation, "local_ollama_evidence_only"
    except Exception as exc:
        log("INFO", f"Local explanation unavailable; evidence template used: {exc}")
    return fallback, "evidence_template"


def analyze_application(application_id: int, force=False):
    init_role_review_db()
    app = get_by_id(int(application_id))
    if not app:
        raise ValueError("Applicant not found.")

    job_title = _clean_role(app.get("job_title"))
    existing = _existing_review(application_id)
    ts = now()

    terminal_reason = terminal_ranking_reason(app)
    if terminal_reason:
        return _save_review(
            application_id, job_title, "EXCLUDED_TERMINAL_STATUS",
            [], [], {
                "ranking_basis": terminal_reason,
                "indeed_status": app.get("indeed_status"),
            }, 0, 0, None, None, ts,
            match_score=0.0, auto_bucket="REMOVED",
            score_breakdown={
                "method": "workflow_status_exclusion",
                "reason": terminal_reason,
            },
        )

    if not meaningful_role(job_title):
        return _save_review(
            application_id, job_title, "WAITING_FOR_ROLE", [], [], {}, 0, 0,
            None, None, ts,
        )

    if not role_accepts_ranking(job_title):
        # Freeze completed ranking/history when the Indeed job is paused/closed.
        # A candidate that never finished ranking is simply held, not deleted.
        if existing and existing.get("analysis_status") == "READY":
            return _decode_review(existing)
        return _save_review(
            application_id, job_title, "ROLE_CLOSED", [], [], {
                "ranking_basis": "Role is paused/closed on Indeed; active ranking is stopped."
            }, 0, 0, None, None, ts,
            match_score=0.0, auto_bucket="ROLE_CLOSED",
        )

    sync_roles_from_applications()
    profile = get_role_profile(job_title) or {}
    description = _clean_description(profile.get("job_description"))
    settings = load_settings()
    embedding_model = str(settings.get("ranking_embedding_model") or EMBEDDING_MODEL)

    if not description:
        return _save_review(
            application_id, job_title, "WAITING_FOR_JOB_DESCRIPTION",
            [], [], {}, 0, 0, None, None, ts,
        )

    requirements = parse_requirements(description)
    jd_version = _description_hash(description)
    role_version_payload = json.dumps({
        "jd_version": jd_version,
        "scoring_model_version": SCORING_MODEL_VERSION,
        "embedding_model": embedding_model,
        "ranking_weights": settings.get("ranking_weights") or CATEGORY_WEIGHTS,
        "thresholds": [
            settings.get("ranking_strong_threshold", 85),
            settings.get("ranking_good_threshold", 70),
            settings.get("ranking_moderate_threshold", 50),
        ],
        "knockouts": profile.get("knockout_requirements_json") or "[]",
    }, sort_keys=True, ensure_ascii=False)
    role_fp = _description_hash(role_version_payload)

    if not requirements:
        return _save_review(
            application_id, job_title, "WAITING_FOR_JOB_DESCRIPTION",
            [], [], {}, 0, 0, None, role_fp, ts,
        )

    resume_path = app.get("resume_path")
    cached_resume_text = str(
        app.get("resume_text_cache")
        or ""
    ).strip()

    file_resume_text = ""
    if resume_path and Path(resume_path).exists():
        file_resume_text = extract_text_from_resume(resume_path).strip()

    # Prefer the richer text source. This fixes the previous gap where Indeed
    # could expose resume text in the candidate drawer without producing a
    # downloaded resume file; email extraction worked, but ranking incorrectly
    # stayed at WAITING_FOR_RESUME.
    if len(file_resume_text) >= len(cached_resume_text):
        resume_text = file_resume_text
        resume_text_source = "downloaded_resume"
    else:
        resume_text = cached_resume_text
        resume_text_source = "indeed_inline_resume"

    if not resume_text:
        return _save_review(
            application_id, job_title, "WAITING_FOR_RESUME",
            requirements, [], {
                "ranking_basis": (
                    "Waiting for resume text from the verified Indeed candidate application."
                ),
                "resume_text_source": "none",
            }, 0, len(requirements),
            None, role_fp, ts,
        )

    fp = _fingerprint(resume_path, description, resume_text)
    if (
        not force
        and existing
        and existing.get("resume_fingerprint") == fp
        and existing.get("role_fingerprint") == role_fp
        and existing.get("scoring_model_version") == SCORING_MODEL_VERSION
        and existing.get("analysis_status") == "READY"
    ):
        # Completed candidates are intentionally skipped until either the
        # resume evidence or the official Indeed job description changes.
        return _decode_review(existing)

    knockout_rules = []
    try:
        knockout_rules = json.loads(profile.get("knockout_requirements_json") or "[]")
    except Exception:
        knockout_rules = []
    knockout_rules = [str(rule).strip() for rule in knockout_rules if str(rule).strip()]
    requirement_keys = {_normalize_text(item.get("requirement")) for item in requirements}
    for rule in knockout_rules:
        if _normalize_text(rule) not in requirement_keys:
            requirements.append({
                "requirement": rule[:280],
                "category": "required_skills",
                "must_have": True,
                "admin_knockout": True,
            })

    chunks = _resume_chunks(resume_text)
    jd_hash = _description_hash(description)
    resume_hash = hashlib.sha256(resume_text.encode("utf-8", "ignore")).hexdigest()
    requirement_vectors, requirement_backend = _cached_vectors(
        f"jd:{jd_hash}",
        [item["requirement"] for item in requirements],
        embedding_model,
    )
    chunk_vectors, resume_backend = _cached_vectors(
        f"resume:{resume_hash}",
        chunks,
        embedding_model,
    )
    evidence = _semantic_evidence(
        requirements,
        requirement_vectors,
        chunks,
        chunk_vectors,
    )
    evidence_by_requirement = {
        _normalize_text(item.get("requirement")): item
        for item in evidence
    }
    knockout_missing = [
        rule for rule in knockout_rules
        if float(evidence_by_requirement.get(_normalize_text(rule), {}).get("coverage") or 0) < 0.45
    ]
    evidenced = sum(
        1 for item in evidence if float(item.get("coverage") or 0) >= 0.55
    )
    score, bucket, breakdown, confidence = _score_evidence(evidence, settings)
    breakdown["embedding_backend"] = (
        embedding_model
        if requirement_backend == resume_backend == embedding_model
        else f"{requirement_backend}/{resume_backend}"
    )
    breakdown["knockout_missing"] = knockout_missing
    explanation, explanation_source = _explain_with_local_model(
        evidence, score, confidence, bucket, settings
    )
    breakdown["explanation_source"] = explanation_source

    summary = {
        "candidate_profile": extract_structured_candidate(resume_text),
        "education_evidence": _extract_lines(resume_text, EDUCATION_HINTS, 6),
        "experience_evidence": _extract_lines(resume_text, EXPERIENCE_HINTS, 6),
        "resume_character_count": len(resume_text),
        "resume_text_source": resume_text_source,
        "ranking_basis": "Local resume embeddings + retrieved evidence + deterministic weighted scoring; protected attributes are excluded",
        "ai_reason": explanation,
        "confidence_score": confidence,
        "knockout_missing": knockout_missing,
    }

    return _save_review(
        application_id,
        job_title,
        "READY",
        requirements,
        evidence,
        summary,
        evidenced,
        len(requirements),
        fp,
        role_fp,
        ts,
        match_score=score,
        auto_bucket=bucket,
        score_breakdown=breakdown,
        confidence_score=confidence,
        jd_version=jd_hash,
        scoring_model_version=SCORING_MODEL_VERSION,
    )


def refresh_role_ranks(job_title: str):
    title = _clean_role(job_title)
    if not meaningful_role(title):
        return 0

    if not role_accepts_ranking(title):
        # Final ranking is intentionally frozen only when the job is closed.
        with conn() as c:
            row = c.execute(
                """
                SELECT COUNT(*) AS n FROM candidate_reviews
                WHERE lower(trim(job_title))=lower(trim(?))
                  AND analysis_status='READY' AND rank_position IS NOT NULL
                """,
                (title,),
            ).fetchone()
            return int(row["n"] or 0)

    with conn() as c:
        terminal_rows = c.execute(
            """
            SELECT a.id, a.indeed_status
            FROM applications a
            WHERE lower(trim(a.job_title))=lower(trim(?))
              AND lower(trim(COALESCE(a.indeed_status,''))) IN (
                  'hired','selected','not selected','rejected','withdrawn','archived'
              )
            """,
            (title,),
        ).fetchall()

        for terminal in terminal_rows:
            reason = (
                "Removed from active ranking: Indeed status is "
                + str(terminal["indeed_status"] or "terminal") + "."
            )
            c.execute(
                """
                UPDATE candidate_reviews
                SET analysis_status='EXCLUDED_TERMINAL_STATUS',
                    rank_position=NULL, auto_shortlisted=0,
                    auto_bucket='REMOVED', score_breakdown_json=?, updated_at=?
                WHERE application_id=?
                """,
                (json.dumps({"method":"workflow_status_exclusion","reason":reason},
                            ensure_ascii=False), now(), terminal["id"]),
            )

        ready = c.execute(
            """
            SELECT cr.application_id, cr.match_score, cr.requirements_evidenced,
                   cr.requirements_total, cr.completed_at
            FROM candidate_reviews cr
            JOIN applications a ON a.id=cr.application_id
            WHERE lower(trim(cr.job_title))=lower(trim(?))
              AND cr.analysis_status='READY'
              AND lower(trim(COALESCE(a.indeed_status,''))) NOT IN (
                  'hired','selected','not selected','rejected','withdrawn','archived'
              )
            ORDER BY cr.match_score DESC,
                     datetime(COALESCE(a.first_seen_at,a.created_at,'')) ASC,
                     requirements_evidenced DESC,
                     requirements_total DESC,
                     application_id ASC
            """,
            (title,),
        ).fetchall()

        c.execute(
            """
            UPDATE candidate_reviews
            SET rank_position=NULL
            WHERE lower(trim(job_title))=lower(trim(?))
              AND analysis_status<>'READY'
            """,
            (title,),
        )

        for index, row in enumerate(ready, start=1):
            c.execute(
                """
                UPDATE candidate_reviews
                SET rank_position=?,
                    auto_shortlisted=CASE WHEN match_score>=? THEN 1 ELSE 0 END,
                    auto_bucket=CASE
                        WHEN match_score>=? THEN 'AUTO_SHORTLIST'
                        WHEN match_score>=55 THEN 'STRONG_MATCH'
                        WHEN match_score>=35 THEN 'POSSIBLE_MATCH'
                        ELSE 'LOW_EVIDENCE'
                    END,
                    updated_at=?
                WHERE application_id=?
                """,
                (
                    index,
                    AUTO_SHORTLIST_THRESHOLD,
                    AUTO_SHORTLIST_THRESHOLD,
                    now(),
                    row["application_id"],
                ),
            )

    return len(ready)


def analyze_role(job_title: str, force=False, limit=1500):
    title = _clean_role(job_title)
    if meaningful_role(title) and not role_accepts_ranking(title):
        return {"analyzed": 0, "skipped_completed": 0, "ranked": refresh_role_ranks(title), "errors": 0, "role_closed": True}
    analyzed = 0
    skipped_completed = 0
    errors = 0

    with conn() as c:
        rows = c.execute(
            """
            SELECT a.id,
                   cr.analysis_status,
                   cr.resume_fingerprint,
                   cr.role_fingerprint
            FROM applications a
            LEFT JOIN candidate_reviews cr ON cr.application_id=a.id
            WHERE lower(trim(a.job_title))=lower(trim(?))
            ORDER BY
                CASE WHEN lower(trim(COALESCE(a.indeed_status,'')))='new'
                     THEN 0 ELSE 1 END,
                datetime(COALESCE(a.last_seen_at,a.first_seen_at,a.created_at)) DESC,
                a.id DESC
            LIMIT ?
            """,
            (title, int(limit)),
        ).fetchall()

    for row in rows:
        try:
            before = _existing_review(row["id"])
            result = analyze_application(row["id"], force=force)
            if (
                before
                and before.get("analysis_status") == "READY"
                and result
                and result.get("analysis_status") == "READY"
                and before.get("resume_fingerprint") == result.get("resume_fingerprint")
                and before.get("role_fingerprint") == result.get("role_fingerprint")
            ):
                skipped_completed += 1
            else:
                analyzed += 1
        except Exception as exc:
            errors += 1
            log(
                "WARN",
                f"Automatic role ranking failed for application {row['id']}: {exc}",
            )

    ranked = refresh_role_ranks(title)
    return {
        "analyzed": analyzed,
        "skipped_completed": skipped_completed,
        "ranked": ranked,
        "errors": errors,
    }


def analyze_scan_results(results, limit=24):
    roles = set()
    processed = 0
    waiting = 0

    ordered_results = sorted(
        list(results or []),
        key=lambda item: 0 if (
            item.get("new_applicant")
            or item.get("current_new")
            or _normalized_indeed_status(item.get("indeed_status")) == "new"
        ) else 1,
    )

    for item in ordered_results[: int(limit)]:
        source_key = item.get("source_key")
        if not source_key:
            continue
        app = get_by_source_key(source_key)
        if not app:
            continue
        title = _clean_role(app.get("job_title"))
        if not meaningful_role(title):
            continue
        roles.add(title)
        try:
            review = analyze_application(app["id"], force=False)
            if review and review.get("analysis_status") == "READY":
                processed += 1
            else:
                waiting += 1
        except Exception as exc:
            waiting += 1
            log("WARN", f"Immediate role ranking waiting for {source_key}: {exc}")

    for title in roles:
        refresh_role_ranks(title)

    return {
        "processed": processed,
        "waiting": waiting,
        "roles": sorted(roles),
    }


def analyze_pending_reviews(limit=40):
    init_role_review_db()
    sync_roles_from_applications()

    with conn() as c:
        rows = c.execute(
            """
            SELECT a.id, a.job_title
            FROM applications a
            LEFT JOIN candidate_reviews cr ON cr.application_id=a.id
            LEFT JOIN role_profiles rp
              ON lower(trim(rp.job_title))=lower(trim(a.job_title))
            WHERE lower(trim(COALESCE(a.job_title,'')))
                    NOT IN ('','the position','position','job','the job','unknown')
              AND lower(trim(COALESCE(a.indeed_status,''))) NOT IN (
                    'hired','selected','not selected','rejected','withdrawn','archived'
                  )
              AND rp.job_description IS NOT NULL
              AND trim(rp.job_description)<>''
              AND (
                    cr.application_id IS NULL
                    OR cr.analysis_status NOT IN ('READY','EXCLUDED_TERMINAL_STATUS')
                  )
            ORDER BY
                CASE WHEN lower(trim(COALESCE(a.indeed_status,'')))='new'
                     THEN 0 ELSE 1 END,
                datetime(COALESCE(a.last_seen_at,a.first_seen_at,a.created_at)) DESC,
                a.id DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()

    done = 0
    touched_roles = set()
    for row in rows:
        try:
            title = _clean_role(row["job_title"])
            if not role_accepts_ranking(title):
                continue
            analyze_application(row["id"], force=False)
            touched_roles.add(title)
            done += 1
        except Exception as exc:
            log(
                "WARN",
                f"Background automatic role ranking failed for {row['id']}: {exc}",
            )

    for title in touched_roles:
        refresh_role_ranks(title)

    return done


def set_manual_status(application_id: int, status: str, note: str | None = None):
    raise ValueError(
        "Manual shortlist is disabled. V11.10 maintains the automated role ranking."
    )


def ranking_status():
    init_role_review_db()
    sync_roles_from_applications()
    with conn() as c:
        role_row = c.execute(
            """
            SELECT
                COUNT(*) AS total_roles,
                SUM(CASE WHEN trim(job_description)<>'' THEN 1 ELSE 0 END)
                    AS roles_ready,
                SUM(CASE WHEN trim(job_description)='' THEN 1 ELSE 0 END)
                    AS roles_waiting_description
            FROM role_profiles rp
            LEFT JOIN recruitment_role_state rrs
              ON lower(trim(rrs.job_title))=lower(trim(rp.job_title))
            WHERE upper(trim(COALESCE(rrs.lifecycle_status,'UNKNOWN'))) <> 'CLOSED'
            """
        ).fetchone()
        review_row = c.execute(
            """
            SELECT
                SUM(CASE WHEN analysis_status='READY' THEN 1 ELSE 0 END)
                    AS ranked_candidates,
                SUM(CASE WHEN analysis_status<>'READY' THEN 1 ELSE 0 END)
                    AS waiting_candidates,
                SUM(CASE WHEN auto_shortlisted=1 THEN 1 ELSE 0 END)
                    AS auto_shortlisted,
                SUM(CASE WHEN analysis_status='EXCLUDED_TERMINAL_STATUS'
                         THEN 1 ELSE 0 END) AS removed_candidates
            FROM candidate_reviews cr
            LEFT JOIN applications a ON a.id=cr.application_id
            LEFT JOIN recruitment_role_state rrs
              ON lower(trim(rrs.job_title))=lower(trim(a.job_title))
            WHERE upper(trim(COALESCE(rrs.lifecycle_status,'UNKNOWN'))) <> 'CLOSED'
            """
        ).fetchone()

    return {
        "total_roles": int(role_row["total_roles"] or 0),
        "roles_ready": int(role_row["roles_ready"] or 0),
        "roles_waiting_description": int(role_row["roles_waiting_description"] or 0),
        "ranked_candidates": int(review_row["ranked_candidates"] or 0),
        "waiting_candidates": int(review_row["waiting_candidates"] or 0),
        "auto_shortlisted": int(review_row["auto_shortlisted"] or 0),
        "removed_candidates": int(review_row["removed_candidates"] or 0),
        "shortlist_threshold": AUTO_SHORTLIST_THRESHOLD,
    }


def role_review_payload(job_title: str):
    init_role_review_db()
    title = _clean_role(job_title)
    profile = get_role_profile(title)

    with conn() as c:
        rows = c.execute(
            """
            SELECT
                a.*,
                cr.analysis_status,
                cr.requirements_json,
                cr.evidence_json,
                cr.summary_json,
                cr.requirements_evidenced,
                cr.requirements_total,
                cr.match_score,
                cr.confidence_score,
                cr.match_classification,
                cr.jd_version,
                cr.scoring_model_version,
                cr.rank_position,
                cr.auto_shortlisted,
                cr.auto_bucket,
                cr.score_breakdown_json,
                cr.completed_at,
                cr.analyzed_at
            FROM applications a
            LEFT JOIN candidate_reviews cr ON cr.application_id=a.id
            WHERE lower(trim(a.job_title))=lower(trim(?))
            ORDER BY
                CASE WHEN cr.analysis_status='READY' THEN 0 ELSE 1 END,
                COALESCE(cr.rank_position, 999999) ASC,
                a.id DESC
            """,
            (title,),
        ).fetchall()

    applicants = []
    for row in rows:
        item = dict(row)
        for key, default in [
            ("requirements_json", []),
            ("evidence_json", []),
            ("summary_json", {}),
            ("score_breakdown_json", {}),
        ]:
            try:
                item[key.replace("_json", "")] = json.loads(
                    item.get(key) or json.dumps(default)
                )
            except Exception:
                item[key.replace("_json", "")] = default
        applicants.append(item)

    removed = [
        x for x in applicants
        if x.get("analysis_status") == "EXCLUDED_TERMINAL_STATUS"
        or _normalized_indeed_status(x.get("indeed_status")) in TERMINAL_INDEED_STATUSES
    ]
    active_applicants = [x for x in applicants if x not in removed]
    auto_shortlist = [
        x for x in active_applicants
        if int(x.get("auto_shortlisted") or 0) == 1
        and x.get("analysis_status") == "READY"
    ]
    remaining = [x for x in active_applicants if x not in auto_shortlist]

    return {
        "role": profile or {
            "job_title": title,
            "job_description": "",
            "description_source": "",
            "description_status": "WAITING",
        },
        "applicants": applicants,
        "active_applicants": active_applicants,
        "auto_shortlist": auto_shortlist,
        "remaining": remaining,
        "removed": removed,
        "ranking": {
            "ranked": sum(1 for x in active_applicants if x.get("analysis_status") == "READY"),
            "waiting": sum(1 for x in active_applicants if x.get("analysis_status") != "READY"),
            "auto_shortlisted": len(auto_shortlist),
            "removed": len(removed),
            "threshold": AUTO_SHORTLIST_THRESHOLD,
        },
        "notice": (
            "Ranking uses only the official role requirements and resume evidence. "
            "Terminal Indeed workflow states are removed from active ranking but retained in history. "
            "Gender/sex is not inferred or used."
        ),
    }
