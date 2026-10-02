import math

def calculate_poisson_probability(lmbda: float, k: int) -> float:
    if lmbda <= 0 or k < 0:
        return 0.0
    return (math.pow(lmbda, k) * math.exp(-lmbda)) / math.factorial(k)

def analyze_pre_match_event(expected_home_goals: float, expected_away_goals: float, line: float = 2.5) -> dict:
    """
    Calcula matriz de probabilidades Poisson pre-partido para Over/Under de goles o córners.
    """
    total_expected = expected_home_goals + expected_away_goals
    max_goals = 10
    
    prob_under = 0.0
    for k in range(int(line) + 1):
        prob_under += calculate_poisson_probability(total_expected, k)

    prob_over = 1.0 - prob_under
    
    fair_odd_over = round(1 / prob_over, 2) if prob_over > 0 else 0
    fair_odd_under = round(1 / prob_under, 2) if prob_under > 0 else 0

    return {
        "expected_total": round(total_expected, 2),
        "line": line,
        "prob_over_pct": round(prob_over * 100, 2),
        "prob_under_pct": round(prob_under * 100, 2),
        "fair_odd_over": fair_odd_over,
        "fair_odd_under": fair_odd_under
    }

def calculate_kelly_stake(probability_pct: float, odd: float, bankroll: float = 1000.0, fraction: float = 0.25) -> dict:
    p = probability_pct / 100.0
    q = 1.0 - p
    b = odd - 1.0

    if b <= 0 or p <= 0:
        return {"has_value": False, "message": "Cuota o probabilidad inválida."}

    # EV% = (p * cuota) - 1
    ev_pct = ((p * odd) - 1.0) * 100.0
    f_kelly = (p * b - q) / b

    if f_kelly <= 0 or ev_pct <= 0:
        return {
            "has_value": False,
            "expected_value_pct": round(ev_pct, 2),
            "recommended_stake_pct": 0.0,
            "recommended_amount": 0.0,
            "message": f"⚠️ Sin Valor Esperado (EV: {ev_pct:.2f}%). Apuesta no recomendada."
        }

    adj_stake_pct = f_kelly * fraction * 100.0
    recommended_amount = (adj_stake_pct / 100.0) * bankroll

    return {
        "has_value": True,
        "expected_value_pct": round(ev_pct, 2),
        "raw_kelly_pct": round(f_kelly * 100, 2),
        "recommended_stake_pct": round(adj_stake_pct, 2),
        "recommended_amount": round(recommended_amount, 2)
    }