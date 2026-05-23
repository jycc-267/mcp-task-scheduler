"""NLP parser that converts natural language scheduling requests into structured TaskSchema objects.

Uses Google Gemini's structured output feature to extract scheduling parameters
from free-form text and return a validated Pydantic model.
"""

import logging
import os
from datetime import datetime

from dotenv import load_dotenv
from google import genai
from pydantic import BaseModel, Field

from app.models import _utcnow

load_dotenv()

logger = logging.getLogger(__name__)

GEMINI_API_KEY_VAR = "GEMINI_API_KEY"
GEMINI_MODEL = "gemini-2.5-flash"


class TaskSchema(BaseModel):
    """Structured output schema for parsed task scheduling requests."""

    description: str = Field(
        description="A clear, concise description of the task to be performed."
    )
    scheduled_at: str = Field(
        description=(
            "ISO 8601 datetime string for when the task should first run. "
            "If the user says 'now', use the current UTC time. "
            "Example: '2026-05-10T17:00:00'"
        )
    )
    cron_expr: str | None = Field(
        default=None,
        description=(
            "Standard 5-field cron expression if the task is recurring. "
            "Example: '0 17 * * 5' for every Friday at 5pm. "
            "None if the task is a one-time execution."
        ),
    )
    user_timezone: str = Field(
        default="UTC",
        description=(
            "The IANA timezone name of the user (e.g., 'America/New_York', 'Asia/Taipei', 'Europe/London'). "
            "Deduce this from the user's request, context or system instructions. If unknown or not specified, default to 'UTC'."
        ),
    )
    dependencies: list[int] = Field(
        default_factory=list,
        description=(
            "List of existing job IDs that must complete before this task runs. "
            "Empty list if no dependencies."
        ),
    )


SYSTEM_PROMPT = """\
You are a task scheduling assistant. Your job is to parse natural language \
scheduling requests and extract structured scheduling parameters.

The current UTC date and time is: {current_time}
The user's local timezone is: {user_timezone}

Rules:
- Extract a clear task description from the user's request.
- Determine the scheduled_at time in ISO 8601 format (UTC).
- If the user references relative times (e.g. 'tomorrow at 3pm', 'in 2 hours'), resolve it against their local timezone ({user_timezone}) first, then convert it to UTC.
- If the task is recurring (e.g., "every Friday", "daily", "weekly"), \
  generate an appropriate 5-field cron expression in the user's local timezone context.
- Identify the user's IANA timezone name and populate user_timezone. If not specified or if the user's query matches the default timezone, set user_timezone to '{user_timezone}'.
- If the user references dependencies on other tasks by ID, include them.
- If the user says "now" or "immediately", use the current time.
"""


def _get_gemini_client() -> genai.Client:
    """Create a Gemini client from the environment API key.

    Raises:
        ValueError: If GEMINI_API_KEY is not set.
    """
    api_key = os.environ.get(GEMINI_API_KEY_VAR)
    if not api_key:
        raise ValueError(
            f"Environment variable '{GEMINI_API_KEY_VAR}' is required but not set."
        )
    return genai.Client(api_key=api_key)


async def parse_task(query: str, default_timezone: str = "UTC") -> TaskSchema:
    """Parse a natural language scheduling request into a structured TaskSchema.

    Uses Gemini's structured output to ensure the response conforms to
    the TaskSchema Pydantic model.

    Args:
        query: Raw natural language string (e.g., "Summarize the news every Friday at 5pm").
        default_timezone: The fallback user timezone if not explicitly defined (default "UTC").

    Returns:
        A validated TaskSchema with extracted scheduling parameters.

    Raises:
        ValueError: If GEMINI_API_KEY is not set.
        google.genai.errors.APIError: If the Gemini API call fails.
    """
    client = _get_gemini_client()
    current_time = _utcnow().strftime("%Y-%m-%dT%H:%M:%S")

    logger.info("Parsing NLP task request in timezone %s: %s", default_timezone, query)

    response = await client.aio.models.generate_content(
        model=GEMINI_MODEL,
        contents=query,
        config=genai.types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT.format(
                current_time=current_time, user_timezone=default_timezone
            ),
            response_mime_type="application/json",
            response_schema=TaskSchema,
            temperature=0.1,
        ),
    )

    parsed = TaskSchema.model_validate_json(response.text)
    logger.info("Parsed task schema: %s", parsed.model_dump())
    return parsed


def parse_task_sync(query: str, default_timezone: str = "UTC") -> TaskSchema:
    """Synchronous version of parse_task for use in threaded contexts.

    Args:
        query: Raw natural language string.
        default_timezone: The fallback user timezone if not explicitly defined (default "UTC").

    Returns:
        A validated TaskSchema with extracted scheduling parameters.

    Raises:
        ValueError: If GEMINI_API_KEY is not set.
        google.genai.errors.APIError: If the Gemini API call fails.
    """
    client = _get_gemini_client()
    current_time = _utcnow().strftime("%Y-%m-%dT%H:%M:%S")

    logger.info("Parsing NLP task request (sync) in timezone %s: %s", default_timezone, query)

    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=query,
        config=genai.types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT.format(
                current_time=current_time, user_timezone=default_timezone
            ),
            response_mime_type="application/json",
            response_schema=TaskSchema,
            temperature=0.1,
        ),
    )

    parsed = TaskSchema.model_validate_json(response.text)
    logger.info("Parsed task schema (sync): %s", parsed.model_dump())
    return parsed
