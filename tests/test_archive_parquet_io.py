"""Tests for timestamp normalization in shared archive Parquet helpers."""

from datetime import UTC, datetime

import pandas as pd

from data_lake.archive.parquet_io import key_tuples


def test_key_tuples_normalizes_datetime_and_pandas_timestamp_to_utc():
    frame = pd.DataFrame(
        {
            "source": ["news", "social"],
            "event_ts": [
                datetime(2026, 1, 2, 3, 4, 5),
                pd.Timestamp("2026-01-02T04:04:05+01:00"),
            ],
        }
    )

    assert key_tuples(frame, ("source", "event_ts")) == {
        ("news", datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)),
        ("social", datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)),
    }
