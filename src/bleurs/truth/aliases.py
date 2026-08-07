"""Import names that do not match their distribution name.

This table is a false-positive guard, and it is the single most important
correctness artifact in the project.

The reasoning: when a module is not installed, the only remaining evidence is
whether a package by that name exists on PyPI. But `import yaml` comes from
`PyYAML` and `import cv2` comes from `opencv-python` -- neither import name is
a PyPI project. Without this table, an uninstalled-but-completely-real import
looks exactly like a hallucination, and we would block valid code. Any name
appearing as a key here is known-real and is never blocked, whatever the
registry says.

The table is necessarily incomplete, and that incompleteness is the one place
a false positive can still enter. It is documented in the README rather than
hidden, and `--strict-imports=false` downgrades every registry-based block to a
warning for anyone who wants the guarantee absolute.

Additions welcome; this is the highest-value PR anyone can send.
"""

from __future__ import annotations

#: import name -> pip install name
IMPORT_TO_DISTRIBUTION: dict[str, str] = {
    # -- the classics
    "yaml": "PyYAML",
    "cv2": "opencv-python",
    "sklearn": "scikit-learn",
    "skimage": "scikit-image",
    "PIL": "Pillow",
    "bs4": "beautifulsoup4",
    "dateutil": "python-dateutil",
    "dotenv": "python-dotenv",
    "serial": "pyserial",
    "usb": "pyusb",
    "OpenSSL": "pyOpenSSL",
    "Crypto": "pycryptodome",
    "Cryptodome": "pycryptodomex",
    "jwt": "PyJWT",
    "jose": "python-jose",
    "nacl": "PyNaCl",
    "git": "GitPython",
    "magic": "python-magic",
    "docx": "python-docx",
    "pptx": "python-pptx",
    "xlsxwriter": "XlsxWriter",
    "fitz": "PyMuPDF",
    "pymupdf": "PyMuPDF",
    "Levenshtein": "python-Levenshtein",
    "slugify": "python-slugify",
    "multipart": "python-multipart",
    "memcache": "python-memcached",
    "snappy": "python-snappy",
    "nmap": "python-nmap",
    "ulid": "python-ulid",
    "socks": "PySocks",
    "dns": "dnspython",
    "Bio": "biopython",
    "zmq": "pyzmq",
    "OpenGL": "PyOpenGL",
    "wx": "wxPython",
    "gi": "PyGObject",
    "cairo": "pycairo",
    "Xlib": "python-xlib",
    "av": "PyAV",
    "ffmpeg": "ffmpeg-python",
    "yt_dlp": "yt-dlp",
    "speech_recognition": "SpeechRecognition",
    "telebot": "pyTelegramBotAPI",
    "discord": "discord.py",
    "faker": "Faker",
    "factory": "factory_boy",
    "attr": "attrs",
    "IPython": "ipython",
    "Cython": "Cython",
    # -- databases
    "MySQLdb": "mysqlclient",
    "psycopg2": "psycopg2-binary",
    "bson": "pymongo",
    "gridfs": "pymongo",
    "sqlalchemy": "SQLAlchemy",
    # -- windows
    "win32api": "pywin32",
    "win32con": "pywin32",
    "win32com": "pywin32",
    "win32gui": "pywin32",
    "win32file": "pywin32",
    "pythoncom": "pywin32",
    "pywintypes": "pywin32",
    # -- google / cloud namespace packages
    "googleapiclient": "google-api-python-client",
    "apiclient": "google-api-python-client",
    "google": "protobuf",
    "google_auth_oauthlib": "google-auth-oauthlib",
    "firebase_admin": "firebase-admin",
    # -- flask / django plugin naming
    "flask_cors": "Flask-Cors",
    "flask_sqlalchemy": "Flask-SQLAlchemy",
    "flask_login": "Flask-Login",
    "flask_migrate": "Flask-Migrate",
    "rest_framework": "djangorestframework",
    "corsheaders": "django-cors-headers",
    "environ": "django-environ",
    # -- ml / ai
    "faiss": "faiss-cpu",
    "sentence_transformers": "sentence-transformers",
    "huggingface_hub": "huggingface-hub",
    "qdrant_client": "qdrant-client",
    "langchain_core": "langchain-core",
    "langchain_community": "langchain-community",
    "langchain_openai": "langchain-openai",
    "tf_keras": "tf-keras",
    "mpl_toolkits": "matplotlib",
    "pandas_datareader": "pandas-datareader",
    # -- misc packaging quirks
    "pkg_resources": "setuptools",
    "setuptools_scm": "setuptools-scm",
    "importlib_metadata": "importlib-metadata",
    "typing_extensions": "typing-extensions",
    "charset_normalizer": "charset-normalizer",
    "requests_toolbelt": "requests-toolbelt",
    "ruamel": "ruamel.yaml",
    "paho": "paho-mqtt",
    "past": "future",
    "concurrent_log_handler": "concurrent-log-handler",
    "dotenv_linter": "dotenv-linter",
    "jaraco": "jaraco.classes",
    "backports": "backports.tarfile",
    "ordered_set": "ordered-set",
    "more_itertools": "more-itertools",
    "pkginfo": "pkginfo",
    "zoneinfo": "backports.zoneinfo",
}

#: Namespace roots whose submodules come from many different distributions.
#: `import azure.storage.blob` must never be judged by looking up "azure".
NAMESPACE_ROOTS: frozenset[str] = frozenset(
    {
        "azure",
        "google",
        "ruamel",
        "jaraco",
        "backports",
        "zope",
        "sphinxcontrib",
        "paste",
        "repoze",
        "mypy_extensions",
        "opentelemetry",
        "oslo",
        "nvidia",
        "ansible_collections",
    }
)


def known_import_name(top_level: str) -> bool:
    """True when we know this import name is real regardless of the registry."""
    return top_level in IMPORT_TO_DISTRIBUTION or top_level in NAMESPACE_ROOTS


def install_name(top_level: str) -> str:
    """What the user should actually `pip install` to get this import."""
    return IMPORT_TO_DISTRIBUTION.get(top_level, top_level)
