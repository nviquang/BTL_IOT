"""Bộ sinh dữ liệu demo (training/demo_data.py), hiệu chỉnh theo số liệu thật trong training/data/."""
import numpy as np
import pytest

from aiot.source_model import LABELS
from aiot.source_window import BASE_S, COLUMNS
from training.demo_data import KINDS, NORMAL_ONLY, fit_real, session

N, C, P = (LABELS.index(x) for x in ("normal", "cooking", "pollution"))


@pytest.fixture(scope="module")
def real():
    return fit_real()


def test_fit_real_reads_current_data(real):
    assert 0.003 < real["noise_sd"] < 0.05 and 3 < real["noise_tau"] < 60
    assert len(real["dust"]) == 2 and max(t["peak"] for t in real["dust"]) > 4.0
    assert all(t["f10"] > 1.0 for t in real["dust"])     # khói nhang/bụi thật: hạt thô hơn nền


@pytest.mark.parametrize("kind", KINDS)
def test_session_shapes_and_labels(real, kind):
    s = session(np.random.default_rng(0), real, kind)
    assert all(len(s[c]) == len(s["label"]) for c in COLUMNS)
    assert (s["label"][:BASE_S] == N).all()
    assert np.nanmax(s["pm25"]) <= 1000.0          # PMS7003 bão hoà như aiot.db.fast_rows
    labs = set(np.unique(s["label"]).tolist())
    if kind in NORMAL_ONLY:
        assert labs == {N}
    if kind == "cooking":
        assert C in labs and P not in labs
    if kind in ("dust", "step_up"):
        assert P in labs and C not in labs
    if kind == "back_to_back":
        first_p = np.argmax(s["label"] == P)
        first_c = np.argmax(s["label"] == C)
        assert 0 < first_p < first_c


def test_session_reproducible(real):
    a = session(np.random.default_rng(3), real, "cooking")
    b = session(np.random.default_rng(3), real, "cooking")
    np.testing.assert_array_equal(a["pm25"], b["pm25"])


def test_session_rejects_unknown_kind(real):
    with pytest.raises(ValueError):
        session(np.random.default_rng(0), real, "smoking")
