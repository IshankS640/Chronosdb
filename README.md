# ChronosDB
See the step-by-step guide delivered with this project. Quick start:
1. docker run --name chronos-pg -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=chronosdb -p 5432:5432 -d postgres:16
2. Load database/01..05 in order (docker cp + psql -f)
3. cd backend && pip install -r requirements.txt && python seed_100k.py
4. uvicorn main:app --port 8000      (backend/)
5. streamlit run app.py              (frontend/)
