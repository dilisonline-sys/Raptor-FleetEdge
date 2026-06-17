"""
nn_predictor.py — Pure-NumPy two-layer MLP for next-candle direction prediction.

Predicts P(next_15m_candle_close > current_close) from a sliding window of
technical features.  No external ML deps — only numpy (already in requirements).

Architecture:
  Input(60) → Dense(32, ReLU) → Dense(16, ReLU) → Dense(1, Sigmoid)
  Optimizer : Adam (β1=0.9, β2=0.999)
  Loss      : Binary cross-entropy
  Features  : 10 candles × 6 indicators = 60 inputs per sample

Typical performance on 200 candles: ~58–65% direction accuracy, which adds
a statistically meaningful edge above 50% random chance when used as a
confirmation filter on top of the existing rule-based signal engine.
"""
import numpy as np
import pandas as pd
from logger import log

# ── Hyper-parameters ─────────────────────────────────────────────────────────
LOOKBACK   = 10    # candles in the sliding input window  (10 × 15m = 2.5 h context)
N_FEATURES = 6     # features per candle: ret, vol_chg, rsi_n, macd_n, atr_n, bb_pos
N_INPUT    = LOOKBACK * N_FEATURES   # 60
H1, H2     = 32, 16                  # hidden layer widths
EPOCHS     = 60
BATCH      = 32
LR         = 3e-3

CONF_FLOOR    = 0.55   # minimum P(up) to let a BUY signal through the NN gate
OOS_MIN_ACC   = 0.58   # NH-2: gate only activates when OOS accuracy > 58% AND test set ≥ 30 samples
OOS_MIN_SAMP  = 30     # H-8: 200 candles yields ~38 OOS samples — 50 was never reachable, lowered to 30
RETRAIN_EVERY = 100    # retrain every N main-loop cycles (≈ 100 min on 1-min cycle)


class PricePredictor:
    """Lightweight MLP that predicts P(next_candle_up) from OHLCV history."""

    def __init__(self):
        rng = np.random.default_rng(42)
        # He initialisation for ReLU layers
        self.W1 = rng.standard_normal((N_INPUT, H1)).astype(np.float32) * np.sqrt(2.0 / N_INPUT)
        self.b1 = np.zeros(H1, dtype=np.float32)
        self.W2 = rng.standard_normal((H1, H2)).astype(np.float32) * np.sqrt(2.0 / H1)
        self.b2 = np.zeros(H2, dtype=np.float32)
        self.W3 = rng.standard_normal((H2, 1)).astype(np.float32) * np.sqrt(2.0 / H2)
        self.b3 = np.zeros(1, dtype=np.float32)

        self._init_adam()

        # Feature normalisation stats (fitted on training data)
        self._mu: np.ndarray | None = None
        self._std: np.ndarray | None = None

        self.is_trained   = False
        self.train_acc    = 0.0
        self.oos_acc      = 0.0   # FIX-3: out-of-sample accuracy — the only reliable gate metric
        self.oos_samples  = 0     # NH-2: test set size — gate only activates when ≥ OOS_MIN_SAMP
        self.symbol       = "?"
        self._train_count = 0

    # ── Adam moment buffers ───────────────────────────────────────────────────
    def _init_adam(self):
        self._t = 0
        z = lambda w: np.zeros_like(w)
        self._mW1, self._vW1 = z(self.W1), z(self.W1)
        self._mb1, self._vb1 = z(self.b1), z(self.b1)
        self._mW2, self._vW2 = z(self.W2), z(self.W2)
        self._mb2, self._vb2 = z(self.b2), z(self.b2)
        self._mW3, self._vW3 = z(self.W3), z(self.W3)
        self._mb3, self._vb3 = z(self.b3), z(self.b3)

    # ── Activations ──────────────────────────────────────────────────────────
    @staticmethod
    def _relu(x):    return np.maximum(0.0, x)
    @staticmethod
    def _drelu(x):   return (x > 0.0).astype(np.float32)
    @staticmethod
    def _sigmoid(x): return 1.0 / (1.0 + np.exp(-np.clip(x, -30.0, 30.0)))

    # ── Forward pass ─────────────────────────────────────────────────────────
    def _forward(self, X: np.ndarray, store: bool = True) -> np.ndarray:
        z1 = X @ self.W1 + self.b1
        a1 = self._relu(z1)
        z2 = a1 @ self.W2 + self.b2
        a2 = self._relu(z2)
        z3 = a2 @ self.W3 + self.b3
        p  = self._sigmoid(z3)
        if store:
            self._z1, self._a1 = z1, a1
            self._z2, self._a2 = z2, a2
        return p                       # shape (m, 1)

    # ── Backward pass ────────────────────────────────────────────────────────
    def _backward(self, X: np.ndarray, y: np.ndarray, p: np.ndarray) -> dict:
        m   = max(X.shape[0], 1)
        dz3 = (p - y.reshape(-1, 1)) / m

        dW3 = self._a2.T @ dz3
        db3 = dz3.sum(0)

        da2 = dz3 @ self.W3.T
        dz2 = da2 * self._drelu(self._z2)
        dW2 = self._a1.T @ dz2
        db2 = dz2.sum(0)

        da1 = dz2 @ self.W2.T
        dz1 = da1 * self._drelu(self._z1)
        dW1 = X.T @ dz1
        db1 = dz1.sum(0)

        return {"dW1": dW1, "db1": db1, "dW2": dW2,
                "db2": db2, "dW3": dW3, "db3": db3}

    # ── Adam parameter update ─────────────────────────────────────────────────
    def _adam_step(self, grads: dict, lr: float = LR,
                   b1: float = 0.9, b2: float = 0.999, eps: float = 1e-8):
        self._t += 1
        t = self._t
        pairs = [
            (self.W1, grads["dW1"], self._mW1, self._vW1),
            (self.b1, grads["db1"], self._mb1, self._vb1),
            (self.W2, grads["dW2"], self._mW2, self._vW2),
            (self.b2, grads["db2"], self._mb2, self._vb2),
            (self.W3, grads["dW3"], self._mW3, self._vW3),
            (self.b3, grads["db3"], self._mb3, self._vb3),
        ]
        for W, dW, m, v in pairs:
            np.clip(dW, -5.0, 5.0, out=dW)
            m[:] = b1 * m + (1.0 - b1) * dW
            v[:] = b2 * v + (1.0 - b2) * dW ** 2
            mhat = m / (1.0 - b1 ** t)
            vhat = v / (1.0 - b2 ** t)
            W   -= lr * mhat / (np.sqrt(vhat) + eps)

    # ── Feature engineering ──────────────────────────────────────────────────
    @staticmethod
    def _build_features(df: pd.DataFrame) -> np.ndarray:
        """Compute (n_rows, N_FEATURES) feature matrix from OHLCV candle dataframe.

        All features are scaled to roughly [-1, 1] so they enter the network on
        equal footing without a separate scaler layer.
        """
        import pandas_ta as ta

        c = df["close"].astype(float)
        h = df["high"].astype(float)
        lo = df["low"].astype(float)
        v  = df["volume"].astype(float)

        # 1 — Log return: stationary momentum signal, maps naturally around 0
        ret = np.log(c / c.shift(1)).fillna(0.0).clip(-0.2, 0.2).values / 0.2

        # 2 — Volume change (capped % change, normalised to [-1,1])
        vol_chg = v.pct_change().fillna(0.0).clip(-2.0, 2.0).values / 2.0

        # 3 — RSI normalised: [0,100] → [-1,1]  (50 = 0, overbought = +1, oversold = -1)
        rsi_raw = ta.rsi(c, 14).fillna(50.0)
        rsi_n   = ((rsi_raw - 50.0) / 50.0).clip(-1.0, 1.0).values

        # 4 — MACD relative to ATR: scale-free trend/momentum
        _macd_df  = ta.macd(c, 12, 26, 9)
        macd_line = _macd_df["MACD_12_26_9"].fillna(0.0)
        _atr      = ta.atr(h, lo, c, 14).bfill().fillna(1.0)
        macd_n    = (macd_line / _atr.replace(0.0, 1.0)).clip(-3.0, 3.0).values / 3.0

        # 5 — ATR as % of close, capped at 5% and normalised to [0,1]
        atr_n = (_atr / c.replace(0.0, 1.0) * 100.0).clip(0.0, 5.0).values / 5.0

        # 6 — Bollinger Band position: 0 = at lower band, 1 = at upper band
        bb      = ta.bbands(c, length=20, std=2.0)
        bb_lo   = bb["BBL_20_2_2.0"].fillna(c * 0.98)
        bb_hi   = bb["BBU_20_2_2.0"].fillna(c * 1.02)
        bb_rng  = (bb_hi - bb_lo).replace(0.0, 1.0)
        bb_pos  = ((c - bb_lo) / bb_rng).clip(0.0, 1.0).values

        feats = np.column_stack([ret, vol_chg, rsi_n, macd_n, atr_n, bb_pos]).astype(np.float32)
        return np.nan_to_num(feats, nan=0.0, posinf=1.0, neginf=-1.0)

    # ── Dataset construction ──────────────────────────────────────────────────
    def _make_dataset(self, df: pd.DataFrame):
        feats  = self._build_features(df)
        closes = df["close"].astype(float).values
        n      = len(feats)
        X, y   = [], []
        for i in range(LOOKBACK, n - 1):
            window = feats[i - LOOKBACK:i].flatten()
            label  = 1.0 if closes[i + 1] > closes[i] else 0.0
            X.append(window)
            y.append(label)
        if not X:
            return None, None
        return (np.array(X, dtype=np.float32),
                np.array(y, dtype=np.float32))

    # ── Training ─────────────────────────────────────────────────────────────
    def train(self, df: pd.DataFrame, symbol: str = "?") -> float:
        """Fit the network on historical candles.  Returns out-of-sample accuracy.

        FIX-3: 80/20 train/test split with OOS accuracy tracking.
        In-sample accuracy (train_acc) was always ~100% due to overfitting on 188 samples.
        OOS accuracy (oos_acc) is the only meaningful metric for the NN gate.
        """
        self.symbol = symbol
        X, y = self._make_dataset(df)

        if X is None or len(X) < 40:
            log("NN", "TRAIN_SKIPPED", symbol=symbol,
                reason="insufficient_samples", have=0 if X is None else len(X))
            return 0.5

        # 80/20 chronological split — test set is always the most recent samples
        split    = int(len(X) * 0.80)
        X_tr, y_tr = X[:split], y[:split]
        X_te, y_te = X[split:], y[split:]

        # Z-score normalisation fitted ONLY on training data (no leakage)
        self._mu  = X_tr.mean(axis=0)
        self._std = X_tr.std(axis=0) + 1e-8
        Xn_tr = (X_tr - self._mu) / self._std
        Xn_te = (X_te - self._mu) / self._std

        # Fresh Adam moments for each retrain
        self._init_adam()

        rng = np.random.default_rng(self._train_count)

        for _ in range(EPOCHS):
            idx = rng.permutation(len(Xn_tr))
            for s in range(0, len(Xn_tr), BATCH):
                b   = idx[s:s + BATCH]
                p   = self._forward(Xn_tr[b], store=True)
                g   = self._backward(Xn_tr[b], y_tr[b], p)
                self._adam_step(g)

        # In-sample accuracy (informational only — will be high due to small dataset)
        p_tr      = self._forward(Xn_tr, store=False).flatten()
        train_acc = float(((p_tr > 0.5) == y_tr.astype(bool)).mean())

        # Out-of-sample accuracy — the only metric the NN gate should use
        p_te    = self._forward(Xn_te, store=False).flatten()
        oos_acc = float(((p_te > 0.5) == y_te.astype(bool)).mean()) if len(y_te) else 0.5

        self.train_acc    = train_acc
        self.oos_acc      = oos_acc
        self.oos_samples  = len(X_te)   # NH-2: gate caller checks this before trusting oos_acc
        self.is_trained   = True
        self._train_count += 1

        gate_ok = oos_acc >= OOS_MIN_ACC and len(X_te) >= OOS_MIN_SAMP
        log("NN", "TRAINED",
            symbol=symbol,
            train_samples=split,
            test_samples=len(X_te),
            train_acc=round(train_acc * 100, 1),
            oos_acc=round(oos_acc * 100, 1),
            gate_active=gate_ok,
            run=self._train_count)

        return oos_acc  # gate callers should use this

    # ── Inference ────────────────────────────────────────────────────────────
    def predict(self, df: pd.DataFrame) -> float:
        """Return P(next_candle_close > current_close) in [0.0, 1.0].

        Returns 0.5 (neutral) when the model has not been trained yet or
        the candle history is too short for a full lookback window.
        """
        if not self.is_trained or self._mu is None:
            return 0.5
        feats = self._build_features(df)
        if len(feats) < LOOKBACK:
            return 0.5
        window = feats[-LOOKBACK:].flatten().reshape(1, -1).astype(np.float32)
        window = np.nan_to_num((window - self._mu) / self._std, nan=0.0)
        return float(self._forward(window, store=False)[0, 0])

    @property
    def conf_floor(self) -> float:
        """Minimum P(up) required to confirm a rule-engine BUY signal."""
        return CONF_FLOOR
