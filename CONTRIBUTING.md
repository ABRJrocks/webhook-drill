# Contributing

Open an issue describing the integration problem and a minimal synthetic reproduction before a large feature change. Keep the core dependency-free and keep failures reproducible.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m unittest discover -s tests -v
```

Tests should establish an observable behavior: raw-byte signature compatibility, duplicate side effects, state consistency, retry policy, transport handling, or CLI/report contracts. Include a failing case where it improves confidence. Do not rely on paid services, live credentials, or public third-party endpoints.

Document new fields in `docs/suites.md` and reject unknown fields or unsupported combinations before network execution. Add a migration note for incompatible report or suite changes. Keep examples executable against the local demo where practical.

