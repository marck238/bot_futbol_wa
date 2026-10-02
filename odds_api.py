import os
import httpx
from datetime import datetime, timezone, timedelta
import math
from analytics import calculate_kelly_stake

ODDS_API_KEY = os.getenv("ODDS_API_KEY")

# Zona horaria local (Uruguay / Argentina GMT-3)
LOCAL_TZ = timezone(timedelta(hours=-3))

# Ligas principales de fútbol en The Odds API
FEATURED_SPORTS = [
    "soccer_epl",
    "soccer_spain_la_liga",
    "soccer_italy_serie_a",
    "soccer_germany_bundesliga",
    "soccer_france_ligue_one",
    "soccer_conmebol_copa_libertadores",
    "soccer_conmebol_copa_sudamericana",
    "soccer_argentina_primera_division",
    "soccer_brazil_campeonato",
    "soccer_usa_mls",
    "soccer_uefa_champs_league",
    "soccer_uefa_europa_league",
    "soccer_mexico_ligamx",
    "soccer_portugal_primeira_liga",
    "soccer_netherlands_eredivisie",
    "soccer_turkey_super_league",
    "soccer_chile_campeonato",
    "soccer_colombia_categoria_primera_a",
    "soccer_japan_j_league",
    "soccer_korea_kleague1",
    "soccer_spl",
    "soccer_australia_aleague"
]


def poisson_pmf(k: int, lamb: float) -> float:
    """Probabilidad puntual de Poisson P(X=k)."""
    return (math.pow(lamb, k) * math.exp(-lamb)) / math.factorial(k)


def calculate_poisson_matrix(home_exp: float, away_exp: float):
    """
    Construye la matriz de marcadores exactos (hasta 8x8)
    y calcula probabilidades independientes para los mercados principales.
    """
    scores = {}
    prob_home = 0.0
    prob_draw = 0.0
    prob_away = 0.0

    for h in range(8):
        p_h = poisson_pmf(h, home_exp)
        for a in range(8):
            p_a = poisson_pmf(a, away_exp)
            p_cell = p_h * p_a
            scores[(h, a)] = p_cell

            if h > a:
                prob_home += p_cell
            elif h == a:
                prob_draw += p_cell
            else:
                prob_away += p_cell

    prob_over_1_5 = sum(p for (h, a), p in scores.items() if (h + a) > 1.5)
    prob_over_2_5 = sum(p for (h, a), p in scores.items() if (h + a) > 2.5)
    prob_under_2_5 = 1.0 - prob_over_2_5
    prob_over_3_5 = sum(p for (h, a), p in scores.items() if (h + a) > 3.5)

    return {
        "Victoria Local": round(prob_home * 100, 2),
        "Empate": round(prob_draw * 100, 2),
        "Victoria Visitante": round(prob_away * 100, 2),
        "Over 1.5 Goles": round(prob_over_1_5 * 100, 2),
        "Over 2.5 Goles": round(prob_over_2_5 * 100, 2),
        "Under 2.5 Goles": round(prob_under_2_5 * 100, 2),
        "Over 3.5 Goles": round(prob_over_3_5 * 100, 2)
    }


async def get_upcoming_ev_picks(
    hours: int = None,
    days_offset: int = None,
    is_weekend: bool = False,
    min_ev: float = 0.0
) -> dict:
    if not ODDS_API_KEY:
        return {"status": "NO_API_KEY", "data": []}

    now = datetime.now(timezone.utc)

    # Definir rango de tiempo en UTC
    if hours:
        time_start = now
        time_end = now + timedelta(hours=hours)
    elif days_offset is not None:
        now_local = now.astimezone(LOCAL_TZ)
        target_local = now_local + timedelta(days=days_offset)
        time_start_local = target_local.replace(hour=0, minute=0, second=0, microsecond=0)
        time_end_local = target_local.replace(hour=23, minute=59, second=59, microsecond=0)
        time_start = time_start_local.astimezone(timezone.utc)
        time_end = time_end_local.astimezone(timezone.utc)
    elif is_weekend:
        now_local = now.astimezone(LOCAL_TZ)
        days_until_saturday = (5 - now_local.weekday()) % 7
        if days_until_saturday == 0 and now_local.weekday() != 5:
            days_until_saturday = 7
        saturday = now_local + timedelta(days=days_until_saturday)
        time_start_local = saturday.replace(hour=0, minute=0, second=0, microsecond=0)
        time_end_local = (saturday + timedelta(days=1)).replace(hour=23, minute=59, second=59, microsecond=0)
        time_start = time_start_local.astimezone(timezone.utc)
        time_end = time_end_local.astimezone(timezone.utc)
    else:
        time_start = now
        time_end = now + timedelta(hours=24)

    results = []

    async with httpx.AsyncClient(timeout=12.0) as client:
        for sport_key in FEATURED_SPORTS:
            url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds/"
            params = {
                "apiKey": ODDS_API_KEY,
                "regions": "eu,us",
                "markets": "h2h,totals",
                "dateFormat": "iso"
            }

            try:
                resp = await client.get(url, params=params)
                if resp.status_code != 200:
                    continue

                matches = resp.json()
                for match in matches:
                    commence_time_str = match.get("commence_time")
                    if not commence_time_str:
                        continue

                    match_dt = datetime.fromisoformat(commence_time_str.replace("Z", "+00:00"))

                    if not (time_start <= match_dt <= time_end):
                        continue

                    home_team = match.get("home_team", "Local")
                    away_team = match.get("away_team", "Visitante")
                    sport_title = match.get("sport_title", "Fútbol")

                    bookmakers = match.get("bookmakers", [])
                    if not bookmakers:
                        continue

                    bm = bookmakers[0]
                    h2h_odds = {}
                    totals_odds = {}

                    for market in bm.get("markets", []):
                        if market["key"] == "h2h":
                            for outcome in market.get("outcomes", []):
                                name = outcome.get("name")
                                price = float(outcome.get("price", 0))
                                if name == home_team:
                                    h2h_odds["Victoria Local"] = price
                                elif name == away_team:
                                    h2h_odds["Victoria Visitante"] = price
                                elif name.lower() == "draw":
                                    h2h_odds["Empate"] = price
                        elif market["key"] == "totals":
                            for outcome in market.get("outcomes", []):
                                name = outcome.get("name")
                                point = outcome.get("point")
                                price = float(outcome.get("price", 0))
                                if point == 2.5:
                                    if name == "Over":
                                        totals_odds["Over 2.5 Goles"] = price
                                    elif name == "Under":
                                        totals_odds["Under 2.5 Goles"] = price
                                elif point == 1.5 and name == "Over":
                                    totals_odds["Over 1.5 Goles"] = price

                    # Estimación Poisson con ventaja de localía y calibración de expectativa
                    odd_h = h2h_odds.get("Victoria Local", 2.20)
                    odd_a = h2h_odds.get("Victoria Visitante", 3.20)

                    rel_strength = odd_a / (odd_h + 0.01)
                    base_total_exp = 2.60

                    if totals_odds.get("Over 2.5 Goles", 2.0) < 1.80:
                        base_total_exp = 2.95
                    elif totals_odds.get("Over 2.5 Goles", 2.0) > 2.15:
                        base_total_exp = 2.25

                    home_exp = max(0.7, round((base_total_exp / 2.0) * math.sqrt(rel_strength) * 1.12, 2))
                    away_exp = max(0.5, round(base_total_exp - home_exp, 2))

                    probs = calculate_poisson_matrix(home_exp, away_exp)

                    match_dt_local = match_dt.astimezone(LOCAL_TZ)
                    formatted_time = match_dt_local.strftime("%H:%M Hs")

                    # Compilar todos los mercados disponibles
                    all_available_markets = {**h2h_odds, **totals_odds}

                    for m_name, bookmaker_odd in all_available_markets.items():
                        if bookmaker_odd <= 1.0 or m_name not in probs:
                            continue

                        estimated_prob = probs[m_name]
                        fair_odd = round(100.0 / estimated_prob, 2) if estimated_prob > 0 else 99.0
                        kelly = calculate_kelly_stake(estimated_prob, bookmaker_odd)
                        ev = kelly.get("expected_value_pct", 0)

                        # Filtro flexible: si min_ev es 0 (menú normal), aceptamos EV >= -3% para mostrar opciones
                        threshold = min_ev if min_ev > 0 else -3.0

                        if ev >= threshold:
                            results.append({
                                "match": f"{home_team} vs {away_team}",
                                "league": sport_title,
                                "time": formatted_time,
                                "market": m_name,
                                "prob": estimated_prob,
                                "fair_odd": fair_odd,
                                "bookmaker_odd": bookmaker_odd,
                                "ev": ev,
                                "stake": kelly.get("recommended_stake_pct", 0)
                            })

            except Exception as e:
                print(f"Error procesando {sport_key}: {e}")
                continue

    # Ordenar por EV descendente (las mejores apuestas al principio)
    results.sort(key=lambda x: x["ev"], reverse=True)
    return {"status": "SUCCESS", "data": results}


async def get_best_parlays(hours: int = None, days_offset: int = None) -> dict:
    single_res = await get_upcoming_ev_picks(hours=hours, days_offset=days_offset, min_ev=-1.0)

    if single_res["status"] == "NO_API_KEY":
        return {"status": "NO_API_KEY", "parlay": None}

    picks = single_res.get("data", [])

    if len(picks) < 2:
        return {"status": "SUCCESS", "parlay": None}

    unique_picks = []
    seen_matches = set()
    for p in sorted(picks, key=lambda x: x["ev"], reverse=True):
        if p["match"] not in seen_matches:
            seen_matches.add(p["match"])
            unique_picks.append(p)
        if len(unique_picks) == 4:
            break

    if len(unique_picks) < 2:
        return {"status": "SUCCESS", "parlay": None}

    total_odd = 1.0
    total_prob = 1.0

    for p in unique_picks:
        total_odd *= p["bookmaker_odd"]
        total_prob *= (p["prob"] / 100.0)

    total_ev = ((total_prob * total_odd) - 1.0) * 100.0

    b = total_odd
    p = total_prob
    raw_kelly = ((p * b - 1.0) / (b - 1.0)) * 100.0 if b > 1.0 else 0.0
    conservative_stake = max(0.5, round(raw_kelly / 8.0, 2)) if total_ev > 0 else 0.5
    recommended_stake = min(conservative_stake, 2.0)

    parlay_data = {
        "legs_count": len(unique_picks),
        "legs": unique_picks,
        "total_odd": round(total_odd, 2),
        "total_prob_pct": round(total_prob * 100, 2),
        "total_ev": round(total_ev, 2),
        "recommended_stake_pct": recommended_stake
    }

    return {"status": "SUCCESS", "parlay": parlay_data}