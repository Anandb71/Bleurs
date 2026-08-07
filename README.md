<h1 align="center">bleurs</h1>

<p align="center">
  <strong>A deterministic firewall for AI coding agents.</strong><br>
  Blocks hallucinated imports and APIs before they reach disk.
</p>

<p align="center">
  <a href="https://github.com/Anandb71/Bleurs/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/Anandb71/Bleurs/actions/workflows/ci.yml/badge.svg"></a>
  <img alt="Python 3.10+" src="https://img.shields.io/badge/python-3.10%2B-blue">
  <img alt="Dependencies: none" src="https://img.shields.io/badge/dependencies-none-brightgreen">
  <a href="LICENSE"><img alt="MIT" src="https://img.shields.io/badge/license-MIT-black"></a>
</p>

---

Your agent writes `from langchain_vectorstore_utils import build_faiss_index`.

The package does not exist. Nobody has ever published it. The code looks perfect,
passes review, and fails four minutes later — or worse, succeeds, because
somebody registered that name on PyPI last week and is now running their code on
your machine.

bleurs sits between the agent and the filesystem and refuses the write.

```
$ bleurs demo

  01_invented_packages.py - A RAG pipeline, in the style LLMs actually write them.
  02_invented_apis.py     - Every module here is real and installed. Every call is not.
  03_clean.py             - Correct code. The only interesting thing here is silence.

01_invented_packages.py
  BLOCK 6:0   import langchain_vectorstore_utils     - no package named 'langchain_vectorstore_utils' exists on PyPI
  BLOCK 7:0   from openai_embeddings_toolkit import  - no package named 'openai_embeddings_toolkit' exists on PyPI
  BLOCK 14:12 lvu.build_faiss_index                  - no package named 'langchain_vectorstore_utils' exists on PyPI
  BLOCK 15:4  lvu.persist                            - no package named 'langchain_vectorstore_utils' exists on PyPI

02_invented_apis.py
  BLOCK 13:11 json.loads_safe                        - json has no attribute 'loads_safe'
  BLOCK 18:11 datetime.datetime.now_utc              - datetime.datetime has no attribute 'now_utc'
  BLOCK 23:11 os.path.join_all                       - os.path has no attribute 'join_all'
  BLOCK 28:11 base64.encode_string                   - base64 has no attribute 'encode_string'

8 hallucinations blocked  30 references verified
```

Note what is *not* in that output. `03_clean.py` is full of the patterns that
make naive checkers fire — a shadowed module name, a guarded optional import,
dynamic attribute access, a submodule that is not an attribute of its parent —
and bleurs says nothing about it. That silence is the hard part.

## Install

```bash
uv tool install bleurs
```

Zero runtime dependencies. It uses the interpreter's own `ast` module and its
own packaging metadata, so there is nothing to provision, no index to build, and
no database to run.

Try it without installing:

```bash
uvx bleurs demo
```

## Use it as a Claude Code hook

This is the point. Checking after the fact is a linter; checking *before the
write* is a firewall.

```bash
bleurs install-hook
```

That registers a `PreToolUse` hook on `Write`, `Edit`, and `MultiEdit`. From
then on, when the agent proposes an edit, bleurs reconstructs what the file
*would* contain, verifies every external reference in it, and rejects the tool
call if any of them is provably fictional. The agent gets told exactly what was
wrong and usually fixes it on the next turn:

```
Bleurs blocked this edit. These references do not exist:
  - app.py:2  from fastapi_auth_helpers import verify - no package named 'fastapi_auth_helpers' exists on PyPI
  - app.py:5  json.loads_safe - json has no attribute 'loads_safe'

Each was checked against the real environment, not guessed.
Fix the reference or install the dependency, then retry.
```

Works with anything that can run a command on a JSON payload — Cursor, Codex,
your own agent loop. It reads a tool call on stdin and answers with an exit
code.

## Use it in CI

```bash
bleurs check src --exclude demo
```

Exit code 1 if anything was blocked. Add `--format json` for machine output, or
`--explain` to see what it could *not* verify and why.

## The one rule

> **BLOCK requires positive evidence of absence.**

Not "we couldn't find it." Not "that looks wrong." Absence, demonstrated by a
named resolver that looked inside a container it successfully opened. Everything
short of that is allowed through, and the reason for abstaining is recorded
rather than discarded.

The asymmetry is deliberate. A missed hallucination costs you one failed test
run, which you were going to have anyway. A false positive costs you the tool —
the user turns it off, and from that moment it catches nothing, forever. Recall
is worth spending. Precision is not.

So bleurs stays quiet on: wildcard imports, names rebound anywhere in the file,
anything inside a `try/except` that would catch it failing, anything inside a
platform or version test, modules with a PEP 562 `__getattr__`, attribute
chains rooted at anything but a module binding, files that fail to parse,
packages whose registry lookup failed for network reasons, and any
project-local module it could not index. Run `--explain` and it will tell you
which of those applied.

That third one has teeth. `ctypes.windll` exists on Windows and nowhere else,
and this is how every cross-platform codebase reaches for it:

```python
try:
    return ctypes.windll.kernel32.GetStdHandle(-11)
except Exception:
    return None
```

On Linux that attribute is genuinely absent, and bleurs can prove it — but the
author already said in code that it might be. Reporting it would be telling
them something they knew, about code that is correct. bleurs found this exact
false positive in its own source, on its own CI, on the first run.

## How it resolves things

Five tiers, cheapest and most certain first. Each can answer *present*,
*absent*, or *I don't know* — and only the middle answer can block.

| Tier | Source of truth | Executes code? | Catches |
|---|---|---|---|
| 0 · local | your project's files, parsed | no | helpers the agent invented in your own codebase |
| 1 · stdlib | `sys.stdlib_module_names` | no | fake stdlib modules |
| 2 · env | installed distribution metadata | no | packages that aren't there |
| 3 · introspect | the real library object | **yes**, sandboxed | **fake APIs on real libraries** |
| 4 · registry | PyPI, cached on disk | no | invented packages / slopsquatting bait |

Tier 3 is the one nothing else does, and it is the only way to catch the failure
mode that survives review: the import is fine, the library is real, and the
method is fiction.

It is also the tier that runs third-party code, because there is no other way to
ask an object what it contains. It happens in an isolated subprocess with a
timeout, once per check run rather than once per reference, and only for
packages already installed in your environment — code you were going to import
anyway. `--no-introspect` turns it off; bleurs then still catches invented
packages and says plainly that it checked no APIs.

## Why this is a real problem

A 2026 study measured package hallucination across five frontier models —
Claude Sonnet 4.6, Claude Haiku 4.5, GPT-5.4-mini, Gemini 2.5 Pro, DeepSeek
V3.2 — over 199,845 prompts. Rates ranged from **4.62% to 6.10%**. The spread
between best and worst model collapsed from 16.5 points in the previous cohort
to 1.48, and **not one of them beat GPT-4 Turbo's older ~3.6%**.

Everyone converged. Nobody improved. **127 package names were invented
identically by all five models** — which is what makes them registrable by an
attacker, and what makes them checkable by us.

This is not a problem that scaling fixes, because it is not a knowledge problem.
It is a grounding problem, and grounding is a job for a deterministic checker.

## What this is not

- **Not a semantic checker.** It proves a symbol exists. It cannot tell you that
  the right symbol is being used the wrong way. The 2026 AST-validation paper
  that measured this approach reports 100% precision and 87.6% recall on
  reference hallucinations, and **0% correction** on contextual mismatches. That
  boundary is real and this tool sits firmly on one side of it.
- **Not a type checker.** mypy and pyright already do that, better. bleurs
  answers a narrower question and answers it without configuration, without
  annotations, and on files that do not exist yet.
- **Not multi-language yet.** Python only. The front-end interface is already
  factored for tree-sitter backends; see below.

### The one place a false positive can still get in

If an import name is not installed, is not stdlib, is not project-local, is not
in [`aliases.py`](src/bleurs/truth/aliases.py), and has no PyPI project of that
name, bleurs blocks it. The residual risk is a real package whose import name
differs from its distribution name and which is missing from that table —
`import cv2` from `opencv-python`, but one we haven't listed.

That table is the highest-value contribution anyone can make to this repo. Run
`--no-strict-imports` to downgrade every registry-based block to a warning if
you want the guarantee absolute.

## Roadmap

- **tree-sitter front-ends** for TypeScript, Go, and Rust behind the existing
  `Analyzer` interface — npm has the same slopsquatting problem and no
  equivalent of `importlib.metadata`.
- **SCIP ingestion** so tier 0 can consume an existing
  [SCIP](https://scip-code.org/) index instead of walking the filesystem, and
  inherit real cross-file name resolution.
- **A recall benchmark** against a public corpus of model-generated code, so
  "87.6%" is a number this repo measures rather than cites.
- **Retrieval**, eventually: once you can prove what exists, you can start
  fetching it instead of generating it.

## Prior art, and credit where it is owed

bleurs is a small idea standing on a lot of other people's work. If you are
building in this space, read these before you read my code:

- **[SCIP](https://sourcegraph.com/blog/announcing-scip)** (Sourcegraph) — the
  code-intelligence index format this should eventually consume rather than
  reinvent. Also [LSIF](https://lsif.dev/), its predecessor.
- **[Stack Graphs](https://arxiv.org/pdf/2211.01224)** (GitHub) — incremental
  name resolution at scale; the right answer to the problem tier 0 solves
  crudely.
- **[Glean](https://glean.software/)** (Meta) and **Kythe** (Google) — typed,
  schema-defined fact databases about source code.
- **[Coccinelle](https://lwn.net/Articles/315686/)** — semantic patches for C.
  Twenty years ahead of the current conversation about AST-level edits.
- **[OpenRewrite](https://docs.openrewrite.org/)** (Moderne) — lossless semantic
  trees and recipe-based transformation.
- **[Automated Software Transplantation](https://dl.acm.org/doi/10.1145/2771783.2771796)**
  (Barr, Harman, Jia, Marginean, Petke — ISSTA 2015) — µSCALPEL moved the H.264
  codec from x264 into VLC automatically. Required reading for anyone who thinks
  "just retrieve the code instead of generating it" is a new idea.
- **[ast-grep](https://ast-grep.github.io/)** and
  **[tree-sitter](https://tree-sitter.github.io/)** — what the non-Python
  front-ends will be built on.
- **[Serena](https://github.com/oraios/serena)** — LSP-backed semantic tooling
  for agents; solves the retrieval half of this problem well.
- *Detecting and Correcting Hallucinations in LLM-Generated Code via
  Deterministic AST Analysis* ([arXiv:2601.19106](https://arxiv.org/abs/2601.19106))
  — the introspection-based validation approach tier 3 implements, and the
  source of the precision/recall figures quoted above.
- *The Range Shrinks, the Threat Remains* ([arXiv:2605.17062](https://arxiv.org/abs/2605.17062))
  — the 2026 frontier-cohort package hallucination measurements.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). The short version: every pull request
must keep `tests/test_no_false_positives.py` green. If your change catches more
hallucinations but makes bleurs blame correct code, the change is wrong.

## License

MIT © Anand Biju
