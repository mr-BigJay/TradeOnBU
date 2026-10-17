from app.services.siwc.model_profiles import (
    get_model_profile,
    resolve_model_selection,
)


def test_model_profiles_bind_luna_to_high_and_sol_to_max():
    assert get_model_profile("gpt-6.1-sol")["reasoning_effort"] == "max"
    assert get_model_profile("gpt-6-luna")["reasoning_effort"] == "high"
    assert get_model_profile("gpt-6-luna-max") is None


def test_model_selection_prefers_saved_profile_without_changing_default():
    assert resolve_model_selection("gpt-6-luna", "gpt-6.1-sol", "max") == ("gpt-6-luna", "high")
    assert resolve_model_selection(None, "gpt-6.1-sol", "max") == ("gpt-6.1-sol", "max")
