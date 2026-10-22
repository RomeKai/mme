# Contributing

- `pip install -e . && python -m unittest discover -s tests` must pass. Tests never touch the network: they use a fake
  transport, the local mock, or a fake agent in a throwaway git repo.
- New provider facts (IDs, prices, limits) go **only** in `src/mme/providers/*.json`, with the source URL and date.
- Anything that spends money needs: a cap, a kill path, and a test proving the cap holds.
- Never write a test or feature that needs the user's real keys. Add a mock scenario instead.
- Keep `mme init` idempotent: it must never overwrite user-edited files without `--force`.
