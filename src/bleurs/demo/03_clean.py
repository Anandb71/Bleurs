# Correct code. The only interesting thing a checker can do here is shut up.
#
# This file is the real test. Catching hallucinations is easy if you are willing
# to be wrong sometimes; the whole difficulty is staying silent on code like
# this, which is full of the patterns that make naive checkers fire: dynamic
# attribute access, a shadowed module name, a guarded optional import, and a
# submodule that is not an attribute of its parent until something imports it.
import base64
import datetime
import json
import os.path
from collections import defaultdict

try:
    import tomllib
except ImportError:  # Python 3.10
    tomllib = None


def load_config(path):
    with open(path, "rb") as handle:
        if tomllib is not None:
            return tomllib.load(handle)
    return {}


def summarize(events):
    buckets = defaultdict(list)
    for event in events:
        buckets[event["kind"]].append(event)

    return {
        "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "counts": {kind: len(items) for kind, items in buckets.items()},
    }


def encode(payload):
    return base64.b64encode(json.dumps(payload).encode()).decode()


def relocate(source, target):
    # `os.path` is a submodule reached through the `os` binding -- a case that
    # breaks checkers which assume attributes and submodules are the same thing.
    return os.path.join(os.path.dirname(target), os.path.basename(source))


def render(template, **values):
    # `json` is rebound below, so no attribute claim through it is trustworthy
    # from here on. The correct behaviour is to stop checking it, not to guess.
    json = template.get("renderer")
    return json.render(**values) if json else ""
