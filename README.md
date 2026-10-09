# Employee Attendance & Analytics API

Solution for HROne Engineering Assignment (Software Engineer Trainee Role).

## Requirements
- Python 3.11+
- MongoDB 6.0+ (Atlas or local container)

## Setup & Running

1. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure environment**:
   Set `MONGO_URI` and `MONGO_DB` environment variables (or rely on `.env` defaults `mongodb://localhost:27017` and `attendance_db`).

3. **Seed Sample Data (Optional)**:
   ```bash
   python sample_seed.py
   ```

4. **Start the API server**:
   ```bash
   uvicorn app.main:app --port 8000
   ```

## Architecture & Features
- **FastAPI + PyMongo**: Built to strictly follow `openapi.yaml` and `DATA_MODEL.md`.
- **Business Rules R1 - R10**: Includes full support for overnight shifts, grace period calculations, half-up rounding using `Decimal`, working day logic, and competition leaderboard ranking.
- **MongoDB Aggregations**: All analytics endpoints (`/analytics/...`) run native MongoDB aggregation pipelines with full index backing (zero `COLLSCAN`s).
- **Concurrency & Audit Trails**: Race-safe punch-in/out endpoints backed by database unique indexes and atomic `find_one_and_update` operations, with audit trails in `history`.
