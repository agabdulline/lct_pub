import numpy as np
import pandas as pd
import pytest
from ml.each_channel.transfer_study import (
    horizon_classes,
    positive_probability,
    subset,
    type_balanced_weights,
)


def test_purged_split_uses_frozen_boundaries():
    f = pd.DataFrame(
        {"date": pd.date_range("2025-01-01", "2025-03-01"), "status": "scored", "target": 0}
    )
    result = subset(f, "custom", {"custom": ("2025-01-01", "2025-02-01")})
    assert result.date.max() == pd.Timestamp("2025-01-02")
    assert (result.date + pd.Timedelta(days=30)).le("2025-02-01").all()


def test_positive_classes_preserve_binary_endpoint():
    f = pd.DataFrame({"target": [0, 1, 1, 1, 1], "lead_days": [np.nan, 1, 7, 14, 29.9]})
    assert horizon_classes(f).tolist() == [0, 1, 2, 3, 3]
    assert np.array_equal(horizon_classes(f) > 0, f.target.eq(1))
    f.loc[1, "lead_days"] = 30
    with pytest.raises(ValueError):
        horizon_classes(f)


def test_positive_probability_handles_missing_classes():
    assert np.allclose(positive_probability([[0.7, 0.3]], [0, 3]), [0.3])
    assert np.allclose(positive_probability([[1.0]], [0]), [0.0])
    assert np.allclose(positive_probability([[0.2, 0.8]], [1, 3]), [1.0])


def test_type_balance_does_not_change_labels():
    types = np.array(["smoke", "smoke", "phase", "manual"])
    weights = type_balanced_weights(types)
    totals = [weights[types == key].sum() for key in np.unique(types)]
    assert np.allclose(totals, totals[0])
    assert np.isclose(weights.mean(), 1)
