# Candidate kit

Read in this order:

1. `PROBLEM_STATEMENT.docx`: what you are asked to do, how it is evaluated.
2. `openapi.yaml`: the API contract and the business rules (R1 - R10).
3. `DATA_MODEL.md`: what is stored in MongoDB, with nine sample documents.
4. `app/main.py`: the starter. Put **all** your code in this one file. It must start with `uvicorn app.main:app --port 8000`.
5. `REVIEW.md`, `DECISIONS.md`: templates to fill in.

`sample_data/` holds the nine sample documents as MongoDB Extended JSON; `sample_seed.py` loads them. The grader's dataset is
much larger and contains cases they do not show.

## Local setup
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python sample_seed.py
uvicorn app.main:app --port 8000 --reload
```
MongoDB **6.0 or newer** is required (the grader uses 7.0). MongoDB Atlas free tier (M0) works.

On Windows PowerShell, copy the environment template with:

```powershell
Copy-Item .env.example .env
```

### Use MongoDB Atlas

1. Create an Atlas cluster and a **database user** (this is separate from your Atlas website login).
2. In **Network Access**, allow the IP address of the machine running this API. Avoid `0.0.0.0/0` unless you intentionally
   want the cluster accessible from every IP.
3. In the cluster's **Connect → Drivers** dialog, copy the Python connection string.
4. Copy `.env.example` to `.env` and set `MONGO_URI` to that string and `MONGO_DB` to the database name. Replace the
   username, password, and cluster host placeholders. URL-encode any special characters in the database username or
   password (for example, `@` as `%40`). Keep `.env` private; it is excluded from Git.
5. Install the dependencies and run `python sample_seed.py` if you want to load the sample data. Then start the API as
   shown above.

The application reads `MONGO_URI` and `MONGO_DB` from the environment or `.env`; `mongodb+srv://` Atlas URIs are
supported by the `pymongo[srv]` dependency. Real environment variables take precedence over values in `.env`.

## Submit
A public Git repository with `app/main.py`, `requirements.txt`, `REVIEW.md`, `DECISIONS.md` and a short `README.md`.
No `.env`, no secrets, no Dockerfile.
