import math
from typing import Dict, Tuple

# Promedios estándar por defecto si la liga no proporciona datos agregados aún
DEFAULT_LEAGUE_AVG = {
    "league_home_avg": 1.45,
    "league_away_avg": 1.15
}

def calculate_dynamic_mus(
    home_stats: Dict[str, float],
    away_stats: Dict[str, float],
    league_stats: Dict[str, float] = DEFAULT_LEAGUE_AVG
) -> Tuple[float, float]:
    """
    Calcula mu_home y mu_away según el rendimiento reciente (últimos N partidos).
    
    Formatos esperados de entrada:
    home_stats = {"gf_home_avg": 1.80, "gc_home_avg": 0.60}  # Goles favor/contra como local
    away_stats = {"gf_away_avg": 1.10, "gc_away_avg": 1.50}  # Goles favor/contra como visitante
    """
    l_home_avg = league_stats.get("league_home_avg", 1.45)
    l_away_avg = league_stats.get("league_away_avg", 1.15)

    # 1. Fuerza de Ataque Local y Debilidad Defensiva Visitante
    home_attack_strength = home_stats["gf_home_avg"] / l_home_avg if l_home_avg > 0 else 1.0
    away_defense_weakness = away_stats["gc_away_avg"] / l_home_avg if l_home_avg > 0 else 1.0

    # 2. Fuerza de Ataque Visitante y Debilidad Defensiva Local
    away_attack_strength = away_stats["gf_away_avg"] / l_away_avg if l_away_avg > 0 else 1.0
    home_defense_weakness = home_stats["gc_home_avg"] / l_away_avg if l_away_avg > 0 else 1.0

    # 3. Cálculo final de Expectativa de Goles (Mu)
    mu_home = home_attack_strength * away_defense_weakness * l_home_avg
    mu_away = away_attack_strength * home_defense_weakness * l_away_avg

    # Acotamiento razonable para evitar divergencias en muestras pequeñas
    mu_home = max(0.20, min(mu_home, 4.50))
    mu_away = max(0.20, min(mu_away, 4.50))

    return round(mu_home, 2), round(mu_away, 2)


def calculate_weighted_average(match_goals: list[int], decay_factor: float = 0.85) -> float:
    """
    Calcula el promedio ponderado dando mayor peso a los partidos más recientes.
    match_goals: Lista ordenada de más reciente a más antiguo [último, penúltimo, ...]
    """
    if not match_goals:
        return 1.0

    weights = [math.pow(decay_factor, i) for i in range(len(match_goals))]
    weighted_sum = sum(g * w for g, w in zip(match_goals, weights))
    total_weight = sum(weights)

    return weighted_sum / total_weight