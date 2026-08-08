#!/usr/bin/env python3
"""
Unit test for jobs.py's queue-behind-an-abort behaviour (#32).

mbsync/bpsync can't stop mid-phase (see sync.py), so aborting one leaves
it running for real, sometimes for minutes. Before this fix that meant
no other job could start until it finished on its own — the only escape
was restarting the server. This checks the actual registry logic with
synthetic slow jobs, no beets/Flask/ffmpeg needed.

Run: python test_jobs.py
"""
import threading

import jobs


def make_slow_job(gate):
    """A work(job) that blocks on `gate` then records that it ran."""
    ran = threading.Event()

    def work(job):
        gate.wait(timeout=5)
        ran.set()
    return work, ran


def main():
    # 1. A job can't start while another is running and not aborted.
    gate_a = threading.Event()
    work_a, ran_a = make_slow_job(gate_a)
    job_a = jobs.Job('sync-a')
    jobs.start(job_a, work_a)
    try:
        jobs.start(jobs.Job('sync-b'), lambda j: None)
        assert False, 'expected RuntimeError while a job is running'
    except RuntimeError:
        pass

    # 2. Abort A (cooperative — like mbsync mid-phase, it keeps running).
    #    A second job now queues instead of being rejected.
    job_a.abort()
    assert not job_a.done.is_set(), 'test setup: A should still be running'
    gate_b = threading.Event()
    work_b, ran_b = make_slow_job(gate_b)
    job_b = jobs.Job('sync-b')
    result = jobs.start(job_b, work_b)
    assert result is job_b
    assert job_b.queued_behind == 'sync-a', job_b.queued_behind
    assert not ran_b.is_set(), 'B must not run while A is still finishing'

    # 3. Only one job may queue behind it — a third is still rejected.
    try:
        jobs.start(jobs.Job('sync-c'), lambda j: None)
        assert False, 'expected RuntimeError with one job already queued'
    except RuntimeError:
        pass

    # 4. Once A actually finishes, B starts automatically — no manual retry,
    #    no server restart.
    gate_a.set()
    assert job_a.done.wait(timeout=5), 'A should finish once its gate opens'
    gate_b.set()
    assert job_b.done.wait(timeout=5), 'B should auto-start once A finishes'
    assert ran_b.is_set(), "B's work must actually have run"

    # 5. Aborting a job while it's still queued (before its turn comes)
    #    skips its work entirely instead of running it anyway.
    gate_d = threading.Event()
    work_d, ran_d = make_slow_job(gate_d)
    job_d = jobs.Job('sync-d')
    jobs.start(job_d, work_d)
    job_d.abort()

    gate_e = threading.Event()
    work_e, ran_e = make_slow_job(gate_e)
    job_e = jobs.Job('sync-e')
    jobs.start(job_e, work_e)
    assert job_e.queued_behind == 'sync-d', job_e.queued_behind
    job_e.abort()            # abort the *queued* job before its turn comes

    gate_d.set()              # let D finish, which should launch E
    assert job_d.done.wait(timeout=5)
    assert job_e.done.wait(timeout=5), 'E should still transition to done'
    assert not ran_e.is_set(), \
        'E was aborted before it started — its work must not run'

    print('ok')


if __name__ == '__main__':
    main()
