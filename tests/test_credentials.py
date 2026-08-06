"""Writes refuse credentials (spec §41).

Token fixtures are built by joining pieces, so no whole realistic token sits in the
repository (GitHub push protection blocks those).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from mnemo.credentials import find_credential
from mnemo.direct import DirectMemory
from mnemo.errors import ErrorCode, MnemoError

RANDOM = "aB3dE5fG7hJ9kL2mN4pQ6rS8tU0vW1xY"  # 32 mixed-case letters and digits

GITHUB = "gh" + "p_" + RANDOM + "Zz9q"
GITHUB_PAT = "github" + "_pat_" + RANDOM
OPENAI = "s" + "k-proj-" + RANDOM
AWS = "AK" + "IA" + "Q7RT2WX9BN4KM3PZ"
PEM = "-----BEGIN " + "OPENSSH PRIVATE KEY-----"
REMOTE_URL = "https://deploy:" + RANDOM[:20] + "@git.example.com/repo.git"
ASSIGNMENT = "DB_PASS" + "WORD=" + "q8Lm2Vx7Rt4Kp9Zw"

MUST_SAVE = [
    "keys look like sk-test-1234",
    "GitHub tokens start with ghp_",
    "the key lives in OPENAI_API_KEY in .env",
    "AWS docs use AKIAIOSFODNN7EXAMPLE",
    "branch ids look like sk-feature-flag-rollout-config-v2",
    "DATABASE_URL=postgres://app:<password>@db.internal/app",
    "postgres://postgres:postgres@localhost:5432/app",
    "redis://default:s3cr3tPass99@127.0.0.1:6379",
    "OPENAI_API_KEY=$OPENAI_API_KEY",
    "API_KEY=<your key>",
    "SECRET_KEY=${DJANGO_SECRET}",
    "tokenizer: bge-small-en-v1.5",
    "max_tokens: 4096",
    "token: TOKEN_FROM_ENV",
    "s" + "k-" + "x" * 40,
    "use https://user:password@host.example.com in the docs",
    "the Slack bot token is in 1Password under 'ci-bot'",
    "token = uuid4().hex[:8]",
]

MUST_REFUSE = [
    (GITHUB, "github_token"),
    (f"push with {GITHUB_PAT} please", "github_token"),
    (OPENAI, "openai_or_anthropic_key"),
    ("s" + "k-" + "0123456789abcdef" * 2, "openai_or_anthropic_key"),
    ("s" + "k-or-v1-" + "0123456789abcdef" * 4, "openai_or_anthropic_key"),
    (f"key={AWS}", "aws_access_key"),
    (f"{PEM}\nb3BlbnNzaC1rZXktdjEAAAAA", "private_key"),
    (REMOTE_URL, "url_password"),
    (ASSIGNMENT, "credential_assignment"),
    ("glpat" + "-" + RANDOM[:20], "gitlab_token"),
    ("xox" + "b-1234567890-" + RANDOM[:16], "slack_token"),
    ("h" + "f_" + RANDOM, "huggingface_token"),
    ("AI" + "za" + RANDOM + "_-x", "google_api_key"),
    ("s" + "k_live_" + RANDOM[:24], "stripe_key"),
    ("m" + "0-" + RANDOM[:24], "mem0_key"),
]


@pytest.mark.parametrize("text", MUST_SAVE)
def test_harmless_text_is_not_a_credential(text):
    assert find_credential(text) is None


@pytest.mark.parametrize(("text", "kind"), MUST_REFUSE)
def test_credentials_are_found_by_kind(text, kind):
    assert find_credential(text) == kind


def _assert_hidden(error: MnemoError, secret: str) -> None:
    """No piece of the secret's random part reaches the message or the details."""
    shown = error.message + repr(error.details)
    for start in range(0, len(secret) - 8):
        assert secret[start : start + 8] not in shown


async def _counts(store):
    return tuple(
        await store.conn.fetchrow(
            "SELECT (SELECT count(*) FROM memory_event), "
            "(SELECT count(*) FROM memory_mutation_receipt), "
            "(SELECT count(*) FROM memory_embedding)"
        )
    )


@pytest.mark.parametrize("field", ["subject", "predicate", "value"])
async def test_create_refuses_a_credential_and_writes_nothing(store, field):
    d = DirectMemory(store)
    args = {"subject": "ci", "predicate": "deploy_token", "value": "in the vault"}
    args[field] = f"the token is {GITHUB}"
    request_id = uuid4()
    before = await _counts(store)
    with pytest.raises(MnemoError) as caught:
        await d.create(args["subject"], args["predicate"], args["value"], request_id=request_id)
    error = caught.value
    assert error.code == ErrorCode.INVALID_INPUT
    assert error.details == {"field": field, "kind": "github_token"}
    assert "placeholder" in error.message
    _assert_hidden(error, GITHUB)
    assert await _counts(store) == before  # no event, receipt or embedding

    # The same request, with the secret left out, goes through.
    args[field] = "in the vault as CI_DEPLOY_TOKEN"
    result = await d.create(
        args["subject"], args["predicate"], args["value"], request_id=request_id
    )
    assert result.status == "applied"


async def test_update_and_correct_refuse_a_credential(store):
    d = DirectMemory(store)
    created = await d.create("db", "url", "postgres://localhost/app", request_id=uuid4())
    for write in (d.update, d.correct):
        with pytest.raises(MnemoError) as caught:
            await write(
                created.fact_id,
                REMOTE_URL,
                expected_event_id=created.event_id,
                request_id=uuid4(),
            )
        assert caught.value.details == {"field": "value", "kind": "url_password"}
        _assert_hidden(caught.value, RANDOM[:20])
    head = await d.get(created.fact_id)
    assert head.current_event_id == created.event_id


async def test_retract_refuses_a_credential_in_its_reason(store):
    d = DirectMemory(store)
    created = await d.create("ci", "deploy_key", "in the vault", request_id=uuid4())
    with pytest.raises(MnemoError) as caught:
        await d.retract(
            created.fact_id,
            expected_event_id=created.event_id,
            request_id=uuid4(),
            reason=f"leaked {OPENAI}",
        )
    assert caught.value.details == {"field": "reason", "kind": "openai_or_anthropic_key"}
    assert "without a reason" in caught.value.message
    _assert_hidden(caught.value, OPENAI)
    result = await d.retract(
        created.fact_id, expected_event_id=created.event_id, request_id=uuid4()
    )
    assert result.status == "applied"


async def test_source_span_is_checked(store):
    with pytest.raises(MnemoError) as caught:
        await DirectMemory(store).create(
            "ci",
            "deploy_key",
            "in the vault",
            request_id=uuid4(),
            source_span={"file": ".env", "text": ASSIGNMENT},
        )
    assert caught.value.details == {"field": "source_span", "kind": "credential_assignment"}


async def test_an_agent_over_stdio_gets_a_structured_refusal(_disposable_test_db, clean_memory):
    import asyncio

    from tests.test_mcp_shared_scope_stdio import WIDGET, agent, create, payload, server_env

    env = server_env(_disposable_test_db, "cred-" + uuid4().hex, "claude-code", WIDGET)
    async with asyncio.timeout(60):
        async with agent(env) as claude:
            result = await claude.call_tool(
                "memory_create", create("ci", "deploy_token", GITHUB, source="user")
            )
            refused = payload(result, code="INVALID_INPUT")
            assert refused["details"] == {"field": "value", "kind": "github_token"}
            assert RANDOM[:8] not in result.content[0].text
            saved = payload(
                await claude.call_tool(
                    "memory_create",
                    create("ci", "deploy_token", "in CI_DEPLOY_TOKEN", source="user"),
                )
            )
            assert saved["status"] == "applied"
