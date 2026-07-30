"""
Drive the beets importer in-process.

`ImportSession.choose_match` is beets' extension point for "ask the user".
`TerminalImportSession` prompts on the terminal; `WebImportSession` instead
pushes the task onto an SSE queue and blocks until an answer arrives over
HTTP.

The importer runs its stages in threads, so `choose_match` is never called
from the Flask request thread. The handshake is a `queue.Queue` per decision:
the importer thread waits on it, the request thread puts the answer in it.

Only one import runs at a time — the beets `config` object and the resume
state in `~/.config/beets/state.pickle` are process-global.
"""
import os
import queue
import threading
import uuid

from beets import config, plugins, ui
from beets.autotag.hooks import AlbumMatch
from beets.importer import Action, ImportAbortError, ImportSession
from beets.util import displayable_path

# A blocked decision gives up after this long, so a closed browser tab
# cannot pin an importer thread forever.
DECISION_TIMEOUT = int(os.environ.get('BEETSGUI_DECISION_TIMEOUT', 900))

# Import modes, mirroring the radio buttons in the Import tab.
MODES = ('interactive', 'fast', 'quiet', 'timid')

# Answers accepted per decision kind.
ALLOWED = {
    'album':     {'apply', 'skip', 'asis', 'tracks', 'albums'},
    'item':      {'apply', 'skip', 'asis'},
    'duplicate': {'skip', 'keep', 'remove', 'merge'},
    'resume':    {'resume', 'restart'},
}

_ABORT = object()   # sentinel put on a pending answer queue to unblock it


# ── Serialisation ─────────────────────────────────────────────────────────────
# Match objects are not JSON-serialisable and dumping __dict__ pulls in the
# whole library. These build exactly the fields the UI needs.

def _track_row(item, info):
    return {
        'index':       info.index,
        'current':     item.title,
        'new':         info.title,
        'current_artist': item.artist,
        'new_artist':  info.artist,
        'current_length': item.length,
        'new_length':  info.length,
    }


def _candidate(match, index):
    info = match.info
    out = {
        'index':          index,
        'similarity':     round((1 - match.distance.distance) * 100, 1),
        'penalties':      match.distance.generic_penalty_keys,
        'disambiguation': match.disambig_string,
        'id':             info.id,
        'artist':         info.artist,
        'title':          info.name,
        'year':           info.get('year'),
        'country':        info.get('country'),
        'media':          info.get('media'),
        'albumtype':      info.get('albumtype'),
        'label':          info.get('label'),
        'data_source':    info.data_source,
        'data_url':       info.data_url,
    }
    if isinstance(match, AlbumMatch):
        pairs = sorted(match.item_info_pairs, key=lambda p: p[1].index or 0)
        out['tracks']       = [_track_row(i, t) for i, t in pairs]
        out['extra_items']  = [i.title for i in match.extra_items]
        out['extra_tracks'] = [t.title for t in match.extra_tracks]
    else:
        out['tracks'] = [_track_row(match.item, info)]
    return out


def serialize_task(task):
    """Build the decision payload for an album or singleton task."""
    is_album = task.is_album
    if is_album:
        current = {'artist': task.cur_artist, 'album': task.cur_album}
    else:
        current = {'artist': task.item.artist, 'title': task.item.title}
    return {
        'kind':           'album' if is_album else 'item',
        'paths':          [displayable_path(p) for p in task.paths],
        'item_count':     len(task.items),
        'current':        current,
        'recommendation': task.rec.name if task.rec is not None else 'none',
        'candidates':     [_candidate(m, i)
                           for i, m in enumerate(task.candidates or [])],
    }


def _summarize(obj, is_album):
    """Describe an existing library album/item for the duplicate prompt."""
    if is_album:
        items = list(obj.items())
        return {
            'artist': obj.albumartist,
            'name':   obj.album,
            'tracks': len(items),
            'format': items[0].format if items else None,
        }
    return {'artist': obj.artist, 'name': obj.title, 'tracks': 1,
            'format': obj.format}


# ── The job: thread, event stream and decision handshake ──────────────────────

class ImportJob:
    """One import run. Owns the worker thread and the pending decision."""

    def __init__(self, paths, mode):
        self.id = uuid.uuid4().hex
        self.paths = paths
        self.mode = mode
        self.events = queue.Queue()
        self.done = threading.Event()
        self.aborted = threading.Event()
        self.decided = 0
        self._lock = threading.Lock()
        self._pending = None
        self.thread = None

    def emit(self, type, **payload):
        self.events.put({'type': type, **payload})

    # -- called from importer threads --

    def ask(self, payload):
        """Publish a decision and block until it is answered.

        Raises ImportAbortError on abort or timeout.
        """
        answer = queue.Queue(1)
        did = uuid.uuid4().hex
        # ponytail: one pending decision at a time — beets' user_query is a
        # single pipeline stage, so only one task is ever waiting.
        with self._lock:
            if self.aborted.is_set():
                raise ImportAbortError()
            self._pending = {
                'decision_id':  did,
                'answer':       answer,
                'n_candidates': len(payload.get('candidates', [])),
                **payload,
            }
            event = {k: v for k, v in self._pending.items() if k != 'answer'}
        self.emit('decision', **event)
        try:
            reply = answer.get(timeout=DECISION_TIMEOUT)
        except queue.Empty:
            self.emit('error', message=f'No decision within {DECISION_TIMEOUT}s — aborting')
            self.aborted.set()
            raise ImportAbortError()
        finally:
            with self._lock:
                self._pending = None
        if reply is _ABORT:
            raise ImportAbortError()
        self.decided += 1
        return reply

    # -- called from Flask request threads --

    def pending(self):
        """The waiting decision as an SSE event, or None."""
        with self._lock:
            if not self._pending:
                return None
            p = {k: v for k, v in self._pending.items() if k != 'answer'}
        return {'type': 'decision', **p}

    def answer(self, decision_id, reply):
        """Release a blocked decision. Returns an error string, or None on success."""
        with self._lock:
            p = self._pending
            if not p:
                return 'no decision is waiting'
            if p['decision_id'] != decision_id:
                return 'stale decision id'
            choice = reply.get('choice')
            if choice not in ALLOWED[p['kind']]:
                return f"choice must be one of {sorted(ALLOWED[p['kind']])}"
            if choice == 'apply':
                try:
                    index = int(reply.get('candidate', 0))
                except (TypeError, ValueError):
                    return 'candidate must be an integer'
                if not 0 <= index < p['n_candidates']:
                    return f"candidate must be 0..{p['n_candidates'] - 1}"
                reply = {'choice': 'apply', 'candidate': index}
            p['answer'].put(reply)
            return None

    def abort(self):
        self.aborted.set()
        with self._lock:
            if self._pending:
                self._pending['answer'].put(_ABORT)


# ── The session ───────────────────────────────────────────────────────────────

class WebImportSession(ImportSession):
    """An import session that asks the browser instead of the terminal."""

    def __init__(self, job, lib, loghandler, paths, query):
        super().__init__(lib, loghandler, paths, query)
        self.job = job

    def should_resume(self, path):
        reply = self.job.ask({
            'kind': 'resume',
            'path': displayable_path(path),
        })
        return reply['choice'] == 'resume'

    def _auto(self, task):
        """Decide without asking, where beets' config says to.

        Same rules as `TerminalImportSession`: quiet mode never prompts, timid
        mode always does, and `none_rec_action` covers hopeless matches.
        Returns a match, an Action, or None to ask the browser.
        """
        rec = task.rec
        if config['import']['quiet']:
            if rec is not None and rec.name == 'strong' and task.candidates:
                return task.candidates[0]
            action = config['import']['quiet_fallback'].as_choice(
                {'skip': Action.SKIP, 'asis': Action.ASIS})
        elif config['import']['timid']:
            return None
        elif rec is not None and rec.name == 'none':
            action = config['import']['none_rec_action'].as_choice(
                {'skip': Action.SKIP, 'asis': Action.ASIS, 'ask': None})
        else:
            return None
        if action is not None:
            self.job.emit('status', message=f'Auto: {action.value.lower()}')
        return action

    def choose_match(self, task):
        auto = self._auto(task)
        if auto is not None:
            return auto
        reply = self.job.ask(serialize_task(task))
        choice = reply['choice']
        if choice == 'apply':
            match = task.candidates[reply['candidate']]
            self.job.emit('status', message=(
                f'Applying {match.info.artist} — {match.info.name}'))
            return match
        self.job.emit('status', message=f'Choice: {choice}')
        return {
            'skip':   Action.SKIP,
            'asis':   Action.ASIS,
            'tracks': Action.TRACKS,
            'albums': Action.ALBUMS,
        }[choice]

    # Singletons take the same payload shape; serialize_task handles both.
    choose_item = choose_match

    def resolve_duplicate(self, task, found_duplicates):
        items = task.imported_items()
        chosen = task.chosen_info()
        reply = self.job.ask({
            'kind':      'duplicate',
            'is_album':  task.is_album,
            'existing':  [_summarize(d, task.is_album) for d in found_duplicates],
            'new': {
                'artist': chosen.get('artist'),
                'name':   chosen.get('album') if task.is_album else chosen.get('title'),
                'tracks': len(items),
                'format': items[0].format if items else None,
            },
        })
        choice = reply['choice']
        if choice == 'skip':
            task.set_choice(Action.SKIP)
        elif choice == 'remove':
            task.should_remove_duplicates = True
        elif choice == 'merge':
            task.should_merge_duplicates = True
        # 'keep': leave the choice intact.


# ── Library and config ────────────────────────────────────────────────────────

_lib = None
_lib_lock = threading.Lock()


def get_library():
    """Open the beets library once, from the config beets itself would use."""
    global _lib
    with _lib_lock:
        if _lib is None:
            plugins.load_plugins()
            _lib = ui._open_library(config)
            plugins.send('library_opened', lib=_lib)
    return _lib


def _apply_mode(mode):
    """Map an Import tab mode onto beets' import config."""
    config['import']['autotag'] = mode != 'fast'
    config['import']['quiet']   = mode == 'quiet'
    config['import']['timid']   = mode == 'timid'


# ── Job registry ──────────────────────────────────────────────────────────────

_jobs = {}
_jobs_lock = threading.Lock()


def get_job(job_id):
    return _jobs.get(job_id)


def current_job():
    """The running job, if any — lets a reloaded page rejoin its import."""
    with _jobs_lock:
        for job in _jobs.values():
            if not job.done.is_set():
                return job
    return None


def _run(job):
    try:
        lib = get_library()
        _apply_mode(job.mode)
        paths = [os.fsencode(p) for p in job.paths]
        label = job.paths[0] if len(job.paths) == 1 else f'{len(job.paths)} folders'
        job.emit('status', message=f'Importing {label} ({job.mode})')
        session = WebImportSession(job, lib, None, paths, None)
        session.run()
        plugins.send('import', lib=lib, paths=paths)
    except Exception as e:
        job.emit('error', message=f'{type(e).__name__}: {e}')
    finally:
        job.emit('done', aborted=job.aborted.is_set(), decided=job.decided)
        job.done.set()


def _resolve_path(path):
    path = os.path.abspath(os.path.expanduser((path or '').strip()))
    if not os.path.exists(path):
        raise ValueError(f'no such file or directory: {path}')
    return path


def start(path=None, mode='interactive', paths=None):
    """Start an import on a background thread. Returns the ImportJob.

    Accepts either a single `path` or a `paths` list — a curated selection
    (e.g. from the Unimported tab or an `fd` search) is passed straight
    through; beets does its own directory/album grouping on whatever paths
    it receives, same as `beet import path1 path2 ...`.

    Raises ValueError on bad input, RuntimeError if an import is running.
    """
    if mode not in MODES:
        raise ValueError(f'mode must be one of {list(MODES)}')
    if paths is None:
        paths = [path]
    if not paths:
        raise ValueError('no path specified')
    resolved = [_resolve_path(p) for p in paths]

    with _jobs_lock:
        for job in list(_jobs.values()):
            if job.done.is_set():
                del _jobs[job.id]
            else:
                raise RuntimeError('an import is already running')
        job = ImportJob(resolved, mode)
        _jobs[job.id] = job

    job.thread = threading.Thread(target=_run, args=(job,), daemon=True)
    job.thread.start()
    return job
