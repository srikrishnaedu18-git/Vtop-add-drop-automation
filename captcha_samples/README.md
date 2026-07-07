# captcha_samples/

This directory holds raw CAPTCHA images captured from the live VTOP portal
so you can **visually verify** that the solver is reading them correctly.

## How it works

Run `test_captcha.py` — it fetches the live CAPTCHA from the portal, saves it
here with a timestamp, solves it with `captcha_solver.py`, and prints the result.

```
python3 test_captcha.py
```

Then open the saved image (e.g. `captcha_20260707_103045.jpg`) and compare it
against the printed solver output. If they match → ✅ solver is working.

## File naming convention

```
captcha_YYYYMMDD_HHMMSS.jpg
```

## Notes

- Images are **git-ignored** (see `.gitignore`) so they won't be committed.
- Only `README.md` is tracked in git.
- Collect a batch of samples to visually audit solver accuracy before running
  the full automation in `main.py`.
