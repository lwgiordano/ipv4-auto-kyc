"""Re-audit `82636da..9ac574f` F1: `hmac_inbound_extra_keys` entries are FULL v2 verification
credentials, and the production boundary used to validate only the active pair.

`api/auth._inbound_secret` resolves a v2 secret out of this mapping, so a one-character rotation
secret — or one filed under an empty key id, which an absent `X-KYC-Key-Id` header resolves to —
authenticated real requests while the boot kill switch reported a clean production config. The
floor is now enforced in both layers: the field validator refuses at construction, and
`production_config_violations` re-checks so an unvalidated `model_copy(update=...)` cannot smuggle
a mapping past the boundary.
"""

import pytest
from pydantic import ValidationError

from kyc_tool.api.auth import _inbound_secret
from kyc_tool.config import Settings, hmac_extra_key_violations, production_config_violations

from .test_production_config import hardened

ACTIVE = "kyc-platform-1"
STRONG = "r" * 32


def _extra(mapping):
    return {"hmac_inbound_key_id": ACTIVE, "hmac_inbound_extra_keys": mapping}


# ── construction layer ────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("mapping", "needle"),
    [
        ({"old": "x"}, "weak"),
        ({"old": "s" * 31}, "weak"),  # one char under the floor
        ({"": STRONG}, "blank/whitespace key_id"),
        ({"   ": STRONG}, "blank/whitespace key_id"),
        ({" padded ": STRONG}, "surrounding whitespace"),
        ({ACTIVE: STRONG}, "collides with the ACTIVE"),
    ],
)
def test_construction_refuses_unsafe_rotation_keys(mapping, needle):
    with pytest.raises(ValidationError, match=needle):
        Settings(**_extra(mapping))


@pytest.mark.parametrize("mapping", [{"old": None}, {7: STRONG}, {"old": 42}])
def test_construction_refuses_non_string_entries(mapping):
    """Pydantic's own dict[str, str] coercion is the authority at this layer, so the message is
    its type error rather than ours. The mapping is refused either way; our type branch below
    covers the boundary path, where model_copy skips Pydantic entirely."""
    with pytest.raises(ValidationError):
        Settings(**_extra(mapping))


@pytest.mark.parametrize("mapping", [{"old": None}, {7: STRONG}, "not-a-mapping", 5])
def test_boundary_catches_non_string_entries_smuggled_by_copy(mapping):
    smuggled = hardened().model_copy(update={"hmac_inbound_extra_keys": mapping})
    violations = production_config_violations(smuggled)
    assert any("str -> str" in v or "must be a mapping" in v for v in violations), violations


def test_malformed_mapping_is_refused():
    with pytest.raises(ValidationError):
        Settings(**_extra(["not", "a", "mapping"]))


def test_a_valid_rotation_pair_is_accepted():
    settings = Settings(**_extra({"kyc-platform-0": STRONG}))
    assert settings.hmac_inbound_extra_keys == {"kyc-platform-0": STRONG}


def test_empty_mapping_is_the_normal_state():
    assert Settings(hmac_inbound_key_id=ACTIVE).hmac_inbound_extra_keys == {}
    assert hmac_extra_key_violations({}, ACTIVE) == []
    assert hmac_extra_key_violations(None, ACTIVE) == []


# ── production boundary (the model_copy bypass) ───────────────────────────────────────────────────
def test_production_boundary_catches_the_model_copy_bypass():
    """Codex's exact reproduction: a hardened production config reported zero violations while
    `_inbound_secret` handed out a one-byte secret and an empty-key-id secret."""
    smuggled = hardened().model_copy(
        update={"hmac_inbound_extra_keys": {"old": "x", "": "y"}}
    )
    violations = production_config_violations(smuggled)
    assert any("weak" in v for v in violations), violations
    assert any("blank/whitespace key_id" in v for v in violations), violations


def test_hardened_production_with_a_strong_rotation_key_still_boots():
    settings = hardened(hmac_inbound_extra_keys={"kyc-platform-0": STRONG})
    assert production_config_violations(settings) == []


def test_production_boundary_catches_a_collision_smuggled_by_copy():
    smuggled = hardened().model_copy(
        update={"hmac_inbound_extra_keys": {"kyc-platform-1": STRONG}}
    )
    assert any("collides" in v for v in production_config_violations(smuggled))


# ── the credential really is live, which is why the floor matters ─────────────────────────────────
def test_rotation_key_resolves_a_verification_secret():
    """The property that makes a weak entry dangerous rather than cosmetic: auth resolves it."""
    settings = Settings(**_extra({"kyc-platform-0": STRONG}))
    assert _inbound_secret(settings, "kyc-platform-0") == STRONG
    assert _inbound_secret(settings, ACTIVE) == settings.hmac_inbound_secret
    assert _inbound_secret(settings, "never-issued") == ""


def test_both_keys_verify_during_overlap_and_only_the_new_one_after_retirement():
    """The rotation procedure the documents promise: add, cut over, retire."""
    overlap = Settings(
        hmac_inbound_key_id="kyc-platform-2",
        hmac_inbound_secret="n" * 32,
        hmac_inbound_extra_keys={"kyc-platform-1": STRONG},
    )
    assert _inbound_secret(overlap, "kyc-platform-2") == "n" * 32
    assert _inbound_secret(overlap, "kyc-platform-1") == STRONG

    retired = overlap.model_copy(update={"hmac_inbound_extra_keys": {}})
    assert _inbound_secret(retired, "kyc-platform-2") == "n" * 32
    assert _inbound_secret(retired, "kyc-platform-1") == ""
