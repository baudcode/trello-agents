"""Calculator tests — agents should make these pass."""

from calculator.calc import Calculator


def test_add():
    c = Calculator()
    assert c.add(2, 3) == 5
    assert c.add(-1, 1) == 0
    assert c.add(0, 0) == 0


def test_subtract():
    c = Calculator()
    assert c.subtract(5, 3) == 2
    assert c.subtract(1, 1) == 0
    assert c.subtract(0, 5) == -5


def test_multiply():
    c = Calculator()
    assert c.multiply(2, 3) == 6
    assert c.multiply(-2, 3) == -6
    assert c.multiply(0, 100) == 0
