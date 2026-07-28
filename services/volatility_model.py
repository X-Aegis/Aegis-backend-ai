"""services/volatility_model.py

NumPy-based LSTM and GRU models for predicting volatility spikes in FX rates.
These models operate on rate sequence returns (pct change) to forecast future
volatility scores in the range [0, 100].
"""

import numpy as np


class BaseVolatilityModel:
    """Base class for Volatility Prediction Models."""
    def __init__(self, seq_len=10, hidden_dim=8, lr=0.01):
        self.seq_len = seq_len
        self.hidden_dim = hidden_dim
        self.lr = lr

    def _sigmoid(self, x):
        return 1.0 / (1.0 + np.exp(-np.clip(x, -50.0, 50.0)))

    def _tanh(self, x):
        return np.tanh(x)


class VolatilityGRUModel(BaseVolatilityModel):
    """Gated Recurrent Unit (GRU) model for volatility forecasting."""
    def __init__(self, seq_len=10, hidden_dim=8, lr=0.01):
        super().__init__(seq_len, hidden_dim, lr)
        
        # input_dim is 1 (rates returns sequence)
        input_dim = 1
        
        # Weight initialization (Xavier/Glorot style)
        limit = np.sqrt(6.0 / (hidden_dim + input_dim))
        
        # Update gate parameters
        self.Wz = np.random.uniform(-limit, limit, (hidden_dim, input_dim))
        self.Uz = np.random.uniform(-limit, limit, (hidden_dim, hidden_dim))
        self.bz = np.zeros((hidden_dim, 1))
        
        # Reset gate parameters
        self.Wr = np.random.uniform(-limit, limit, (hidden_dim, input_dim))
        self.Ur = np.random.uniform(-limit, limit, (hidden_dim, hidden_dim))
        self.br = np.zeros((hidden_dim, 1))
        
        # Candidate parameters
        self.Wh = np.random.uniform(-limit, limit, (hidden_dim, input_dim))
        self.Uh = np.random.uniform(-limit, limit, (hidden_dim, hidden_dim))
        self.bh = np.zeros((hidden_dim, 1))
        
        # Dense output parameters (hidden_dim -> 1)
        self.Wy = np.random.uniform(-limit, limit, (1, hidden_dim))
        self.by = np.zeros((1, 1))

    def forward(self, X):
        """
        Forward pass for GRU.
        X: shape (seq_len, 1) return sequence
        Returns:
            y: predicted volatility score (0-100)
            cache: dict containing intermediate variables for BPTT
        """
        seq_len = X.shape[0]
        h = np.zeros((self.hidden_dim, 1))
        
        hs = { -1: h.copy() }
        zs = {}
        rs = {}
        h_cands = {}
        xs = {}
        
        for t in range(seq_len):
            x_t = X[t, :].reshape(-1, 1)
            xs[t] = x_t
            
            z_t = self._sigmoid(np.dot(self.Wz, x_t) + np.dot(self.Uz, h) + self.bz)
            r_t = self._sigmoid(np.dot(self.Wr, x_t) + np.dot(self.Ur, h) + self.br)
            h_cand = self._tanh(np.dot(self.Wh, x_t) + np.dot(self.Uh, r_t * h) + self.bh)
            
            h = (1 - z_t) * h + z_t * h_cand
            
            hs[t] = h.copy()
            zs[t] = z_t
            rs[t] = r_t
            h_cands[t] = h_cand
            
        raw_out = np.dot(self.Wy, h) + self.by
        y = self._sigmoid(raw_out) * 100.0
        
        cache = {
            "xs": xs,
            "hs": hs,
            "zs": zs,
            "rs": rs,
            "h_cands": h_cands,
            "raw_out": raw_out,
            "y": y
        }
        return y[0, 0], cache

    def train_step(self, X, y_target):
        """
        Performs a single BPTT update.
        X: shape (seq_len, 1)
        y_target: float (0-100) target volatility score
        """
        y_pred, cache = self.forward(X)
        dy = (y_pred - y_target)
        
        sig = y_pred / 100.0
        d_raw_out = dy * 100.0 * sig * (1.0 - sig)
        
        dWy = np.dot(d_raw_out, cache["hs"][self.seq_len - 1].T)
        dby = d_raw_out
        
        dWz = np.zeros_like(self.Wz)
        dUz = np.zeros_like(self.Uz)
        dbz = np.zeros_like(self.bz)
        
        dWr = np.zeros_like(self.Wr)
        dUr = np.zeros_like(self.Ur)
        dbr = np.zeros_like(self.br)
        
        dWh = np.zeros_like(self.Wh)
        dUh = np.zeros_like(self.Uh)
        dbh = np.zeros_like(self.bh)
        
        dh = np.dot(self.Wy.T, d_raw_out)
        
        for t in reversed(range(self.seq_len)):
            h_prev = cache["hs"][t - 1]
            x_t = cache["xs"][t]
            z_t = cache["zs"][t]
            r_t = cache["rs"][t]
            h_cand = cache["h_cands"][t]
            
            dh_cand = dh * z_t
            dh_prev = dh * (1 - z_t)
            
            dz_t = dh * (h_cand - h_prev)
            d_net_z = dz_t * z_t * (1 - z_t)
            dWz += np.dot(d_net_z, x_t.T)
            dUz += np.dot(d_net_z, h_prev.T)
            dbz += d_net_z
            
            d_net_h = dh_cand * (1.0 - h_cand ** 2)
            dWh += np.dot(d_net_h, x_t.T)
            dUh += np.dot(d_net_h, (r_t * h_prev).T)
            dbh += d_net_h
            
            dr_t = np.dot(self.Uh.T, d_net_h) * h_prev
            d_net_r = dr_t * r_t * (1 - r_t)
            dWr += np.dot(d_net_r, x_t.T)
            dUr += np.dot(d_net_r, h_prev.T)
            dbr += d_net_r
            
            dh = dh_prev + np.dot(self.Uz.T, d_net_z) + np.dot(self.Ur.T, d_net_r) + np.dot(self.Uh.T, d_net_h) * r_t
            
        # Gradient clipping
        dWy = np.clip(dWy, -1.0, 1.0)
        dby = np.clip(dby, -1.0, 1.0)
        dWz = np.clip(dWz, -1.0, 1.0)
        dUz = np.clip(dUz, -1.0, 1.0)
        dbz = np.clip(dbz, -1.0, 1.0)
        dWr = np.clip(dWr, -1.0, 1.0)
        dUr = np.clip(dUr, -1.0, 1.0)
        dbr = np.clip(dbr, -1.0, 1.0)
        dWh = np.clip(dWh, -1.0, 1.0)
        dUh = np.clip(dUh, -1.0, 1.0)
        dbh = np.clip(dbh, -1.0, 1.0)
            
        # Parameter update
        self.Wy -= self.lr * dWy
        self.by -= self.lr * dby
        self.Wz -= self.lr * dWz
        self.Uz -= self.lr * dUz
        self.bz -= self.lr * dbz
        self.Wr -= self.lr * dWr
        self.Ur -= self.lr * dUr
        self.br -= self.lr * dbr
        self.Wh -= self.lr * dWh
        self.Uh -= self.lr * dUh
        self.bh -= self.lr * dbh
        
        return 0.5 * (y_pred - y_target) ** 2


class VolatilityLSTMModel(BaseVolatilityModel):
    """Long Short-Term Memory (LSTM) model for volatility forecasting."""
    def __init__(self, seq_len=10, hidden_dim=8, lr=0.01):
        super().__init__(seq_len, hidden_dim, lr)
        
        input_dim = 1
        limit = np.sqrt(6.0 / (hidden_dim + input_dim))
        
        # Forget gate
        self.Wf = np.random.uniform(-limit, limit, (hidden_dim, input_dim))
        self.Uf = np.random.uniform(-limit, limit, (hidden_dim, hidden_dim))
        self.bf = np.zeros((hidden_dim, 1))
        
        # Input gate
        self.Wi = np.random.uniform(-limit, limit, (hidden_dim, input_dim))
        self.Ui = np.random.uniform(-limit, limit, (hidden_dim, hidden_dim))
        self.bi = np.zeros((hidden_dim, 1))
        
        # Candidate memory gate
        self.Wc = np.random.uniform(-limit, limit, (hidden_dim, input_dim))
        self.Uc = np.random.uniform(-limit, limit, (hidden_dim, hidden_dim))
        self.bc = np.zeros((hidden_dim, 1))
        
        # Output gate
        self.Wo = np.random.uniform(-limit, limit, (hidden_dim, input_dim))
        self.Uo = np.random.uniform(-limit, limit, (hidden_dim, hidden_dim))
        self.bo = np.zeros((hidden_dim, 1))
        
        # Dense output layer
        self.Wy = np.random.uniform(-limit, limit, (1, hidden_dim))
        self.by = np.zeros((1, 1))

    def forward(self, X):
        """
        Forward pass for LSTM.
        X: shape (seq_len, 1) return sequence
        """
        seq_len = X.shape[0]
        h = np.zeros((self.hidden_dim, 1))
        c = np.zeros((self.hidden_dim, 1))
        
        hs = { -1: h.copy() }
        cs = { -1: c.copy() }
        fs = {}
        is_gate = {}
        c_cands = {}
        os_gate = {}
        c_tanhs = {}
        xs = {}
        
        for t in range(seq_len):
            x_t = X[t, :].reshape(-1, 1)
            xs[t] = x_t
            
            f_t = self._sigmoid(np.dot(self.Wf, x_t) + np.dot(self.Uf, h) + self.bf)
            i_t = self._sigmoid(np.dot(self.Wi, x_t) + np.dot(self.Ui, h) + self.bi)
            c_cand = self._tanh(np.dot(self.Wc, x_t) + np.dot(self.Uc, h) + self.bc)
            
            c = f_t * c + i_t * c_cand
            o_t = self._sigmoid(np.dot(self.Wo, x_t) + np.dot(self.Uo, h) + self.bo)
            
            c_tanh = self._tanh(c)
            h = o_t * c_tanh
            
            hs[t] = h.copy()
            cs[t] = c.copy()
            fs[t] = f_t
            is_gate[t] = i_t
            c_cands[t] = c_cand
            os_gate[t] = o_t
            c_tanhs[t] = c_tanh
            
        raw_out = np.dot(self.Wy, h) + self.by
        y = self._sigmoid(raw_out) * 100.0
        
        cache = {
            "xs": xs,
            "hs": hs,
            "cs": cs,
            "fs": fs,
            "is_gate": is_gate,
            "c_cands": c_cands,
            "os_gate": os_gate,
            "c_tanhs": c_tanhs,
            "raw_out": raw_out,
            "y": y
        }
        return y[0, 0], cache

    def train_step(self, X, y_target):
        y_pred, cache = self.forward(X)
        dy = (y_pred - y_target)
        
        sig = y_pred / 100.0
        d_raw_out = dy * 100.0 * sig * (1.0 - sig)
        
        dWy = np.dot(d_raw_out, cache["hs"][self.seq_len - 1].T)
        dby = d_raw_out
        
        dWf, dUf, dbf = np.zeros_like(self.Wf), np.zeros_like(self.Uf), np.zeros_like(self.bf)
        dWi, dUi, dbi = np.zeros_like(self.Wi), np.zeros_like(self.Ui), np.zeros_like(self.bi)
        dWc, dUc, dbc = np.zeros_like(self.Wc), np.zeros_like(self.Uc), np.zeros_like(self.bc)
        dWo, dUo, dbo = np.zeros_like(self.Wo), np.zeros_like(self.Uo), np.zeros_like(self.bo)
        
        dh = np.dot(self.Wy.T, d_raw_out)
        dc = np.zeros((self.hidden_dim, 1))
        
        for t in reversed(range(self.seq_len)):
            h_prev = cache["hs"][t - 1]
            c_prev = cache["cs"][t - 1]
            x_t = cache["xs"][t]
            
            f_t = cache["fs"][t]
            i_t = cache["is_gate"][t]
            c_cand = cache["c_cands"][t]
            o_t = cache["os_gate"][t]
            c_tanh = cache["c_tanhs"][t]
            
            # dh_t = o_t * dtanh(c_t) + ...
            # do_t = dh * c_tanh
            do_t = dh * c_tanh
            d_net_o = do_t * o_t * (1.0 - o_t)
            dWo += np.dot(d_net_o, x_t.T)
            dUo += np.dot(d_net_o, h_prev.T)
            dbo += d_net_o
            
            # dc = dh * o_t * (1 - c_tanh^2) + dc_next * f_next
            dc = dh * o_t * (1.0 - c_tanh ** 2) + dc
            
            # dc_cand = dc * i_t
            dc_cand = dc * i_t
            d_net_c = dc_cand * (1.0 - c_cand ** 2)
            dWc += np.dot(d_net_c, x_t.T)
            dUc += np.dot(d_net_c, h_prev.T)
            dbc += d_net_c
            
            # di_t = dc * c_cand
            di_t = dc * c_cand
            d_net_i = di_t * i_t * (1.0 - i_t)
            dWi += np.dot(d_net_i, x_t.T)
            dUi += np.dot(d_net_i, h_prev.T)
            dbi += d_net_i
            
            # df_t = dc * c_prev
            df_t = dc * c_prev
            d_net_f = df_t * f_t * (1.0 - f_t)
            dWf += np.dot(d_net_f, x_t.T)
            dUf += np.dot(d_net_f, h_prev.T)
            dbf += d_net_f
            
            # Backprop to prior cell state
            dc = dc * f_t
            
            # Backprop to prior hidden state
            dh = (
                np.dot(self.Uf.T, d_net_f) +
                np.dot(self.Ui.T, d_net_i) +
                np.dot(self.Uc.T, d_net_c) +
                np.dot(self.Uo.T, d_net_o)
            )
            
        # Gradient clipping
        dWy = np.clip(dWy, -1.0, 1.0)
        dby = np.clip(dby, -1.0, 1.0)
        
        dWf = np.clip(dWf, -1.0, 1.0)
        dUf = np.clip(dUf, -1.0, 1.0)
        dbf = np.clip(dbf, -1.0, 1.0)
        
        dWi = np.clip(dWi, -1.0, 1.0)
        dUi = np.clip(dUi, -1.0, 1.0)
        dbi = np.clip(dbi, -1.0, 1.0)
        
        dWc = np.clip(dWc, -1.0, 1.0)
        dUc = np.clip(dUc, -1.0, 1.0)
        dbc = np.clip(dbc, -1.0, 1.0)
        
        dWo = np.clip(dWo, -1.0, 1.0)
        dUo = np.clip(dUo, -1.0, 1.0)
        dbo = np.clip(dbo, -1.0, 1.0)
            
        # Parameter updates
        self.Wy -= self.lr * dWy
        self.by -= self.lr * dby
        
        self.Wf -= self.lr * dWf
        self.Uf -= self.lr * dUf
        self.bf -= self.lr * dbf
        
        self.Wi -= self.lr * dWi
        self.Ui -= self.lr * dUi
        self.bi -= self.lr * dbi
        
        self.Wc -= self.lr * dWc
        self.Uc -= self.lr * dUc
        self.bc -= self.lr * dbc
        
        self.Wo -= self.lr * dWo
        self.Uo -= self.lr * dUo
        self.bo -= self.lr * dbo
        
        return 0.5 * (y_pred - y_target) ** 2


class VolatilityModelWrapper:
    """Wrapper that facilitates training and predicting with either LSTM or GRU."""
    def __init__(self, model_type="gru", seq_len=10, hidden_dim=8, lr=0.01):
        self.model_type = model_type.lower()
        if self.model_type == "lstm":
            self.model = VolatilityLSTMModel(seq_len, hidden_dim, lr)
        else:
            self.model = VolatilityGRUModel(seq_len, hidden_dim, lr)

    def fit(self, rate_sequence, epochs=100, target_window=5):
        """
        Fits the model parameters on historical rate sequence.
        """
        rates = np.array(rate_sequence, dtype=float)
        if len(rates) < self.model.seq_len + target_window + 1:
            return []
            
        returns = np.diff(rates) / rates[:-1]
        
        # Calculate target rolling volatility
        vol = []
        for i in range(len(returns)):
            if i < target_window - 1:
                vol.append(np.nan)
            else:
                window_returns = returns[i - target_window + 1 : i + 1]
                vol.append(np.std(window_returns))
        vol = np.array(vol)
        
        # Normalize targets to [0, 100]
        valid_idx = ~np.isnan(vol)
        valid_vol = vol[valid_idx]
        if len(valid_vol) == 0:
            return []
            
        min_v, max_v = valid_vol.min(), valid_vol.max()
        if max_v == min_v:
            targets = np.zeros_like(vol)
            targets[valid_idx] = 50.0
        else:
            targets = np.zeros_like(vol)
            targets[valid_idx] = (vol[valid_idx] - min_v) / (max_v - min_v) * 100.0
            
        # Create sliding window samples
        X_samples = []
        y_samples = []
        for i in range(self.model.seq_len, len(returns)):
            if np.isnan(targets[i]):
                continue
            x_seq = returns[i - self.model.seq_len : i].reshape(-1, 1)
            X_samples.append(x_seq)
            y_samples.append(targets[i])
            
        if len(X_samples) == 0:
            return []
            
        losses = []
        for epoch in range(epochs):
            epoch_loss = 0
            for X_s, y_s in zip(X_samples, y_samples):
                epoch_loss += self.model.train_step(X_s, y_s)
            losses.append(epoch_loss / len(X_samples))
            
        return losses

    def predict(self, rate_sequence):
        """
        Emits prediction score in range [0, 100].
        """
        rates = np.array(rate_sequence, dtype=float)
        if len(rates) < self.model.seq_len + 1:
            return 50.0
            
        returns = np.diff(rates) / rates[:-1]
        x_seq = returns[-self.model.seq_len:].reshape(-1, 1)
        score, _ = self.model.forward(x_seq)
        return float(score)
