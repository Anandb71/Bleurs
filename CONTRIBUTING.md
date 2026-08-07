# Contributing

## The one rule

`tests/test_no_false_positives.py` must stay green.

Every test in that file asserts that bleurs stays *quiet* on correct code. If a
change catches more hallucinations but makes bleurs blame something valid, the
change is wrong — however good the new detection is. A tool that cries wolf gets
switched off, and a switched-off firewall catches nothing.

When you add detection, add the matching silence test in the same PR.

## Setup

```bash
git clone https://github.com/Anandb71/Bleurs
cd Bleurs
pip install -e ".[dev]"
pytest
```

No dependencies beyond pytest. If your change needs a runtime dependency, open
an issue first — zero-deps is a product decision, not an accident.

## The highest-value contribution

[`src/bleurs/truth/aliases.py`](src/bleurs/truth/aliases.py) maps import names
to distribution names for packages where they differ (`cv2` → `opencv-python`,
`yaml` → `PyYAML`). It is the only place a false positive can still enter: a
real package missing from that table, not installed locally, looks exactly like
an invented one.

If you know a pair that isn't listed, add it. One-line PRs very welcome.

## Adding a language

The front-end contract is in
[`src/bleurs/analyze/__init__.py`](src/bleurs/analyze/__init__.py): turn source
text into `Reference` objects and a set of reasons the file can't be fully
verified. Register it in `for_path`.

The hard part is never the parsing — it is the ground truth. Before starting,
work out what plays the role of `importlib.metadata` and runtime introspection
in your language. For TypeScript that's `node_modules` plus `.d.ts`; for Go it's
the module cache. If you can't answer "how do I *prove* this symbol is absent",
the front-end will only ever be able to abstain.

## Style

- Comments explain *why*, especially why a check is deliberately conservative.
  The abstain rules look like missing features until you know what they prevent.
- New abstain conditions get an `AbstainReason` so `--explain` can surface them.
  Silently skipping a check is the one thing worse than a false positive.
- Every `BLOCK` must name the resolver that proved it, so the precision claim
  stays auditable.

## Reporting a false positive

Open an issue with the snippet and the output of:

```bash
bleurs check yourfile.py --explain --format json
```

False positives are the highest-priority bug class in this repo. They get fixed
before features.
