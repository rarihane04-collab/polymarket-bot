import math
from scipy.stats import norm
import time

def price_binary_call(S0: float, K: float, sigma_4h: float, hours_left: float) -> float:
    """
    Price a binary cash-or-nothing call option using Black-Scholes.
    P_YES = e^(-r*T) * N(d2)
    
    Where:
    - S0: current spot price
    - K: Price to Beat (open_4h price)
    - sigma_4h: volatility for 4H horizon
    - hours_left: time left in the 4H window
    """
    if hours_left <= 0:
        return 1.0 if S0 > K else 0.0
        
    if sigma_4h <= 0:
        return 1.0 if S0 > K else 0.0
        
    # T is time left as fraction of a year
    T = hours_left / 8760.0
    r = 0.0  # Risk free rate is negligible for 4H
    
    # We need annualized sigma for the standard BS formula since T is in years.
    # We know sigma_4h = sigma_annuel / sqrt(2190).
    # So sigma_annuel = sigma_4h * sqrt(2190)
    sigma_annuel = sigma_4h * math.sqrt(2190)
    
    if sigma_annuel == 0:
        return 1.0 if S0 > K else 0.0

    d2 = (math.log(S0 / K) + (r - (sigma_annuel**2) / 2) * T) / (sigma_annuel * math.sqrt(T))
    
    # P_YES = e^(-r*T) * N(d2)
    # Since r = 0, e^(-r*T) = 1
    p_yes = norm.cdf(d2)
    
    return p_yes

class Pricer:
    def __init__(self):
        pass
        
    def evaluate(self, spot: float, open_price: float, sigma_4h: float, kline_start_time_ms: int) -> float:
        """
        Evaluates the fair value of YES for the current 4H window.
        """
        # A 4H window is 4 * 60 * 60 * 1000 = 14,400,000 ms
        window_duration_ms = 4 * 60 * 60 * 1000
        current_time_ms = int(time.time() * 1000)
        
        elapsed_ms = current_time_ms - kline_start_time_ms
        left_ms = window_duration_ms - elapsed_ms
        hours_left = max(0.0, left_ms / (1000 * 60 * 60))
        
        # Fair value of YES
        fair_value = price_binary_call(spot, open_price, sigma_4h, hours_left)
        return fair_value
