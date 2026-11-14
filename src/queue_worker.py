"""Write path for memories.

mem0 calls are synchronous and slow (embedding + several LLM calls against a
CPU-only Ollama). Running them on the asyncio event loop froze the whole
server — /health, /mcp, the UI — for the duration of every add, so concurrent
agents piled up, timed out and the liveness probe killed the pod.

All writes therefore go through:
  * a single dedicated writer thread (Ollama can't parallelise on CPU anyway,
    and it keeps mem0's history DB free of concurrent writers), and
  * a queue — Redis (durable) when REDIS_URL is set, otherwise a bounded
    in-process asyncio.Queue — consumed by one background task.
"""
import asyncio
import functools
import json
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from config import settings

logger = logging.getLogger(__name__)

_QUEUE_KEY = "remnant:queue:add_memory"
_client = None
_worker_task: Optional[asyncio.Task] = None
_local_queue: Optional[asyncio.Queue] = None

_write_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mem-write")


class QueueFull(Exception):
    pass


async def run_write(fn, *args, **kwargs):
    """Run a blocking mem0 write on the single writer thread."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_write_executor, functools.partial(fn, *args, **kwargs))


async def _get_client():
    global _client
    if _client is None:
        import redis.asyncio as aioredis
        _client = aioredis.from_url(settings.redis_url, decode_responses=True)
    return _client


def _get_local_queue() -> asyncio.Queue:
    global _local_queue
    if _local_queue is None:
        _local_queue = asyncio.Queue(maxsize=settings.write_queue_max)
    return _local_queue


async def queue_depth() -> int:
    if settings.redis_url:
        client = await _get_client()
        return await client.llen(_QUEUE_KEY)
    return _get_local_queue().qsize()


async def enqueue(content: str, agent_id: str, infer: bool, category: str, tags: list) -> str:
    job_id = str(uuid.uuid4())
    job = {
        "job_id": job_id,
        "content": content,
        "agent_id": agent_id,
        "infer": infer,
        "category": category,
        "tags": tags,
    }
    if settings.redis_url:
        client = await _get_client()
        await client.rpush(_QUEUE_KEY, json.dumps(job))
    else:
        try:
            _get_local_queue().put_nowait(job)
        except asyncio.QueueFull:
            raise QueueFull(f"write queue full ({settings.write_queue_max} pending)")
    await _ensure_worker()
    return job_id


async def _ensure_worker():
    global _worker_task
    if _worker_task is None or _worker_task.done():
        _worker_task = asyncio.create_task(_run_worker())


async def _next_job() -> Optional[dict]:
    if settings.redis_url:
        client = await _get_client()
        result = await client.blpop(_QUEUE_KEY, timeout=5)
        if result is None:
            return None
        return json.loads(result[1])
    return await _get_local_queue().get()


async def _run_worker():
    import memory as mem_store
    logger.info("add_memory worker started (%s)", "redis" if settings.redis_url else "in-process")
    while True:
        job = None
        try:
            job = await _next_job()
            if job is None:
                continue
            await run_write(
                mem_store.add,
                job["content"],
                agent_id=job.get("agent_id", ""),
                infer=job.get("infer", True),
                category=job.get("category", ""),
                tags=job.get("tags", []),
            )
            logger.info("Processed queue job %s", job.get("job_id", "?"))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Queue job %s failed (dropped): %.120r",
                (job or {}).get("job_id", "?"), (job or {}).get("content", ""),
            )
            await asyncio.sleep(1)
