# Implementation Plan: Production-grade Remote MCP Task Scheduler

## Overview
This plan outlines the upgrade of the existing local `stdio`-based MCP task scheduler to a robust, production-grade remote server using the `fastmcp` framework. The upgrade includes advanced orchestration (cron and DAGs), an LLM pre-parsing middleware for natural language scheduling, and the addition of MCP Resources and Prompts.

## Codebase Context
- **Codebase structure**: A modular Python application built with `uv`, organized around `app/` (models, database, scheduler, mcp_server).
- **Codebase features**: A custom watcher/worker background scheduler utilizing SQLite, SQLAlchemy, and a time-bucket based queue.
- **Codebase dependencies**: Python 3.13, `mcp`, `fastmcp`, `sqlalchemy`, and `pytest`.

## Requirements
- Migrate existing vanilla Python MCP setup to `fastmcp` for decorator-based routing and Pydantic validation.
- Implement cron-based recurring jobs and dependency-driven job chaining (DAGs).
- Build an LLM pre-parsing middleware connecting to a live API (e.g., Gemini/Claude) to structure NLP input before internal execution.
- Implement MCP Resources to expose live job details/logs.
- Implement MCP Prompts, specifically a `daily_review` parameterizable prompt.
- Ensure all logic is fully testable via `pytest` and managed via `uv`.
- Prepare for deployment to Prefect Horizon cloud platform (implying remote-friendly transport like SSE/HTTP).

## Architecture Changes
- `app/models.py`: Update `Job` model to support recurring configurations (cron string), dependency mapping (parent/child job IDs), and extended metadata (logs).
- `app/scheduler.py`: Integrate an async scheduler (like `APScheduler`) or heavily augment the watcher loop to evaluate cron expressions and resolve DAG dependencies.
- `app/llm_parser.py` (New): Implement the LLM middleware to intercept raw natural language inputs and output structured schemas for `task_create`.
- `app/mcp_server.py`: Rewrite using `FastMCP` class. Add `@mcp.tool`, `@mcp.resource`, and `@mcp.prompt` decorators. Swap stdio to an SSE/HTTP transport compatible with remote clients.

## Implementation Steps

### Phase 1: FastMCP Migration & Server Setup
1. **Migrate Tool Definitions** (File: `app/mcp_server.py`)
   - Action: Replace vanilla `mcp.server.Server` with `from fastmcp import FastMCP`. Convert `handle_create_task`, `handle_get_status`, etc., to use `@mcp.tool()` decorators.
   - Why: FastMCP provides built-in Pydantic validation and cleaner routing.
   - Dependencies: None
   - Risk: Low

2. **Add Remote Transport Support** (File: `app/mcp_server.py`)
   - Action: Configure the `FastMCP` entrypoint to support both `stdio` and HTTP/SSE transports (using FastMCP's built-in FastAPI/SSE integrations).
   - Why: To ensure compatibility with remote connections from heavy MCP clients.
   - Dependencies: Phase 1, Step 1
   - Risk: Medium

### Phase 2: Database Schema & Orchestration Upgrades
1. **Extend Job Model** (File: `app/models.py`)
   - Action: Add fields to `Job`: `cron_expr` (String, nullable), `parent_job_id` (Integer, nullable, Foreign Key to `Job.id`), and `logs` (Text, nullable).
   - Why: Required to support recurring jobs and DAG execution chains.
   - Dependencies: None
   - Risk: Low

2. **Implement Cron & DAG Resolution** (File: `app/scheduler.py`)
   - Action: Update `watcher_loop` or integrate `APScheduler`. For recurring jobs, calculate the next `scheduled_at` based on `cron_expr` upon completion. For DAGs, update watcher to only queue jobs if their `parent_job_id` is resolved as `completed`.
   - Why: Enables advanced task orchestration while surviving server restarts via SQLite.
   - Dependencies: Phase 2, Step 1
   - Risk: High

### Phase 3: LLM Pre-Parsing Middleware
1. **Implement NLP Parser Tool** (File: `app/llm_parser.py`)
   - Action: Create a function that takes a natural language string, calls an LLM API, and uses structured outputs to return a validated Pydantic `TaskSchema` (description, scheduled_at, cron_expr, dependencies).
   - Why: Pre-parses raw user requests into strict parameters before hitting `task_create`.
   - Dependencies: None
   - Risk: Medium

2. **Integrate Middleware into MCP** (File: `app/mcp_server.py`)
   - Action: Create a new tool `@mcp.tool() def nlp_task_create(query: str)` which calls the `llm_parser`, then internally calls the standard `task_create` logic.
   - Why: Exposes the NLP capability to the MCP client cleanly.
   - Dependencies: Phase 3, Step 1
   - Risk: Low

### Phase 4: Expanded MCP Capabilities (Resources & Prompts)
1. **Implement Job Resources** (File: `app/mcp_server.py`)
   - Action: Use `@mcp.resource("job://{job_id}/logs")` to return live logs/details of a specific job by querying the database.
   - Why: Satisfies the requirement to expose system health and live execution details.
   - Dependencies: Phase 2, Step 1
   - Risk: Low

2. **Implement Daily Review Prompt** (File: `app/mcp_server.py`)
   - Action: Use `@mcp.prompt("daily_review")` to aggregate recently completed tasks and upcoming chains from the DB, injecting them into a prompt template for the LLM client.
   - Why: Provides contextual aggregation for the user's daily standup/review.
   - Dependencies: Phase 1, Step 1
   - Risk: Low

## Testing Strategy
- Unit tests: `tests/test_scheduler.py` (validate cron timing and DAG dependency resolution), `tests/test_llm_parser.py` (mock LLM API to test structured schema extraction).
- [MCP Inspector](https://github.com/modelcontextprotocol/inspector): run with `npx @modelcontextprotocol/inspector uv run python -m app.mcp_server` to test MCP server.
- Integration tests: `tests/test_mcp_server.py` (simulate FastMCP tool calls, resource fetching, and prompt generation via TestClient).

## Risks & Mitigations
- **Risk**: SQLite concurrency issues with multiple HTTP requests (SSE) and background threads. -> Mitigation: Continue using `check_same_thread=False` but ensure tight, short-lived sessions and consider WAL mode for SQLite.
- **Risk**: LLM pre-parsing API failures or latency. -> Mitigation: Implement robust error handling, retries, and fallback to direct programmatic `task_create` if the NLP middleware fails.
- **Risk**: Complex DAG cyclical dependencies. -> Mitigation: Implement a topological sort/cycle detection check during DAG job creation.

## Success Criteria
- [ ] FastMCP is the active framework serving tools, resources, and prompts over SSE/stdio.
- [ ] Jobs can be scheduled with cron expressions and correctly recur.
- [ ] Job B only executes when its parent Job A successfully completes.
- [ ] `nlp_task_create` successfully uses an LLM to convert "Remind me to call John every Tuesday" into a structured cron job.
- [ ] `job://{job_id}/logs` successfully returns live job text.
- [ ] `daily_review` prompt successfully formats DB data.

## Best Practices to Enforce
1. **Be Specific**: Strict Pydantic models for the LLM structured output.
2. **Consider Edge Cases**: Parent job fails (child should be cancelled/skipped).
3. **Minimize Changes**: Build upon the existing `watcher_loop` where possible before rewriting it entirely.
4. **Maintain Patterns**: Use dependency injection for DB sessions across FastMCP route handlers.
5. **Enable Testing**: Keep LLM logic completely separated from DB logic to allow mocking.
