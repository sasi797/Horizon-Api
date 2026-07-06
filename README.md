# BTS Backend API

## Quick Start

### 1. Start PostgreSQL + Redis via Docker
```bash
docker-compose up postgres redis -d
```

### 2. Install dependencies (local dev)
```bash
pip install -r requirements.txt
```

### 3. Run the API
```bash
python.exe -m venv .venv
.venv\Scripts\Activate.ps1 
pip install -r requirements.txt
.venv\Scripts\uvicorn main:app --reload
.venv\Scripts\celery -A app.tasks.celery_app worker --loglevel=info -P solo
.venv\Scripts\celery -A app.tasks.celery_app beat --loglevel=info
uvicorn main:app --reload --port 8000

### 4. Or run everything with Docker
```bash
docker-compose up --build
```

## API Docs
- Swagger UI: http://localhost:8000/docs
- ReDoc:       http://localhost:8000/redoc

## Default Login
| Email           | Password   | Role  |
|-----------------|------------|-------|
| admin@bts.com   | Admin@123  | admin |
| james@bts.com   | Admin@123  | agent |
| sophie@bts.com  | Admin@123  | agent |

## API Endpoints

### Auth
| Method | Path            | Description          |
|--------|-----------------|----------------------|
| POST   | /auth/login     | Login, get JWT       |
| POST   | /auth/refresh   | Refresh access token |
| POST   | /auth/logout    | Revoke tokens        |
| GET    | /auth/me        | Current user info    |

### Bookings
| Method | Path                        | Description         |
|--------|-----------------------------|---------------------|
| GET    | /bookings                   | List bookings       |
| POST   | /bookings                   | Create booking      |
| GET    | /bookings/{id}              | Get booking detail  |
| PUT    | /bookings/{id}              | Update booking      |
| PATCH  | /bookings/{id}/status       | Update status       |
| DELETE | /bookings/{id}              | Delete booking      |

### Agents
| Method | Path           | Description     |
|--------|----------------|-----------------|
| GET    | /agents        | List agents     |
| POST   | /agents        | Create agent    |
| GET    | /agents/{id}   | Get agent       |
| PUT    | /agents/{id}   | Update agent    |
| DELETE | /agents/{id}   | Delete agent    |

### Attendance
| Method | Path                  | Description             |
|--------|-----------------------|-------------------------|
| GET    | /attendance?date=     | Get attendance by date  |
| POST   | /attendance           | Bulk upsert attendance  |
| GET    | /attendance/summary   | Summary counts by date  |

### Allocations
| Method | Path                       | Description              |
|--------|----------------------------|--------------------------|
| GET    | /allocations/status        | Current pointer + pool   |
| POST   | /allocations/run           | Run round-robin          |
| GET    | /allocations/log           | Allocation history       |
| POST   | /allocations/reset-pointer | Reset pointer to 0       |

### Pending Queue
| Method | Path                            | Description           |
|--------|---------------------------------|-----------------------|
| GET    | /pending-queue                  | List pending items    |
| POST   | /pending-queue/assign           | Assign specific agent |
| POST   | /pending-queue/auto-assign-all  | Auto-assign all       |
| DELETE | /pending-queue/{booking_id}     | Remove from queue     |

### Shifts
| Method | Path           | Description   |
|--------|----------------|---------------|
| GET    | /shifts        | List shifts   |
| POST   | /shifts        | Create shift  |
| GET    | /shifts/{id}   | Get shift     |
| PUT    | /shifts/{id}   | Update shift  |
| DELETE | /shifts/{id}   | Delete shift  |

### Reports
| Method | Path                              | Description              |
|--------|-----------------------------------|--------------------------|
| GET    | /reports/stats                    | Overall stats            |
| GET    | /reports/trend?days=7             | Bookings trend           |
| GET    | /reports/priority-distribution    | Priority breakdown       |
| GET    | /reports/daily-summary?days=7     | Daily summary table      |

### Dashboard
| Method | Path              | Description         |
|--------|-------------------|---------------------|
| GET    | /dashboard/stats  | Dashboard KPI stats |

## Architecture
- **FastAPI** — async Python web framework
- **PostgreSQL 16** — primary relational database
- **Redis 7** — JWT refresh token storage, round-robin pointer, stats cache
- **SQLAlchemy 2 async** — ORM with asyncpg driver
- **JWT** — access token (15 min) + refresh token (7 days, Redis-backed)

