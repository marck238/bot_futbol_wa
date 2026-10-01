import math

def poisson_pmf(k: int, lamb: float) -> float:
    """Calcula la probabilidad puntual P(X = k) para una distribución de Poisson."""
    return (lamb ** k) * math.exp(-lamb) / math.factorial(k)

def calculate_match_probabilities(home_lambda: float, away_lambda: float):
    home_win = 0.0
    draw = 0.0
    away_win = 0.0
    over_2_5 = 0.0
    under_2_5 = 0.0

    # Matriz de marcadores exactos (de 0x0 hasta 9x9)
    for h in range(10):
        p_h = poisson_pmf(h, home_lambda)
        for a in range(10):
            p_a = poisson_pmf(a, away_lambda)
            p_cell = p_h * p_a

            # Mercado 1X2
            if h > a:
                home_win += p_cell
            elif h == a:
                draw += p_cell
            else:
                away_win += p_cell

            # Mercado Over / Under 2.5
            if (h + a) > 2.5:
                over_2_5 += p_cell
            else:
                under_2_5 += p_cell

    # Mercado Ambos Anotan (BTTS)
    btts_yes = (1 - math.exp(-home_lambda)) * (1 - math.exp(-away_lambda))
    btts_no = 1.0 - btts_yes

    return {
        "home_win": home_win,
        "draw": draw,
        "away_win": away_win,
        "over_2_5": over_2_5,
        "under_2_5": under_2_5,
        "btts_yes": btts_yes,
        "btts_no": btts_no
    }

def calculate_ev(prob: float, odds: float) -> float:
    """Calcula el Valor Esperado (EV%)"""
    ev = (prob * odds) - 1
    return round(ev * 100, 2)

def calculate_kelly_stake(prob: float, odds: float, fraction: float = 0.25) -> float:
    """Calcula el Stake Kelly Fraccionado (por defecto 1/4 Kelly)"""
    b = odds - 1
    p = prob
    q = 1 - p
    f_star = (b * p - q) / b
    
    if f_star <= 0:
        return 0.0
    
    kelly_fractional = f_star * fraction
    return round(kelly_fractional * 100, 2)