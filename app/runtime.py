"""One supervised polling worker and scheduler, with a PostgreSQL session lock."""
import asyncio
from contextlib import asynccontextmanager, suppress
import hashlib
import logging
import signal

from sqlalchemy import text


logger = logging.getLogger(__name__)


def worker_lock_key(bot_id):
    digest = hashlib.sha256(f"ano_bot:polling:{bot_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


@asynccontextmanager
async def singleton_worker(engine, bot_id):
    # Keep a dedicated session checked out; transaction-pooled proxies must not
    # be used for this connection. A new deploy waits for the old one to stop.
    connection = await engine.connect()
    try:
        waiting_logged = False
        while True:
            row = (await connection.execute(text(
                "SELECT pg_try_advisory_lock(:key), pg_backend_pid()"
            ), {"key": worker_lock_key(bot_id)})).one()
            await connection.commit()
            if row[0]:
                yield connection, row[1]
                break
            if not waiting_logged:
                logger.info("Another worker owns this bot; waiting for its database lock.")
                waiting_logged = True
            await asyncio.sleep(1)
    finally:
        # Discard the physical session, releasing any session advisory lock.
        # Never return a still-locked connection to the application pool.
        try:
            await connection.invalidate()
        finally:
            await connection.close()


async def check_worker_lock(connection, backend_pid):
    try:
        current_pid = await asyncio.wait_for(
            connection.scalar(text("SELECT pg_backend_pid()")), timeout=5,
        )
        await connection.commit()
        if current_pid != backend_pid:
            raise RuntimeError
    except asyncio.CancelledError:
        raise
    except Exception:
        raise RuntimeError("Database singleton session lost; stopping worker.") from None


async def watch_worker_lock(connection, backend_pid):
    while True:
        await check_worker_lock(connection, backend_pid)
        await asyncio.sleep(5)


async def stop_polling(dispatcher, polling):
    if not polling.done():
        try:
            await asyncio.wait_for(dispatcher.stop_polling(), timeout=10)
        except (RuntimeError, TimeoutError):
            polling.cancel()
    await asyncio.gather(polling, return_exceptions=True)
    # aiogram 3.31 tracks concurrent update handlers separately from polling.
    # Let their current database work finish before closing the pool.
    active = tuple(getattr(dispatcher, "_handle_update_tasks", ()))
    if active:
        _, pending = await asyncio.wait(active, timeout=5)
        for task in pending:
            task.cancel()
        await asyncio.gather(*active, return_exceptions=True)


async def exclusive_worker(bot, dispatcher, engine, initialize, scheduler):
    async with singleton_worker(engine, bot.id) as (connection, backend_pid):
        await initialize()
        await check_worker_lock(connection, backend_pid)
        # Preserve queued Telegram updates if this token previously used webhooks.
        await bot.delete_webhook(drop_pending_updates=False)
        polling = asyncio.create_task(dispatcher.start_polling(
            bot, handle_signals=False, close_bot_session=False,
        ), name="telegram-polling")
        jobs = [
            asyncio.create_task(scheduler(bot), name="reminder-scheduler"),
            asyncio.create_task(watch_worker_lock(connection, backend_pid), name="worker-lock-watchdog"),
        ]
        logger.info("Bot worker ready: one poller and one reminder scheduler.")
        try:
            done, _ = await asyncio.wait([polling, *jobs], return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            raise RuntimeError("A required worker task exited unexpectedly.")
        finally:
            for task in jobs:
                task.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
            await stop_polling(dispatcher, polling)


async def run_bot(bot, dispatcher, engine, initialize, scheduler, stop_event=None):
    stop_event = stop_event or asyncio.Event()
    loop = asyncio.get_running_loop()
    installed = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
            installed.append(sig)
        except (NotImplementedError, RuntimeError):
            # Windows console interruption still cancels asyncio.run normally.
            pass
    worker = asyncio.create_task(exclusive_worker(bot, dispatcher, engine, initialize, scheduler))
    stopping = asyncio.create_task(stop_event.wait())
    try:
        done, _ = await asyncio.wait([worker, stopping], return_when=asyncio.FIRST_COMPLETED)
        if worker in done:
            await worker
        else:
            logger.info("Shutdown requested; stopping polling and scheduler.")
    finally:
        stopping.cancel()
        worker.cancel()
        try:
            await asyncio.wait_for(asyncio.gather(worker, stopping, return_exceptions=True), timeout=25)
        except TimeoutError:
            logger.warning("Worker cleanup exceeded the shutdown deadline.")
        finally:
            try:
                await bot.session.close()
            finally:
                await engine.dispose()
                for sig in installed:
                    with suppress(NotImplementedError):
                        loop.remove_signal_handler(sig)
