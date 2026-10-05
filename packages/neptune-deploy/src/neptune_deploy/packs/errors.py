"""The one error the pack compiler raises: an input it refuses, with a code and a JSON pointer."""


class PackError(ValueError):
    """A spec, snapshot or template the compiler refuses (ADR 0013 §8).

    ``code`` is stable and machine-readable; ``pointer`` is a JSON pointer into the refused document
    (``""`` for the whole document). Nothing is written when one is raised.
    """

    def __init__(self, code: str, message: str, pointer: str = "") -> None:
        super().__init__(f"{code}: {message}" + (f" (at {pointer})" if pointer else ""))
        self.code = code
        self.pointer = pointer
