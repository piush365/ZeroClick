"""
core/llm_agent.py
-----------------
The brain of ZeroClick.
All Gemini LLM calls live here.
"""

import os
import json
import time
from google import genai
from dotenv import load_dotenv
from pydantic import BaseModel
from typing import Optional

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "../.env"))

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
MODEL     = "gemini-2.5-flash"
MOCK_MODE = os.getenv("MOCK_MODE", "false").lower() == "true"


# ── Output schemas ────────────────────────────────────────────────────────────

class IntentResult(BaseModel):
    intent: str
    confidence: float
    summary: str

class TimeSlot(BaseModel):
    participant: str
    date: str
    start_time: str
    end_time: str
    timezone: str

class AvailabilityResult(BaseModel):
    participants: list[str]
    slots: list[TimeSlot]
    overlap: list[TimeSlot]
    suggested_meeting: Optional[TimeSlot]
    needs_clarification: bool
    clarification_question: Optional[str]

class ThreadSummary(BaseModel):
    topic: str
    latest_status: str
    key_decisions: list[str]
    action_items: list[str]
    participants: list[str]

class BRDDocument(BaseModel):
    project_name: str
    stakeholders: list[str]
    functional_requirements: list[str]
    non_functional_requirements: list[str]
    decisions_made: list[str]
    open_questions: list[str]
    timelines: list[str]
    feature_priority: list[str]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _call_llm(prompt: str) -> str:
    """
    Send a prompt to Gemini and return the raw text response.
    - Returns mock responses if MOCK_MODE=true
    - Retries once after 60s if rate limited
    - Guards against empty responses
    """
    if MOCK_MODE:
        p = prompt.lower()
        if "intent" in p:
            return '{"intent": "scheduling", "confidence": 0.95, "summary": "Sender wants to schedule a meeting"}'
        elif "availability" in p:
            return '{"participants": ["alice@gmail.com", "bob@gmail.com"], "slots": [{"participant": "alice@gmail.com", "date": "2026-03-26", "start_time": "15:00", "end_time": "17:00", "timezone": "Asia/Kolkata"}, {"participant": "bob@gmail.com", "date": "2026-03-26", "start_time": "14:00", "end_time": "16:00", "timezone": "Asia/Kolkata"}], "overlap": [{"participant": "ALL", "date": "2026-03-26", "start_time": "15:00", "end_time": "16:00", "timezone": "Asia/Kolkata"}], "suggested_meeting": {"participant": "ALL", "date": "2026-03-26", "start_time": "15:00", "end_time": "16:00", "timezone": "Asia/Kolkata"}, "needs_clarification": false, "clarification_question": null}'
        elif "brd" in p or "business" in p or "requirements" in p:
            return '{"project_name": "Mock Project", "stakeholders": ["Alice - PM", "Bob - Dev"], "functional_requirements": ["FR1: The system shall provide user login", "FR2: The system shall provide a dashboard"], "non_functional_requirements": ["NFR1: The system must load in under 2 seconds", "NFR2: The system must support 1000 concurrent users"], "decisions_made": ["Use React Native for frontend", "Google OAuth for login"], "open_questions": ["Do we need offline mode?"], "timelines": ["Beta launch - April 30"], "feature_priority": ["P1: Login screen", "P1: Dashboard", "P2: Offline mode"]}'
        elif "summary" in p or "status" in p or "thread" in p:
            return '{"topic": "Project status update", "latest_status": "Development on track, beta April 30", "key_decisions": ["React Native chosen", "Google OAuth only"], "action_items": ["Bob: set up repo by Friday"], "participants": ["alice@gmail.com", "bob@gmail.com"]}'
        else:
            return "This is a mock response from ZeroClick."

    for attempt in range(2):
        try:
            response = client.models.generate_content(
                model=MODEL,
                contents=prompt
            )
            # Guard against empty response
            if not response.text:
                raise ValueError("Gemini returned an empty response")
            return response.text.strip()
        except Exception as e:
            if "429" in str(e) and attempt == 0:
                print("[LLM] Rate limited — waiting 60 seconds before retry...")
                time.sleep(60)
            else:
                raise
    raise RuntimeError("LLM call failed after 2 attempts")


def _parse_json(text: str) -> dict:
    """Strip markdown fences and parse JSON."""
    clean = text.replace("```json", "").replace("```", "").strip()
    return json.loads(clean)


# ── Agent functions ───────────────────────────────────────────────────────────

def detect_intent(subject: str, body: str) -> IntentResult:
    prompt = f"""
You are an email classification agent for ZeroClick.

Analyze this email and classify its PRIMARY intent.

Subject: {subject}
Body:
{body}

Respond ONLY with a JSON object, no explanation, no markdown:
{{
  "intent": "scheduling" or "update_request" or "other",
  "confidence": a number between 0.0 and 1.0,
  "summary": "one sentence describing what the sender wants"
}}

Rules:
- "scheduling" = sender wants to find a meeting time or share availability
- "update_request" = sender wants the latest status of a project or task
- "other" = anything else
"""
    raw = _call_llm(prompt)
    data = _parse_json(raw)
    return IntentResult(**data)


def extract_availability(thread: str, reference_date: str) -> AvailabilityResult:
    prompt = f"""
You are a scheduling intelligence agent for ZeroClick.

Today's date: {reference_date}

Read this email thread and extract ALL availability slots mentioned by each person.
Then find time slots where ALL participants are free at the same time.

Email thread:
{thread}

Respond ONLY with a JSON object, no explanation, no markdown:
{{
  "participants": ["email1", "email2"],
  "slots": [
    {{
      "participant": "email",
      "date": "YYYY-MM-DD",
      "start_time": "HH:MM",
      "end_time": "HH:MM",
      "timezone": "Asia/Kolkata"
    }}
  ],
  "overlap": [
    {{
      "participant": "ALL",
      "date": "YYYY-MM-DD",
      "start_time": "HH:MM",
      "end_time": "HH:MM",
      "timezone": "Asia/Kolkata"
    }}
  ],
  "suggested_meeting": {{
    "participant": "ALL",
    "date": "YYYY-MM-DD",
    "start_time": "HH:MM",
    "end_time": "HH:MM",
    "timezone": "Asia/Kolkata"
  }},
  "needs_clarification": false,
  "clarification_question": null
}}

Rules:
- participants must be email addresses, not names
- if no overlap exists set overlap to [] and suggested_meeting to null
- if slots are too vague set needs_clarification to true
- resolve relative dates using today: {reference_date}
- default timezone to Asia/Kolkata if not mentioned
"""
    raw = _call_llm(prompt)
    data = _parse_json(raw)
    return AvailabilityResult(**data)


def summarize_thread(thread: str) -> ThreadSummary:
    prompt = f"""
You are a thread intelligence agent for ZeroClick.

Read this email thread and extract a structured summary.

Email thread:
{thread}

Respond ONLY with a JSON object, no explanation, no markdown:
{{
  "topic": "what this thread is about in one short phrase",
  "latest_status": "current status based on the most recent email",
  "key_decisions": ["decision 1", "decision 2"],
  "action_items": ["person: what they need to do"],
  "participants": ["email or name of everyone in the thread"]
}}
"""
    raw = _call_llm(prompt)
    data = _parse_json(raw)
    return ThreadSummary(**data)


def generate_brd(thread: str) -> BRDDocument:
    prompt = f"""
You are a Business Analyst AI agent for ZeroClick.

Read this email thread and extract a complete Business Requirements Document.

Thread:
{thread}

Respond ONLY with a JSON object, no explanation, no markdown:
{{
  "project_name": "inferred project name",
  "stakeholders": ["Name - Role"],
  "functional_requirements": [
    "FR1: The system shall...",
    "FR2: The system shall..."
  ],
  "non_functional_requirements": [
    "NFR1: The system must...",
    "NFR2: The system must..."
  ],
  "decisions_made": ["decision 1", "decision 2"],
  "open_questions": ["unresolved question 1"],
  "timelines": ["milestone - date"],
  "feature_priority": ["P1: feature name", "P2: feature name"]
}}

Be thorough. Label every requirement clearly (FR1, FR2, NFR1, etc).
"""
    raw = _call_llm(prompt)
    data = _parse_json(raw)
    return BRDDocument(**data)


def compose_reply(intent: str, context: dict, recipient_name: str) -> str:
    """
    Compose a reply email. Always appends AI disclaimer.
    """
    if intent == "scheduling":
        if context.get("needs_clarification"):
            prompt = f"""
Write a short professional email asking for meeting availability clarification.
Question to ask: {context.get("clarification_question")}
Recipient name: {recipient_name}
Tone: warm and professional. Under 80 words.
Return ONLY the email body, no subject line, no sign-off.
"""
        elif context.get("suggested_meeting"):
            slot = context["suggested_meeting"]
            prompt = f"""
Write a short professional email confirming a meeting has been scheduled.
Meeting date: {slot.get("date")}
Meeting time: {slot.get("start_time")} to {slot.get("end_time")} {slot.get("timezone")}
Recipient name: {recipient_name}
Mention that a Google Calendar invite has been sent to all participants.
Tone: warm and professional. Under 100 words.
Return ONLY the email body, no subject line, no sign-off.
"""
        else:
            prompt = f"""
Write a short professional email saying no common availability was found
and asking participants to suggest new time slots.
Recipient name: {recipient_name}
Tone: warm and professional. Under 80 words.
Return ONLY the email body, no subject line, no sign-off.
"""
    elif intent == "update_request":
        prompt = f"""
Write a professional email providing a project status update.
Topic: {context.get("topic")}
Latest status: {context.get("latest_status")}
Key decisions: {context.get("key_decisions", [])}
Action items: {context.get("action_items", [])}
Recipient name: {recipient_name}
Tone: clear and professional. Under 150 words.
Return ONLY the email body, no subject line, no sign-off.
"""
    else:
        return ""

    body = _call_llm(prompt).strip()
    disclaimer = (
        "\n\n---\n"
        "⚠️ This message was generated by ZeroClick, an AI email assistant. "
        "Actions taken are automated. For urgent matters, contact the team directly."
    )
    return body + disclaimer