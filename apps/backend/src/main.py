from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from src.routers.counter import router as counter_router

app = FastAPI(
    title="TrueAlpha API",
    description="Backtesting Engine & Bounded Context Platform API",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(counter_router, prefix="/api")

@app.get("/health")
async def health_check():
    return {"status": "healthy", "service": "truealpha-backend"}
