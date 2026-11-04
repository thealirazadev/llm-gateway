"""Error envelopes are contract items: code and type must match docs/api-contracts.md."""

from app.errors import budget_exceeded, envelope


def test_budget_exceeded_uses_the_documented_code_and_type() -> None:
    error = budget_exceeded()

    assert error.status_code == 429
    assert envelope(error) == {
        "error": {
            "message": "Monthly budget exceeded for this key.",
            "type": "insufficient_quota",
            "code": "budget_exceeded",
        }
    }
