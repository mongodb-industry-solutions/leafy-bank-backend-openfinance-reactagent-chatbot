import logging
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Open Finance Chatbot API",
    version="0.1.0",
    description="Multi-agent chatbot for Open Finance consent management and financial advice"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
async def read_root(request: Request):
    return {"message": "Server is running"}
