"""Independent durable-queue lanes under one process-owned mutation lease."""
import threading
import time


def run_lanes(cycle, stop, *, once=False, refresh_seconds=3600, clock=time.monotonic):
    """Run ads and cleanup independently; always drain both before returning.

    ``cycle(lane, stopped)`` owns all thread-local clients and returns its idle
    delay. A stop/lease exception is retained by the coordinator; workers see
    a cooperative stop first, so an in-flight request can save its receipt.
    Periodic refresh returns only after both lanes drain, allowing safe
    reconciliation with no live task/reservation owners.
    """
    cancelled = threading.Event()
    mutex = threading.Lock()
    failures = []
    deadline = clock() + refresh_seconds

    def fail(exc):
        with mutex:
            if not failures:
                failures.append(exc)
            elif isinstance(failures[0], InterruptedError) and not isinstance(exc, InterruptedError):
                # A cooperative handoff must never hide a concurrent write or
                # ledger failure reported while the sibling lane drains.
                failures[0] = exc
        cancelled.set()

    def stopped():
        if cancelled.is_set():
            return True
        try:
            should_stop = stop() or clock() >= deadline
        except BaseException as exc:
            fail(exc)
            return True
        if should_stop:
            cancelled.set()
        return cancelled.is_set()

    def run(lane):
        try:
            while not stopped():
                delay = cycle(lane, stopped)
                if once:
                    break
                until = clock() + max(0, delay)
                while not stopped() and clock() < until:
                    cancelled.wait(min(.25, max(0, until - clock())))
        except BaseException as exc:
            fail(exc)

    threads = [threading.Thread(target=run, args=(lane,), name='cover-' + lane)
               for lane in ('ads', 'cleanup')]
    try:
        for thread in threads:
            thread.start()
        while any(thread.is_alive() for thread in threads):
            stopped()
            for thread in threads:
                thread.join(.1)
    finally:
        cancelled.set()
        for thread in threads:
            if thread.ident is not None:
                thread.join()
    if failures:
        raise failures[0]
