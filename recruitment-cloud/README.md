# Nunes Recruitment Cloud

A standalone Vercel deployment in `recruitment-cloud/`. The existing Windows/Indeed collector and frontend remain separate.

## Implemented workflow

- HR sign-in with HTTP-only signed sessions and CSRF protection.
- Create roles with an actual job description.
- Add candidates manually, upload PDF/DOCX/TXT resumes, or import CSV batches (up to 500 rows).
- OpenAI extracts structured resume details for HR to verify before saving. Text-based PDF resumes preview inline; DOCX files can be downloaded alongside extracted text.
- Compare applicants within the same role. Scores include evidence, strengths and gaps. No automatic shortlist or rejection.
- Candidate identity protection per job: normalized email, normalized phone and exact resume-text fingerprint. Unique database constraints and a role lock protect concurrent imports.
- Each new application queues one acknowledgement from `nuneslead@gmail.com`.
- Only an authenticated HR approval queues the second email (shortlist). It is due at 9 AM Asia/Kolkata on the next Monday–Friday working day. Friday, Saturday and Sunday approvals go to Monday. Public holidays are not currently excluded.
- Email outbox prevents duplicate jobs and concurrent sends. An uncertain SMTP outcome is held for review and never retried automatically. A function interrupted after claiming may remain `sending`; inspect Gmail Sent before manual recovery. This deliberately favors avoiding duplicate mail over blind retry.
- Full CSV export includes candidate details, extracted profile, resume text and AI evidence.

## Deployment

Import `Nunes-instruments/nunes-recruitment-console` into Vercel and set Root Directory to `recruitment-cloud`. Framework: Flask. The app uses the native Vercel Python runtime.

Set these **encrypted environment variables** for Production, then redeploy. Use separate test credentials and a separate database for Preview; never copy production candidate data or email credentials into preview deployments:

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | A dedicated PostgreSQL database connection string, with TLS enabled |
| `HR_PASSWORD` | Long random HR login password |
| `SESSION_SECRET` | Random signing secret of at least 32 bytes |
| `OPENAI_API_KEY` | OpenAI API key with available API billing |
| `OPENAI_MODEL` | Optional, defaults to `gpt-4.1-mini` |
| `GMAIL_APP_PASSWORD` | Google app password for `nuneslead@gmail.com` |
| `CRON_SECRET` | Random secret used to authenticate the cron endpoint |

No candidate data, API keys, email passwords or local database files belong in this repository. The live app does not fall back to temporary SQLite storage on Vercel. Until PostgreSQL is configured, it shows setup status and blocks imports. Connection badges indicate environment configuration, not successful end-to-end service verification.

Schema initializes on the first authenticated workspace load or import. Use a dedicated database/user with permissions for these application tables: jobs, candidates, identities, outbox, audit.

### Email timing and scale

The included Vercel cron is daily at 03:30 UTC (09:00 IST). Vercel Hobby does not promise exact execution time, and scheduled cron runs apply to production deployments. Each run currently processes up to 20 emails; the HR “Send due emails” action processes up to 10. For larger application volumes, configure a more frequent authenticated external scheduler or upgrade the cron plan before relying on next-day delivery for every candidate. Invoke `GET /api/cron` with `Authorization: Bearer <CRON_SECRET>`. The app will never send a scheduled shortlist before its due timestamp.

The browser can be closed for scheduled emails, but the browser must stay open during “Assess unranked”; candidates already assessed remain saved if it closes. Missing or unreadable resumes remain visible and unranked. Scanned PDFs need OCR first. No automatic Indeed scraping, Gmail inbox extraction or WhatsApp messaging is claimed by this cloud app; use export/import for existing applications.

### CSV format

```csv
name,email,phone,city,source,resume_text
Example Candidate,example@example.com,9876543210,Coimbatore,Indeed export,"Relevant resume evidence here"
```

Phone numbers with +91 normalize to Indian 10-digit numbers. The same person applying to different jobs is treated as a distinct application. An acknowledgement is per application, not globally per person.

## Local development and tests

```sh
pip install -r requirements.txt
export LOCAL_DB=/absolute/path/recruitment.sqlite
export HR_PASSWORD='a-long-local-password'
export SESSION_SECRET='a-long-random-local-secret'
python app.py
python -m unittest discover -s tests -v
```

Open http://localhost:3000. Local SQLite is only a development/test option. Automated tests mock AI and SMTP; they never contact candidates. Live OpenAI, SMTP and PostgreSQL require configured credentials and a controlled end-to-end test before operational use.
