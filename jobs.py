"""
One background beets operation at a time, streamed to the browser.

Import, cover art and convert all drive beets against the same
process-global `config` and `Library` singletons (see
`importsession.get_library`), and beets' own command code was never
written to survive two threads mutating those at once — the same
constraint `libops._console_lock` exists for, one level down. So the
registry here is deliberately global and single-flight: the invariant is
"one beets job at a time", not "one import at a time", which is why
import doesn't get a registry of its own.

A job owns a thread, an event queue drained by an SSE endpoint, and an
abort flag. Abort is cooperative: the worker checks `job.aborted` between
units of work. Only import can also be blocked mid-unit waiting on a
human, so only `ImportJob` overrides `abort()` to unblock that wait.
"""
import queue
import threading
import uuid


class Job:
    """A background operation with an SSE event stream."""

    # Bounds memory to a constant regardless of library size when nobody's
    # listening (tab closed, SSE dropped) — status events are progress, not
    # state, so losing the oldest ones is fine. A reconnecting client
    # re-syncs via /jobs/current and replay() rather than the queue's
    # contents, so this holds even for ImportJob's decisions (see
    # importsession.py: the pending decision's source of truth is
    # self._pending, not this queue — replay() never reads from here).
    _MAX_BUFFERED_EVENTS = 200

    def __init__(self, kind, **meta):
        self.id = uuid.uuid4().hex
        self.kind = kind
        self.meta = meta
        self.events = queue.Queue(maxsize=self._MAX_BUFFERED_EVENTS)
        self.done = threading.Event()
        self.aborted = threading.Event()
        self.result = {}      # merged into the final 'done' event
        self.thread = None

    def emit(self, type, **payload):
        """Never blocks the worker thread: if the buffer is full (nobody's
        draining it), drop the oldest event to make room instead of
        waiting for a consumer that may never show up."""
        event = {'type': type, **payload}
        try:
            self.events.put_nowait(event)
        except queue.Full:
            try:
                self.events.get_nowait()
            except queue.Empty:
                pass
            self.events.put_nowait(event)

    def replay(self):
        """Events a reconnecting client must be given immediately.

        Only import has one — a decision already waiting for an answer,
        which the client would otherwise sit out the whole decision
        timeout to see again. Everything else just streams live.
        """
        return []

    def abort(self):
        self.aborted.set()

    def summary(self):
        return {'id': self.id, 'kind': self.kind, 'aborting': self.aborted.is_set(),
                **self.meta}


# ── Registry ──────────────────────────────────────────────────────────────

_jobs = {}
_lock = threading.Lock()


def get(job_id):
    return _jobs.get(job_id)


def current():
    """The running job, if any — lets a reloaded page rejoin its stream."""
    with _lock:
        for job in _jobs.values():
            if not job.done.is_set():
                return job
    return None


def start(job, work):
    """Register `job` and run `work(job)` on a background thread.

    Raises RuntimeError if any job is still running. The 'done' event is
    emitted from a finally block, so a client's stream always terminates
    even when the work raises.
    """
    with _lock:
        for existing in list(_jobs.values()):
            if existing.done.is_set():
                del _jobs[existing.id]
            else:
                raise RuntimeError(
                    f'another job is already running ({existing.kind})')
        _jobs[job.id] = job

    def run():
        try:
            work(job)
        except Exception as e:
            job.emit('error', message=f'{type(e).__name__}: {e}')
        finally:
            job.emit('done', aborted=job.aborted.is_set(), **job.result)
            job.done.set()

    job.thread = threading.Thread(target=run, daemon=True)
    job.thread.start()
    return job
