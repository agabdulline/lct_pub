import numpy as np
import pandas as pd
from ml.each_channel.fleet_context_study import window_counts


def test_window_counts_excludes_event_at_decision_time():
    points = pd.to_datetime(["2025-01-03", "2025-01-10", "2025-01-11"])
    dates = pd.to_datetime(["2025-01-10"])

    result = window_counts(points, dates, 7)

    assert result.tolist() == [1]
    assert np.issubdtype(result.dtype, np.integer)
