"""Start the Washroom Bhoot server:  python run.py"""

import os

import uvicorn
from dotenv import load_dotenv

if __name__ == "__main__":
    load_dotenv()
    uvicorn.run(
        "app.main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        # Behind Render's proxy, trust X-Forwarded-* so the app knows it's served over https.
        forwarded_allow_ips="*" if os.getenv("RENDER") else None,
    )
