import math

def calculate_poisson_probability(lmbda: float, k: int) -> float:
    """Calcula la probabilidad exacta de 'k' eventos con una media 'lmbda' (Poisson)."""
    if lmbda <= 0 or k < 0:
        return 0.0
    return (math.pow(lmbda, k) * math.exp(-lmbda)) / math.factorial(k)

def poisson_over_under(lmbda: float, threshold: float) -> dict:
    """
    Calcula probabilidades acumuladas Over/Under para una media dada (ej. goles o córners).
    """
    max_k = int(threshold)
    prob_under_or_equal = sum(calculate_poisson_probability(lmbda, k) for k in range(max_k + 1))
    prob_over = 1.0 - prob_under_or_equal

    return {
        "lmbda": lmbda,
        "threshold": threshold,
        "prob_over": round(prob_over * 100, 2),
        "prob_under": round(prob_under_or_equal * 100, 2),
        "fair_odd_over": round(1 / prob_over, 2) if prob_over > 0 else 0,
        "fair_odd_under": round(1 / prob_under_or_equal, 2) if prob_under_or_equal > 0 else 0,
    }

def calculate_kelly_stake(probability_pct: float, odd: float, bankroll: float = 1000.0, fraction: float = 0.25) -> dict:
    """
    Calcula la fracción de banca a apostar según el Criterio de Kelly (con fraccionamiento de seguridad).
    f* = (p * b - q) / b
    Donde:
      b = cuota decimal - 1
      p = probabilidad estimada (0 a 1)
      q = 1 - p
    """
    p = probability_pct / 100.0
    q = 1.0 - p
    b = odd - 1.0

    if b <= 0 or p <= 0:
        return {"error": "Cuota o probabilidad inválida."}

    # Fracción teórica de Kelly
    f_kelly = (p * b - q) / b

    if f_kelly <= 0:
        return {
            "has_value": False,
            "recommended_stake_pct": 0.0,
            "recommended_amount": 0.0,
            "message": "⚠️ Sin valor esperado positivo (EV-). No se recomienda apostar."
        }

    # Kelly fraccionado para mitigar la varianza (por defecto 1/4 Kelly)
    adj_stake_pct = f_kelly * fraction * 100.0
    recommended_amount = (adj_stake_pct / 100.0) * bankroll

    return {
        "has_value": True,
        "raw_kelly_pct": round(f_kelly * 100, 2),
        "recommended_stake_pct": round(adj_stake_pct, 2),
        "recommended_amount": round(recommended_amount, 2),
        "expected_value_pct": round(((p * odd) - 1.0) * 100, 2)
    }