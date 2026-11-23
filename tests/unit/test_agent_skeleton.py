"""Skeleton checks of the agent-loop package: public names it relies on, the injected ports (M2, L4, R1-L2)."""
from __future__ import annotations

import dataclasses
import typing

import pytest

from chimericmesh import diagnostics
from chimericmesh.agent import errors
from chimericmesh.agent import ports as agent_ports
from chimericmesh.approvals import scope

from approvals_helpers import make_manager


def test_diagnostics_public_sets_are_frozensets():
    assert isinstance(diagnostics.SKIP_DIRS, frozenset)
    assert isinstance(diagnostics.CODE_SUFFIXES, frozenset)
    assert ".git" in diagnostics.SKIP_DIRS and ".py" in diagnostics.CODE_SUFFIXES


def test_scope_public_names_exist():
    assert callable(scope.check_file) and callable(scope.glob_to_regex)
    assert isinstance(scope.SOURCE_SUFFIXES, frozenset) and ".py" in scope.SOURCE_SUFFIXES
    assert scope.PROTECTED_PATTERNS and all(isinstance(p, str) for p in scope.PROTECTED_PATTERNS)
    assert diagnostics.CODE_SUFFIXES <= scope.SOURCE_SUFFIXES


def _members(proto: type) -> set[str]:
    return {n for n in vars(proto) if not n.startswith("_")}


def test_approval_reader_is_a_three_method_read_only_protocol():
    reader = agent_ports.ApprovalReader
    assert getattr(reader, "_is_protocol", False)
    assert _members(reader) == {"approval_of", "rule", "status"}
    for mutator in ("create_rule", "revoke_rule", "resume", "enable_rule", "patch_rule", "pause"):
        assert mutator not in _members(reader)


def test_approval_manager_satisfies_the_reader():
    rig = make_manager("bare")
    try:
        assert isinstance(rig.manager, agent_ports.ApprovalReader)
        assert not isinstance(object(), agent_ports.ApprovalReader)
    finally:
        rig.close()


def test_agent_ports_is_frozen_and_names_the_injected_interfaces():
    assert dataclasses.is_dataclass(agent_ports.AgentPorts)
    assert agent_ports.AgentPorts.__dataclass_params__.frozen
    names = [f.name for f in dataclasses.fields(agent_ports.AgentPorts)]
    assert names == ["state", "registry", "approvals", "protected_dirs", "workspace_root"]
    ports = agent_ports.AgentPorts(state=object(), registry=object(), approvals=None,
                                   protected_dirs=(), workspace_root=None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        ports.state = None  # type: ignore[misc]


def test_errors_have_the_designed_shape():
    for cls in (errors.UnknownService, errors.UnknownProposal, errors.NoCodeFolder, errors.InvalidQuery,
                errors.TooManyWaiters, errors.SearchBusy):
        assert issubclass(cls, errors.AgentError)
    assert errors.TooManyWaiters(retry_after=5).retry_after == 5
    assert isinstance(errors.SearchBusy(), errors.AgentError)


def test_no_agent_limits_type_exists():
    """R1-L1: the constants in ``agent.limits`` are the only limits."""
    import chimericmesh.agent as agent
    assert not hasattr(agent, "AgentLimits")
    assert not hasattr(agent_ports, "AgentLimits")


def test_prose_says_three_method_protocol():
    doc = agent_ports.ApprovalReader.__doc__ or ""
    assert "three-method" in doc
    assert typing.get_type_hints(agent_ports.AgentPorts) is not None
