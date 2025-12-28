"""
Crawl4AI Microservice for Firecrawl
A standalone scraping service using crawl4ai as the engine.
Picks up scraping jobs from Firecrawl's NuQ queue and returns markdown results.
"""

import os
import asyncio
import logging
import signal
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

import asyncpg
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from scraper import Crawl4AIScraper

# Configure logging
logging.basicConfig(
    level=os.getenv("LOGGING_LEVEL", "INFO").upper(),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("crawl4ai-service")

# Configuration
NUQ_DATABASE_URL = os.getenv("NUQ_DATABASE_URL", "")  # Empty = standalone mode (no queue)
PORT = int(os.getenv("PORT", "3001"))
MAX_CONCURRENT_SCRAPES = int(os.getenv("MAX_CONCURRENT_SCRAPES", "5"))
LOCK_RENEW_INTERVAL = int(os.getenv("LOCK_RENEW_INTERVAL", "10"))  # seconds
JOB_POLL_INTERVAL = float(os.getenv("JOB_POLL_INTERVAL", "0.5"))  # seconds
MAX_BACKOFF_INTERVAL = float(os.getenv("MAX_BACKOFF_INTERVAL", "10.0"))  # seconds

# Use specific engine marker to filter jobs for this service
CRAWL4AI_ENGINE_MARKER = os.getenv("CRAWL4AI_ENGINE_MARKER", "crawl4ai")

# Check if we're running in standalone mode (no database)
STANDALONE_MODE = not NUQ_DATABASE_URL

# Global state
is_shutting_down = False
db_pool: Optional[asyncpg.Pool] = None
scraper: Optional[Crawl4AIScraper] = None
worker_tasks: list[asyncio.Task] = []
active_jobs: dict[str, asyncio.Task] = {}


class HealthResponse(BaseModel):
    status: str
    active_scrapes: int
    max_concurrent_scrapes: int
    database_connected: bool


class ScrapeRequest(BaseModel):
    url: str
    wait_after_load: int = 0
    timeout: int = 30000
    headers: Optional[dict[str, str]] = None
    skip_tls_verification: bool = False
    wait_for_selector: Optional[str] = None
    extract_markdown: bool = True
    load_all_content: bool = False
    load_all_content_timeout: int = 30000


class ScrapeResponse(BaseModel):
    content: str
    markdown: Optional[str] = None
    pageStatusCode: int
    contentType: Optional[str] = None
    pageError: Optional[str] = None


async def get_db_pool() -> asyncpg.Pool:
    """Get or create database connection pool."""
    global db_pool
    if db_pool is None:
        db_pool = await asyncpg.create_pool(
            NUQ_DATABASE_URL,
            min_size=2,
            max_size=MAX_CONCURRENT_SCRAPES + 2,
            command_timeout=60
        )
    return db_pool


async def close_db_pool():
    """Close database connection pool."""
    global db_pool
    if db_pool:
        await db_pool.close()
        db_pool = None


async def get_job_to_process(pool: asyncpg.Pool) -> Optional[dict]:
    """
    Atomically fetch and lock a job from the queue.
    Only picks up jobs marked for crawl4ai engine.
    """
    lock_id = str(uuid.uuid4())

    async with pool.acquire() as conn:
        # Use FOR UPDATE SKIP LOCKED to atomically claim a job
        # Filter for jobs that have engine='crawl4ai' in their data
        row = await conn.fetchrow("""
            UPDATE nuq.queue_scrape
            SET status = 'active',
                lock = $1,
                locked_at = NOW()
            WHERE id = (
                SELECT id FROM nuq.queue_scrape
                WHERE status = 'queued'
                  AND (data->>'engine' = $2 OR data->'scrapeOptions'->>'engine' = $2)
                ORDER BY priority ASC, created_at ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            RETURNING id, data, priority, created_at, owner_id, group_id
        """, lock_id, CRAWL4AI_ENGINE_MARKER)

        if row:
            return {
                "id": str(row["id"]),
                "lock": lock_id,
                "data": row["data"],
                "priority": row["priority"],
                "created_at": row["created_at"],
                "owner_id": str(row["owner_id"]) if row["owner_id"] else None,
                "group_id": str(row["group_id"]) if row["group_id"] else None,
            }
        return None


async def renew_lock(pool: asyncpg.Pool, job_id: str, lock: str) -> bool:
    """Renew the lock on a job to prevent it from being reaped."""
    async with pool.acquire() as conn:
        result = await conn.execute("""
            UPDATE nuq.queue_scrape
            SET locked_at = NOW()
            WHERE id = $1 AND lock = $2 AND status = 'active'
        """, uuid.UUID(job_id), uuid.UUID(lock))
        return result == "UPDATE 1"


async def job_finish(pool: asyncpg.Pool, job_id: str, lock: str, returnvalue: dict) -> bool:
    """Mark a job as completed with its return value."""
    async with pool.acquire() as conn:
        result = await conn.execute("""
            UPDATE nuq.queue_scrape
            SET status = 'completed',
                lock = NULL,
                locked_at = NULL,
                finished_at = NOW(),
                returnvalue = $3
            WHERE id = $1 AND lock = $2 AND status = 'active'
        """, uuid.UUID(job_id), uuid.UUID(lock), returnvalue)

        if result == "UPDATE 1":
            # Send notification for listeners
            await conn.execute(
                "SELECT pg_notify('nuq.queue_scrape', $1)",
                f"{job_id}|completed"
            )
            return True
        return False


async def job_fail(pool: asyncpg.Pool, job_id: str, lock: str, failed_reason: str) -> bool:
    """Mark a job as failed with an error message."""
    async with pool.acquire() as conn:
        result = await conn.execute("""
            UPDATE nuq.queue_scrape
            SET status = 'failed',
                lock = NULL,
                locked_at = NULL,
                finished_at = NOW(),
                failedreason = $3
            WHERE id = $1 AND lock = $2 AND status = 'active'
        """, uuid.UUID(job_id), uuid.UUID(lock), failed_reason)

        if result == "UPDATE 1":
            # Send notification for listeners
            await conn.execute(
                "SELECT pg_notify('nuq.queue_scrape', $1)",
                f"{job_id}|failed"
            )
            return True
        return False


async def process_job(job: dict) -> dict:
    """Process a single scrape job using crawl4ai."""
    global scraper

    job_data = job["data"]
    url = job_data.get("url", "")
    scrape_options = job_data.get("scrapeOptions", {})

    # Extract options
    timeout = scrape_options.get("timeout", 30000)
    wait_for = scrape_options.get("waitFor", 0)
    headers = scrape_options.get("headers", {})
    skip_tls = scrape_options.get("skipTlsVerification", False)
    wait_for_selector = scrape_options.get("waitForSelector")

    logger.info(f"Processing job {job['id']} for URL: {url}")

    try:
        result = await scraper.scrape(
            url=url,
            timeout=timeout,
            wait_after_load=wait_for,
            headers=headers,
            skip_tls_verification=skip_tls,
            wait_for_selector=wait_for_selector
        )

        return {
            "success": True,
            "data": {
                "url": url,
                "html": result.get("html", ""),
                "markdown": result.get("markdown", ""),
                "statusCode": result.get("status_code", 200),
                "contentType": result.get("content_type", "text/html"),
            },
            "scrape_id": job["id"],
        }
    except Exception as e:
        logger.error(f"Error processing job {job['id']}: {e}")
        return {
            "success": False,
            "error": str(e),
            "scrape_id": job["id"],
        }


async def lock_renewal_task(pool: asyncpg.Pool, job_id: str, lock: str):
    """Periodically renew the lock on a job."""
    while not is_shutting_down:
        await asyncio.sleep(LOCK_RENEW_INTERVAL)
        if not await renew_lock(pool, job_id, lock):
            logger.warning(f"Failed to renew lock for job {job_id}")
            break


async def worker_loop(worker_id: int):
    """Main worker loop that polls for and processes jobs."""
    global is_shutting_down, scraper

    pool = await get_db_pool()
    backoff = JOB_POLL_INTERVAL

    logger.info(f"Worker {worker_id} started")

    while not is_shutting_down:
        try:
            job = await get_job_to_process(pool)

            if job is None:
                # No job available, backoff
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.5, MAX_BACKOFF_INTERVAL)
                continue

            # Reset backoff on successful job fetch
            backoff = JOB_POLL_INTERVAL

            logger.info(f"Worker {worker_id} picked up job {job['id']}")

            # Start lock renewal task
            renewal_task = asyncio.create_task(
                lock_renewal_task(pool, job["id"], job["lock"])
            )
            active_jobs[job["id"]] = renewal_task

            try:
                # Process the job
                result = await process_job(job)

                if result.get("success"):
                    await job_finish(pool, job["id"], job["lock"], result)
                    logger.info(f"Worker {worker_id} completed job {job['id']}")
                else:
                    await job_fail(pool, job["id"], job["lock"], result.get("error", "Unknown error"))
                    logger.warning(f"Worker {worker_id} failed job {job['id']}: {result.get('error')}")

            finally:
                # Cancel lock renewal
                renewal_task.cancel()
                try:
                    await renewal_task
                except asyncio.CancelledError:
                    pass
                active_jobs.pop(job["id"], None)

        except Exception as e:
            logger.error(f"Worker {worker_id} error: {e}")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_INTERVAL)

    logger.info(f"Worker {worker_id} stopped")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    global scraper, worker_tasks, is_shutting_down

    logger.info("Starting Crawl4AI service...")

    if STANDALONE_MODE:
        logger.info("Running in STANDALONE mode (no database/queue workers)")
    else:
        logger.info(f"Running in QUEUE mode, connecting to: {NUQ_DATABASE_URL[:50]}...")

    # Initialize scraper
    scraper = Crawl4AIScraper(max_concurrent=MAX_CONCURRENT_SCRAPES)
    await scraper.initialize()

    # Only initialize database and workers if not in standalone mode
    if not STANDALONE_MODE:
        # Initialize database pool
        await get_db_pool()

        # Start worker tasks
        for i in range(MAX_CONCURRENT_SCRAPES):
            task = asyncio.create_task(worker_loop(i))
            worker_tasks.append(task)

        logger.info(f"Started {MAX_CONCURRENT_SCRAPES} worker(s)")
    else:
        logger.info("Scraper ready - use /scrape endpoint for direct scraping")

    yield

    # Shutdown
    logger.info("Shutting down Crawl4AI service...")
    is_shutting_down = True

    # Wait for workers to finish
    for task in worker_tasks:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # Close resources
    await scraper.close()
    if not STANDALONE_MODE:
        await close_db_pool()

    logger.info("Crawl4AI service stopped")


app = FastAPI(
    title="Crawl4AI Service",
    description="Standalone scraping microservice using crawl4ai for Firecrawl",
    version="1.0.0",
    lifespan=lifespan
)


@app.get("/health", response_model=HealthResponse)
async def health_check():
    """Health check endpoint."""
    db_connected = False

    if STANDALONE_MODE:
        # In standalone mode, we don't need database
        db_connected = True  # Not applicable
    else:
        try:
            pool = await get_db_pool()
            async with pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            db_connected = True
        except Exception as e:
            logger.error(f"Database health check failed: {e}")

    return HealthResponse(
        status="healthy" if (db_connected or STANDALONE_MODE) and scraper else "unhealthy",
        active_scrapes=len(active_jobs),
        max_concurrent_scrapes=MAX_CONCURRENT_SCRAPES,
        database_connected=db_connected
    )


@app.post("/scrape", response_model=ScrapeResponse)
async def scrape_url(request: ScrapeRequest):
    """
    Direct scrape endpoint (similar to playwright-service).
    This allows the service to be called directly without using the queue.
    """
    global scraper

    if not scraper:
        raise HTTPException(status_code=503, detail="Scraper not initialized")

    try:
        result = await scraper.scrape(
            url=request.url,
            timeout=request.timeout,
            wait_after_load=request.wait_after_load,
            headers=request.headers,
            skip_tls_verification=request.skip_tls_verification,
            wait_for_selector=request.wait_for_selector,
            load_all_content=request.load_all_content,
            load_all_content_timeout=request.load_all_content_timeout
        )

        page_error = None
        if result.get("status_code", 200) >= 400:
            page_error = f"HTTP {result.get('status_code')}"

        return ScrapeResponse(
            content=result.get("html", ""),
            markdown=result.get("markdown") if request.extract_markdown else None,
            pageStatusCode=result.get("status_code", 200),
            contentType=result.get("content_type", "text/html"),
            pageError=page_error
        )

    except Exception as e:
        logger.error(f"Scrape error for {request.url}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/metrics")
async def metrics():
    """Basic metrics endpoint."""
    return {
        "active_jobs": len(active_jobs),
        "max_concurrent_scrapes": MAX_CONCURRENT_SCRAPES,
        "workers": len(worker_tasks),
        "is_shutting_down": is_shutting_down,
    }


def handle_shutdown(signum, frame):
    """Handle shutdown signals."""
    global is_shutting_down
    logger.info(f"Received signal {signum}, initiating shutdown...")
    is_shutting_down = True


if __name__ == "__main__":
    import uvicorn

    # Register signal handlers
    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    # Map 'warn' to 'warning' for uvicorn compatibility
    log_level = os.getenv("LOGGING_LEVEL", "info").lower()
    if log_level == "warn":
        log_level = "warning"

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=PORT,
        log_level=log_level
    )
