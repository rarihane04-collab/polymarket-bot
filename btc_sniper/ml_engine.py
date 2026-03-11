import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from sklearn.calibration import CalibratedClassifierCV
from collections import deque
import threading
import btc_sniper.config as config

class MLEngine:
    """
    Ensemble de 3 modèles entraînés en continu.
    
    MODÈLE 1 : GradientBoosting sur features Binance
    MODÈLE 2 : LogisticRegression sur features OB
    MODÈLE 3 : GradientBoosting sur features COMBINÉES
    """

    BINANCE_FEATURES = [
        "rsi", "ema_delta_pct", "momentum_pct",
        "volume_surge", "window_delta_pct",
        "tick_velocity", "candle_body_pct",
        "upper_wick_pct", "lower_wick_pct",
        "volatility_1m",
    ]

    OB_FEATURES = [
        "imb_l5", "imb_consistency", "imb_score",
        "flow_delta", "mid_vel_score", "spread_pct",
        "wmp_vs_mid", "liq_ratio",
    ]

    COMBINED_FEATURES = BINANCE_FEATURES + OB_FEATURES

    MIN_SAMPLES   = 50    # minimum pour entraîner
    RETRAIN_EVERY = 20    # ré-entraîner toutes les 20 fenêtres

    def __init__(self):
        # Modèle 1: Binance
        self.m1 = CalibratedClassifierCV(
            GradientBoostingClassifier(
                n_estimators=50,
                max_depth=3,
                learning_rate=0.1,
                subsample=0.8,
            ),
            method="isotonic", cv=3
        )
        # Modèle 2: OB
        self.m2 = CalibratedClassifierCV(
            LogisticRegression(
                C=1.0,
                max_iter=500,
                class_weight="balanced",
            ),
            method="sigmoid", cv=3
        )
        # Modèle 3: Combined
        self.m3 = CalibratedClassifierCV(
            GradientBoostingClassifier(
                n_estimators=100,
                max_depth=4,
                learning_rate=0.05,
                subsample=0.8,
                min_samples_leaf=5,
            ),
            method="isotonic", cv=3
        )
        self.scaler_ob   = StandardScaler()
        self.scaler_all  = StandardScaler()

        self.samples:  deque = deque(maxlen=500)
        self.trained:  bool  = False
        self.n_trains: int   = 0
        self._lock           = threading.Lock()
        self.weights         = [0.35, 0.25, 0.40]

    def add_sample(self, binance_feats: dict, ob_feats: dict, label: int):
        """Ajoute un sample d'entraînement."""
        sample = {**binance_feats, **ob_feats, "label": label}
        with self._lock:
            self.samples.append(sample)
            n = len(self.samples)
            if n >= self.MIN_SAMPLES and n % self.RETRAIN_EVERY == 0:
                threading.Thread(target=self._retrain, daemon=True).start()

    def _retrain(self):
        """Entraîne les 3 modèles en background."""
        try:
            with self._lock:
                df = pd.DataFrame(list(self.samples))

            labels = df["label"].values
            if len(np.unique(labels)) < 2:
                # Besoin des deux classes pour entraîner
                return

            X1 = df[self.BINANCE_FEATURES].fillna(0).values
            X2 = df[self.OB_FEATURES].fillna(0).values
            X3 = df[self.COMBINED_FEATURES].fillna(0).values

            # Fit scalers
            X2_sc = self.scaler_ob.fit_transform(X2)
            X3_sc = self.scaler_all.fit_transform(X3)

            # Fit models
            self.m1.fit(X1,    labels)
            self.m2.fit(X2_sc, labels)
            self.m3.fit(X3_sc, labels)
            
            self.trained   = True
            self.n_trains += 1
            config.debug_logger.info(
                f"ML | Retrained #{self.n_trains} on {len(df)} samples "
                f"(UP:{labels.sum()} DOWN:{len(labels)-labels.sum()})"
            )
        except Exception as e:
            config.debug_logger.error(f"ML | Retrain failed: {e}")

    def predict(self, binance_feats: dict, ob_feats: dict) -> dict:
        """Prédit la probabilité UP/DOWN."""
        if not self.trained:
            return {
                "p_up": 0.5, "p_down": 0.5,
                "direction": "WAITING",
                "confidence": 0.0,
                "edge": 0.0,
                "trained": False,
                "n_samples": len(self.samples),
            }

        try:
            X1 = np.array([[binance_feats.get(f, 0) for f in self.BINANCE_FEATURES]])
            X2 = np.array([[ob_feats.get(f, 0) for f in self.OB_FEATURES]])
            
            combined = {**binance_feats, **ob_feats}
            X3 = np.array([[combined.get(f, 0) for f in self.COMBINED_FEATURES]])

            X2_sc = self.scaler_ob.transform(X2)
            X3_sc = self.scaler_all.transform(X3)

            p1 = self.m1.predict_proba(X1)[0][1]
            p2 = self.m2.predict_proba(X2_sc)[0][1]
            p3 = self.m3.predict_proba(X3_sc)[0][1]

            # Weighted ensemble
            w = self.weights
            p_up = w[0]*p1 + w[1]*p2 + w[2]*p3
            p_down = 1 - p_up
            conf = max(p_up, p_down)
            edge = abs(p_up - 0.5)

            return {
                "p_up":       round(float(p_up), 4),
                "p_down":     round(float(p_down), 4),
                "direction":  "UP" if p_up > p_down else "DOWN",
                "confidence": round(float(conf), 4),
                "edge":       round(float(edge), 4),
                "trained":    True,
                "n_samples":  len(self.samples),
                "breakdown": {
                    "m1_binance": round(float(p1), 4),
                    "m2_ob":      round(float(p2), 4),
                    "m3_combined":round(float(p3), 4),
                }
            }
        except Exception as e:
            config.debug_logger.error(f"ML | Predict failed: {e}")
            return {
                "p_up": 0.5, "p_down": 0.5,
                "direction": "ERROR",
                "confidence": 0.0,
                "edge": 0.0,
                "trained": True,
                "n_samples": len(self.samples),
            }
