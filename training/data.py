"""
Chuẩn bị chuỗi thời gian cho huấn luyện LSTM (chạy trên laptop/Colab, không chạy trên Pi).

Quy ước:
- Dữ liệu là DataFrame có DatetimeIndex theo giờ (mỗi dòng là một giờ). Giờ bị thiếu là
  giờ không có trong index hoặc có NaN; không nội suy ở đây, các cửa sổ chạm giờ thiếu
  bị bỏ hẳn.
- Chia tập theo thứ tự thời gian, không xáo trộn (chrono_split).
- MinMaxScaler chỉ fit trên tập train (fit_scaler). Không dùng lại normalize() trong
  notebook Chương 12, vì hàm đó fit trên toàn chuỗi nên rò rỉ thông tin của tập test.
- Cửa sổ trượt: n_in giờ trước -> n_out giờ tiếp theo của cột đích (make_windows).
"""
import numpy as np
import pandas as pd
from sklearn.preprocessing import MinMaxScaler

HOUR = pd.Timedelta(hours=1)


def chrono_split(df: pd.DataFrame, train: float = 0.7, val: float = 0.15):
    """(train, val, test) theo thứ tự thời gian, không chồng lấn, không xáo trộn.

    Số dòng train = round(n * train), val = round(n * val), phần còn lại là test."""
    if not (0 < train < 1 and 0 <= val < 1 and train + val < 1):
        raise ValueError("cần 0 < train < 1, 0 <= val < 1 và train + val < 1")
    df = df.sort_index()
    n = len(df)
    n_train = int(round(n * train))
    n_val = int(round(n * val))
    return (df.iloc[:n_train], df.iloc[n_train:n_train + n_val],
            df.iloc[n_train + n_val:])


def fit_scaler(train_df: pd.DataFrame, columns) -> MinMaxScaler:
    """MinMaxScaler fit trên các cột `columns` của tập train (bỏ qua NaN khi fit).

    Dùng scaler này để biến đổi cả val và test; thứ tự cột của scaler là `columns`."""
    scaler = MinMaxScaler()
    scaler.fit(train_df[list(columns)])
    return scaler


def add_time_features(df: pd.DataFrame) -> pd.DataFrame:
    """Bản sao của df thêm hour_sin, hour_cos theo giờ trong ngày (giờ địa phương của index).

    Mã hoá vòng tròn để 23 giờ và 0 giờ nằm cạnh nhau."""
    out = df.copy()
    angle = 2 * np.pi * out.index.hour.to_numpy() / 24
    out["hour_sin"] = np.sin(angle)
    out["hour_cos"] = np.cos(angle)
    return out


def make_windows(df: pd.DataFrame, target: str = "pm25", n_in: int = 24, n_out: int = 6,
                 scaler: MinMaxScaler | None = None):
    """Tạo cửa sổ trượt (X, y, target_times).

    - X: (số cửa sổ, n_in, số cột), cột theo đúng thứ tự trong df.
    - y: (số cửa sổ, n_out), giá trị cột `target` ở n_out giờ ngay sau đầu vào.
    - target_times: DatetimeIndex, mốc giờ đích đầu tiên của từng cửa sổ.
    Cửa sổ nào có giờ đầu vào hoặc giờ đích bị thiếu (không có trong index hoặc NaN) thì bỏ.
    scaler (nếu có) phải fit trên đúng các cột của df; cột đích được biến đổi cùng scaler."""
    if target not in df.columns:
        raise ValueError(f"cột đích {target!r} không có trong df")
    if not isinstance(df.index, pd.DatetimeIndex):
        raise TypeError("df cần DatetimeIndex theo giờ")
    df = df.sort_index()
    if df.index.has_duplicates:
        raise ValueError("index có giờ trùng lặp")
    span = n_in + n_out
    n_feat = df.shape[1]
    empty = (np.empty((0, n_in, n_feat)), np.empty((0, n_out)),
             pd.DatetimeIndex([], tz=df.index.tz))
    if len(df) == 0:
        return empty

    # Đưa về lưới giờ đều: giờ thiếu trong index thành dòng NaN
    full = pd.date_range(df.index[0], df.index[-1], freq="h")
    if len(full) and not df.index.isin(full).all():
        raise ValueError("index phải là các mốc đầu giờ cách nhau đúng 1 giờ")
    grid = df.reindex(full)
    values = scaler.transform(grid) if scaler is not None else grid.to_numpy(dtype=float)
    values = np.asarray(values, dtype=float)
    if len(values) < span:
        return empty

    ok = ~np.isnan(values).any(axis=1)
    # Số giờ hợp lệ trong [s, s + span) bằng hiệu tổng tích luỹ
    csum = np.concatenate([[0], np.cumsum(ok)])
    starts = np.arange(len(values) - span + 1)
    starts = starts[(csum[starts + span] - csum[starts]) == span]

    t_col = df.columns.get_loc(target)
    X = np.stack([values[s:s + n_in] for s in starts]) if len(starts) else empty[0]
    y = (np.stack([values[s + n_in:s + span, t_col] for s in starts])
         if len(starts) else empty[1])
    return X, y, full[starts + n_in]


def difference(s: pd.Series, lag: int = 1) -> pd.Series:
    """Sai phân theo vị trí: s[t] - s[t - lag], bỏ `lag` phần tử đầu.

    s cần là chuỗi giờ đều (reindex trước nếu có giờ thiếu); NaN bên trong được giữ lại."""
    if lag < 1:
        raise ValueError("lag phải >= 1")
    return s.diff(lag).iloc[lag:]


def invert_difference(pred, history, lag: int = 1) -> np.ndarray:
    """Khôi phục giá trị gốc từ chuỗi sai phân `pred`.

    history là các giá trị gốc ngay trước phần tử đầu tiên của pred (cần ít nhất `lag`
    giá trị). out[i] = pred[i] + (history[-lag + i] nếu i < lag, ngược lại out[i - lag])."""
    pred = np.asarray(pred, dtype=float)
    hist = np.asarray(history, dtype=float)
    if len(hist) < lag:
        raise ValueError(f"cần ít nhất {lag} giá trị lịch sử, có {len(hist)}")
    buf = np.concatenate([hist[-lag:], np.empty(len(pred))])
    for i, d in enumerate(pred):
        buf[lag + i] = d + buf[i]
    return buf[lag:]
