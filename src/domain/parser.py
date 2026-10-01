import re

def parse_bet_command(text: str):
    """
    Soporta comandos con lambdas dinámicos y mercados opcionales:
    - /analizar Real Madrid vs Barcelona (2.10, 1.40) Over 2.5 @ 1.85
    - /analizar Real Madrid vs Barcelona 2.10 - 1.40 | BTTS @ 1.75
    - Peñarol vs Nacional (1.6, 0.9) 2.20
    """
    clean_text = text.strip()
    if clean_text.lower().startswith("/analizar"):
        clean_text = clean_text[9:].strip()

    # 1. Extraer cuota al final (@ 2.10 o simplemente 2.10)
    odds_match = re.search(r'(?:\s+@|\s+)\s*(\d+(?:\.\d+)?)$', clean_text)
    if not odds_match:
        return None
    
    odds = float(odds_match.group(1))
    body = clean_text[:odds_match.start()].strip()

    # 2. Separar por ' vs '
    vs_split = re.split(r'\s+vs\s+', body, flags=re.IGNORECASE, maxsplit=1)
    if len(vs_split) < 2:
        return None

    home_team = vs_split[0].strip()
    rest = vs_split[1].strip()

    # 3. Extraer lambdas opcionales: (2.1, 1.4), [2.1, 1.4], 2.1 - 1.4, 2.1/1.4
    lambda_pattern = r'[\(\[]?\s*(\d+(?:\.\d+)?)\s*[\,\-\/]\s*(\d+(?:\.\d+)?)\s*[\)\]]?'
    lambda_match = re.search(lambda_pattern, rest)

    home_lambda = 1.85
    away_lambda = 1.30

    if lambda_match:
        home_lambda = float(lambda_match.group(1))
        away_lambda = float(lambda_match.group(2))
        rest_before = rest[:lambda_match.start()].strip()
        rest_after = rest[lambda_match.end():].strip()
        rest = (rest_before + " " + rest_after).strip()

    # 4. Extraer mercado opcional
    if "|" in rest:
        parts = rest.split("|", 1)
        away_team = parts[0].strip()
        raw_market = parts[1].strip().lower()
    else:
        market_pattern = r'(over\s*2\.5|\+2\.5|under\s*2\.5|\-2\.5|btts(?:\s*s[ií]|\s*yes|\s*no)?|ambos\s*(?:no\s*)?(?:anotan|marcan)|empate|draw|x|local|visita|visitante|1|2)$'
        m_match = re.search(market_pattern, rest, re.IGNORECASE)
        if m_match:
            raw_market = m_match.group(1).strip().lower()
            away_team = rest[:m_match.start()].strip()
        else:
            away_team = rest.strip()
            raw_market = ""

    if not away_team:
        return None

    market_key = "home_win"
    market_name = f"Gana {home_team}"

    if raw_market in ["over 2.5", "over2.5", "+2.5", "mas 2.5", "over"]:
        market_key = "over_2_5"
        market_name = "Over 2.5 Goles"
    elif raw_market in ["under 2.5", "under2.5", "-2.5", "menos 2.5", "under"]:
        market_key = "under_2_5"
        market_name = "Under 2.5 Goles"
    elif raw_market in ["btts", "btts si", "btts sí", "btts yes", "ambos anotan", "ambos marcan", "btts_yes"]:
        market_key = "btts_yes"
        market_name = "Ambos Anotan (Sí)"
    elif raw_market in ["btts no", "ambos no anotan", "ambos no marcan", "btts_no"]:
        market_key = "btts_no"
        market_name = "Ambos Anotan (No)"
    elif raw_market in ["empate", "x", "draw"]:
        market_key = "draw"
        market_name = "Empate"
    elif raw_market in ["visitante", "visita", "gana visita", "gana visitante", "2"]:
        market_key = "away_win"
        market_name = f"Gana {away_team}"
    elif raw_market in ["local", "gana local", "1"]:
        market_key = "home_win"
        market_name = f"Gana {home_team}"

    return {
        "home_team": home_team,
        "away_team": away_team,
        "home_lambda": home_lambda,
        "away_lambda": away_lambda,
        "odds": odds,
        "market_key": market_key,
        "market_name": market_name,
        "match_description": f"{home_team} vs {away_team}"
    }