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

A job owns a thread, a fan-out set of per-listener event queues (one per
connected SSE stream — see subscribe()/unsubscribe()), and an abort flag.
Abort is cooperative: the worker checks `job.aborted` between units of
work. Only import can also be blocked mid-unit waiting on a human, so
only `ImportJob` overrides `abort()` to unblock that wait.
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
        # Set by start() if this job was queued behind one still finishing
        # an abort (#32) — the kind of the job it's waiting on, so the UI
        # can say what's actually happening instead of a silent "Working…".
        self.queued_behind = None
        # One queue per connected SSE stream, not one shared queue — two
        # tabs open on the same job each got a *subset* of events before
        # (both calling .get() on one queue splits the stream between them)
        # rather than each seeing the full thing. See subscribe()/unsubscribe().
        self._listeners = []
        self._listeners_lock = threading.Lock()
        self.done = threading.Event()
        self.aborted = threading.Event()
        self.result = {}      # merged into the final 'done' event
        self.thread = None

    def subscribe(self):
        """Register a new listener queue for this job. Call unsubscribe()
        with the returned queue when the connection ends."""
        q = queue.Queue(maxsize=self._MAX_BUFFERED_EVENTS)
        with self._listeners_lock:
            self._listeners.append(q)
        return q

    def unsubscribe(self, q):
        with self._listeners_lock:
            if q in self._listeners:
                self._listeners.remove(q)

    def emit(self, type, **payload):
        """Fan out to every connected listener. Never blocks the worker
        thread: if a listener's buffer is full (a slow or gone consumer),
        drop that listener's oldest event to make room rather than waiting
        for a consumer that may never show up."""
        event = {'type': type, **payload}
        with self._listeners_lock:
            listeners = list(self._listeners)
        for q in listeners:
            try:
                q.put_nowait(event)
            except queue.Full:
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                q.put_nowait(event)

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
                'queued_behind': self.queued_behind, **self.meta}


# ── Registry ──────────────────────────────────────────────────────────────

_jobs = {}
_lock = threading.Lock()
# Jobs queued behind one still finishing an abort (#32), in run order. Still
# never more than one *running* at a time — that's the mutation hazard the
# registry exists to prevent (#29) — this is only a waiting line for whoever
# goes next, same as the queue a paused printer builds instead of rejecting
# every job sent to it.
_pending = []


def get(job_id):
    return _jobs.get(job_id)


def current():
    """The running job, if any — lets a reloaded page rejoin its stream."""
    with _lock:
        for job in _jobs.values():
            if not job.done.is_set():
                return job
    return None


def queued():
    """Jobs waiting to start once `current()` finishes, in run order."""
    with _lock:
        return [job for job, _ in _pending]


def dequeue(job_id):
    """Remove a job that's still waiting in line (not the running one —
    that's what /abort is for). Returns True if it was actually queued."""
    with _lock:
        for i, (job, _) in enumerate(_pending):
            if job.id == job_id:
                del _pending[i]
                _jobs.pop(job_id, None)
                return True
    return False


def move_queued(job_id, delta):
    """Move a queued job earlier (delta=-1) or later (delta=+1) in line.
    Returns True if moved, False if not queued or already at that end."""
    with _lock:
        for i, (job, _) in enumerate(_pending):
            if job.id == job_id:
                j = i + delta
                if 0 <= j < len(_pending):
                    _pending[i], _pending[j] = _pending[j], _pending[i]
                    return True
                return False
    return False


def start(job, work):
    """Register `job` and run `work(job)` on a background thread.

    Raises RuntimeError if another job is running and hasn't been aborted.
    If the running job *has* been aborted but beets gives it no way to stop
    mid-unit (mbsync/bpsync — see sync.py), `job` is queued instead of
    rejected — behind any job already waiting — and starts automatically
    the instant its turn comes, so the caller never has to poll and retry
    by hand. Jobs still never run concurrently — see the `_pending` note
    above — this only removes the manual retry.

    The 'done' event is emitted from a finally block, so a client's stream
    always terminates even when the work raises.
    """
    with _lock:
        for existing in list(_jobs.values()):
            if existing.done.is_set():
                del _jobs[existing.id]
                continue
            if not existing.aborted.is_set():
                raise RuntimeError(
                    f'another job is already running ({existing.kind})')
            job.queued_behind = existing.kind
            _jobs[job.id] = job
            _pending.append((job, work))
            return job
        _jobs[job.id] = job

    _launch(job, work)
    return job


def _launch(job, work):
    def run():
        try:
            if not job.aborted.is_set():
                work(job)
        except Exception as e:
            job.emit('error', message=f'{type(e).__name__}: {e}')
        finally:
            job.emit('done', aborted=job.aborted.is_set(), **job.result)
            job.done.set()
            _start_pending()

    job.thread = threading.Thread(target=run, daemon=True)
    job.thread.start()


def _start_pending():
    """Launch the next queued job, if any, now that its predecessor is done."""
    with _lock:
        nxt = _pending.pop(0) if _pending else None
    if nxt:
        _launch(*nxt)
