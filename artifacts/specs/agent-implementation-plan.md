# Implementation Plan: Agentic Worker Integration

## Overview
This plan outlines the implementation of an autonomous agent within the task scheduler. Currently, background tasks are executed using a single, zero-shot LLM completion in the worker thread. The new architecture will replace this static approach with an "Agent" that can utilize tools, perform multi-step reasoning (e.g., ReAct loop), and autonomously interact with its environment to complete complex, scheduled tasks before returning a final result to the database.

## Codebase Context
- **Codebase structure**: A Python 3.13 backend using `FastMCP` for the server interface and SQLAlchemy with SQLite for persistence. Background processing is handled by custom threaded loops (`app/scheduler.py`).
- **Codebase features**: Natural language scheduling via Gemini (`app/llm_parser.py`), fault-tolerant task queueing, database partitioning using time buckets, and an MCP server.
- **Codebase dependencies**: `fastmcp`, `sqlalchemy`, `google-genai` (for Gemini access), `pydantic`.

## Requirements
- Introduce a specialized agent implementation that can utilize tools (e.g., file reading, web searching, or custom python functions) rather than just generating text.
- The agent must be fully integrated into the existing `worker_loop` in `app/scheduler.py`.
- The agent must preserve the existing fault-tolerant execution constraints (e.g., timeouts, crash recovery).
- Maintain immutable data patterns and structured error handling.
- Keep the `app` directory clean with high cohesion (create a dedicated `agent.py` module).

## Architecture Changes
- **`app/agent.py`**: A new module to encapsulate the agent's core loop, tool definitions, and state management.
- **`app/scheduler.py`**: Modify the `_execute_with_llm` function (or replace it) to instantiate the new agent and invoke its run method, passing the task description as the goal.

## Implementation Steps

### Phase 1: Core Agent Module Creation
1. **Create Agent Module** (File: `app/agent.py`)
   - Action: Implement a new `TaskAgent` class that wraps the `google.genai` client.
   - Why: Decouples the complex agentic loop (tool calling, chat history management) from the basic queue worker logic.
   - Dependencies: None
   - Risk: Low

2. **Define Agent Tools** (File: `app/agent.py` or new `app/tools.py`)
   - Action: Define an initial set of Python functions that the Gemini LLM can call as tools (e.g., web scraping, calculator, or basic file read/write). Decorate or schema-wrap them for `google.genai`.
   - Why: An agent is only as capable as its tools. The scheduled tasks need these tools to interact with the world.
   - Dependencies: Requires Step 1
   - Risk: Medium (Tool error handling must be robust so a tool failure doesn't crash the agent loop).

### Phase 2: Integration with Scheduler Worker
1. **Refactor Worker Execution** (File: `app/scheduler.py`)
   - Action: Import `TaskAgent` from `app.agent`. Replace the single `client.models.generate_content` call inside `_execute_with_llm` with the initialization and execution of the `TaskAgent` loop.
   - Why: Transitions the background worker from a zero-shot generator to an autonomous agent.
   - Dependencies: Requires Phase 1
   - Risk: Medium (The agent loop may take longer than the zero-shot generation; we must ensure `LLM_TIMEOUT_SECONDS` appropriately bounds the *entire* agent execution).

2. **Enhance Logging for Agent Steps** (File: `app/scheduler.py`)
   - Action: Capture the intermediate steps (tool calls, thoughts) from the `TaskAgent` and append them to the `job.logs` field alongside the final `result_text`.
   - Why: Crucial for debugging and providing users visibility into *how* the agent accomplished the task.
   - Dependencies: Requires Phase 2, Step 1
   - Risk: Low

## Testing Strategy
- **Unit tests**: `tests/test_agent.py` to test the agent loop with mocked tools and ensure it correctly handles tool errors and reaches a final answer.
- **Integration tests**: Submit a task via `nlp_task_create` that explicitly requires a tool (e.g., "Find the current weather and save it"), wait for the scheduler to pick it up, and verify the DB result contains the tool's output.

## Risks & Mitigations
- **Risk**: The agent loop gets stuck in an infinite loop calling the same tool repeatedly. 
  -> Mitigation: Implement a strict `max_iterations` limit inside the `TaskAgent` loop and rely on the existing `concurrent.futures.ThreadPoolExecutor` timeout.
- **Risk**: Tool execution throws unexpected system errors, crashing the worker thread.
  -> Mitigation: Wrap all tool invocations in a `try/except` block that returns the error message back to the LLM, allowing it to self-correct.

## Success Criteria
- [ ] `app/agent.py` is created with a working tool-calling loop using `google.genai`.
- [ ] `app/scheduler.py` worker threads utilize the new agent to process pending jobs.
- [ ] The agent's intermediate thoughts and tool calls are captured in the job's `logs` field in the database.
- [ ] The entire execution remains safely bounded by timeouts and handles API failures gracefully.

## Best Practices to Enforce
1. **Be Specific**: Keep tools well-typed with Pydantic or native Python type hints so the LLM understands the schema.
2. **Consider Edge Cases**: Handle LLM parsing errors if it returns invalid tool arguments.
3. **Minimize Changes**: Keep the `watcher_loop` and `reaper_loop` untouched; only the execution payload logic changes.
4. **Maintain Patterns**: Use `uv` strictly. Use dependency injection for the LLM client.

## Technical Debt to Address
- The current `scheduler.py` file is growing large (~340 lines). The execution logic (`_execute_with_llm`) and worker logic could eventually be moved to a separate `app/worker.py` module to maintain high cohesion and file size < 400 lines.
