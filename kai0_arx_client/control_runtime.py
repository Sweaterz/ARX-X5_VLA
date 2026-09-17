"""Cancelable upper-layer I/O. No robot commands in these worker threads."""
import threading
import time


class IOJob:
    def __init__(self, fn, dispose=None):
        self.done = threading.Event()
        self.lock = threading.Lock()
        self.abandoned = False
        self.result = None
        self.error = None
        self.dispose = dispose
        def run():
            try:
                self.result = fn()
            except BaseException as exc:
                self.error = exc
            with self.lock:
                if self.abandoned:
                    self._dispose()
                self.done.set()
        threading.Thread(target=run, daemon=True).start()

    def _dispose(self):
        if self.dispose:
            try:
                self.dispose(self.result)
            except Exception:
                pass
            self.dispose = None

    def abandon(self):
        # Cleanup may itself block; never execute it on the control loop.
        def clean():
            with self.lock:
                self.abandoned = True
                if self.done.is_set():
                    self._dispose()
        threading.Thread(target=clean, daemon=True).start()


class IOLanes:
    def __init__(self):
        self.jobs = {}

    def call(self, lane, fn, tick, timeout, dispose=None):
        old = self.jobs.get(lane)
        if old and not old.done.is_set():
            raise RuntimeError(f'{lane}: 上一次 I/O 尚未结束，拒绝叠加任务')
        tick()
        job = IOJob(fn, dispose)
        self.jobs[lane] = job
        deadline = time.monotonic() + timeout
        try:
            while True:
                tick()  # also checks after I/O completion, before accepting its result
                if time.monotonic() >= deadline:
                    raise TimeoutError(f'{lane}: I/O 超时；结果作废')
                if job.done.wait(.02):
                    tick()
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f'{lane}: I/O 超时；结果作废')
                    if job.error:
                        raise job.error
                    return job.result
        except BaseException:
            job.abandon()
            raise

    def pending(self, lane):
        job = self.jobs.get(lane)
        return bool(job and not job.done.is_set())


def background_close(resource):
    if resource is not None:
        def close():
            try: resource.close()
            except Exception: pass
        threading.Thread(target=close, daemon=True).start()
