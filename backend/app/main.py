"""FastAPI 应用工厂与路由装配。"""

from fastapi import FastAPI

from app.routers import health

app = FastAPI(title="LiveReview", version="0.1.1")

app.include_router(health.router)
