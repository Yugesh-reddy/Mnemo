"""Refuse credentials in memory writes (spec §41).

A memory can never be erased: retract keeps it in history, and history, ``memory_get``
with an event id and export all return it. So text shaped like a credential is
refused before it is embedded or written. The patterns are deterministic and stricter
than the installer preview's (§40), which hides anything token-shaped because hiding
too much there costs nothing, while refusing a harmless memory costs the agent a retry.
"""

from __future__ import annotations

import re

from mnemo.errors import ErrorCode, MnemoError

_START = r"(?<![A-Za-z0-9_-])"  # a token starts at a word edge; "-" counts as inside


def _token(prefix: str, body: str) -> re.Pattern[str]:
    return re.compile(rf"{_START}{prefix}(?P<body>{body})")


def _random_run(body: str) -> bool:
    """A hyphen-free run of 20 or more characters with a digit: every OpenAI and
    Anthropic key has one, and a kebab-case name such as sk-feature-flag-... does not."""
    return any(len(run) >= 20 and _has_digit(run) for run in body.split("-"))


def _has_digit(body: str) -> bool:
    return any(c.isdigit() for c in body)


def _letter_and_digit(body: str) -> bool:
    return _has_digit(body) and any(c.isalpha() for c in body)


# (kind, pattern, extra test on the random part, or None)
_TOKENS = (
    ("github_token", _token("gh[pousr]_", r"[A-Za-z0-9]{30,}"), None),
    ("github_token", _token("github_pat_", r"[A-Za-z0-9_]{22,}"), None),
    ("gitlab_token", _token("glpat-", r"[\w-]{20,}"), None),
    ("stripe_key", _token("[sr]k_live_", r"[A-Za-z0-9]{20,}"), None),
    ("openai_or_anthropic_key", _token("sk-", r"[\w-]{20,}"), _random_run),
    ("slack_token", _token("xox[abpr]-", r"[\w-]{20,}"), None),
    ("huggingface_token", _token("hf_", r"[A-Za-z0-9]{30,}"), None),
    ("mem0_key", _token("m0-", r"[\w-]{20,}"), _has_digit),
    ("aws_access_key", _token("AKIA", r"[0-9A-Z]{16}(?![A-Za-z0-9])"), None),
    ("google_api_key", _token("AIza", r"[\w-]{30,}"), None),
)
_PRIVATE_KEY = re.compile(r"-----BEGIN (?:(?:RSA|EC|DSA|OPENSSH|ENCRYPTED) )?PRIVATE KEY-----")
_URL_PASSWORD = re.compile(
    r"[A-Za-z][A-Za-z0-9+.-]*://[^\s/:@]+:(?P<body>[^\s/@]+)@(?P<host>\[[^\]\s]*\]|[^\s/:@?#]+)"
)
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]", "host.docker.internal"})
# KEY=value or key: value, where the name ends in a credential word: DB_PASSWORD,
# client_secret, GITHUB_TOKEN, SECRET_KEY. "tokenizer" and "max_tokens" do not end in one.
_ASSIGNMENT = re.compile(
    r"(?<![\w.-])[\w.-]*(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|"
    r"private[_-]?key|secret[_-]?key)[\"']?\s*[:=]\s*[\"']?(?P<body>[^\s\"',;]{12,})",
    re.I,
)
_CODE = re.compile(r"[()\[\]{}]")
_DUMMY = re.compile(r"example|dummy|fake|placeholder|redacted", re.I)
_PLACEHOLDER_WORDS = frozenset({"password", "passwd", "pass", "secret", "changeme", "token"})


def _placeholder(body: str) -> bool:
    """A stand-in, not a credential: <token>, ${VAR}, $VAR, ***, xxxx, one repeated
    character, or a value naming itself as an example."""
    core = body.strip("-_")
    return (
        not core
        or core[0] in "<${%"
        or len(set(core.lower())) == 1
        or core.lower() in _PLACEHOLDER_WORDS
        or core.lower().startswith("your")
        or _DUMMY.search(core) is not None
    )


def find_credential(text: str) -> str | None:
    """The kind of the first credential in ``text``, or None. Never the credential."""
    for kind, pattern, check in _TOKENS:
        for match in pattern.finditer(text):
            body = match["body"]
            if not _placeholder(body) and (check is None or check(body)):
                return kind
    if _PRIVATE_KEY.search(text):
        return "private_key"
    for match in _URL_PASSWORD.finditer(text):
        if match["host"].lower() not in _LOCAL_HOSTS and not _placeholder(match["body"]):
            return "url_password"
    for match in _ASSIGNMENT.finditer(text):
        body = match["body"]
        # Code such as `token = uuid4().hex[:8]` is not a credential.
        if _letter_and_digit(body) and not _CODE.search(body) and not _placeholder(body):
            return "credential_assignment"
    return None


def refuse_credentials(field: str, text: str, *, retry: str | None = None) -> None:
    """Raise INVALID_INPUT naming ``field`` and the credential's kind if ``text`` holds
    one. The message and details never contain any part of the credential."""
    kind = find_credential(text)
    if kind is None:
        return
    raise MnemoError(
        ErrorCode.INVALID_INPUT,
        f"{field} looks like a credential ({kind}). Memories cannot hold secrets: "
        + (
            retry
            or "save where it lives instead (an environment variable's name or a file "
            "path) or use a placeholder such as <token>."
        ),
        field=field,
        kind=kind,
    )
