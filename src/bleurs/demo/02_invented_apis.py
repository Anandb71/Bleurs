# Every module here is real and installed. Every highlighted call is not.
# This is the failure mode no package checker catches: the import is fine,
# the method is fiction, and it looks completely plausible on review.
import base64
import datetime
import json
import os.path
from pathlib import Path


def load_config(path):
    # `json.loads_safe` does not exist. `json.loads` does.
    return json.loads_safe(Path(path).read_text())


def stamp():
    # `datetime.datetime.now_utc` does not exist. `now(datetime.UTC)` does.
    return datetime.datetime.now_utc().isoformat()


def archive_name(parts):
    # `os.path.join_all` does not exist. `os.path.join(*parts)` does.
    return os.path.join_all(parts)


def encode(payload):
    # `base64.encode_string` does not exist. `b64encode` does.
    return base64.encode_string(payload)
