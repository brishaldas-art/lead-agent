import os
from dotenv import load_dotenv
from google import genai

load_dotenv()
api_key = os.getenv("GEMINI_API_KEY", "")
if not api_key or api_key.startswith("paste_"):
    raise SystemExit("GEMINI_API_KEY is missing in .env")

client = genai.Client(api_key=api_key)
chain = (os.getenv("MODEL_FAST", "") + "," + os.getenv("MODEL_SMART", "")).split(",")
available = {m.name.removeprefix("models/") for m in client.models.list()}

for name in dict.fromkeys(c.strip() for c in chain if c.strip()):
    if name not in available:
        print(f"MISSING  {name}")
        continue
    try:
        client.models.generate_content(model=name, contents="ok")
        print(f"WORKS    {name}")
    except Exception as e:
        print(f"FAILS    {name}: {str(e)[:90]}")
