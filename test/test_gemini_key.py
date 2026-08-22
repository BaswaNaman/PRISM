"""
Standalone Gemini API key test.
Run this directly: python test_gemini_key.py
This is completely separate from the main app - it just checks whether
your key works at all, and shows exactly what's in your .env file
(with most of the key hidden for safety).
"""
import os
from dotenv import load_dotenv

load_dotenv()

key = os.environ.get("GEMINI_API_KEY")

print("=" * 60)
if not key:
    print("❌ No GEMINI_API_KEY found in environment at all.")
    print("   This means the .env file isn't being read.")
    print("   Check: is the file named exactly '.env' (not .env.txt)?")
    print("   Check: is it in the SAME folder as this script?")
else:
    print(f"Key found. Length: {len(key)} characters")
    print(f"Key repr (shows hidden whitespace/quotes): {repr(key)}")
    print(f"First 6 chars: {key[:6]}")
    print(f"Last 4 chars: {key[-4:]}")
print("=" * 60)

if key:
    try:
        from google import genai
        client = genai.Client(api_key=key.strip())
        response = client.models.generate_content(
            model="gemini-3.5-flash",
            contents="Say the word 'success' and nothing else."
        )
        print("✅ API CALL SUCCEEDED. Response:", response.text)
    except Exception as e:
        print("❌ API CALL FAILED.")
        print("Full error:", e)
