import sys
import os
import uvicorn

if __name__ == "__main__":
    print("=" * 70)
    print("🚀 PRODUCT INTELLIGENCE HACKATHON APP STARTING...")
    print("   Open your browser at: http://127.0.0.1:8000")
    print("=" * 70)
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)
