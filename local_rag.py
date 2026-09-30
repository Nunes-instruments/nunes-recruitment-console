from __future__ import annotations

import hashlib
import math
import re
import threading
from pathlib import Path

from config import STATE_DIR

EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
SCORING_MODEL_VERSION = "local-rag-v1"
EMBEDDING_CACHE_DIR = STATE_DIR / "models" / "fastembed"

PROTECTED_ATTRIBUTE_RE = re.compile(
    r"(?i)\b(?:gender|male|female|woman|women|man|men|sex|age|years\s+old|"
    r"date\s+of\s+birth|dob|religion|caste|race|ethnicity|marital|married|"
    r"unmarried|pregnan\w*|disab\w*|medical\s+condition|health\s+condition|"
    r"sexual\s+orientation|political\s+views?|nationality|citizenship)\b"
)

STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "has", "have", "in", "is", "it", "of", "on", "or", "our", "the",
    "their", "this", "to", "with", "will", "you", "your", "candidate",
    "candidates", "requirement", "requirements", "ability", "strong", "good",
    "excellent", "knowledge", "skills", "skill", "work", "working", "required",
    "preferred", "must", "mandatory", "minimum", "essential", "experience",
}

CATEGORY_WEIGHTS = {
    "required_skills": 35.0,
    "relevant_experience": 25.0,
    "responsibilities": 20.0,
    "domain_experience": 10.0,
    "education_certifications": 5.0,
    "preferred_skills": 5.0,
}

HEADING_CATEGORIES = (
    ("preferred_skills", re.compile(r"(?i)\b(?:preferred|desirable|nice\s+to\s+have|advantageous)\b")),
    ("relevant_experience", re.compile(r"(?i)\b(?:experience|years?\s+of\s+experience|work\s+history)\b")),
    ("responsibilities", re.compile(r"(?i)\b(?:responsibilities|duties|what\s+you(?:'|\s)?ll\s+do|key\s+activities)\b")),
    ("domain_experience", re.compile(r"(?i)\b(?:industry|domain|sector|market\s+experience)\b")),
    ("education_certifications", re.compile(r"(?i)\b(?:education|qualification|certification|licen[cs]e)\b")),
    ("required_skills", re.compile(r"(?i)\b(?:required\s+skills|must\s+have|requirements|qualifications)\b")),
)

SKILL_TERMS = (
    "instrumentation", "industrial automation", "process control", "PLC", "SCADA",
    "DCS", "HMI", "VFD", "electrical", "mechanical", "calibration", "sensors",
    "transmitters", "pressure instruments", "temperature instruments", "valves",
    "pneumatics", "hydraulics", "AutoCAD", "Tally", "GST", "TDS", "Excel",
    "CRM", "ERP", "SAP", "Python", "SQL", "Java", "Power BI", "accounting",
    "sales", "technical sales", "customer support", "quotation", "procurement",
)

_model = None
_model_error = None
_model_lock = threading.Lock()


class EmbeddingUnavailable(RuntimeError):
    pass


def normalize_text(value: str) -> str:
    value = str(value or "").lower()
    value = re.sub(r"[^a-z0-9+#./&\- ]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def split_text_chunks(text: str, max_chars: int = 520, overlap_chars: int = 80) -> list[str]:
    """Split extracted text into evidence-sized chunks without dropping source wording."""
    paragraphs = [
        re.sub(r"\s+", " ", part).strip()
        for part in re.split(r"\n\s*\n|\r\n|\n|(?<=[.!?])\s+", str(text or ""))
    ]
    chunks: list[str] = []
    current = ""

    for paragraph in paragraphs:
        if len(paragraph) < 18 or PROTECTED_ATTRIBUTE_RE.search(paragraph):
            continue
        remaining = paragraph
        while len(remaining) > max_chars:
            boundary = remaining.rfind(" ", 0, max_chars)
            if boundary < max_chars // 2:
                boundary = max_chars
            piece = remaining[:boundary].strip()
            if piece:
                chunks.append(piece)
            remaining = remaining[max(0, boundary - overlap_chars):].strip()
        if not remaining:
            continue
        if current and len(current) + len(remaining) + 1 <= max_chars:
            current = f"{current} {remaining}"
        else:
            if current:
                chunks.append(current)
            current = remaining

    if current:
        chunks.append(current)
    return chunks[:2000]


def _requirement_category(text: str, heading_category: str) -> str:
    normalized = normalize_text(text)
    if heading_category != "required_skills":
        return heading_category
    if re.search(r"\b(?:years?|minimum|experience|worked|employment)\b", normalized):
        return "relevant_experience"
    if re.search(r"\b(?:responsible|responsibilities|manage|lead|deliver|prepare|support|coordinate)\b", normalized):
        return "responsibilities"
    if re.search(r"\b(?:industry|sector|domain|manufacturing|instrumentation|automotive|pharma|banking)\b", normalized):
        return "domain_experience"
    if re.search(r"\b(?:degree|diploma|bachelor|master|certification|certified|license|licence)\b", normalized):
        return "education_certifications"
    return "required_skills"


def _section_category(heading: str) -> str:
    for category, pattern in HEADING_CATEGORIES:
        if pattern.search(heading):
            return category
    return "required_skills"


def parse_jd_requirements(description: str) -> list[dict]:
    """Parse JD bullets/sentences, preserving section context and must-have flags."""
    requirements: list[dict] = []
    seen: set[str] = set()
    current_category = "required_skills"
    headings = {
        "requirements", "required skills", "required qualifications", "qualifications",
        "responsibilities", "duties", "preferred qualifications", "preferred skills",
        "preferred requirements", "education", "certifications", "experience",
        "industry experience", "nice to have", "minimum qualifications",
        "job description", "job summary", "key responsibilities", "mandatory requirements"
    }

    metadata_prefixes = (
        "company:", "department:", "job type:", "location:", "salary:",
        "position:", "role:", "experience level:", "salary range:"
    )

    for raw_line in re.split(r"\r?\n+", str(description or "")):
        raw_heading = re.sub(r"^\s*(?:[-*•▪◦]+|\d+[.)]|[A-Za-z][.)])\s*", "", raw_line).strip().rstrip(":").strip().lower()
        line = re.sub(r"^\s*(?:[-*•▪◦]+|\d+[.)]|[A-Za-z][.)])\s*", "", raw_line)
        line = re.sub(r"\s+", " ", line).strip(" -:;\t")
        if not line:
            continue

        low_line = line.lower()
        if low_line.startswith(metadata_prefixes) or raw_heading in headings or (len(raw_heading) <= 80 and raw_line.strip().endswith(":")):
            current_category = _section_category(raw_heading)
            continue

        parts = re.split(r"(?<=[.;])\s+|\s*[;•]\s*", line)
        for part in parts:
            requirement = part.strip(" .;:-\t")
            if len(requirement) < 3 or PROTECTED_ATTRIBUTE_RE.search(requirement):
                continue
            if requirement.lower().startswith(metadata_prefixes):
                continue
            for category, pattern in HEADING_CATEGORIES:
                if pattern.search(requirement[:100]):
                    current_category = category
                    break
            category = _requirement_category(requirement, current_category)
            key = normalize_text(requirement)
            if not key or key in seen or len(key.split()) < 2:
                continue
            seen.add(key)
            must_have = bool(re.search(
                r"(?i)\b(?:must\s+have|mandatory|essential|required|minimum\s+of|at\s+least)\b",
                requirement,
            ))
            requirements.append({
                "requirement": requirement[:280],
                "category": category,
                "must_have": must_have,
            })
            if len(requirements) >= 64:
                return requirements
    return requirements


def extract_structured_candidate(text: str) -> dict:
    """Conservative resume facts; every returned value is copied from resume evidence."""
    source_lines = [
        re.sub(r"\s+", " ", line).strip(" \t-•")
        for line in re.split(r"\r?\n+|(?<=[.!?])\s+", str(text or ""))
    ]
    source_lines = [line for line in source_lines if line and not PROTECTED_ATTRIBUTE_RE.search(line)]
    flattened = "\n".join(source_lines)
    years = [float(match.group(1)) for match in re.finditer(
        r"(?i)\b(\d{1,2}(?:\.\d+)?)\s*\+?\s*(?:years?|yrs?)\b", flattened
    )]
    titles = [
        line[:180] for line in source_lines
        if re.search(r"(?i)\b(?:engineer|technician|manager|executive|analyst|accountant|sales|consultant|specialist|supervisor|director|operator)\b", line)
    ][:12]
    education = [
        line[:240] for line in source_lines
        if re.search(r"(?i)\b(?:bachelor|master|degree|diploma|B\.?E\.?|B\.?Tech|M\.?Tech|B\.?Com|M\.?Com|MBA|university|college)\b", line)
    ][:10]
    certifications = [
        line[:240] for line in source_lines
        if re.search(r"(?i)\b(?:certified|certification|license|licence|OSHA|PMP|Six Sigma|ISO\s*\d+)\b", line)
    ][:10]
    skills = [term for term in SKILL_TERMS if re.search(rf"(?i)(?<![\w]){re.escape(term)}(?![\w])", flattened)]
    industry_terms = [
        term for term in ("instrumentation", "manufacturing", "automotive", "pharmaceutical", "construction", "banking", "retail", "oil and gas", "power generation", "process industry")
        if re.search(rf"(?i)\b{re.escape(term)}\b", flattened)
    ]
    languages = [
        line[:160] for line in source_lines
        if re.search(r"(?i)\b(?:languages?|fluent in|proficient in)\b", line)
    ][:6]

    return {
        "total_experience_years_stated": max(years) if years else None,
        "experience_evidence": [line[:280] for line in source_lines if re.search(r"(?i)\b(?:experience|worked|employment|career)\b", line)][:12],
        "job_titles": titles,
        "skills": skills,
        "education": education,
        "certifications": certifications,
        "industries": industry_terms,
        "responsibilities": [line[:280] for line in source_lines if re.search(r"(?i)\b(?:responsible|managed|led|prepared|designed|developed|supported|coordinated|maintained)\b", line)][:16],
        "projects_and_achievements": [line[:280] for line in source_lines if re.search(r"(?i)\b(?:project|achievement|award|improved|increased|reduced|delivered)\b", line)][:12],
        "languages": languages,
    }


def _hashed_fallback_embedding(text: str, dimensions: int = 384) -> list[float]:
    tokens = [token for token in re.findall(r"[a-z0-9+#.]{2,}", normalize_text(text)) if token not in STOP_WORDS]
    vector = [0.0] * dimensions
    features = tokens + [f"{left}_{right}" for left, right in zip(tokens, tokens[1:])]
    for feature in features:
        digest = hashlib.sha256(feature.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % dimensions
        sign = 1.0 if digest[4] & 1 else -1.0
        vector[index] += sign
    norm = math.sqrt(sum(value * value for value in vector))
    if norm:
        vector = [value / norm for value in vector]
    return vector


def embed_texts(texts: list[str], model_name: str = EMBEDDING_MODEL) -> tuple[list[list[float]], str]:
    """Embed locally only. No resume or JD text is sent to an external API."""
    clean_texts = [str(text or "") for text in texts]
    if not clean_texts:
        return [], "empty"
    try:
        from fastembed import TextEmbedding

        global _model, _model_error
        with _model_lock:
            if _model is None:
                EMBEDDING_CACHE_DIR.mkdir(parents=True, exist_ok=True)
                _model = TextEmbedding(
                    model_name=model_name,
                    cache_dir=str(EMBEDDING_CACHE_DIR),
                )
            model = _model
        vectors = [[float(value) for value in vector] for vector in model.embed(clean_texts)]
        if len(vectors) != len(clean_texts):
            raise RuntimeError("Embedding model returned an incomplete batch.")
        _model_error = None
        return vectors, model_name
    except Exception as exc:
        _model_error = str(exc)
        return [_hashed_fallback_embedding(text) for text in clean_texts], "lexical-hash-fallback"


def embedding_status() -> dict:
    return {
        "model": EMBEDDING_MODEL,
        "backend": "fastembed-local" if _model is not None else "not-loaded",
        "error": _model_error,
        "cache_dir": str(EMBEDDING_CACHE_DIR),
    }


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or len(left) != len(right):
        return 0.0
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return sum(a * b for a, b in zip(left, right)) / (left_norm * right_norm)
