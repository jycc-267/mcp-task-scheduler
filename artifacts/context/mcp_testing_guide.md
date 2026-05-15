### 1. Why Claude understands natural language but Inspector needs ISO format

When you type "schedule a task at 7pm" in **Claude Desktop**, Claude's AI model acts as a middleman. It reads your text, looks at its internal clock (which knows your current date and timezone), and realizes it needs to call the `task_create` tool. The AI then automatically translates your natural language into the strict ISO 8601 string that the tool's JSON schema demands, and fires the tool behind the scenes.

In the **MCP Inspector**, you are bypassing the AI completely. You are acting as the client directly invoking the tool. Because there is no LLM to translate for you, you have to type the exact raw JSON arguments (including the ISO format) that the Python backend expects!

### 2. How to test job chaining and recurring jobs

I noticed that while you updated your database models and scheduler logic to support `cron_expr` and `parent_job_id`, **you hadn't exposed those fields to Claude yet!** Your `task_create` tool in `app/mcp_server.py` was still only accepting `description` and `scheduled_at`.

I went ahead and updated `app/mcp_server.py` for you so the tool now accepts `cron_expr` and `parent_job_id`. *(You will need to restart Claude Desktop or the Inspector for them to detect the updated tool schema).*

**How to test in Claude Desktop:**
Now that the tool exposes these fields, Claude knows how to use them. You can literally just ask in plain English:
* **For Cron:** *"Schedule a recurring task to say 'Good morning' every day at 9am. Please use the appropriate cron expression."*
* **For Chaining:** *"Create a task to summarize the news at 8pm, but make it wait for job ID 1 to finish first by setting the parent job ID."*

**How to test in the MCP Inspector:**
Select the `task_create` tool and provide the new arguments in your JSON payload. 

For a recurring job:
```json
{
  "description": "Daily Report",
  "scheduled_at": "2026-05-10T09:00:00",
  "cron_expr": "0 9 * * *"
}
```

For a chained job:
```json
{
  "description": "Send Report Email",
  "scheduled_at": "2026-05-10T09:05:00",
  "parent_job_id": 5
}
```

### 3. How to test Thread Safety and Concurrency

To strictly validate the thread safety and database concurrency of the architecture, a dedicated automated test suite has been built in `tests/test_concurrency.py`. 

You can execute these tests locally via your terminal:
```bash
uv run pytest tests/test_concurrency.py
```

**Here is what the tests cover:**

- **test_thread_safe_set**: Spawns 20 total threads running concurrently—10 threads rapidly slamming the set with .add() operations, followed by 10 threads slamming it with .discard() operations. It perfectly verifies that the threading.Lock prevents any data corruption, ensuring 100% thread safety independently of the GIL.
- **test_sqlite_wal_concurrent_reads_writes**: Simulates heavy UI and background load by spawning 5 writer threads and 5 reader threads simultaneously hammering the database. It asserts that the PRAGMA journal_mode=WAL and QueuePool configurations successfully manage concurrent connections without throwing any OperationalError: database is locked exceptions.
- **test_watcher_worker_segregation_pattern**: Programmatically simulates the full step-by-step lifecycle between the Watcher and Worker. It strictly asserts that:
    - The Watcher reads and discovers jobs, routing them to enqueued_job_ids and GLOBAL_JOB_QUEUE.
    - The job's database status remains securely as "pending" after the Watcher acts.
    - The Worker successfully handles the queue pop and owns the mutation transition to "running" and ultimately "completed".
    - The ThreadSafeSet cleanly discards the ID when finished.

