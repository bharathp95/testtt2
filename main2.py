#this talks to tech info workspace

import os
import time
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
import requests

# Gemini (Google GenAI) SDK
from google import genai

# -----------------------
#  CONFIG — replace with env vars or keep as-is if you prefer
# -----------------------

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not all([SLACK_BOT_TOKEN, SLACK_APP_TOKEN, GEMINI_API_KEY]):
    raise ValueError("Missing required environment variables")
# -----------------------
#  Initialize Gemini client and Slack app
# -----------------------
client = genai.Client(api_key=GEMINI_API_KEY)
app = App(token=SLACK_BOT_TOKEN)

# Determine bot user id so we can ignore our own messages
try:
    auth_info = app.client.auth_test()
    BOT_USER_ID = auth_info.get("user_id")
except Exception:
    BOT_USER_ID = None

# --- Memory to store user conversation context ---
user_conversations = {}

# --- Helper: Ask Gemini ---
def ask_gemini(messages, model="gemini-2.5-flash", retries=5, backoff=2.0):
    """
    messages: list of dicts {"role": "system"|"user"|"assistant", "content": str}
    Returns the generated text or error string.
    """
    prompt_parts = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if role == "system":
            prompt_parts.append("[SYSTEM]\n" + content + "\n")
        elif role == "assistant":
            prompt_parts.append("[ASSISTANT]\n" + content + "\n")
        else:
            prompt_parts.append("[USER]\n" + content + "\n")

    prompt = "\n".join(prompt_parts).strip()

    attempt = 0
    while True:
        try:
            response = client.models.generate_content(
                model=model,
                contents=prompt
            )

            # Extract text from typical response shapes:
            text = None
            if hasattr(response, "text") and response.text:
                text = response.text
            elif hasattr(response, "output") and response.output:
                first = response.output[0]
                text = getattr(first, "content", None) or getattr(first, "text", None)

            if text is None:
                return "⚠️ Gemini returned no text. Raw response: " + str(response)

            return text

        except Exception as e:
            attempt += 1
            if attempt > retries:
                return f"⚠️ Gemini API error after {attempt} attempts: {e}"
            time.sleep(backoff * attempt)


# --- Detect category from user message ---
def detect_category(text):
    text = text.lower()
    if "lms" in text:
        return "LMS"
    elif "ebs" in text:
        return "EBS"
    elif "salesforce" in text or "sf" in text:
        return "Salesforce"
    return None


# -----------------------
# Shift schedule (IST) — update entries if needed
# Use 24-hour times, start_time inclusive, end_time exclusive. If end <= start -> wraps midnight.
# Note: 'handle' currently holds Slack username handle (not user ID). To make clickable mention,
# set 'slack_id' to the user's Slack ID (e.g., "U12345678") and use f"<@{slack_id}>".
# -----------------------
SHIFT_TEAM = [
    {"name": "Prashanth Gopinath", "handle": "pgopinat", "slack_id": None, "start": dt_time(23, 0), "end": dt_time(5, 0)},
    {"name": "Arunava Das",         "handle": "arudas",    "slack_id": None, "start": dt_time(14, 0), "end": dt_time(23, 0)},
    {"name": "Mohammad Zaiyauddin", "handle": "mzaiyauddin","slack_id": None, "start": dt_time(5, 0),  "end": dt_time(14, 0)},
]

IST = ZoneInfo("Asia/Kolkata")

def now_ist():
    return datetime.now(IST).time()

def is_time_in_range(start: dt_time, end: dt_time, current: dt_time) -> bool:
    """Return True if current is within [start, end) considering wrap-around."""
    if start < end:
        return start <= current < end
    # wrap-around: e.g., 23:00 -> 05:00
    return current >= start or current < end

def find_support_on_shift() -> dict | None:
    """Return the dict of the on-shift support person, or None if none matched."""
    current = now_ist()
    for entry in SHIFT_TEAM:
        if is_time_in_range(entry["start"], entry["end"], current):
            return entry
    return None


# --- Main message handler ---
@app.event("message")
def handle_message(event, say):
    print("EVENT RECEIVED:", event)
    user = event.get("user")
    subtype = event.get("subtype")
    text = event.get("text", "").strip()

    # Ignore system or bot messages or empty text
    if not user or text == "":
        return

    # Ignore messages from the bot itself if detected
    if BOT_USER_ID and user == BOT_USER_ID:
        return

    # If user already has a conversation and it's closed, allow greeting to restart
    if user in user_conversations:
        convo_state = user_conversations[user].get("stage")
        if convo_state == "closed":
            if text.lower().strip() in ["hi", "hello", "hey", "start"]:
                # reset conversation
                user_conversations[user] = {
                    "messages": [
                        {"role": "system", "content": (
                            "You are RedHatDocs Assistant, a helpful Red Hat support agent. "
                            "Your task is to help users troubleshoot LMS, EBS, and Salesforce related issues. "
                            "Always start by confirming which system they are asking about. "
                            "After that, ask for a short description of the issue, "
                            "and then provide concise, accurate, Red Hat-specific answers using internal documentation knowledge. "
                            "Be polite, brief, and clear."
                            "if needed go to redhat.com and get information from there before answering, and show relevant links."
                            "refer public Knowledge base articles and also give links"
                        )}
                    ],
                    "stage": "ask_category",
                    "category": None,
                    "has_issue": False,
                }
                say(f"Hi <@{user}> 👋 Do you have any issues related to *LMS*, *EBS*, or *Salesforce*?")
                return
            else:
                # If closed and not greeting, ignore further messages
                return

    # Initialize new conversation if user is new
    if user not in user_conversations:
        user_conversations[user] = {
            "messages": [
                {"role": "system", "content": (
                    "You are RedHatDocs Assistant, a helpful Red Hat support agent. "
                    "Your task is to help users troubleshoot LMS, EBS, and Salesforce related issues. "
                    "Always start by confirming which system they are asking about. "
                    "After that, ask for a short description of the issue, "
                    "and then provide concise, accurate, Red Hat-specific answers using internal documentation knowledge. "
                    "Be polite, brief, and clear."
                )}
            ],
            "stage": "ask_category",
            "category": None,
            "has_issue": False,
        }

        # Start by asking the first question
        say(f" Disclaimer: You are about to use a Red Hat tool that utilizes AI technology to provide you with relevant information. By proceeding to use the tool, you acknowledge that the tool and any output provided are only intended for internal use and that information should only be shared with those with a legitimate business purpose.  Do not include any personal information or customer-specific information in your input. Responses provided by tools utilizing AI technology should be reviewed and verified prior to use. \n Hi <@{user}> 👋 Do you have any issues related to *LMS*, *EBS*, or *Salesforce*?")
        return

    convo = user_conversations[user]

    # If waiting for resolution confirmation
    if convo.get("stage") == "await_resolution":
        # user should reply yes/no
        lower = text.lower().strip()
        if lower in ["yes", "y", "resolved", "fixed"]:
            say(f"Great — glad it's resolved, <@{user}>! If you need anything else, say 'hi' to start a new request.")
            convo["stage"] = "closed"
            return
        elif lower in ["no", "n", "not yet", "not resolved"]:
            # user wants more help; revert to ask_issue to get more details
            convo["stage"] = "ask_issue"
            say("Okay — please provide more details about the issue so I can help further.")
            return
        else:
            say("Please reply with `yes` or `no` to let me know if this resolved your issue.")
            return

    # If still asking category
    if convo["stage"] == "ask_category":
        category = detect_category(text)
        if category:
            convo["category"] = category
            convo["stage"] = "ask_issue"
            say(f"Got it — {category} issue. 👍 Could you please describe the problem in a few sentences?")
        else:
            # User says it's not LMS/EBS/SF -> ping on-shift support and STOP (do not call LLM)
            on_shift = find_support_on_shift()
            if on_shift:
                name = on_shift["name"]
                handle = on_shift.get("handle")
                slack_id = on_shift.get("slack_id")

                # Prefer clickable mention if you have Slack user ID
                if slack_id:
                    mention = f"<@{slack_id}>"
                else:
                    mention = f"@{handle}" if handle else name

                # Ping support and instruct user; then CLOSE the conversation so bot doesn't continue
                say(f"🔔 Pinging support: {mention} ({name}) — you are currently on shift (IST).")
                say(f"<@{user}>, please contact {mention} directly or provide a short note in that thread. "
                    "I've notified them for you.")
            else:
                # No one found on shift — polite redirect and CLOSE
                say(
                    "This channel is primarily for *LMS*, *EBS*, or *Salesforce* queries. "
                    "I couldn't identify a scheduled support person for the current IST time. "
                    "Please contact the appropriate support channel or provide details and I'll try to route it."
                )

            # Mark conversation closed so we DON'T move to ask_issue / call the LLM
            convo["stage"] = "closed"
        return

    # If waiting for user issue description
    if convo["stage"] == "ask_issue":
        convo["messages"].append({
            "role": "user",
            "content": f"Issue related to {convo.get('category') or 'unspecified'}: {text}"
        })
        convo["stage"] = "answer"

        # FIRST: find who is on support shift in IST and ping them (always, per your request)
        on_shift = find_support_on_shift()
        if on_shift:
            handle = on_shift.get("handle")
            name = on_shift.get("name")
            slack_id = on_shift.get("slack_id")
            if slack_id:
                mention = f"<@{slack_id}>"
            else:
                mention = f"@{handle}" if handle else name
            # Notify the on-shift person
            say(f"🔔 Pinging support: {mention} ({name}) — you are currently on shift (IST).")
        else:
            say("🔔 No scheduled support person found for current IST time. Proceeding to fetch an answer.")

        say(f"Thanks <@{user}>! Let me check Red Hat docs for you 🔍")

        # Build a targeted query for the assistant to answer concisely
        query = (
            f"The user has a {convo.get('category') or 'unspecified'} issue. "
            f"Provide a concise, Red Hat-specific troubleshooting answer for this problem:\n\n{text}"
        )

        # Append assistant prompt (we're asking the model to produce the answer)
        convo["messages"].append({"role": "assistant", "content": query})
        reply = ask_gemini(convo["messages"])
        # After pinging support, present the LLM answer
        say(reply)
        convo["messages"].append({"role": "assistant", "content": reply})
        convo["has_issue"] = True

        # Now ask for resolution confirmation and wait
        convo["stage"] = "await_resolution"
        say("Did this resolve your issue? Please reply with `yes` or `no`.")
        return

    # If already answered once → continue normal conversation (user provided follow-up after resolved=no)
    if convo["has_issue"]:
        convo["messages"].append({"role": "user", "content": text})
        reply = ask_gemini(convo["messages"])
        say(reply)
        convo["messages"].append({"role": "assistant", "content": reply})
        # After follow-up reply, again ask resolution
        convo["stage"] = "await_resolution"
        say("Did this resolve your issue? Please reply with `yes` or `no`.")


# --- Run bot ---
if __name__ == "__main__":
    # Quick safety check for tokens (only warn)
    if not SLACK_BOT_TOKEN or not SLACK_APP_TOKEN or not GEMINI_API_KEY:
        print("Warning: One or more tokens are empty. Set SLACK_BOT_TOKEN, SLACK_APP_TOKEN, GEMINI_API_KEY (env or script).")
    print("Starting Slack Gemini bot with shift ping...")
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    handler.start()
