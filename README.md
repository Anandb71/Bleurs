<div align="center">

# bleurs

**Ground truth for AI coding agents.**

Blocks the APIs that don't exist. Serves the ones that do. One index, both directions.

[![CI](https://github.com/Anandb71/Bleurs/actions/workflows/ci.yml/badge.svg)](https://github.com/Anandb71/Bleurs/actions/workflows/ci.yml)
![Python 3.10+](https://img.shields.io/badge/python-3.10%20%E2%80%93%203.13-blue)
![Dependencies: none](https://img.shields.io/badge/dependencies-0-brightgreen)
![Platforms](https://img.shields.io/badge/linux%20%C2%B7%20macos%20%C2%B7%20windows-tested-lightgrey)
[![License: MIT](https://img.shields.io/badge/license-MIT-black)](LICENSE)

<img src="docs/demo.svg" alt="bleurs demo: eight hallucinations blocked, thirty references verified" width="100%">

</div>

Look at what is **not** in that output.

`03_clean.py` was checked and produced nothing. It is deliberately full of the
patterns that make naive checkers fire — a shadowed module name, a guarded
optional import, dynamic attribute access, a submodule reached through its
parent binding. Catching invented packages is easy if you are willing to be
wrong sometimes. That silence is the hard part.

---

## Two problems, one index

Coding agents fail in two ways, and both are the same missing thing.

**They assert what isn't there.** Invented packages, invented methods on real
libraries, helpers that were never written.

**They don't know what is there.** So they read whole files to find out, fill
the context window, get compacted, forget, and read them again. Then they guess,
which returns you to the first problem.

Every tool in this space picks one. A linter tells you that you were wrong. A
RAG index tells you what might be relevant. Neither can do the other's job,
because they're built on different substrates — one on rules, one on embeddings.

bleurs does both from the same place, because **proving a name is absent and
enumerating the names that are present are the same query.** When it opens the
`json` module to prove `loads_safe` isn't in it, it is holding the complete,
exact, current list of what *is* in it. Throwing that away was the bug.

So a rejection isn't a rejection. It's an answer:

```
Bleurs blocked this edit. These references do not exist:
  - t.py:5  base64.encode_string - base64 has no attribute 'encode_string'
  - t.py:5  json.loads_safe - json has no attribute 'loads_safe'

What those containers actually provide:

base64  [module]
  b64encode(s, altchars=None)
  b64decode(s, altchars=None, validate=False)
  urlsafe_b64encode(s)
  standard_b64encode(s)
  ...

json  [module]
  dumps(obj, *, skipkeys=False, ensure_ascii=True, ...)
  loads(s, *, cls=None, object_hook=None, ...)
  ...
```

The agent fixes it on the next turn because it now *has* the ground truth — not
because it guessed better. And it never opened a file to get it.

---

## Quickstart

```bash
uvx --from git+https://github.com/Anandb71/Bleurs bleurs demo
```

Zero dependencies, nothing to provision, no index to build. It reads your
interpreter's own `ast` module and its own packaging metadata.

> **PyPI release pending.** Once it lands this is just `uvx bleurs demo`.

**Put it in the edit path** — this is the point. Checking after the fact is a
linter; checking before the write is a firewall.

```bash
uv tool install git+https://github.com/Anandb71/Bleurs
bleurs install-hook
```

That registers a `PreToolUse` hook on `Write`, `Edit`, and `MultiEdit`. From
then on, when your agent proposes an edit, bleurs reconstructs what the file
*would* contain, verifies every external reference in it, and rejects the tool
call if any is provably fictional. The agent gets told exactly what was wrong
and usually fixes it on the next turn:

```
Bleurs blocked this edit. These references do not exist:
  - app.py:2  from fastapi_auth_helpers import verify - no package named 'fastapi_auth_helpers' exists on PyPI
  - app.py:5  json.loads_safe - json has no attribute 'loads_safe'

Each was checked against the real environment, not guessed.
Fix the reference or install the dependency, then retry.
```

Works with anything that can run a command on a JSON payload — Cursor, Codex,
your own agent loop. It reads a tool call on stdin and answers with an exit code.

**Put it in CI:**

```bash
bleurs check src --exclude demo
```

Exit 1 if anything was blocked. `--format json` for machine output, `--explain`
to see what it could *not* verify and why.

**Give your agent the index** — so it stops reading files to learn APIs:

```bash
claude mcp add bleurs -- bleurs mcp
```

Two tools: `surface` (what exists) and `verify` (what doesn't). More on the
first one below, because it's the half that changes how a session runs.

---

## Stop reading files to learn APIs

Compaction is a response to a symptom. The disease is that agents read whole
files to answer questions that a few lines would settle — and then, after being
compacted, read them again.

```bash
$ bleurs surface src/bleurs/hook.py --stats
```
```
hook  [module]
  # Claude Code PreToolUse adapter.
  ALLOW
  DENY
  run(argv: list[str] | None=None) -> int

~27 tokens vs ~1098 for the whole file — 41x smaller
```

The projection is lossy about implementation and **lossless about the
interface**, which is the only thing a caller reasons over. You do not need a
function's body to call the function correctly. You need its signature.

And unlike a summary, it is *derived rather than remembered*. It can be
recomputed from the code as it is right now, so it cannot drift, go stale, or be
forgotten expensively. That is the actual answer to "without compaction":

> **Context becomes a cache, not a ledger.**

You don't compact a cache. You miss, and refetch — and here a miss costs a few
hundred tokens instead of several thousand.

### Measured, not claimed

`benchmark/surface_savings.py` runs over **393 real third-party files** from
site-packages — deliberately not this repo, whose comment density would flatter
the result.

| | |
|---|---|
| Whole files | ~1,323,878 tokens |
| Projected surfaces | ~181,326 tokens |
| **Aggregate reduction** | **7.3x** |
| Per-file median | 6.6x |
| Per-file p25 / p75 | 4.7x / 10.7x |
| Worst / best case | 1.8x / 154.6x |

Reproduce it yourself:

```bash
python benchmark/surface_savings.py
```

Token counts are estimated at 4 chars/token rather than measured with a real
tokenizer, because shipping one would mean shipping a dependency. The estimate
applies identically to both sides of every ratio, so it cancels.

---

## What it catches, honestly compared

|  | invented package | invented API on a real library | invented helper in your own repo | tells you the name was **never published** | works on unwritten files | zero config |
|---|:--:|:--:|:--:|:--:|:--:|:--:|
| ruff / pyflakes | ✗ | ✗ | ✗ | ✗ | ✗ | ✓ |
| mypy / pyright | partial | ✓ | ✓ | ✗ | ✗ | ✗ |
| pip-audit / safety | ✗ | ✗ | ✗ | only known CVEs | ✗ | ✓ |
| running the tests | ✓ | ✓ | ✓ | ✗ | ✗ | — |
| **bleurs** | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |

Type checkers are genuinely good at most of this column, and if you already run
pyright in strict mode you have a lot of it covered. Two things they do not do.

**They cannot tell "not installed" from "never existed."** mypy says
`Cannot find implementation or library stub for module named 'X'` whether X is a
package you forgot to install or a name no human has ever published. That
distinction is the whole security story — one is a missing dependency, the other
is an open supply-chain hole waiting for someone to register it.

**They run after the file exists.** bleurs checks the proposed content of an
edit that has not happened yet, which is the only place you can actually stop it.

---

## The one rule

> **BLOCK requires positive evidence of absence.**

Not "we couldn't find it." Not "that looks wrong." Absence, demonstrated by a
named resolver that looked inside a container it successfully opened.

```mermaid
flowchart LR
    A["reference in the<br/>proposed edit"] --> B["resolve against<br/>five tiers"]
    B --> C{what happened}
    C -->|"found it"| D["ALLOW"]
    C -->|"opened the container,<br/>name was not in it"| E["BLOCK"]
    C -->|"could not open<br/>the container"| F["ALLOW<br/><i>and record why</i>"]

    style D fill:#1a7f37,stroke:#1a7f37,color:#fff
    style E fill:#b62324,stroke:#b62324,color:#fff
    style F fill:#7d4e00,stroke:#7d4e00,color:#fff
```

That third branch is the one everyone else collapses into the second, and it is
why their tools get switched off in week two.

The asymmetry is deliberate. A missed hallucination costs one failed test run
you were going to have anyway. A false positive costs you the tool — the user
disables it, and from that moment it catches nothing, forever. **Recall is worth
spending. Precision is not.**

<details>
<summary><b>Everything bleurs deliberately stays quiet about</b></summary>

<br>

- wildcard imports (`from x import *`) — the namespace becomes unknowable
- any name rebound anywhere in the file, in all six forms Python offers
- anything inside a `try/except` that would catch it failing
- anything inside a platform or version test (`sys.platform`, `os.name`, `platform.system()`, `sys.version_info`)
- modules with a PEP 562 `__getattr__` — they synthesize attributes on demand, so `dir()` proves nothing
- attribute chains rooted at anything but a module binding (`f().x`, `d["k"].y`)
- files that fail to parse — a syntax error is not a hallucination, and Python's own message is better than ours
- packages whose registry lookup failed for network reasons
- any project-local module it could not index

`bleurs check --explain` tells you which of these applied to your file. Silently
skipping a check is the one thing worse than a false positive.

</details>

---

## How it resolves things

Five tiers, cheapest and most certain first. Each answers *present*, *absent*,
or *I don't know* — and only the middle answer can block.

| Tier | Source of truth | Runs code? | Catches |
|---|---|:--:|---|
| **0 · local** | your project's files, parsed | no | helpers the agent invented in your own codebase |
| **1 · stdlib** | `sys.stdlib_module_names` | no | fake stdlib modules |
| **2 · env** | installed distribution metadata | no | packages that aren't there |
| **3 · introspect** | the real library object | **yes**, sandboxed | **fake APIs on real libraries** |
| **4 · registry** | PyPI, cached on disk | no | invented packages · slopsquatting bait |

Tier 3 is the one nothing else does, and it catches the failure mode that
survives code review: the import is fine, the library is real, and the method is
fiction.

**Tiers 0 and 3 are also what `surface` runs on.** Projecting an API and proving
one absent are the same operation read in opposite directions — which is why
both halves of this tool are one index rather than two systems bolted together.

It is also the only tier that executes third-party code, because there is no
other way to ask an object what it contains. That happens in an isolated
subprocess with a timeout, batched once per check run rather than once per
reference, and only for packages already installed in your environment — code
you were about to import anyway. `--no-introspect` turns it off, and bleurs then
says plainly that it verified no APIs rather than pretending the file is clean.

**Speed:** ~300 ms to check one file end-to-end, ~200 ms of which is Python
interpreter startup. Registry answers are cached on disk, so the network is hit
once per package name, ever.

---

## Why this doesn't get fixed by better models

A 2026 study measured package hallucination across five frontier models —
Claude Sonnet 4.6, Claude Haiku 4.5, GPT-5.4-mini, Gemini 2.5 Pro, DeepSeek
V3.2 — over 199,845 prompts.

| | |
|---|---|
| Hallucination rate | **4.62% – 6.10%** |
| Spread between best and worst model | 1.48 pts, down from 16.5 in the previous cohort |
| Models that beat GPT-4 Turbo's older ~3.6% | **none** |
| Package names invented *identically* by all five | **127** |

Everyone converged. Nobody improved. And those 127 shared inventions are exactly
what makes the attack economical: an adversary registers the name once and waits
for five different models to recommend it.

This is not a knowledge problem that scale fixes. It is a grounding problem, and
grounding is a job for a deterministic checker.

---

## Why not just…

<details>
<summary><b>…use mypy or pyright?</b></summary>

<br>

Do, they're excellent. They need configuration, they need stubs for untyped
dependencies, they benefit enormously from annotations, and they run on files
that exist. bleurs needs none of that and runs on the proposed edit.

More importantly, a type checker cannot distinguish a package you forgot to
install from one that was never published. That distinction is the difference
between a missing dependency and a supply-chain vulnerability.

They compose well: pyright for types, bleurs for grounding.

</details>

<details>
<summary><b>…use a RAG index or one of the code-graph MCP servers?</b></summary>

<br>

For finding code, they're good, and several are more capable at search than
bleurs is. Two differences.

**Embeddings retrieve what's similar; this retrieves what's true.** A vector
index returns chunks that look relevant, ranked by a similarity score, and a
chunk boundary can cut a signature in half. A surface is the complete public
API of a container, derived from the runtime object or the parse tree, with no
ranking step to be wrong about.

**Nobody else closes the loop.** A code-graph server answers when asked. It has
no idea the agent just wrote something false, because it isn't in the write
path. bleurs is, and that means the moment of failure is also the moment of
retrieval — which is the only moment you can be sure the agent actually needs
the information.

They compose fine. Use a graph server to find things; use this to be certain
about them.

</details>

<details>
<summary><b>…just run the code?</b></summary>

<br>

You will, and it will catch these. The question is when. An import error surfaces
at import time; an invented method three branches deep surfaces the first time
that branch runs, which may be in production. And by then the agent has written
four more files on top of the mistake.

The value is in the position, not the cleverness — 300 ms before the write beats
four minutes into a test run beats a week into staging.

</details>

<details>
<summary><b>…you import arbitrary packages to check them. For a security tool?</b></summary>

<br>

Fair, and it's the sharpest objection to the design.

The mitigation is scope: tier 3 only ever imports packages **already installed in
your environment** — code that was going to execute the moment you ran your
program. It does not install anything, does not touch the network, and never
imports a name it just learned about. Anything not installed is settled by
metadata and the registry, with no execution at all.

It runs in a subprocess launched with `-I` (isolated mode), with a timeout, so a
package that hangs, prints, or calls `sys.exit` on import cannot take the checker
with it. `--no-introspect` disables the tier entirely, and bleurs then reports
that it verified no APIs instead of implying the file was checked.

</details>

<details>
<summary><b>…won't this slow my agent down?</b></summary>

<br>

~300 ms per edit, most of it Python startup. Set against an agent turn measured
in seconds and a wrong-turn recovery measured in minutes, it is not close.

If it matters to you, `--offline --no-introspect` runs in ~200 ms and still
catches invented packages that aren't installed.

</details>

<details>
<summary><b>…what happens when it's wrong?</b></summary>

<br>

Two failure directions, treated very differently.

A **missed** hallucination is a normal miss — `--explain` will usually tell you
it abstained and why. Open an issue if it should have been catchable.

A **false positive** is the highest-priority bug class in this repo and gets
fixed before features. `tests/test_no_false_positives.py` is written first,
kept first, and gates every pull request.

</details>

---

## What this is not

- **Not a semantic checker.** It proves a symbol exists. It cannot tell you the
  right symbol is being used the wrong way. The 2026 AST-validation paper that
  measured this approach reports 100% precision and 87.6% recall on reference
  hallucinations, and **0% correction** on contextual mismatches. That boundary
  is real and bleurs sits firmly on one side of it.
- **Not a type checker.** Narrower question, no configuration, no annotations.
- **Not a replacement for conversation compaction.** It removes the dominant
  *cause* of context pressure in a coding session — file reads — and makes
  forgetting cheap to recover from. Your chat history is still your chat
  history.
- **Not a search engine.** `surface` answers "what does this contain" for a
  target you name. It will not find the target for you. Pair it with a
  code-search tool.
- **Not multi-language yet.** Python only. The front-end interface is already
  factored for tree-sitter backends — see the roadmap.

### The one place a false positive can still get in

If an import name is not installed, not stdlib, not project-local, not in
[`aliases.py`](src/bleurs/truth/aliases.py), and has no PyPI project of that
name, bleurs blocks it. The residual risk is a real package whose import name
differs from its distribution name and which is missing from that table —
`import cv2` shipping from `opencv-python`, but one nobody has listed yet.

That table is the highest-value contribution anyone can make here, and it is a
one-line PR. Run `--no-strict-imports` to downgrade every registry-based block
to a warning if you want the guarantee absolute.

---

## It found this bug in itself

On the very first CI run, the job that checks bleurs against its own source
failed on Linux:

```
BLOCK 44:19 ctypes.windll.kernel32 — ctypes has no attribute 'windll'
```

Correct evidence, wrong conclusion. `report.py` reaches for `ctypes.windll` to
enable ANSI colours on Windows, inside a `try/except`, and that attribute really
is absent on Linux. But the author had already said in code that it might be:

```python
try:
    return ctypes.windll.kernel32.GetStdHandle(-11)
except Exception:
    return None
```

Reporting that would be telling them something they knew, about code that is
correct. The fix generalized the old try/except-`ImportError` special case into
a full guarded-reference rule covering exception handlers matched per reference
kind and platform/version conditionals on both branches. Seven regression tests,
[three commits](https://github.com/Anandb71/Bleurs/commit/c64bcb7).

This is the class of bug that decides whether a tool like this is usable, and
the reason the dogfood job exists.

---

## Roadmap

- **tree-sitter front-ends** for TypeScript, Go, and Rust behind the existing
  `Analyzer` interface. npm has the same slopsquatting problem with a bigger
  blast radius. The hard part isn't parsing — it's finding what plays the role
  of `importlib.metadata` and runtime introspection in each language.
- **SCIP ingestion** so tier 0 consumes an existing [SCIP](https://scip-code.org/)
  index instead of walking the filesystem, inheriting real cross-file name
  resolution rather than reimplementing it badly.
- **A recall benchmark** against a public corpus of model-generated code, so
  "87.6%" becomes a number this repo measures instead of one it cites.
- **Task-scoped working sets.** `surface` answers one target at a time. The
  next step is projecting the whole set of modules a task actually touches into
  one budgeted context block, recomputed on demand instead of accumulated.
- **Grounded generation.** The end state, and the reason the original design
  notes existed. Once you can enumerate exactly what a piece of code is allowed
  to reference, you can hand that set to the model *before* it writes rather
  than checking afterwards — the difference between a spell-checker and a
  keyboard that only has real words on it.

---

## Prior art, and credit where it's owed

bleurs is a small idea standing on a lot of other people's work. If you are
building in this space, read these before you read my code.

| | |
|---|---|
| [**SCIP**](https://sourcegraph.com/blog/announcing-scip) · Sourcegraph | The code-intelligence index format this should consume rather than reinvent. Also [LSIF](https://lsif.dev/), its predecessor. |
| [**Stack Graphs**](https://arxiv.org/pdf/2211.01224) · GitHub | Incremental name resolution at scale — the right answer to what tier 0 does crudely. |
| [**Glean**](https://glean.software/) · Meta, and **Kythe** · Google | Typed, schema-defined fact databases about source code. |
| [**Coccinelle**](https://lwn.net/Articles/315686/) | Semantic patches for C. Twenty years ahead of the current conversation about AST-level edits. |
| [**OpenRewrite**](https://docs.openrewrite.org/) · Moderne | Lossless semantic trees and recipe-based transformation. |
| [**Automated Software Transplantation**](https://dl.acm.org/doi/10.1145/2771783.2771796) · Barr, Harman, Jia, Marginean, Petke (ISSTA 2015) | µSCALPEL moved the H.264 codec from x264 into VLC automatically, in 26 hours against 20 days by hand. Required reading for anyone who thinks "retrieve the code instead of generating it" is a new idea. |
| [**ast-grep**](https://ast-grep.github.io/) and [**tree-sitter**](https://tree-sitter.github.io/) | What the non-Python front-ends will be built on. |
| [**Serena**](https://github.com/oraios/serena) | LSP-backed semantic tooling for agents; solves the retrieval half of this problem well. |
| [arXiv:2601.19106](https://arxiv.org/abs/2601.19106) | *Detecting and Correcting Hallucinations in LLM-Generated Code via Deterministic AST Analysis* — the introspection-based validation approach tier 3 implements, and the source of the precision/recall figures above. |
| [arXiv:2605.17062](https://arxiv.org/abs/2605.17062) | *The Range Shrinks, the Threat Remains* — the 2026 frontier-cohort package hallucination measurements. |

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). The short version: every pull request
must keep `tests/test_no_false_positives.py` green. If your change catches more
hallucinations but makes bleurs blame correct code, the change is wrong.

Good first contributions: a missing pair in [`aliases.py`](src/bleurs/truth/aliases.py),
a false positive you hit in the wild, or a language front-end.

## License

MIT © [Anand Biju](https://github.com/Anandb71)
