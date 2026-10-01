from typing import List, Dict

def calculate_parlay(selections: List[Dict]) -> Dict:
    """
    Calcula la probabilidad acumulada, cuota total, EV conjunto y Stake Kelly
    para una apuesta combinada de 2 a 5 partidos.
    """
    combined_odds = 1.0
    combined_prob = 1.0

    for sel in selections:
        combined_odds *= sel["odds"]
        combined_prob *= sel["prob"]

    # EV conjunto = (Probabilidad Acumulada * Cuota Acumulada) - 1
    combined_ev = (combined_prob * combined_odds) - 1.0
    ev_percentage = round(combined_ev * 100, 2)

    # Kelly para combinadas: mayor varianza -> fraccionamiento conservador (1/8 Kelly)
    b = combined_odds - 1.0
    p = combined_prob
    q = 1.0 - p
    
    f_star = (b * p - q) / b if b > 0 else 0
    kelly_stake = round(max(0, f_star * 0.125) * 100, 2) if f_star > 0 else 0.0

    return {
        "num_legs": len(selections),
        "selections": selections,
        "combined_odds": round(combined_odds, 2),
        "combined_prob_pct": round(combined_prob * 100, 1),
        "ev_percentage": ev_percentage,
        "kelly_stake": kelly_stake
    }