"""Engine identity. Bump ENGINE_BUILD_ID in a reviewed commit
when scoring/gate/validator/decision SEMANTICS change — the discipline each
policy JSON's `version` has (a whole-tree guard test forces a conscious
bump-or-repin on ANY src change). Format: eng-<positive int>."""

ENGINE_BUILD_ID = "eng-2"
