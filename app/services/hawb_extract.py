import base64
import json
import logging
from pathlib import Path

import anthropic

from app.config import settings

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT_PATH = Path(__file__).parent.parent.parent / "hawb_system_prompt.txt"


def extract_jobs(file_bytes: bytes, filename: str) -> list[dict]:
    """Call Claude on a HAWB PDF and return one dict per split-out HAWB job."""
    system_prompt = _SYSTEM_PROMPT_PATH.read_text()
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    content = [
        {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": base64.standard_b64encode(file_bytes).decode("utf-8"),
            },
        },
        {
            "type": "text",
            "text": "Split this document into one job per HAWB and extract all shipment data.",
        },
    ]

    logger.info("Calling Claude API for HAWB extraction on %s (%d bytes)", filename, len(file_bytes))
    with client.beta.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=64000,
        system=system_prompt,
        messages=[{"role": "user", "content": content}],
        betas=["output-128k-2025-02-19"],
    ) as stream:
        raw_text = stream.get_final_text()

    logger.info("Claude API response received for %s", filename)
    result = _parse_json(raw_text)
    return result.get("jobs", [])


def _parse_json(raw_text: str) -> dict:
    text = raw_text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(l for l in lines if not l.strip().startswith("```")).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        logger.error("JSON parse failed. Raw response (first 500 chars): %s", text[:500])
        raise
