from job_discovery.services.instahyre import experience_includes


def test_experience_range_includes_candidate_years() -> None:
    assert experience_includes("5-8", 6)
    assert experience_includes("6-10", 6)
    assert experience_includes("6+", 6)


def test_experience_range_rejects_incompatible_or_invalid_values() -> None:
    assert not experience_includes("7-10", 6)
    assert not experience_includes("7+", 6)
    assert not experience_includes(None, 6)
    assert not experience_includes("unknown", 6)
