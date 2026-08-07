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
state in `~/.config/beets/state.pickle` are process-global. That limit is
enforced in `jobs.py`, and it is shared with the other background jobs
(cover art, convert), which mutate those same globals.
"""
import os
import queue
import threading
import uuid

from beets import config, context, plugins, ui
try:
    from beets.autotag.match import AlbumMatch   # beets >= 2.13
except ImportError:
    from beets.autotag.hooks import AlbumMatch    # beets < 2.13
try:
    from beets.importer import DuplicateAction    # beets >= 2.13
except ImportError:
    DuplicateAction = None                        # beets < 2.13
from beets.importer import Action, ImportAbortError, ImportSession
from beets.util import displayable_path

import jobs

# A blocked decision gives up after this long, so a closed browser tab
# cannot pin an importer thread forever.
DECISION_TIMEOUT = int(os.environ.get('BEETSGUI_DECISION_TIMEOUT', 900))

# Import modes, mirroring the radio buttons in the Import tab.
MODES = ('interactive', 'fast', 'quiet', 'timid')

# File-handling choices, mirroring the Import tab's radio group.
HANDLING = ('copy', 'move', 'keep')

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


# ── Quality ranking ─────────────────────────────────────────────────────────
# This is a DJ/audiophile library: a lossy file silently overwriting a
# lossless one is data loss that only shows up in a club. The invariant is
# not a preference — lossless may replace lossy, lossy must never replace
# lossless.

LOSSLESS_FORMATS = {'AIFF', 'ALAC', 'FLAC', 'WAVE', 'APE', 'WAVPACK', 'DSD STREAM FILE'}

# The app's own convert step never lands above 24-bit ALAC (32-bit float
# sources are downsampled there too), so ranking a 32-bit float AIFF at its
# raw bitdepth would make it look better than what it will actually become.
_MAX_RANKED_BITDEPTH = 24


def _quality_fields(item):
    """format/bitdepth/samplerate or format/bitrate for one track, for the UI."""
    if item is None:
        return {}
    fmt = (item.format or '').upper()
    if fmt in LOSSLESS_FORMATS:
        return {'bitdepth': item.bitdepth, 'samplerate': item.samplerate}
    return {'bitrate': item.bitrate}


def _track_quality(item):
    fmt = (item.format or '').upper()
    if fmt in LOSSLESS_FORMATS:
        return (1, min(item.bitdepth or 0, _MAX_RANKED_BITDEPTH), item.samplerate or 0, 0)
    return (0, 0, 0, item.bitrate or 0)


def quality_rank(items):
    """Comparable quality tuple for one or more tracks.

    Lossless always outranks lossy, regardless of bitdepth/bitrate — that
    is the invariant, not a tiebreaker. An album's rank is its *worst*
    track's, so one lossy track in an otherwise-lossless release can't
    make the release count as equal to a fully lossless one.
    """
    items = list(items)
    if not items:
        return (0, 0, 0, 0)
    return min(_track_quality(i) for i in items)


def _summarize(obj, is_album):
    """Describe an existing library album/item for the duplicate prompt."""
    if is_album:
        items = list(obj.items())
        first = items[0] if items else None
        out = {
            'artist': obj.albumartist,
            'name':   obj.album,
            'tracks': len(items),
            'format': first.format if first else None,
        }
    else:
        first = obj
        out = {'artist': obj.artist, 'name': obj.title, 'tracks': 1,
               'format': obj.format}
    out.update(_quality_fields(first))
    return out


# ── The job: thread, event stream and decision handshake ──────────────────────

class ImportJob(jobs.Job):
    """One import run. Adds the decision handshake to the base job: the
    importer thread blocks in `ask()` until a request thread `answer()`s."""

    def __init__(self, paths, mode, handling='copy', incremental=False,
                 singleton=False, log_path=None):
        super().__init__('import')
        self.paths = paths
        self.mode = mode
        self.handling = handling
        self.incremental = incremental
        self.singleton = singleton
        self.log_path = log_path
        self.result['decided'] = 0
        self._lock = threading.Lock()
        self._pending = None

    def summary(self):
        return {'id': self.id, 'kind': self.kind, 'aborting': self.aborted.is_set(),
                'paths': self.paths, 'mode': self.mode, 'handling': self.handling,
                'incremental': self.incremental, 'singleton': self.singleton}

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
        self.result['decided'] += 1
        return reply

    # -- called from Flask request threads --

    def pending(self):
        """The waiting decision as an SSE event, or None."""
        with self._lock:
            if not self._pending:
                return None
            p = {k: v for k, v in self._pending.items() if k != 'answer'}
        return {'type': 'decision', **p}

    def replay(self):
        p = self.pending()
        return [p] if p else []

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
            # A decision is single-use, and clearing it here rather than in
            # ask()'s finally is what makes that true: a duplicate answer (a
            # double-clicked button, a retried request) used to be accepted
            # and then block in put() on the maxsize=1 queue *while holding
            # this lock*, which wedges ask()'s own cleanup, pending() and
            # abort() — permanently, since nothing else drains that queue.
            # Rejected answers above must leave the decision open, so this
            # only happens once the reply is known to be valid.
            self._pending = None
            try:
                p['answer'].put_nowait(reply)
            except queue.Full:
                return 'decision already answered'
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

    def _decide_duplicate(self, task, found_duplicates):
        """Shared logic behind both beets duplicate-resolution APIs below.

        Returns 'skip', 'keep', 'remove' or 'merge'.
        """
        items = task.imported_items()
        chosen = task.chosen_info()

        new_rank = quality_rank(items)
        existing_ranks = [
            quality_rank(d.items() if task.is_album else [d])
            for d in found_duplicates
        ]
        # The best copy already in the library — 'remove' deletes every
        # found duplicate at once, so it's only recommended when the
        # incoming files beat all of them, and auto-skip only fires when
        # they're worse than all of them.
        existing_rank = max(existing_ranks) if existing_ranks else new_rank

        if new_rank < existing_rank:
            self.job.emit('status', message=(
                'Auto-skip: an existing copy is higher quality'))
            return 'skip'

        payload = {
            'kind':      'duplicate',
            'is_album':  task.is_album,
            'existing':  [_summarize(d, task.is_album) for d in found_duplicates],
            'new': {
                'artist': chosen.get('artist'),
                'name':   chosen.get('album') if task.is_album else chosen.get('title'),
                'tracks': len(items),
                'format': items[0].format if items else None,
                **_quality_fields(items[0] if items else None),
            },
        }
        if new_rank > existing_rank:
            payload['recommendation'] = 'remove'
        reply = self.job.ask(payload)
        return reply['choice']

    if DuplicateAction is not None:
        # beets >= 2.13: ImportSession.resolve_duplicate was replaced by
        # get_duplicate_action, which returns the decision instead of
        # mutating the task — beets' own pipeline now does the
        # skip/remove/merge dispatch (see importer/stages.py).
        def get_duplicate_action(self, task, found_duplicates):
            choice = self._decide_duplicate(task, found_duplicates)
            return {
                'skip':   DuplicateAction.SKIP,
                'keep':   DuplicateAction.KEEP,
                'remove': DuplicateAction.REMOVE,
                'merge':  DuplicateAction.MERGE,
            }[choice]
    else:
        # beets < 2.13: resolve_duplicate mutates the task directly.
        def resolve_duplicate(self, task, found_duplicates):
            choice = self._decide_duplicate(task, found_duplicates)
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
    """Open the beets library once, from the config beets itself would use.

    beets binds its "music directory" (used to resolve an item's path,
    which is stored relative to it) via a contextvars.ContextVar, set once
    in Library.__init__ — on whichever thread happens to trigger that first
    call. Flask's threaded dev server hands each request a new thread, so
    every request on a *different* thread than the one that opened the
    library would see item.path resolve to the bare relative bytes instead
    of an absolute path. Re-binding it here, on every call, fixes that for
    the calling thread regardless of which thread did the actual open.
    """
    global _lib
    with _lib_lock:
        if _lib is None:
            plugins.load_plugins()
            _lib = ui._open_library(config)
            plugins.send('library_opened', lib=_lib)
    context.set_music_dir(_lib.directory)
    return _lib


def _apply_options(job):
    """Map the Import tab's choices onto beets' import config.

    Same config keys the `beet import` CLI sets from its own flags
    (`config['import'].set_args(opts)` in beets' own command handler) —
    matched here since the web session drives the same pipeline stages.
    """
    config['import']['autotag'] = job.mode != 'fast'
    config['import']['quiet']   = job.mode == 'quiet'
    config['import']['timid']   = job.mode == 'timid'
    config['import']['move']    = job.handling == 'move'
    config['import']['copy']    = job.handling == 'copy'
    if job.handling == 'keep':
        config['import']['write'] = False
    config['import']['incremental'] = bool(job.incremental)
    config['import']['singletons']  = bool(job.singleton)
    if job.log_path:
        config['import']['log'] = job.log_path


def _run(job):
    """The import itself. jobs.start() owns the thread, the error handling
    and the final 'done' event."""
    lib = get_library()
    _apply_options(job)
    paths = [os.fsencode(p) for p in job.paths]
    label = job.paths[0] if len(job.paths) == 1 else f'{len(job.paths)} folders'
    job.emit('status', message=f'Importing {label} ({job.mode})')
    session = WebImportSession(job, lib, None, paths, None)
    session.run()
    plugins.send('import', lib=lib, paths=paths)


def _resolve_path(path):
    path = os.path.abspath(os.path.expanduser((path or '').strip()))
    if not os.path.exists(path):
        raise ValueError(f'no such file or directory: {path}')
    return path


def start(path=None, mode='interactive', paths=None, handling='copy',
          incremental=False, singleton=False, log_path=None):
    """Start an import on a background thread. Returns the ImportJob.

    Accepts either a single `path` or a `paths` list — a curated selection
    (e.g. from the Unimported tab or an `fd` search) is passed straight
    through; beets does its own directory/album grouping on whatever paths
    it receives, same as `beet import path1 path2 ...`.

    Raises ValueError on bad input, RuntimeError if a job is running.
    """
    if mode not in MODES:
        raise ValueError(f'mode must be one of {list(MODES)}')
    if handling not in HANDLING:
        raise ValueError(f'handling must be one of {list(HANDLING)}')
    if paths is None:
        paths = [path]
    if not paths:
        raise ValueError('no path specified')
    resolved = [_resolve_path(p) for p in paths]
    if log_path:
        log_path = os.path.abspath(os.path.expanduser(log_path))

    job = ImportJob(resolved, mode, handling, incremental, singleton, log_path)
    return jobs.start(job, _run)
