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


def make_slow_job(gate, run_order=None, label=None):
    """A work(job) that blocks on `gate`, then records that it ran (and,
    if given a shared list, its label — for checking run *order*)."""
    ran = threading.Event()

    def work(job):
        gate.wait(timeout=5)
        if run_order is not None:
            run_order.append(label)
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

    # 3. A *second* job can also queue behind the first — the queue has no
    #    depth limit, only "one running at a time" is enforced.
    gate_c = threading.Event()
    work_c, ran_c = make_slow_job(gate_c)
    job_c = jobs.Job('sync-c')
    jobs.start(job_c, work_c)
    assert jobs.queued() == [job_b, job_c], jobs.queued()

    # 4. Once A finishes, B starts automatically; once B finishes, C does —
    #    in the order they queued, no manual retry, no server restart.
    gate_a.set()
    assert job_a.done.wait(timeout=5), 'A should finish once its gate opens'
    gate_b.set()
    assert job_b.done.wait(timeout=5), 'B should auto-start once A finishes'
    assert ran_b.is_set(), "B's work must actually have run"
    # C is now running (launched the instant B finished), not queued.
    assert jobs.current() is job_c, jobs.current()
    assert jobs.queued() == [], jobs.queued()
    gate_c.set()
    assert job_c.done.wait(timeout=5), 'C should auto-start once B finishes'
    assert ran_c.is_set(), "C's work must actually have run"

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

    # 6. A queued (not running) job can be removed from the line entirely —
    #    it never runs, and whoever was behind it moves up.
    gate_f = threading.Event()
    work_f, ran_f = make_slow_job(gate_f)
    job_f = jobs.Job('sync-f')
    jobs.start(job_f, work_f)
    job_f.abort()

    gate_g = threading.Event()
    work_g, ran_g = make_slow_job(gate_g)
    job_g = jobs.Job('sync-g')
    jobs.start(job_g, work_g)

    gate_h = threading.Event()
    work_h, ran_h = make_slow_job(gate_h)
    job_h = jobs.Job('sync-h')
    jobs.start(job_h, work_h)
    assert jobs.queued() == [job_g, job_h]

    assert jobs.dequeue(job_g.id) is True
    assert jobs.queued() == [job_h], jobs.queued()
    assert jobs.dequeue(job_f.id) is False, \
        'the running job is not "queued" — /abort is what cancels it'
    assert jobs.dequeue('no-such-id') is False

    gate_f.set()               # let F finish — G was dequeued, so H is next
    assert job_f.done.wait(timeout=5)
    gate_h.set()
    assert job_h.done.wait(timeout=5), 'H should auto-start once F finishes'
    assert ran_h.is_set()
    assert not ran_g.is_set(), 'G was dequeued — its work must never run'

    # 7. A queued job's position can be changed, and the actual run order
    #    reflects the new order, not the original queueing order.
    run_order = []
    gate_i = threading.Event()
    work_i, _ = make_slow_job(gate_i)
    job_i = jobs.Job('sync-i')
    jobs.start(job_i, work_i)
    job_i.abort()

    gate_j = threading.Event()
    work_j, _ = make_slow_job(gate_j, run_order, 'J')
    job_j = jobs.Job('sync-j')
    jobs.start(job_j, work_j)

    gate_k = threading.Event()
    work_k, _ = make_slow_job(gate_k, run_order, 'K')
    job_k = jobs.Job('sync-k')
    jobs.start(job_k, work_k)

    gate_l = threading.Event()
    work_l, _ = make_slow_job(gate_l, run_order, 'L')
    job_l = jobs.Job('sync-l')
    jobs.start(job_l, work_l)
    assert jobs.queued() == [job_j, job_k, job_l]

    assert jobs.move_queued(job_j.id, -1) is False, 'J is already at the front'
    assert jobs.move_queued(job_l.id, +1) is False, 'L is already at the back'
    assert jobs.move_queued(job_l.id, -1) is True   # [J, L, K]
    assert jobs.queued() == [job_j, job_l, job_k], jobs.queued()

    gate_i.set()
    for gate in (gate_j, gate_l, gate_k):
        gate.set()
    assert job_k.done.wait(timeout=5), 'the last-run job should finish last'
    assert run_order == ['J', 'L', 'K'], run_order

    print('ok')


if __name__ == '__main__':
    main()
