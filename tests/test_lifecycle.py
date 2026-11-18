import pytest

from app.core.errors import AppError
from app.services.lifecycle import require_transition


def test_declared_lifecycle_transitions_are_allowed():
    require_transition("load", "POSTED", "MATCHED")
    require_transition("match", "ACCEPTED", "CONFIRMED")
    require_transition("trip", "AT_PICKUP", "LOADED")
    require_transition("dispatch", "OFFERED", "ACCEPTED")


def test_skipping_a_lifecycle_step_is_rejected():
    with pytest.raises(AppError) as rejected:
        require_transition("trip", "SCHEDULED", "DELIVERED")
    assert rejected.value.status == 409

