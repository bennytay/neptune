import neptune_context


def test_version_is_dotted_integers() -> None:
    parts = neptune_context.__version__.split(".")
    assert len(parts) == 3
    assert all(part.isdigit() for part in parts)
