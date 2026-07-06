# BTS — Bookings to Ticket System
## Technical Documentation
**Version:** 1.0.0 | **Date:** 2026-05-19 | **Platform:** PostgreSQL 16 · FastAPI · Redis

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Technology Stack](#2-technology-stack)
3. [Architecture Summary](#3-architecture-summary)
4. [Database Schema](#4-database-schema)
   - 4.1 Enumerations
   - 4.2 Tables
   - 4.3 Relationships & Referential Integrity
   - 4.4 Indexing Strategy
   - 4.5 Triggers
5. [API Reference](#5-api-reference)
   - 5.1 Authentication
   - 5.2 Agents
   - 5.3 Shifts
   - 5.4 Bookings
   - 5.5 Attendance
   - 5.6 Allocations
   - 5.7 Pending Queue
   - 5.8 Reports
   - 5.9 Dashboard
6. [Authentication & Security Model](#6-authentication--security-model)
7. [Allocation Engine](#7-allocation-engine)
8. [Caching Strategy](#8-caching-strategy)
9. [Configuration & Environment](#9-configuration--environment)

---

## 1. System Overview

BTS (Bookings to Ticket System) is a backend REST API designed to manage cargo/logistics bookings, the agents who handle them, and operational reporting. It acts as the server-side layer for a frontend dashboard and handles the full lifecycle of a booking — from receipt through agent assignment to completion.

**Core responsibilities:**
- Authenticating and authorising users (admin, supervisor, agent roles)
- Managing bookings with structured cargo metadata
- Managing agent profiles and their shift assignments
- Tracking daily agent attendance
- Automatically allocating bookings to available agents using a round-robin engine
- Holding unassignable bookings in a pending queue for manual or batch resolution
- Serving real-time operational statistics and trend reports to the dashboard

---

## 2. Technology Stack

| Layer | Technology | Version |
|---|---|---|
| Web Framework | FastAPI | 0.115.5 |
| ASGI Server | Uvicorn | 0.32.1 |
| ORM | SQLAlchemy (async) | 2.0.36 |
| Database Driver | asyncpg | 0.30.0 |
| Database | PostgreSQL | 16 |
| Cache / Session Store | Redis | 5.x (hiredis) |
| Auth Tokens | python-jose (JWT, HS256) | 3.3.0 |
| Password Hashing | passlib (bcrypt) | 1.7.4 |
| Settings Management | pydantic-settings | 2.6.1 |
| Schema Migrations | Alembic | 1.14.0 |

All I/O is fully asynchronous — both the database layer (asyncpg) and the cache layer (redis.asyncio) operate without blocking the event loop.

---

## 3. Architecture Summary

```
Frontend (React / Next.js)
        │
        │  HTTPS / JSON
        ▼
┌──────────────────────────────────┐
│        FastAPI Application       │
│                                  │
│  Routers (per-domain)            │
│  ├── /auth                       │
│  ├── /agents                     │
│  ├── /shifts                     │
│  ├── /bookings                   │
│  ├── /attendance                 │
│  ├── /allocations                │
│  ├── /pending-queue              │
│  ├── /reports                    │
│  └── /dashboard                  │
│                                  │
│  Dependencies                    │
│  ├── get_db()  → AsyncSession    │
│  ├── get_redis() → Redis client  │
│  └── get_current_user() → JWT    │
└────────────┬─────────────────────┘
             │
    ┌────────┴────────┐
    ▼                 ▼
PostgreSQL 16      Redis
(persistent data)  (JWT sessions, allocation
                    pointer, report cache)
```

All routes except `GET /health` are protected by JWT Bearer token authentication. Redis holds three types of runtime data:
1. Refresh tokens (keyed by user ID, TTL = 7 days)
2. Revoked access tokens (keyed by token string, TTL = remaining token lifetime)
3. Cached query results for dashboard and report endpoints (TTL = 60–300 seconds)
4. The round-robin allocation pointer (persistent integer)

---

## 4. Database Schema

### 4.1 Enumerations

Four native PostgreSQL enum types constrain critical string fields, preventing invalid values at the database level.

| Enum Name | Allowed Values |
|---|---|
| `priority_enum` | `Urgent`, `Standard`, `Economy` |
| `booking_status_enum` | `Pending`, `In Progress`, `Completed` |
| `attendance_status_enum` | `Present`, `Absent`, `On Break`, `Late` |
| `user_role_enum` | `admin`, `agent`, `supervisor` |

### 4.2 Tables

#### `users` — Authentication Accounts

Stores login credentials and role information for anyone who accesses the system.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | UUID | PK, default `gen_random_uuid()` | |
| `name` | VARCHAR(100) | NOT NULL | Display name |
| `email` | VARCHAR(150) | NOT NULL, UNIQUE | Login identifier |
| `password_hash` | VARCHAR(255) | NOT NULL | bcrypt hash, never stored as plaintext |
| `role` | `user_role_enum` | NOT NULL, default `agent` | Controls access level |
| `is_active` | BOOLEAN | NOT NULL, default `TRUE` | Soft disable without deletion |
| `created_at` | TIMESTAMPTZ | NOT NULL, default `NOW()` | |
| `updated_at` | TIMESTAMPTZ | NOT NULL, default `NOW()` | Auto-maintained by trigger |

---

#### `shifts` — Work Shift Definitions

Defines the named time bands that agents are assigned to.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | UUID | PK, default `gen_random_uuid()` | |
| `name` | VARCHAR(100) | NOT NULL | Human-readable name (e.g. "Morning") |
| `code` | VARCHAR(10) | NOT NULL, UNIQUE | Short code (e.g. "AM") |
| `start_time` | TIME | NOT NULL | Shift start (time-of-day, no date) |
| `end_time` | TIME | NOT NULL | Shift end |
| `created_at` | TIMESTAMPTZ | NOT NULL | |
| `updated_at` | TIMESTAMPTZ | NOT NULL | Auto-maintained by trigger |

---

#### `agents` — Agent Profiles

Represents operational agents who handle bookings. An agent may optionally be linked to a user account (for login access) and is assigned a shift.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | UUID | PK, default `gen_random_uuid()` | |
| `user_id` | UUID | FK → `users.id` ON DELETE SET NULL | Optional; links agent to a login account |
| `name` | VARCHAR(100) | NOT NULL | |
| `email` | VARCHAR(150) | NOT NULL, UNIQUE | Contact email |
| `shift_id` | UUID | FK → `shifts.id` ON DELETE SET NULL | Which shift this agent works |
| `created_at` | TIMESTAMPTZ | NOT NULL | |
| `updated_at` | TIMESTAMPTZ | NOT NULL | Auto-maintained by trigger |

Deleting a user does not delete the agent; `user_id` is set to NULL, preserving operational history.

---

#### `bookings` — Booking Records

The central entity. Represents a cargo booking request with all logistics metadata and tracks its full lifecycle.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | VARCHAR(25) | PK | Human-readable format: `BKG-{YEAR}-{5-digit number}` |
| `subject` | VARCHAR(255) | NOT NULL | Brief description of the booking |
| `priority` | `priority_enum` | NOT NULL, default `Standard` | Urgency level |
| `status` | `booking_status_enum` | NOT NULL, default `Pending` | Lifecycle stage |
| `agent_id` | UUID | FK → `agents.id` ON DELETE SET NULL | Assigned agent (NULL until allocated) |
| `sender_email` | VARCHAR(150) | NOT NULL | Originating client/contact email |
| `cargo_type` | VARCHAR(100) | — | Type of cargo |
| `pickup_location` | VARCHAR(255) | — | Origin address |
| `delivery_location` | VARCHAR(255) | — | Destination address |
| `cargo_weight` | NUMERIC(10,2) | — | Weight in kg |
| `cargo_volume` | NUMERIC(10,2) | — | Volume in m³ |
| `shipping_mode` | VARCHAR(100) | — | Air / Sea / Land / etc. |
| `special_instructions` | TEXT | — | Free-text instructions |
| `remarks` | TEXT | — | Internal notes |
| `received_at` | TIMESTAMPTZ | NOT NULL, default `NOW()` | When booking entered the system |
| `assigned_at` | TIMESTAMPTZ | — | Set when an agent is first assigned |
| `completed_at` | TIMESTAMPTZ | — | Set when status moves to `Completed` |
| `created_at` | TIMESTAMPTZ | NOT NULL | |
| `updated_at` | TIMESTAMPTZ | NOT NULL | Auto-maintained by trigger |

The human-readable booking ID (`BKG-2026-00042`) makes it easy to reference specific bookings in communication with clients or operations staff.

---

#### `attendance` — Daily Agent Attendance

Records the attendance status of each agent per day per shift. The unique constraint ensures no duplicate entry can exist for the same agent on the same date in the same shift.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | UUID | PK, default `gen_random_uuid()` | |
| `agent_id` | UUID | NOT NULL, FK → `agents.id` ON DELETE CASCADE | |
| `shift_id` | UUID | FK → `shifts.id` ON DELETE SET NULL | Which shift this record is for |
| `date` | DATE | NOT NULL | Calendar date |
| `status` | `attendance_status_enum` | NOT NULL, default `Present` | |
| `check_in` | TIMESTAMPTZ | — | Actual clock-in time |
| `check_out` | TIMESTAMPTZ | — | Actual clock-out time |
| `created_at` | TIMESTAMPTZ | NOT NULL | |
| `updated_at` | TIMESTAMPTZ | NOT NULL | Auto-maintained by trigger |

**Unique constraint:** `(agent_id, date, shift_id)` — prevents duplicate attendance records.

Attendance is directly used by the allocation engine: only agents whose attendance record for today shows `Present` are eligible for booking assignment.

---

#### `allocation_log` — Booking-to-Agent Assignment History

An immutable audit trail of every automated allocation event. Records exactly which pointer value was used at the time of allocation.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | UUID | PK | |
| `booking_id` | VARCHAR(25) | NOT NULL, FK → `bookings.id` ON DELETE CASCADE | |
| `agent_id` | UUID | NOT NULL, FK → `agents.id` ON DELETE CASCADE | |
| `pointer_value` | INTEGER | NOT NULL | Round-robin index used at time of allocation |
| `pool_size` | INTEGER | NOT NULL | Number of eligible agents at time of allocation |
| `allocated_at` | TIMESTAMPTZ | NOT NULL, default `NOW()` | |

This table is append-only in normal operation — it is never updated after creation, providing a trustworthy audit record.

---

#### `pending_queue` — Bookings Awaiting Manual or Batch Allocation

Holds bookings that could not be automatically assigned (e.g. because no agents were present). Each booking can only appear once.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | UUID | PK | |
| `booking_id` | VARCHAR(25) | NOT NULL, UNIQUE, FK → `bookings.id` ON DELETE CASCADE | |
| `reason` | VARCHAR(255) | NOT NULL | Why it is queued (e.g. "No agents available") |
| `pending_since` | TIMESTAMPTZ | NOT NULL, default `NOW()` | Used to sort oldest-first |
| `created_at` | TIMESTAMPTZ | NOT NULL | |

When a booking is successfully allocated (via `/allocations/run` or `/pending-queue/assign`), its row is deleted from this table automatically.

---

### 4.3 Relationships & Referential Integrity

```
users ──────────────── agents (user_id, SET NULL on delete)
                          │
              ┌───────────┤
              │           │
           shifts       attendance (agent_id, CASCADE on delete)
         (shift_id,       │
        SET NULL)       shifts (shift_id, SET NULL)
              │
           bookings (agent_id, SET NULL on delete)
              │
       ┌──────┤
       │      │
  allocation  pending_queue
     _log      (CASCADE on delete)
  (CASCADE)
```

**Key design decisions:**
- Deleting a **user** does not cascade to agents — the agent record is preserved for historical purposes.
- Deleting an **agent** cascades to attendance records (no longer meaningful) but only nullifies the agent reference on bookings (history is preserved).
- Deleting a **booking** cascades to allocation_log and pending_queue (both are derived from the booking and become meaningless without it).

### 4.4 Indexing Strategy

| Table | Index | Purpose |
|---|---|---|
| `users` | `idx_users_email` | Fast login lookup by email |
| `agents` | `idx_agents_email` | Uniqueness check and lookup |
| `agents` | `idx_agents_user_id` | Joining agents to users |
| `agents` | `idx_agents_shift` | Filtering agents by shift |
| `bookings` | `idx_bookings_status` | Filtering by lifecycle stage |
| `bookings` | `idx_bookings_priority` | Filtering by priority |
| `bookings` | `idx_bookings_agent` | Fetching bookings by agent |
| `bookings` | `idx_bookings_received` | Ordering by date (DESC) |
| `attendance` | `idx_attendance_agent` | Fetching records by agent |
| `attendance` | `idx_attendance_date` | Filtering by date (DESC) — most common query pattern |
| `allocation_log` | `idx_allocation_booking` | Log lookup by booking |
| `allocation_log` | `idx_allocation_agent` | Log lookup by agent |
| `allocation_log` | `idx_allocation_time` | Ordered log view |
| `pending_queue` | `idx_pending_queue_since` | Oldest-first ordering for FIFO assignment |

### 4.5 Triggers

A single `set_updated_at()` function is reused across five tables. It fires `BEFORE UPDATE` on each row and sets `updated_at = NOW()`, ensuring the timestamp is always accurate without requiring application-layer code.

Tables covered: `users`, `shifts`, `agents`, `bookings`, `attendance`.

---

## 5. API Reference

All endpoints require a valid JWT Bearer token in the `Authorization` header unless otherwise noted. Tokens are obtained via `POST /auth/login`.

Base URL (local development): `http://localhost:8000`

---

### 5.1 Authentication — `/auth`

#### `POST /auth/login`
Authenticate with email and password. Returns a short-lived access token (15 min) and a longer-lived refresh token (7 days).

**Request body:**
```json
{
  "email": "admin@example.com",
  "password": "secret"
}
```

**Response:**
```json
{
  "access_token": "<JWT>",
  "refresh_token": "<JWT>"
}
```

**Errors:** `401` invalid credentials · `403` account disabled

---

#### `POST /auth/refresh`
Exchange a valid refresh token for a new access token and a rotated refresh token. The old refresh token is invalidated.

**Request body:**
```json
{
  "refresh_token": "<JWT>"
}
```

**Response:** Same shape as `/auth/login`

**Errors:** `401` invalid or mismatched refresh token

---

#### `POST /auth/logout`
Revokes the current access token (adds it to a Redis blocklist for its remaining TTL) and deletes the refresh token from Redis.

**Headers:** `Authorization: Bearer <access_token>`

**Response:** `204 No Content`

---

#### `GET /auth/me`
Returns the authenticated user's profile.

**Response:**
```json
{
  "id": "uuid",
  "name": "Alice",
  "email": "alice@example.com",
  "role": "admin"
}
```

---

### 5.2 Agents — `/agents`

Full CRUD for agent profiles. All endpoints require authentication.

#### `GET /agents`
Returns all agents, ordered by name, with their shift details included.

**Response:** Array of agent objects
```json
[
  {
    "id": "uuid",
    "name": "John Doe",
    "email": "john@example.com",
    "shift_id": "uuid",
    "shift": { "id": "uuid", "name": "Morning", "code": "AM", "start_time": "08:00", "end_time": "16:00" },
    "user_id": "uuid",
    "created_at": "...",
    "updated_at": "..."
  }
]
```

---

#### `POST /agents`
Create a new agent. Email must be unique.

**Request body:**
```json
{
  "name": "Jane Smith",
  "email": "jane@example.com",
  "shift_id": "uuid",
  "user_id": "uuid"
}
```

**Response:** `201` with the created agent object

**Errors:** `400` email already exists

---

#### `GET /agents/{agent_id}`
Fetch a single agent by UUID.

**Response:** Agent object (with shift details)

**Errors:** `404` not found

---

#### `PUT /agents/{agent_id}`
Update an agent's details. Only fields included in the request are updated (partial update supported).

**Response:** Updated agent object

**Errors:** `404` not found

---

#### `DELETE /agents/{agent_id}`
Permanently delete an agent record.

**Response:** `204 No Content`

**Errors:** `404` not found

---

### 5.3 Shifts — `/shifts`

Full CRUD for work shift definitions.

#### `GET /shifts`
Returns all shifts ordered by start time.

#### `POST /shifts`
Create a shift. `code` must be unique.

**Request body:**
```json
{
  "name": "Night",
  "code": "NT",
  "start_time": "22:00",
  "end_time": "06:00"
}
```

**Errors:** `400` shift code already exists

#### `GET /shifts/{shift_id}`
Fetch a single shift by UUID.

#### `PUT /shifts/{shift_id}`
Update a shift (partial update supported).

#### `DELETE /shifts/{shift_id}`
Delete a shift. Agents assigned to this shift will have their `shift_id` set to NULL.

---

### 5.4 Bookings — `/bookings`

Manages the full booking lifecycle.

#### `GET /bookings`
Returns a paginated, filterable list of bookings ordered by `received_at` descending.

**Query parameters:**

| Parameter | Type | Description |
|---|---|---|
| `status` | string | Filter: `Pending`, `In Progress`, `Completed` |
| `priority` | string | Filter: `Urgent`, `Standard`, `Economy` |
| `agent_id` | UUID | Filter to a specific agent's bookings |
| `skip` | int (≥0) | Pagination offset (default: 0) |
| `limit` | int (1–200) | Page size (default: 50) |

---

#### `POST /bookings`
Create a new booking. If no `id` is provided, one is auto-generated in the format `BKG-{YEAR}-{NNNNN}`.

**Request body:**
```json
{
  "subject": "Urgent chemical shipment",
  "priority": "Urgent",
  "sender_email": "client@corp.com",
  "cargo_type": "Chemicals",
  "pickup_location": "Port Klang, Malaysia",
  "delivery_location": "Singapore Port",
  "cargo_weight": 1250.50,
  "cargo_volume": 42.00,
  "shipping_mode": "Sea",
  "special_instructions": "Hazardous — Class 3",
  "remarks": "Client requested priority handling"
}
```

**Response:** `201` with the created booking object

**Errors:** `400` booking ID already exists

---

#### `GET /bookings/{booking_id}`
Fetch a single booking by ID, with agent details populated.

**Errors:** `404` not found

---

#### `PUT /bookings/{booking_id}`
Full or partial update of a booking. Automatically sets `assigned_at` when an `agent_id` is first provided, and `completed_at` when status moves to `Completed`.

**Errors:** `404` not found

---

#### `PATCH /bookings/{booking_id}/status`
Lightweight endpoint to update only the booking status.

**Request body:**
```json
{
  "status": "Completed"
}
```

Automatically sets `completed_at` when status transitions to `Completed`.

---

#### `DELETE /bookings/{booking_id}`
Permanently delete a booking (also removes any pending queue or allocation log entries via cascade).

**Response:** `204 No Content`

---

### 5.5 Attendance — `/attendance`

Tracks daily agent attendance using an upsert pattern.

#### `GET /attendance?date=YYYY-MM-DD`
Returns all attendance records for a given date. Optionally filter by `shift_id`.

**Query parameters:**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `date` | date | Yes | Calendar date (YYYY-MM-DD) |
| `shift_id` | UUID | No | Filter to a specific shift |

**Response:** Array of attendance records with embedded agent details.

---

#### `POST /attendance`
Bulk upsert attendance records for a given date. Uses PostgreSQL's `ON CONFLICT DO UPDATE` to handle re-submission (e.g. correcting a check-in time) without creating duplicates.

**Request body:**
```json
{
  "date": "2026-05-19",
  "shift_id": "uuid",
  "records": [
    {
      "agent_id": "uuid",
      "date": "2026-05-19",
      "status": "Present",
      "check_in": "2026-05-19T08:02:00Z",
      "check_out": null,
      "shift_id": null
    }
  ]
}
```

The `shift_id` in `records[n]` overrides the top-level `shift_id` if provided, allowing mixed-shift submissions in a single request.

**Response:** All attendance records for the submitted date.

---

#### `GET /attendance/summary?date=YYYY-MM-DD`
Returns a count breakdown of attendance statuses for a given date.

**Response:**
```json
{
  "date": "2026-05-19",
  "present": 12,
  "absent": 3,
  "on_break": 1,
  "late": 2,
  "total": 18
}
```

---

### 5.6 Allocations — `/allocations`

The core booking-to-agent assignment engine. See [Section 7](#7-allocation-engine) for a detailed explanation of the algorithm.

#### `GET /allocations/status`
Returns the current state of the allocation pointer and identifies the next agent in the round-robin queue.

**Response:**
```json
{
  "pointer": 4,
  "pool_size": 10,
  "next_agent_id": "uuid",
  "next_agent_name": "John Doe"
}
```

---

#### `POST /allocations/run`
Assigns a booking to the next available present agent using the round-robin pointer.

**Request body:**
```json
{
  "booking_id": "BKG-2026-00042"
}
```

**What happens:**
1. Retrieves the list of agents marked `Present` today.
2. Atomically increments the Redis pointer.
3. Selects the agent at `pointer % pool_size`.
4. Updates the booking (`agent_id`, `status = In Progress`, `assigned_at`).
5. Removes the booking from the pending queue if it was there.
6. Writes an entry to `allocation_log`.

**Response:** The allocation log entry with embedded agent details.

**Errors:** `409` no present agents available · `404` booking not found

---

#### `GET /allocations/log`
Returns a paginated log of all allocation events ordered by time descending.

**Query parameters:** `skip` (default 0), `limit` (default 50)

---

#### `POST /allocations/reset-pointer`
Resets the round-robin pointer in Redis back to 0.

**Response:** `204 No Content`

---

### 5.7 Pending Queue — `/pending-queue`

Manages bookings that are queued and awaiting assignment.

#### `GET /pending-queue`
Returns all items in the pending queue, oldest first, with full booking details.

---

#### `POST /pending-queue/assign`
Manually assign a specific queued booking to a specific agent.

**Request body:**
```json
{
  "booking_id": "BKG-2026-00042",
  "agent_id": "uuid"
}
```

Sets the booking to `In Progress` and removes it from the queue.

**Errors:** `404` not in pending queue · `404` booking not found

---

#### `POST /pending-queue/auto-assign-all`
Batch-assigns every booking in the pending queue using the round-robin algorithm, distributing work evenly across all present agents. Processes items in FIFO order (oldest pending first).

**Response:**
```json
{ "assigned": 7 }
```

**Errors:** `409` no present agents available

---

#### `DELETE /pending-queue/{booking_id}`
Removes a booking from the pending queue without assigning it (e.g. if the booking was cancelled).

**Response:** `204 No Content`

---

### 5.8 Reports — `/reports`

Aggregated analytics endpoints for the operations reporting view.

#### `GET /reports/stats`
Overall booking statistics. Results are cached in Redis for 5 minutes.

**Response:**
```json
{
  "total_bookings": 500,
  "completed": 420,
  "pending": 30,
  "sla_breach": 5,
  "completion_rate": 84.0
}
```

`sla_breach` counts bookings still in `Pending` or `In Progress` status that were received more than 24 hours ago.

---

#### `GET /reports/trend?days=7`
Returns a day-by-day trend of received vs. completed bookings for the last N days (1–90).

**Response:**
```json
[
  { "date": "13 May", "received": 45, "completed": 38 },
  { "date": "14 May", "received": 52, "completed": 47 }
]
```

---

#### `GET /reports/priority-distribution`
Returns the percentage breakdown of bookings by priority level, suitable for a pie/donut chart.

**Response:**
```json
[
  { "name": "Urgent",   "value": 20, "color": "#ef4444" },
  { "name": "Standard", "value": 65, "color": "#6366f1" },
  { "name": "Economy",  "value": 15, "color": "#22c55e" }
]
```

---

#### `GET /reports/daily-summary?days=7`
Returns a tabular daily summary for the last N days (1–30), ordered newest first.

**Response:**
```json
[
  {
    "date": "19 May 2026",
    "received": 52,
    "completed": 47,
    "pending": 5,
    "rate": 90
  }
]
```

---

### 5.9 Dashboard — `/dashboard`

#### `GET /dashboard/stats`
Real-time operational counts. Cached in Redis for 60 seconds.

**Response:**
```json
{
  "total_bookings": 500,
  "pending": 30,
  "in_progress": 50,
  "completed": 420
}
```

---

### Health Check

#### `GET /health`
Unauthenticated endpoint for uptime monitoring and load-balancer health checks.

**Response:**
```json
{ "status": "ok", "version": "1.0.0" }
```

---

## 6. Authentication & Security Model

### Token Strategy

BTS uses a **dual-token JWT scheme:**

| Token | Lifetime | Storage | Purpose |
|---|---|---|---|
| Access Token | 15 minutes | Client memory / header | Authorises each API request |
| Refresh Token | 7 days | Redis (server-side) + client | Obtains new access tokens |

Access tokens are signed with HS256 using a configurable secret key and carry the user's UUID as the `sub` claim.

### Token Revocation

Access tokens are stateless by design (short expiry). On logout, the token is added to a Redis blocklist (`revoked:{token}`) with a TTL equal to its remaining lifetime. The `get_current_user` dependency checks this blocklist on every request.

Refresh tokens are stored server-side in Redis (`refresh:{user_id}`). Each successful refresh rotates the token (one-time-use). Logout deletes the refresh token from Redis immediately, preventing reuse.

### Password Security

Passwords are stored exclusively as bcrypt hashes via passlib. The plain-text password is never persisted and is not logged.

### Role-Based Access

Three roles are defined: `admin`, `supervisor`, `agent`. Role information is available via `GET /auth/me` and can be used by the frontend to conditionally render administrative features. The backend currently enforces authentication on all routes; role-based endpoint restrictions can be layered on top as the system grows.

---

## 7. Allocation Engine

The allocation engine distributes incoming bookings evenly across available agents using a **persistent round-robin pointer** stored in Redis.

### How It Works

1. **Pool determination:** At the time of allocation, the system queries the `attendance` table for all agents with `status = 'Present'` for the current calendar date. This pool is ordered by agent name for determinism.

2. **Atomic pointer increment:** The Redis `INCR` command atomically increments the pointer and returns the new value. The pre-increment value is used as the current index, and the pointer is then set to `(pointer + 1) % pool_size` to keep it in bounds.

3. **Agent selection:** `assigned_agent = pool[pointer % pool_size]`

4. **Booking update:** The booking's `agent_id` is set, `status` changed to `In Progress`, and `assigned_at` timestamped.

5. **Audit log:** An `allocation_log` row is written recording the pointer value and pool size at the moment of allocation.

### Why Redis for the Pointer

Storing the pointer in Redis rather than the database provides:
- **Atomicity:** `INCR` is an atomic Redis operation, preventing race conditions under concurrent allocation requests.
- **Speed:** Sub-millisecond reads, no database round-trip for pointer management.
- **Simplicity:** The pointer can be reset via a single `SET` command without a database migration.

### Pending Queue Fallback

When no agents are available (pointer check would return an empty pool), the system does not allocate the booking. Instead, the booking can be added to the `pending_queue` table. The `POST /pending-queue/auto-assign-all` endpoint can be triggered later to batch-process all queued bookings once agents become available.

---

## 8. Caching Strategy

| Cache Key | TTL | Contents |
|---|---|---|
| `bts:dashboard:stats` | 60 seconds | Dashboard booking counts |
| `bts:reports:stats` | 300 seconds | Report-level aggregate statistics |
| `refresh:{user_id}` | 7 days | Refresh token string |
| `revoked:{token}` | Remaining token TTL | Revocation flag |
| `bts:allocation:pointer` | Persistent | Round-robin pointer (no expiry) |

Dashboard stats are cached for 60 seconds (acceptable staleness for a live dashboard). Report stats are cached for 5 minutes (appropriate for analytical views that are queried less frequently). Both caches are invalidated by TTL expiry; write-through invalidation can be added if lower latency is required.

---

## 9. Configuration & Environment

All configuration is managed via environment variables loaded from a `.env` file. The following variables are available:

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://bts_user:bts_pass@localhost:5432/bts_db` | Async PostgreSQL connection string |
| `REDIS_URL` | `redis://:bts_redis_pass@localhost:6379/0` | Redis connection string |
| `SECRET_KEY` | *(change in production)* | JWT signing secret (minimum 32 characters) |
| `ALGORITHM` | `HS256` | JWT signing algorithm |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `15` | Access token lifetime |
| `REFRESH_TOKEN_EXPIRE_DAYS` | `7` | Refresh token lifetime |
| `CORS_ORIGINS` | `http://localhost:3000,http://localhost:3001` | Comma-separated allowed origins |

**Production checklist:**
- Replace `SECRET_KEY` with a cryptographically random value (32+ characters)
- Use strong, unique passwords for PostgreSQL and Redis
- Restrict `CORS_ORIGINS` to the production frontend domain only
- Run behind HTTPS — JWT tokens must not be transmitted over plain HTTP

---

*Document generated from source: `d:\BTS\Backend-API` — BTS v1.0.0*
