import sqlite3
import json
import re
import hashlib
from datetime import datetime, timezone

DB_PATH = r"C:\Users\NUNES\AppData\Local\NunesRecruitmentConsole\automation.db"

def clean_title(val):
    if not val:
        return ""
    s = str(val).strip()
    # Replace unicode replacement character \ufffd or weird dashes with standard en-dash –
    s = s.replace("\ufffd", "–").replace("â€“", "–").replace("—", "–")
    # Normalize spaces around dashes
    s = re.sub(r"\s*–\s*", " – ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def extract_cand_id(url):
    m = re.search(r'id=([a-f0-9]+)', url or '')
    return m.group(1) if m else None

def get_stable_key(profile_url, candidate_name):
    cid = extract_cand_id(profile_url)
    if cid:
        return hashlib.sha1(f"id:{cid}".encode("utf-8")).hexdigest()
    clean_name = re.sub(r"\s+", " ", str(candidate_name or "")).strip().lower()
    if clean_name:
        return hashlib.sha1(f"name:{clean_name}".encode("utf-8")).hexdigest()
    return hashlib.sha1((profile_url or "").encode("utf-8")).hexdigest()

def run_cleanup():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    print("--- 1. CLEANING JUNK ROLES FROM recruitment_role_state ---")
    junk_patterns = [
        "Upgrade to Premium", "Sponsorship ended", "day", "Role", "Messages",
        "All jobs", "View billing history", "Update payment method",
        "View performance report", "Cookies, privacy and terms", "Security",
        "Billing", "Mayiladuthurai District", "in a",
        "Female Business Development Manager", "Data entry executive",
        "Customer Sales Support Representative", "Rathinapuri, Coimbatore"
    ]
    for pattern in junk_patterns:
        c.execute("DELETE FROM recruitment_role_state WHERE job_title LIKE ?", (f"%{pattern}%",))
        c.execute("DELETE FROM role_profiles WHERE job_title LIKE ?", (f"%{pattern}%",))
    print(f"Junk roles cleaned. Remaining role rows: {c.execute('SELECT COUNT(*) FROM recruitment_role_state').fetchone()[0]}")

    print("\n--- 2. UPDATING AUTHORITATIVE INDEED ROLES IN recruitment_role_state ---")
    # The 8 real roles from Indeed employer dashboard (September 2026):
    authoritative_roles = [
        {
            "job_title": "Marketing & Lead Coordination Executive",
            "lifecycle_status": "OPEN",
            "indeed_status": "Open",
            "candidate_total_hint": 983,
            "candidate_new_hint": 7,
            "indeed_job_url": "https://employers.indeed.com/jobs/view?employerJobId=aXJpOi8vYXBpcy5pbmRlZWQuY29tL0VtcGxveWVySm9iLzM4YTEyYmFmLTY5MDktNDE3ZS05OTNhLTlmZGUwNTQ0ZTc3MQ%3D%3D"
        },
        {
            "job_title": "Purchase Executive",
            "lifecycle_status": "OPEN",
            "indeed_status": "Open",
            "candidate_total_hint": 143,
            "candidate_new_hint": 16,
            "indeed_job_url": "https://employers.indeed.com/jobs/view?employerJobId=aXJpOi8vYXBpcy5pbmRlZWQuY29tL0VtcGxveWVySm9iLzAwMWJlYWNlLTQzYzktNGQxNy04NWQ4LTU4ZmFiZWVlNDE4MA%3D%3D"
        },
        {
            "job_title": "Driver cum Electrician – Technical Support Assistant",
            "lifecycle_status": "OPEN",
            "indeed_status": "Open",
            "candidate_total_hint": 307, # 71 open + 236 paused
            "candidate_new_hint": 19,
            "indeed_job_url": "https://employers.indeed.com/jobs/view?employerJobId=aXJpOi8vYXBpcy5pbmRlZWQuY29tL0VtcGxveWVySm9iLzVhZGQ4OTk1LWM1ZDQtNDg3ZS1iODRiLTFiZjg1NDI1OWEyNg%3D%3D"
        },
        {
            "job_title": "Urgent Hiring – Data Entry & CRM Executive",
            "lifecycle_status": "PAUSED",
            "indeed_status": "Paused",
            "candidate_total_hint": 1057,
            "candidate_new_hint": 51,
            "indeed_job_url": "https://employers.indeed.com/jobs/view?employerJobId=aXJpOi8vYXBpcy5pbmRlZWQuY29tL0VtcGxveWVySm9iL2MxMjVmMjE3LTQ0ZDQtNDYxNy05Mzc1LTFhZTMxYjBmNzY2ZA%3D%3D"
        },
        {
            "job_title": "Accounts Executive / Accountant – TallyPrime, GST & TDS",
            "lifecycle_status": "PAUSED",
            "indeed_status": "Paused",
            "candidate_total_hint": 272,
            "candidate_new_hint": 0,
            "indeed_job_url": "https://employers.indeed.com/jobs/view?employerJobId=aXJpOi8vYXBpcy5pbmRlZWQuY29tL0VtcGxveWVySm9iLzVkY2EyMDRmLWQwMGEtNGQ1Mi1hY2M5LTQ3NjcyMWRhYTYzZQ%3D%3D"
        },
        {
            "job_title": "Data Entry Operator",
            "lifecycle_status": "PAUSED",
            "indeed_status": "Paused",
            "candidate_total_hint": 24,
            "candidate_new_hint": 0,
            "indeed_job_url": "https://employers.indeed.com/jobs/view?employerJobId=aXJpOi8vYXBpcy5pbmRlZWQuY29tL0VtcGxveWVySm9iL2Q0M2EzNjY5LTgxYjgtNDIxNC1hY2UyLTc3ZGFmNjFjNmIzOA%3D%3D"
        },
        {
            "job_title": "Accounts Executive – TallyPrime, GST & E-Way Bill ( Female candidates only )",
            "lifecycle_status": "PAUSED",
            "indeed_status": "Paused",
            "candidate_total_hint": 101,
            "candidate_new_hint": 0,
            "indeed_job_url": "https://employers.indeed.com/jobs/view?employerJobId=aXJpOi8vYXBpcy5pbmRlZWQuY29tL0VtcGxveWVySm9iL2Q2NTkzYjM0LTE4ZDAtNGJkZC04OTIxLTAwZGYyMmY4YTI0Mw%3D%3D"
        },
        {
            "job_title": "Business Development Executive",
            "lifecycle_status": "FLAGGED",
            "indeed_status": "Flagged",
            "candidate_total_hint": 0,
            "candidate_new_hint": 0,
            "indeed_job_url": "https://employers.indeed.com/jobs/view?employerJobId=aXJpOi8vYXBpcy5pbmRlZWQuY29tL0VtcGxveWVySm9iLzhhMDY1MTY3LTNmYWYtNGI2Mi05NjE3LTBkOWQ2MmI4OGM3Yw%3D%3D"
        }
    ]

    now_iso = datetime.now(timezone.utc).isoformat()

    # Clear old malformed rows in recruitment_role_state and re-insert clean authoritative rows
    c.execute("DELETE FROM recruitment_role_state")
    for r in authoritative_roles:
        c.execute("""
            INSERT INTO recruitment_role_state (
                job_title, lifecycle_status, indeed_status, indeed_job_url,
                first_seen_at, last_seen_at, candidate_total_hint, candidate_new_hint,
                updated_at, report_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'IDLE')
        """, (
            r["job_title"], r["lifecycle_status"], r["indeed_status"], r["indeed_job_url"],
            now_iso, now_iso, r["candidate_total_hint"], r["candidate_new_hint"], now_iso
        ))

        c.execute("""
            INSERT INTO role_profiles (
                job_title, indeed_job_url, description_status, created_at, updated_at
            ) VALUES (?, ?, 'WAITING', ?, ?)
            ON CONFLICT(job_title) DO UPDATE SET
                indeed_job_url=COALESCE(excluded.indeed_job_url, role_profiles.indeed_job_url),
                updated_at=excluded.updated_at
        """, (r["job_title"], r["indeed_job_url"], now_iso, now_iso))

    print(f"Authoritative roles written: {len(authoritative_roles)}")

    print("\n--- 3. UPDATING SYSTEM_STATE SNAPSHOT ---")
    exact_total_all = sum(r["candidate_total_hint"] for r in authoritative_roles)
    exact_total_new = sum(r["candidate_new_hint"] for r in authoritative_roles)
    snapshot_roles = []
    for r in authoritative_roles:
        snapshot_roles.append({
            "job_title": r["job_title"],
            "job_status": r["indeed_status"],
            "job_url": r["indeed_job_url"],
            "candidate_total": r["candidate_total_hint"],
            "candidate_new": r["candidate_new_hint"],
            "candidate_total_hint": r["candidate_total_hint"],
            "candidate_new_hint": r["candidate_new_hint"],
            "applicant_count": r["candidate_total_hint"],
            "active_applicant_count": r["candidate_total_hint"],
            "role_key": hashlib.sha1(f"{r['indeed_job_url']}::{r['job_title']}".encode("utf-8")).hexdigest()
        })

    snapshot_payload = {
        "captured_at": now_iso,
        "role_count": len(authoritative_roles),
        "total_applicants": exact_total_all,
        "total_new": exact_total_new,
        "roles": snapshot_roles
    }

    state_updates = [
        ("indeed_jobs_role_snapshot", json.dumps(snapshot_payload, ensure_ascii=False)),
        ("indeed_jobs_total_applicants", str(exact_total_all)),
        ("indeed_jobs_total_new", str(exact_total_new)),
        ("indeed_jobs_role_count", str(len(authoritative_roles))),
        ("indeed_jobs_role_snapshot_at", now_iso),
        ("role_discovery_roles_found", str(len(authoritative_roles))),
        ("role_discovery_status", "ACTIVE")
    ]
    for k, v in state_updates:
        c.execute("""
            INSERT INTO system_state (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """, (k, v, now_iso))
    print(f"Snapshot updated: Total All = {exact_total_all}, Total New = {exact_total_new}, Roles = {len(authoritative_roles)}")

    print("\n--- 4. CLEANING & CANONICALIZING APPLICATIONS ---")
    # First, normalize job titles in applications
    c.execute("""
        UPDATE applications SET job_title = 'Driver cum Electrician – Technical Support Assistant'
        WHERE job_title LIKE '%Driver%'
    """)
    c.execute("""
        UPDATE applications SET job_title = 'Purchase Executive'
        WHERE job_title LIKE '%Purchase%'
    """)
    c.execute("""
        UPDATE applications SET job_title = 'Marketing & Lead Coordination Executive'
        WHERE job_title LIKE '%Marketing%'
    """)
    c.execute("""
        UPDATE applications SET job_title = 'Urgent Hiring – Data Entry & CRM Executive'
        WHERE job_title LIKE '%Data Entry & CRM%'
    """)
    c.execute("""
        UPDATE applications SET job_title = 'Accounts Executive / Accountant – TallyPrime, GST & TDS'
        WHERE job_title LIKE '%Accounts%' AND job_title NOT LIKE '%Female%'
    """)

    # Read all applications
    apps = c.execute("""
        SELECT id, source_key, profile_url, candidate_name, candidate_email, candidate_phone,
               job_title, indeed_status, send_status, resume_text_cache, created_at, updated_at
        FROM applications
        ORDER BY id ASC
    """).fetchall()

    sent_app_ids = {r[0] for r in c.execute("SELECT application_id FROM sent_responses WHERE application_id IS NOT NULL").fetchall()}
    flow_app_ids = {r[0] for r in c.execute("SELECT application_id FROM recruitment_candidate_flow").fetchall()}

    groups = {}
    for a in apps:
        cid = extract_cand_id(a["profile_url"])
        if not cid:
            cid = re.sub(r"\s+", " ", str(a["candidate_name"] or "")).strip().lower()
        if not cid:
            cid = f"raw_app_{a['id']}"
        groups.setdefault(cid, []).append(a)

    print(f"Total applications in DB before dedup: {len(apps)}")
    print(f"Unique candidate profiles identified: {len(groups)}")

    kept_ids = set()
    dup_ids = []
    remap = {}

    # Pass 1: Plan which rows to keep and which to delete
    primary_updates = {}
    for cid, rows in groups.items():
        if len(rows) == 1:
            primary = rows[0]
            p_id = primary["id"]
            kept_ids.add(p_id)
            primary_updates[p_id] = {
                "name": primary["candidate_name"],
                "email": primary["candidate_email"],
                "phone": primary["candidate_phone"],
                "resume": primary["resume_text_cache"],
                "url": primary["profile_url"],
            }
            continue

        def score(r):
            s = 0
            if r["id"] in flow_app_ids:
                s += 100000
            if r["id"] in sent_app_ids:
                s += 50000
            if (r["send_status"] or "").upper() == "SENT":
                s += 20000
            if r["candidate_email"] and "@" in r["candidate_email"]:
                s += 10000
            if r["resume_text_cache"]:
                s += min(len(r["resume_text_cache"]), 5000)
            if r["candidate_phone"]:
                s += 500
            return s

        sorted_rows = sorted(rows, key=score, reverse=True)
        primary = sorted_rows[0]
        p_id = primary["id"]
        kept_ids.add(p_id)

        email = primary["candidate_email"]
        phone = primary["candidate_phone"]
        resume = primary["resume_text_cache"]
        url = primary["profile_url"]

        for d in sorted_rows[1:]:
            d_id = d["id"]
            dup_ids.append(d_id)
            remap[d_id] = p_id

            if not email and d["candidate_email"]:
                email = d["candidate_email"]
            if not phone and d["candidate_phone"]:
                phone = d["candidate_phone"]
            if not resume and d["resume_text_cache"]:
                resume = d["resume_text_cache"]
            if (not url or "tab=" in url) and d["profile_url"]:
                url = d["profile_url"]

        primary_updates[p_id] = {
            "name": primary["candidate_name"],
            "email": email,
            "phone": phone,
            "resume": resume,
            "url": url,
        }

    print(f"Kept candidate applications: {len(kept_ids)}")
    print(f"Duplicate applications to delete: {len(dup_ids)}")

    # Pass 2: Remap foreign keys in dependent tables
    for d_id, p_id in remap.items():
        c.execute("SELECT source_key, job_title FROM applications WHERE id = ?", (p_id,))
        p_row = c.fetchone()
        p_key = p_row["source_key"] if p_row else None
        p_job = p_row["job_title"] if p_row else None

        c.execute("UPDATE sent_responses SET application_id = ?, source_key = COALESCE(?, source_key) WHERE application_id = ?", (p_id, p_key, d_id))
        c.execute("UPDATE recruitment_candidate_flow SET application_id = ? WHERE application_id = ?", (p_id, d_id))
        c.execute("UPDATE recruitment_notifications SET application_id = ? WHERE application_id = ?", (p_id, d_id))

        c.execute("SELECT * FROM candidate_reviews WHERE application_id = ?", (d_id,))
        d_rev = c.fetchone()
        if d_rev:
            c.execute("SELECT * FROM candidate_reviews WHERE application_id = ?", (p_id,))
            p_rev = c.fetchone()
            if not p_rev:
                c.execute("UPDATE candidate_reviews SET application_id = ?, job_title = ? WHERE application_id = ?", (p_id, p_job, d_id))
            else:
                c.execute("DELETE FROM candidate_reviews WHERE application_id = ?", (d_id,))

    # Pass 3: Delete duplicates FIRST so applications has no duplicate rows
    if dup_ids:
        # Delete in chunks
        chunk_size = 500
        for i in range(0, len(dup_ids), chunk_size):
            chunk = dup_ids[i:i + chunk_size]
            placeholders = ",".join("?" for _ in chunk)
            c.execute(f"DELETE FROM applications WHERE id IN ({placeholders})", chunk)
        print(f"Deleted {len(dup_ids)} duplicate application rows.")

    # Pass 4: Cleanly update the remaining primary rows
    for p_id, fields in primary_updates.items():
        c.execute("""
            UPDATE applications
            SET candidate_email = ?, candidate_phone = ?, resume_text_cache = ?, profile_url = ?
            WHERE id = ?
        """, (fields["email"], fields["phone"], fields["resume"], fields["url"], p_id))

    # Clean up candidate_reviews for any orphaned applications
    c.execute("DELETE FROM candidate_reviews WHERE application_id NOT IN (SELECT id FROM applications)")

    print(f"\nFinal applications count: {c.execute('SELECT COUNT(*) FROM applications').fetchone()[0]}")
    print(f"Final reviews count: {c.execute('SELECT COUNT(*) FROM candidate_reviews').fetchone()[0]}")
    print(f"Final sent responses count: {c.execute('SELECT COUNT(*) FROM sent_responses').fetchone()[0]}")
    print(f"Final interview flow count: {c.execute('SELECT COUNT(*) FROM recruitment_candidate_flow').fetchone()[0]}")

    print("\nCandidates per role in database:")
    for row in c.execute("SELECT job_title, count(*) FROM applications GROUP BY job_title").fetchall():
        print(f"  {row[0]}: {row[1]}")

    conn.commit()
    conn.close()
    print("\nDatabase repair and sync completed successfully!")

if __name__ == "__main__":
    run_cleanup()
