"""
Format conversion as a background job (#16) — beets' convert plugin,
driven without its CLI wrapper.

`ConvertPlugin.convert_func` can't be reused directly: it wants a real
optparse namespace and has an interactive "Convert? (Y/n)" prompt baked
in. The plugin's own auto-convert-on-import path (`auto_convert_keep`)
shows the level below that — build a default opts namespace from the
subcommand's parser, resolve it with `_get_opts_and_config`, and call
`_parallel_convert` directly — so that's what this does.

Progress and abort come from a trick rather than a reimplementation:
`_parallel_convert` builds `Pipeline([iter(items), ...])`, so passing a
*generator* as `items` makes the source stage ours. It emits a status
event per item and stops feeding when the job is aborted, and beets'
converter stages are untouched.
"""
import os

from beets import plugins
from beets.util import displayable_path, pipeline

import jobs
from importsession import get_library
from libops import split_query


def _plugin():
    for p in plugins.find_plugins():
        if p.name == 'convert':
            return p
    raise RuntimeError(
        "the 'convert' plugin is not enabled — add it under plugins: in your "
        "beets config (and set convert.dest, or pass a destination here)")


def _feed(items, job):
    """Source stage: report progress and honour abort.

    ponytail: the pipeline reads ahead, so the count is "fed to the
    converter", not "finished encoding" — it can lead actual progress by
    the queue size plus the thread count. Fine for a status line; if it
    ever needs to be exact, the converter stage itself has to report.
    """
    for i, item in enumerate(items, 1):
        if job.aborted.is_set():
            return
        # Untagged files are exactly the ones people convert, so fall back
        # to the filename rather than showing "[1/3]  —  ".
        label = (f'{item.artist} — {item.title}' if item.title
                 else os.path.basename(displayable_path(item.path)))
        job.emit('status', message=f'[{i}/{len(items)}] {label}')
        job.result['sent'] = i
        yield item


def _run(job):
    lib = get_library()
    plugin = _plugin()

    opts = plugin.commands()[0].parser.get_default_values()
    opts.format = job.meta.get('format') or None
    opts.pretend = bool(job.meta.get('pretend'))
    opts.album = False
    opts.yes = True                       # the UI's own confirm stands in
    if job.meta.get('dest'):
        opts.dest = job.meta['dest']

    (dest, threads, path_formats, fmt, pretend, hardlink, link, _playlist,
     force) = plugin._get_opts_and_config(opts)

    items = list(lib.items(split_query(job.meta.get('query', ''))))
    if not items:
        job.emit('status', message='No tracks match that query.')
        job.result.update(sent=0, total=0, pretend=pretend)
        return

    job.emit('status', message=(
        f'{"Previewing" if pretend else "Converting"} {len(items)} track'
        f'{"" if len(items) == 1 else "s"} to {fmt}'))

    # ponytail: reports tracks *sent to* the converter, not tracks encoded.
    # convert_item silently skips items whose target already exists or that
    # fail to encode, and reports nothing back, so a "converted" count here
    # would be a claim this code can't actually check — which is worse than
    # a vaguer true one. The destination folder is the source of truth.
    job.result.update(sent=0, total=len(items), pretend=pretend)

    # Same two stages `_parallel_convert` builds, but assembled here so the
    # inter-stage buffer can be 1 instead of DEFAULT_QUEUE_SIZE (16).
    # Abort stops the source feeding, so anything already buffered still
    # gets encoded — with 16 in flight that made abort take minutes on
    # real audio (measured). At 1 it is bounded by the worker count.
    stages = [plugin.convert_item(dest, opts.keep_new, path_formats, fmt,
                                  pretend, link, hardlink, force)
              for _ in range(threads)]
    pipeline.Pipeline([_feed(items, job), stages]).run_parallel(queue_size=1)


def start(query='', fmt=None, pretend=True, dest=None):
    """Start a convert job. `pretend` defaults to True: this writes files,
    so the safe default is the preview the old UI also defaulted to.

    Raises RuntimeError if another job is running.
    """
    job = jobs.Job('convert', query=query, format=fmt,
                   pretend=bool(pretend), dest=dest)
    return jobs.start(job, _run)
