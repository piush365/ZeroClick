"""
agents/orchestrator.py
----------------------
The main loop of ZeroClick.
Polls inbox → detects intent → routes to correct handler → takes action.
Keeps an in-memory store of everything it has processed.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import date
from dataclasses import dataclass, asdict
from typing import Optional

from core.llm_agent import (
    detect_intent,
    extract_availability,
    summarize_thread,
    generate_brd,
    compose_reply,
)
from core.email_client import fetch_unread_emails, send_email
from core.calendar_client import create_meeting, check_for_duplicate


# ── In-memory store ───────────────────────────────────────────────────────────
# Keeps track of every email the agent has seen and what it did with it.
# In a production app this would be a database — for now a list is fine.

@dataclass
class ProcessedEmail:
    uid: str
    subject: str
    sender: str
    sender_name: str
    intent: str
    summary: str
    status: str          # processed | scheduled | clarification_sent | no_overlap | skipped | error
    meeting: Optional[dict] = None
    brd: Optional[dict] = None
    thread_summary: Optional[dict] = None
    timestamp: str = ""


# Global state — persists as long as the server is running
inbox_store: list[ProcessedEmail] = []
processed_uids: set = set()


# ── Scheduling handler ────────────────────────────────────────────────────────

def _handle_scheduling(email_msg) -> ProcessedEmail:
    """
    Full scheduling pipeline:
    1. Extract availability from the email body
    2. Check for duplicates
    3. Create calendar event if overlap found
    4. Send confirmation or clarification email
    """
    today = date.today().isoformat()
    availability = extract_availability(email_msg.body, reference_date=today)

    result = ProcessedEmail(
        uid         = email_msg.uid,
        subject     = email_msg.subject,
        sender      = email_msg.sender,
        sender_name = email_msg.sender_name,
        intent      = "scheduling",
        summary     = f"Scheduling request from {email_msg.sender_name}",
        status      = "processing",
        timestamp   = email_msg.date,
    )

    if availability.needs_clarification:
        # Agent doesn't have enough info — ask for clarification
        reply = compose_reply(
            intent="scheduling",
            context={
                "needs_clarification": True,
                "clarification_question": availability.clarification_question,
            },
            recipient_name=email_msg.sender_name,
        )
        send_email(
            to=[email_msg.sender],
            subject=f"Re: {email_msg.subject}",
            body=reply,
        )
        result.status = "clarification_sent"

    elif availability.suggested_meeting:
        slot = availability.suggested_meeting

        # Check calendar for duplicate before creating
        is_duplicate = check_for_duplicate(
            attendees=[email_msg.sender],
            date=slot.date,
            start_time=slot.start_time,
        )

        if is_duplicate:
            result.status = "duplicate_skipped"
            print(f"[Orchestrator] Duplicate meeting skipped for {email_msg.subject}")

        else:
            # Create the calendar event
            # Clean attendees — keep only real email addresses
            # Always include the sender, filter out fake/example domains
            raw_attendees = availability.participants
            valid_attendees = [
                a for a in raw_attendees
                if "@" in a
                and not any(fake in a for fake in ["example.com", "test.com", "fake.com"])
            ]
            # Always make sure the sender is included
            if email_msg.sender not in valid_attendees:
                valid_attendees.append(email_msg.sender)
            # And the agent itself
            agent_email = os.getenv("ASSISTANT_EMAIL")
            if agent_email and agent_email not in valid_attendees:
                valid_attendees.append(agent_email)

            event = create_meeting(
                title=f"Meeting: {email_msg.subject}",
                date=slot.date,
                start_time=slot.start_time,
                end_time=slot.end_time,
                attendees=valid_attendees,
                timezone=slot.timezone,
            )

            if event:
                result.meeting = {
                    "event_id"     : event.event_id,
                    "title"        : event.title,
                    "start"        : event.start,
                    "end"          : event.end,
                    "meet_link"    : event.meet_link,
                    "calendar_link": event.calendar_link,
                    "attendees"    : event.attendees,
                }
                result.status = "scheduled"

                # Send confirmation email
                reply = compose_reply(
                    intent="scheduling",
                    context={"suggested_meeting": {
                        "date"      : slot.date,
                        "start_time": slot.start_time,
                        "end_time"  : slot.end_time,
                        "timezone"  : slot.timezone,
                    }},
                    recipient_name=email_msg.sender_name,
                )
                send_email(
                    to=valid_attendees,
                    subject=f"Meeting Confirmed: {email_msg.subject}",
                    body=reply,
                )
            else:
                result.status = "error"

    else:
        # No overlap found
        reply = compose_reply(
            intent="scheduling",
            context={},
            recipient_name=email_msg.sender_name,
        )
        send_email(
            to=[email_msg.sender],
            subject=f"Re: {email_msg.subject}",
            body=reply,
        )
        result.status = "no_overlap"

    return result


# ── Update request handler ────────────────────────────────────────────────────

def _handle_update_request(email_msg) -> ProcessedEmail:
    """
    Thread intelligence pipeline:
    1. Summarize the email thread
    2. Compose and send a status update reply
    """
    summary = summarize_thread(email_msg.body)

    result = ProcessedEmail(
        uid            = email_msg.uid,
        subject        = email_msg.subject,
        sender         = email_msg.sender,
        sender_name    = email_msg.sender_name,
        intent         = "update_request",
        summary        = summary.latest_status,
        status         = "processing",
        thread_summary = {
            "topic"         : summary.topic,
            "latest_status" : summary.latest_status,
            "key_decisions" : summary.key_decisions,
            "action_items"  : summary.action_items,
            "participants"  : summary.participants,
        },
        timestamp = email_msg.date,
    )

    reply = compose_reply(
        intent="update_request",
        context=result.thread_summary,
        recipient_name=email_msg.sender_name,
    )
    send_email(
        to=[email_msg.sender],
        subject=f"Re: {email_msg.subject}",
        body=reply,
    )
    result.status = "processed"
    return result


# ── BRD generator (called directly from API) ──────────────────────────────────

def generate_brd_from_thread(thread_text: str) -> dict:
    """
    Standalone BRD generation — triggered manually from the frontend.
    Not part of the automatic polling loop.
    """
    brd = generate_brd(thread_text)
    return {
        "project_name"               : brd.project_name,
        "stakeholders"               : brd.stakeholders,
        "functional_requirements"    : brd.functional_requirements,
        "non_functional_requirements": brd.non_functional_requirements,
        "decisions_made"             : brd.decisions_made,
        "open_questions"             : brd.open_questions,
        "timelines"                  : brd.timelines,
        "feature_priority"           : brd.feature_priority,
    }


# ── Main polling function ─────────────────────────────────────────────────────

def run_once() -> list[dict]:
    """
    Fetch unread emails and process each one.
    Called by the background poller every 30 seconds.
    Returns list of newly processed emails.
    """
    emails = fetch_unread_emails()
    new_results = []

    for email_msg in emails:
        # Skip if already processed in this session
        if email_msg.uid in processed_uids:
            continue

        print(f"[Orchestrator] Processing: '{email_msg.subject}' from {email_msg.sender}")

        try:
            # Detect what the sender wants
            intent_result = detect_intent(email_msg.subject, email_msg.body)
            print(f"[Orchestrator] Intent: {intent_result.intent} (confidence: {intent_result.confidence:.2f})")

            # Skip low-confidence classifications but still log them
            if intent_result.confidence < 0.6:
                print(f"[Orchestrator] Low confidence ({intent_result.confidence:.2f}) — skipping")
                result = ProcessedEmail(
                    uid         = email_msg.uid,
                    subject     = email_msg.subject,
                    sender      = email_msg.sender,
                    sender_name = email_msg.sender_name,
                    intent      = "other",
                    summary     = f"Low confidence: {intent_result.summary}",
                    status      = "skipped",
                    timestamp   = email_msg.date,
                )
                processed_uids.add(email_msg.uid)
                inbox_store.insert(0, result)
                continue

            # Route to correct handler
            if intent_result.intent == "scheduling":
                result = _handle_scheduling(email_msg)
            elif intent_result.intent == "update_request":
                result = _handle_update_request(email_msg)
            else:
                result = ProcessedEmail(
                    uid         = email_msg.uid,
                    subject     = email_msg.subject,
                    sender      = email_msg.sender,
                    sender_name = email_msg.sender_name,
                    intent      = "other",
                    summary     = intent_result.summary,
                    status      = "skipped",
                    timestamp   = email_msg.date,
                )

            # Mark as processed
            processed_uids.add(email_msg.uid)
            inbox_store.insert(0, result)
            new_results.append(asdict(result))

        except Exception as e:
            # One email failing should not stop the others
            print(f"[Orchestrator] Error processing '{email_msg.subject}': {e}")
            processed_uids.add(email_msg.uid)  # mark as seen so we don't retry forever
            error_result = ProcessedEmail(
                uid         = email_msg.uid,
                subject     = email_msg.subject,
                sender      = email_msg.sender,
                sender_name = email_msg.sender_name,
                intent      = "unknown",
                summary     = f"Processing error: {str(e)}",
                status      = "error",
                timestamp   = email_msg.date,
            )
            inbox_store.insert(0, error_result)  # newest first
            new_results.append(asdict(error_result))

    return new_results


def get_inbox_state() -> list[dict]:
    """Return the full in-memory inbox as a list of dicts for the API."""
    return [asdict(e) for e in inbox_store]