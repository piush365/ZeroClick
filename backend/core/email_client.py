import imaplib
import smtplib
import email
import os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import decode_header
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "../.env"))

ASSISTANT_EMAIL = os.getenv("ASSISTANT_EMAIL")
EMAIL_PASSWORD  = os.getenv("EMAIL_PASSWORD")
IMAP_SERVER     = os.getenv("IMAP_SERVER", "imap.gmail.com")
IMAP_PORT       = int(os.getenv("IMAP_PORT", 993))
SMTP_SERVER     = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT       = int(os.getenv("SMTP_PORT", 587))


@dataclass
class EmailMessage:
    uid: str
    subject: str
    sender: str        # full email address
    sender_name: str   # just the display name
    body: str
    thread_id: str     # Message-ID header, used for threading
    date: str


def _decode_header_value(value: str) -> str:
    """Decode encoded email headers (handles UTF-8, base64, etc)."""
    if not value:
        return ""
    parts = decode_header(value)
    decoded = ""
    for part, encoding in parts:
        if isinstance(part, bytes):
            decoded += part.decode(encoding or "utf-8", errors="replace")
        else:
            decoded += part
    return decoded


def _extract_body(msg) -> str:
    """
    Pull plain text body out of an email message object.
    Handles both simple and multipart emails.
    """
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition  = str(part.get("Content-Disposition", ""))
            if content_type == "text/plain" and "attachment" not in disposition:
                payload = part.get_payload(decode=True)
                return payload.decode("utf-8", errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            return payload.decode("utf-8", errors="replace")
    return ""


def fetch_unread_emails() -> list[EmailMessage]:
    """
    Connect to Gmail via IMAP and fetch all unread emails.
    Returns a list of EmailMessage objects.
    Marks them as read after fetching.
    """
    messages = []

    try:
        # Connect and login
        mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
        mail.login(ASSISTANT_EMAIL, EMAIL_PASSWORD)
        mail.select("INBOX")

        # Search for unread emails
        status, data = mail.search(None, "UNSEEN", "NOT", "FROM", "noreply", "NOT", "FROM", "no-reply", "NOT", "FROM", "notification")
        if status != "OK" or not data[0]:
            mail.logout()
            return []

        uids = data[0].split()
        print(f"[Email] Found {len(uids)} unread email(s)")

        for uid in uids:
            # Fetch the full email
            status, msg_data = mail.fetch(uid, "(RFC822)")
            if status != "OK":
                continue

            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)

            # Parse fields
            subject     = _decode_header_value(msg.get("Subject", ""))
            from_header = _decode_header_value(msg.get("From", ""))
            thread_id   = msg.get("Message-ID", uid.decode())
            date        = msg.get("Date", "")
            body        = _extract_body(msg)

            # Split "Name <email>" into name and email
            if "<" in from_header:
                sender_name  = from_header.split("<")[0].strip().strip('"')
                sender_email = from_header.split("<")[1].strip(">").strip()
            else:
                sender_name  = from_header
                sender_email = from_header
            
            # Skip newsletters, notifications, and automated emails
            SKIP_SENDERS = [
                "noreply", "no-reply", "notification", "newsletter",
                "mailer-daemon", "postmaster", "donotreply",
                "googleplay", "accounts.google", "workspace-noreply",
                "spotify", "wattpad", "youtube", "instagram",
                "openai", "supercell", "cutout.pro"
            ]
            if any(skip in sender_email.lower() for skip in SKIP_SENDERS):
                print(f"[Email] Skipping automated email from {sender_email}")
                continue

            messages.append(EmailMessage(
                uid         = uid.decode(),
                subject     = subject,
                sender      = sender_email,
                sender_name = sender_name,
                body        = body,
                thread_id   = thread_id,
                date        = date,
            ))

        mail.logout()

    except Exception as e:
        print(f"[Email] IMAP error: {e}")

    return messages


def send_email(to: list[str], subject: str, body: str) -> bool:
    """
    Send an email via SMTP.
    Returns True if sent successfully, False otherwise.
    """
    try:
        msg = MIMEMultipart()
        msg["From"]    = ASSISTANT_EMAIL
        msg["To"]      = ", ".join(to)
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.login(ASSISTANT_EMAIL, EMAIL_PASSWORD)
            server.sendmail(ASSISTANT_EMAIL, to, msg.as_string())

        print(f"[Email] Sent to {to}")
        return True

    except Exception as e:
        print(f"[Email] SMTP error: {e}")
        return False