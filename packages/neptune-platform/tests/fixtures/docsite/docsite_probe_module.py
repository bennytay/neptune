"""A module the docs site's strictness probe documents (tests/test_docsite_build.py).

Its base class and annotations name types the probe site does not document, as the real API
reference's do.
"""

import collections
import decimal
import fractions


class Probe(collections.OrderedDict[str, int]):
    """A class whose base the site does not document."""


def probe(x: fractions.Fraction) -> decimal.Decimal:
    """A function whose annotations the site does not document."""
    return decimal.Decimal(x.numerator) / x.denominator
