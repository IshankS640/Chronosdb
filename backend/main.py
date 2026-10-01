from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import db
from routers import member1_concurrency, member2_healing, member3_benchmark


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_pool()
    yield
    await db.close_pool()


app = FastAPI(title="ChronosDB API", version="1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

app.include_router(member1_concurrency.router)
app.include_router(member2_healing.router)
app.include_router(member3_benchmark.router)


@app.get("/api/health")
async def health():
    async with db.get_pool().acquire() as conn:
        rows, tele = await db.xray_fetch(conn, "SELECT version() AS version, now() AS server_time", explain=False)
    return db.envelope(rows[0], tele)
