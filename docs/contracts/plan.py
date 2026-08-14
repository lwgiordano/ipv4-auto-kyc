"""Typed cutover plans: the last authored-prose surface in `Procedure`, closed (Wave 1 / F4a).

Codex inverted every bundle-activation prerequisite — start with the flag ON, skip the seed and
read-back, ignore an unavailable historical bundle, keep the old workers rolling — and every
verifier passed, because `when` and `blocks_start` were free prose checked by keyword presence,
and a sentence can keep all its keywords while reversing its meaning.

The prose is no longer authorable. A plan is a PHASE plus prerequisites drawn from a CLOSED set of
typed kinds; each kind owns its published sentence (its parameters are typed slots, not text), the
phase owns which kinds are required and in what order, and per-phase role floors are data. The
inversions therefore stop being detectable defects and become unconstructable ones:

  * the flag-off prerequisite HAS no state parameter — "start with the flag on" has no syntax;
  * omitting the seed/read-back, the backlog preflight, or the stop-and-attest fails the phase's
    required-kind set at construction;
  * reordering retention-suspension after the diagnostic fails the phase's canonical order;
  * shrinking the stop-set below the phase's role floor fails the floor.

What remains authored: a procedure's short SUBJECT (a noun phrase, length-capped and refused if it
tries to be a sentence) and each prerequisite's EVIDENCE quote, which must appear verbatim in the
digest-bound playbook section — the same discipline as rollback facts. The executable bindings —
the flag's Settings default, the role vocabulary against `ProcessRole`, the preflight and
diagnostic commands against the ref's typed command records, 024 still PENDING — live in the
release verifier.
"""

from dataclasses import dataclass

# ── the closed role vocabulary ────────────────────────────────────────────────────────────────
# Mirrors kyc_tool.config.ProcessRole. This module stays import-pure (no kyc_tool import), so the
# release verifier asserts the two vocabularies are EQUAL — a role added to the enum forces a
# decision here, and a typo here cannot survive against the enum.
PLAN_ROLES = ("api", "pipeline_worker", "outbox_worker", "dev_worker", "retention")

# ── phases ────────────────────────────────────────────────────────────────────────────────────
PHASE_SECURITY_FULL_WINDOW = "security_full_window"
PHASE_FLAG_ACTIVATION = "flag_activation"
PHASE_SCHEMA_MAINTENANCE = "schema_maintenance"
PHASES = (PHASE_SECURITY_FULL_WINDOW, PHASE_FLAG_ACTIVATION, PHASE_SCHEMA_MAINTENANCE)

# ── platform commitments (closed) ─────────────────────────────────────────────────────────────
COMMIT_PAUSE_BUFFER = "pause_and_buffer"
COMMIT_RESIGN_RETRIES = "resign_retries"
_COMMITMENT_SENTENCES = {
    COMMIT_PAUSE_BUFFER: "pause and buffer every event type for the window",
    COMMIT_RESIGN_RETRIES: "re-sign each retry with a fresh timestamp against the same "
                           "Idempotency-Key",
}

# ── stop-set rationales (closed, phase-owned) ─────────────────────────────────────────────────
_STOP_RATIONALES = {
    PHASE_FLAG_ACTIVATION: "A rolling flip lets peers resolve different bundles for the same run.",
    PHASE_SCHEMA_MAINTENANCE: "Not only the publishers: the API and the pipeline workers write "
                              "too.",
}


def _require_evidence(kind: str, evidence: str) -> None:
    if type(evidence) is not str or not evidence.strip():
        raise ValueError(f"{kind}: a prerequisite must quote the playbook sentence it came from")


@dataclass(frozen=True)
class PinnedImage:
    """The reviewed image exists, its digest is recorded, and every step runs pinned to it."""

    evidence: str
    recovery_module_only_in_new: bool = False

    def __post_init__(self) -> None:
        _require_evidence("PinnedImage", self.evidence)

    @property
    def sentence(self) -> str:
        base = ("The reviewed image is published and its digest recorded; every step that runs "
                "code is pinned to that digest")
        if self.recovery_module_only_in_new:
            return base + ", and the recovery module exists only in the new image."
        return base + "."


@dataclass(frozen=True)
class PlatformWrittenCommitment:
    """The platform confirmed, in writing, a closed set of window commitments."""

    commitments: tuple[str, ...]
    evidence: str

    def __post_init__(self) -> None:
        _require_evidence("PlatformWrittenCommitment", self.evidence)
        unknown = [c for c in self.commitments if c not in _COMMITMENT_SENTENCES]
        if unknown or not self.commitments:
            raise ValueError(f"unknown or empty commitments: {unknown}")

    @property
    def sentence(self) -> str:
        parts = [_COMMITMENT_SENTENCES[c] for c in self.commitments]
        return ("The platform has confirmed IN WRITING that it will "
                + ", and ".join(parts) + ".")


@dataclass(frozen=True)
class TrustedPathProbes:
    evidence: str

    def __post_init__(self) -> None:
        _require_evidence("TrustedPathProbes", self.evidence)

    @property
    def sentence(self) -> str:
        return ("You can probe each new replica directly on its trusted path, not through the "
                "load balancer: an edge 403 or 503 must never be read as an application answer.")


@dataclass(frozen=True)
class FlagStillOff:
    """The rolling part is deployed and the activation flag is still OFF.

    There is deliberately NO state parameter: "start with the flag on" is not a value this type
    can hold. The flag's environment name is a typed slot the verifier binds to the real Settings
    field and its default.
    """

    flag_env: str
    evidence: str

    def __post_init__(self) -> None:
        _require_evidence("FlagStillOff", self.evidence)
        if not self.flag_env.startswith("KYC_"):
            raise ValueError("flag_env must be the KYC_ environment name of a Settings field")

    @property
    def sentence(self) -> str:
        return f"The rolling part is already deployed and {self.flag_env} is still OFF."


@dataclass(frozen=True)
class SeedAndReadBack:
    evidence: str

    def __post_init__(self) -> None:
        _require_evidence("SeedAndReadBack", self.evidence)

    @property
    def sentence(self) -> str:
        return "The policy bundle is seeded and read back successfully."


@dataclass(frozen=True)
class BacklogPreflight:
    evidence: str

    def __post_init__(self) -> None:
        _require_evidence("BacklogPreflight", self.evidence)

    @property
    def sentence(self) -> str:
        return ("The pinnable-backlog preflight is green: an unavailable historical bundle "
                "dead-letters rather than being silently skipped.")


@dataclass(frozen=True)
class RetentionSuspendedAttested:
    evidence: str

    def __post_init__(self) -> None:
        _require_evidence("RetentionSuspendedAttested", self.evidence)

    @property
    def sentence(self) -> str:
        return ("Retention is suspended, every active retention task has exited, and you hold "
                "orchestrator evidence that zero are running.")


@dataclass(frozen=True)
class PreWindowDiagnostic:
    evidence: str

    def __post_init__(self) -> None:
        _require_evidence("PreWindowDiagnostic", self.evidence)

    @property
    def sentence(self) -> str:
        return ("The pre-window diagnostic has run under that suspension and is green. It runs "
                "BEFORE any outage precisely so a missing authoritative callback is found while "
                "there is still time to restore it.")


@dataclass(frozen=True)
class AuthoritativeBackup:
    evidence: str

    def __post_init__(self) -> None:
        _require_evidence("AuthoritativeBackup", self.evidence)

    @property
    def sentence(self) -> str:
        return ("An authoritative backup is available. On diagnostic failure the only paths are "
                "restoring the exact callback row or remaining on the prior revision in "
                "BLOCKED_NO_AUTHORITATIVE_MAPPING. Never fabricate a callback, delete a decision, "
                "or fall back to decided_at.")


@dataclass(frozen=True)
class StoppedAttestedZero:
    """Every named role can be stopped and attested at zero. Roles come from the closed
    vocabulary; the phase's role FLOOR is enforced by the plan, so a shrunken stop-set is
    unconstructable rather than merely detectable."""

    roles: tuple[str, ...]
    evidence: str

    def __post_init__(self) -> None:
        _require_evidence("StoppedAttestedZero", self.evidence)
        unknown = [r for r in self.roles if r not in PLAN_ROLES]
        if unknown or not self.roles:
            raise ValueError(f"unknown or empty roles: {unknown}")
        if len(set(self.roles)) != len(self.roles):
            raise ValueError("duplicate roles in the stop-set")

    def sentence_for(self, phase: str) -> str:
        listed = ", ".join(self.roles)
        rationale = _STOP_RATIONALES.get(phase, "")
        base = f"Every one of these roles can be stopped and attested at zero: {listed}."
        return f"{base} {rationale}".strip()


# Which kinds each phase REQUIRES, in canonical order. Both the set and the order are the phase's
# — a plan that omits, adds, or reorders a prerequisite fails at construction.
PHASE_REQUIRED_KINDS = {
    PHASE_SECURITY_FULL_WINDOW: (PinnedImage, PlatformWrittenCommitment, TrustedPathProbes),
    PHASE_FLAG_ACTIVATION: (FlagStillOff, SeedAndReadBack, BacklogPreflight, StoppedAttestedZero),
    PHASE_SCHEMA_MAINTENANCE: (RetentionSuspendedAttested, PreWindowDiagnostic,
                               AuthoritativeBackup, StoppedAttestedZero),
}

# The minimum stop-set per phase, and it is a floor, not a suggestion.
PHASE_ROLE_FLOORS = {
    PHASE_FLAG_ACTIVATION: frozenset({"pipeline_worker"}),
    PHASE_SCHEMA_MAINTENANCE: frozenset(PLAN_ROLES),
}

_WHEN_TEMPLATES = {
    PHASE_SECURITY_FULL_WINDOW: (
        "{subject}. It carries no migration and still requires a full window, because during any "
        "overlap an old replica still honours the forgery the release closes. Other releases "
        "classified full-maintenance get their own procedure; do not reuse this one."
    ),
    PHASE_FLAG_ACTIVATION: (
        "{subject}. The migration and provenance columns deploy rolling; the flag flip "
        "({flag_env}) does not."
    ),
    PHASE_SCHEMA_MAINTENANCE: (
        "{subject}. It provides best-effort local supersession only; PLATFORM ordering authority "
        "remains absent until 024 is built and active, and 024 is pending."
    ),
}


@dataclass(frozen=True)
class ProcedurePlanContract:
    """A phase, a subject, and the phase's required prerequisites — nothing else.

    `subject` is the one authored slot: a short noun phrase naming what this cutover is. It is
    length-capped and refused if it tries to be prose (no sentence punctuation), so it cannot
    carry an obligation. Everything normative is a typed kind whose sentence it owns.
    """

    phase: str
    subject: str
    prerequisites: tuple

    def __post_init__(self) -> None:
        if self.phase not in PHASES:
            raise ValueError(f"unknown phase {self.phase!r}")
        if type(self.subject) is not str or not self.subject.strip():
            raise ValueError("subject must be a nonempty noun phrase")
        if len(self.subject) > 90 or any(mark in self.subject for mark in (". ", "! ", "; ")):
            raise ValueError(
                "subject is a NAME, not prose — an obligation smuggled into it would be the F4a "
                "defect returning through the one authored slot"
            )
        required = PHASE_REQUIRED_KINDS[self.phase]
        actual = tuple(type(p) for p in self.prerequisites)
        if actual != required:
            raise ValueError(
                f"phase {self.phase!r} requires exactly {[k.__name__ for k in required]} in that "
                f"order; got {[k.__name__ for k in actual]}"
            )
        floor = PHASE_ROLE_FLOORS.get(self.phase)
        if floor is not None:
            stop = next(p for p in self.prerequisites if isinstance(p, StoppedAttestedZero))
            missing = floor - set(stop.roles)
            if missing:
                raise ValueError(
                    f"phase {self.phase!r} requires the stop-set to include {sorted(missing)}"
                )

    def when(self) -> str:
        slots = {"subject": self.subject}
        if self.phase == PHASE_FLAG_ACTIVATION:
            flag = next(p for p in self.prerequisites if isinstance(p, FlagStillOff))
            slots["flag_env"] = flag.flag_env
        return _WHEN_TEMPLATES[self.phase].format(**slots)

    def blocks_start(self) -> tuple[str, ...]:
        out = []
        for prerequisite in self.prerequisites:
            if isinstance(prerequisite, StoppedAttestedZero):
                out.append(prerequisite.sentence_for(self.phase))
            else:
                out.append(prerequisite.sentence)
        return tuple(out)
