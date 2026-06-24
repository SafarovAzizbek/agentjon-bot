import os
from dotenv import load_dotenv
load_dotenv()

from google import genai
from google.genai import types

def _serialize_history(history) -> list[dict]:
    if not history:
        return []
    res = []
    for turn in history:
        role = turn.role
        parts = []
        for part in turn.parts:
            if part.text:
                parts.append({"text": part.text})
            else:
                parts.append({"text": "[Media omitted]"})
        res.append({"role": role, "parts": parts})
    return res

def _deserialize_history(history_data: list[dict]) -> list[types.Content]:
    res = []
    for turn in history_data:
        parts = []
        for p in turn.get("parts", []):
            parts.append(types.Part.from_text(text=p.get("text", "")))
        res.append(types.Content(role=turn.get("role", "user"), parts=parts))
    return res

client = genai.Client()
session = client.chats.create(model="gemini-3.5-flash")
session.send_message("Hello!")

data = _serialize_history(session.history)
print("Serialized:", data)

restored = _deserialize_history(data)
print("Deserialized:", restored)

# test creating a session with history
session2 = client.chats.create(model="gemini-3.5-flash", history=restored)
print("Session 2 started successfully with history length:", len(session2.history))
