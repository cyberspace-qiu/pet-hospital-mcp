"""Input validation tests for the ``list_pets`` tool.

Every parameter the upstream Go REST API accepts has at least one negative
test: unknown values, out-of-range numerics, type mismatches, NaN/Infinity,
and unknown fields. Pydantic raises before the upstream is ever contacted,
so no HTTP fixture is needed here.
"""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from pet_hospital_mcp.tools.list_pets import ListPetsInput


def test_default_input_accepts_all_none() -> None:
    """No parameters => a valid, fully-optional input."""
    inp = ListPetsInput()
    assert inp.q is None
    assert inp.species is None
    assert inp.page is None
    assert inp.pageSize is None


def test_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError) as exc:
        ListPetsInput(notARealField="oops")
    assert "extra" in str(exc.value).lower() or "forbidden" in str(exc.value).lower()


def test_rejects_invalid_species() -> None:
    with pytest.raises(ValidationError):
        ListPetsInput(species="外星人")


def test_rejects_invalid_status() -> None:
    with pytest.raises(ValidationError):
        ListPetsInput(status="已治愈")


def test_rejects_invalid_sortby() -> None:
    with pytest.raises(ValidationError):
        ListPetsInput(sortBy="patientName")


def test_rejects_invalid_order() -> None:
    with pytest.raises(ValidationError):
        ListPetsInput(order="random")


def test_rejects_page_below_one() -> None:
    with pytest.raises(ValidationError):
        ListPetsInput(page=0)


def test_rejects_pagesize_below_one() -> None:
    with pytest.raises(ValidationError):
        ListPetsInput(pageSize=0)


def test_rejects_pagesize_above_500() -> None:
    with pytest.raises(ValidationError):
        ListPetsInput(pageSize=501)


def test_accepts_pagesize_boundary_500() -> None:
    inp = ListPetsInput(pageSize=500)
    assert inp.pageSize == 500


def test_rejects_negative_min() -> None:
    with pytest.raises(ValidationError):
        ListPetsInput(min=-0.01)


def test_rejects_negative_max() -> None:
    with pytest.raises(ValidationError):
        ListPetsInput(max=-1.0)


def test_rejects_min_greater_than_max() -> None:
    with pytest.raises(ValidationError) as exc:
        ListPetsInput(min=100, max=50)
    assert "min must be <= max" in str(exc.value)


def test_rejects_nan_min() -> None:
    with pytest.raises(ValidationError) as exc:
        ListPetsInput(min=math.nan)
    assert "nan" in str(exc.value).lower() or "finite" in str(exc.value).lower()


def test_rejects_nan_max() -> None:
    with pytest.raises(ValidationError) as exc:
        ListPetsInput(max=math.nan)
    assert "nan" in str(exc.value).lower() or "finite" in str(exc.value).lower()


def test_rejects_infinity_min() -> None:
    with pytest.raises(ValidationError):
        ListPetsInput(min=math.inf)


def test_rejects_infinity_max() -> None:
    with pytest.raises(ValidationError):
        ListPetsInput(max=-math.inf)


def test_rejects_wrong_type_for_page() -> None:
    with pytest.raises(ValidationError):
        ListPetsInput(page="1")  # type: ignore[arg-type]


def test_rejects_wrong_type_for_species() -> None:
    with pytest.raises(ValidationError):
        ListPetsInput(species=123)  # type: ignore[arg-type]


def test_accepts_valid_combo() -> None:
    inp = ListPetsInput(
        q="肠胃炎",
        species="犬",
        status="待就诊",
        sortBy="totalCost",
        order="desc",
        page=1,
        pageSize=20,
        min=100,
        max=1000,
    )
    assert inp.species == "犬"
    assert inp.min == 100
    assert inp.max == 1000


def test_accepts_min_equal_max() -> None:
    """Edge case: zero-width range is legal (filters to a single cost)."""
    inp = ListPetsInput(min=500, max=500)
    assert inp.min == inp.max
