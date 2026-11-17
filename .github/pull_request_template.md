Closes #

<!-- Use `Refs #N` instead for partial PRs. One `Closes #N` per line. -->

## What and why


## Checklist

- [ ] Issue's `model:` / `effort:` labels and plan-mode line were followed (or I said why not)
- [ ] **Design doc:** if this changes behaviour described in `docs/design/`, the doc is updated in this PR (or "n/a: touches no design")
- [ ] New `test_*.py`? Its step is in `.github/workflows/test.yml` (`python3 scripts/check_repo.py` checks this)
- [ ] Tests ran on beets 2.14.1 / Python 3.12 (CI's pins), not whatever is installed locally
- [ ] Nothing here touches `~/.config/beets` or `/Volumes`; tests use a throwaway `BEETSDIR` (and, once #159 lands, a throwaway app data folder)
- [ ] UI change: `test_smoke.py` extended
