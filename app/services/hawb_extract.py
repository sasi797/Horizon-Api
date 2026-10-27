import base64
import json
import logging
from pathlib import Path

import anthropic

from app.config import settings

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT_PATH = Path(__file__).parent.parent.parent / "hawb_system_prompt.txt"
_BLIND_SYSTEM_PROMPT_PATH = Path(__file__).parent.parent.parent / "hawb_blind_system_prompt.txt"
_BLIND_MERGE_SYSTEM_PROMPT_PATH = Path(__file__).parent.parent.parent / "hawb_blind_merge_system_prompt.txt"
_THINVENTORY_SYSTEM_PROMPT_PATH = Path(__file__).parent.parent.parent / "hawb_thinventory_system_prompt.txt"
_THINVENTORY_LABEL_SYSTEM_PROMPT_PATH = Path(__file__).parent.parent.parent / "hawb_thinventory_label_system_prompt.txt"

# Document kinds returned by classify_document.
KIND_MARKEN = "marken"
KIND_THINVENTORY_NOTE = "thinventory_note"
KIND_THINVENTORY_LABEL = "thinventory_label"

_CLASSIFY_PROMPT = (
    "Identify which company's template this logistics PDF is. Answer with exactly one word:\n"
    "- thinventory_note: a Thinventory \"Transport Management\" Delivery Note (TMS Job Number, Collection Details / Delivery Details, SKU table)\n"
    "- thinventory_label: a small Thinventory delivery/locker label (Thinventory logo, barcode, Locker Bank, Tracking Number, Cust Ref)\n"
    "- marken: anything else, including Marken HAWB labels and booking forms\n"
    "Reply with only the single word."
)


def classify_document(file_bytes: bytes, filename: str) -> str:
    """Decide which company template a PDF is, so the right extraction prompt
    runs. Any failure or unrecognised answer falls back to Marken, i.e. the
    pre-existing behaviour, so classification can never make things worse."""
    try:
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            messages=[{"role": "user", "content": [
                {"type": "document", "source": {
                    "type": "base64", "media_type": "application/pdf",
                    "data": base64.standard_b64encode(file_bytes).decode("utf-8"),
                }},
                {"type": "text", "text": _CLASSIFY_PROMPT},
            ]}],
        )
        answer = "".join(b.text for b in response.content if b.type == "text").strip().lower()
    except Exception:
        logger.error("Document classification failed for %s, defaulting to marken", filename, exc_info=True)
        return KIND_MARKEN
    kind = next((k for k in (KIND_THINVENTORY_NOTE, KIND_THINVENTORY_LABEL) if k in answer), KIND_MARKEN)
    logger.info("Classified %s as %s", filename, kind)
    return kind


def extract_jobs(file_bytes: bytes, filename: str, kind: str | None = None) -> list[dict]:
    """Call Claude on a HAWB PDF and return one dict per split-out HAWB job.

    `kind` is the classify_document result; None classifies here. Marken
    PDFs use the original prompt, Thinventory delivery notes their own."""
    if kind is None:
        kind = classify_document(file_bytes, filename)
    if kind == KIND_THINVENTORY_LABEL:
        return extract_thinventory_label_jobs(file_bytes, filename)
    prompt_path = _THINVENTORY_SYSTEM_PROMPT_PATH if kind == KIND_THINVENTORY_NOTE else _SYSTEM_PROMPT_PATH
    system_prompt = prompt_path.read_text()
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


def extract_thinventory_label_jobs(file_bytes: bytes, filename: str) -> list[dict]:
    """Extract Thinventory locker labels. They carry no HAWB number — each is
    merged into its Thinventory delivery note by Customer Reference at ingest."""
    system_prompt = _THINVENTORY_LABEL_SYSTEM_PROMPT_PATH.read_text()
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
        {"type": "text", "text": "Extract the label data from this document."},
    ]

    logger.info("Calling Claude API for Thinventory label extraction on %s (%d bytes)", filename, len(file_bytes))
    with client.beta.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=16000,
        system=system_prompt,
        messages=[{"role": "user", "content": content}],
        betas=["output-128k-2025-02-19"],
    ) as stream:
        raw_text = stream.get_final_text()

    return _parse_json(raw_text).get("jobs", [])


def extract_blind_candidates(file_bytes: bytes, filename: str) -> list[dict]:
    """Call Claude on a booking-form PDF (an "MF-PCS"-named attachment) and
    return one dict per HAWB found, in the same schema as extract_jobs, so it
    can be matched/merged against a companion plain-HAWB label job.
    """
    system_prompt = _BLIND_SYSTEM_PROMPT_PATH.read_text()
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
            "text": "Extract the HAWB booking data from this document.",
        },
    ]

    logger.info("Calling Claude API for blind HAWB candidate extraction on %s (%d bytes)", filename, len(file_bytes))
    with client.beta.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=16000,
        system=system_prompt,
        messages=[{"role": "user", "content": content}],
        betas=["output-128k-2025-02-19"],
    ) as stream:
        raw_text = stream.get_final_text()

    logger.info("Claude API response received for %s", filename)
    result = _parse_json(raw_text)
    return result.get("jobs", [])


def merge_blind_job(plain_job: dict, blind_job: dict, email_body: str | None) -> dict:
    """Merge a plain-label job dict and a booking-form job dict (same
    hawb_number) plus the carrying email's body text into one job dict,
    resolving whichever fields the label withheld.
    """
    system_prompt = _BLIND_MERGE_SYSTEM_PROMPT_PATH.read_text()
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    content = [
        {"type": "text", "text": "LABEL_JOB:\n" + json.dumps(plain_job, default=str)},
        {"type": "text", "text": "BOOKING_JOB:\n" + json.dumps(blind_job, default=str)},
        {"type": "text", "text": "EMAIL_BODY:\n" + (email_body or "(no email body available)")},
        {"type": "text", "text": "Merge these into one job object per the instructions."},
    ]

    logger.info("Calling Claude API to merge blind HAWB %s", plain_job.get("hawb_number") or blind_job.get("hawb_number"))
    with client.beta.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=16000,
        system=system_prompt,
        messages=[{"role": "user", "content": content}],
        betas=["output-128k-2025-02-19"],
    ) as stream:
        raw_text = stream.get_final_text()

    return _parse_json(raw_text)


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
