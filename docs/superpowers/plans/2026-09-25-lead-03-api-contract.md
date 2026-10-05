# LEAD-03 API Contract Implementation Plan

> **For agentic workers:** This plan records the documentation-only execution for the frozen V1.0 REST contract.

**Goal:** Freeze the V1.0 REST routes, schemas, status codes, stable errors, authentication, permissions, idempotency semantics, and OpenAPI generation handoff.

**Architecture:** `docs/architecture/03-api-contract.md` is the human-readable contract. `shared/api-contract/openapi.json` is the checked-in OpenAPI baseline that ENG-02 must reproduce from FastAPI/Pydantic. `docs/task-cards/LEAD-03-api-contract.md` records the release gate and downstream engineering instructions.

**Tech Stack:** FastAPI, Pydantic, OpenAPI 3.1, Next.js generated TypeScript client.

---

### Task 1: Freeze the human-readable API contract

**Files:**
- Modify: `docs/architecture/03-api-contract.md`

- [ ] Replace relative routes with canonical `/api/v1` routes.
- [ ] Document route table, request/response schemas, status codes, auth/permission matrix, error mapping, idempotency behavior, duplicate body conflict behavior, OpenAPI generation rules, and ENG-02/03/08 instructions.
- [ ] Record unresolved decisions explicitly without adding unapproved endpoints or changing database/state-machine rules.

### Task 2: Add the OpenAPI baseline artifact

**Files:**
- Create: `shared/api-contract/openapi.json`

- [ ] Encode the frozen public and internal-boundary paths, reusable schemas, security scheme, common responses, and `202` generation semantics.
- [ ] Keep this artifact aligned with the human-readable contract; ENG-02 owns runtime export from FastAPI.

### Task 3: Update the LEAD-03 handoff gate

**Files:**
- Modify: `docs/task-cards/LEAD-03-api-contract.md`

- [ ] State the exact review gate, output files, downstream execution order, and stop condition after technical-lead review.

### Task 4: Self-review

- [ ] Search for non-canonical API paths in the changed contract files.
- [ ] Parse the OpenAPI JSON and verify every documented route has a path entry.
- [ ] Confirm no FastAPI Router, frontend page, database field, state-machine transition, point rule, or Git commit was added.
