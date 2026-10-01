import numpy as np
from scipy.stats import poisson

def calcular_matriz_probabilidades(lambda_local: float, lambda_visitante: float, max_goles: int = 6) -> np.ndarray:
    """
    Genera una matriz de probabilidades de marcadores exactos usando distribución de Poisson.
    Filas: Goles Local | Columnas: Goles Visitante.
    """
    prob_local = [poisson.pmf(i, lambda_local) for i in range(max_goles)]
    prob_visitante = [poisson.pmf(j, lambda_visitante) for j in range(max_goles)]
    return np.outer(prob_local, prob_visitante)

def calcular_probabilidades_mercados(matriz: np.ndarray) -> dict:
    """
    Calcula las probabilidades de los mercados principales a partir de la matriz de goles.
    """
    prob_local = float(np.sum(np.tril(matriz, -1)))
    prob_empate = float(np.sum(np.diag(matriz)))
    prob_visitante = float(np.sum(np.triu(matriz, 1)))

    filas, columnas = matriz.shape
    over_25 = 0.0
    for i in range(filas):
        for j in range(columnas):
            if i + j > 2.5:
                over_25 += matriz[i, j]
    under_25 = 1.0 - over_25

    btts_si = float(np.sum(matriz[1:, 1:]))
    btts_no = 1.0 - btts_si

    return {
        "1": round(prob_local, 4),
        "X": round(prob_empate, 4),
        "2": round(prob_visitante, 4),
        "Over_2.5": round(over_25, 4),
        "Under_2.5": round(under_25, 4),
        "BTTS_Si": round(btts_si, 4),
        "BTTS_No": round(btts_no, 4)
    }

def calcular_ev(probabilidad: float, cuota: float) -> float:
    """
    Calcula el Valor Esperado (EV) en porcentaje.
    Un EV > 0 indica una apuesta con valor (+EV).
    """
    ev = (probabilidad * cuota) - 1.0
    return round(ev * 100, 2)

def criterio_kelly(probabilidad: float, cuota: float, fraccion: float = 0.25) -> float:
    """
    Calcula el Stake recomendado (% de Bankroll) usando Fractional Kelly (default 1/4 Kelly).
    """
    b = cuota - 1.0
    q = 1.0 - probabilidad
    f_star = (probabilidad * b - q) / b
    if f_star <= 0:
        return 0.0
    return round(f_star * fraccion * 100, 2)

if __name__ == "__main__":
    # Ejemplo de prueba rápida:
    # Local promedio 1.8 goles esperados, Visitante 1.1 goles esperados
    l_loc, l_vis = 1.8, 1.1
    matriz = calcular_matriz_probabilidades(l_loc, l_vis)
    mercados = calcular_probabilidades_mercados(matriz)
    
    print("📊 Probabilidades estimadas:", mercados)
    
    # Prueba de EV+ para victoria local a cuota 2.10
    p_local = mercados["1"]
    cuota_casa = 2.10
    ev = calcular_ev(p_local, cuota_casa)
    stake = criterio_kelly(p_local, cuota_casa)
    
    print(f"🎯 EV+: {ev}% | Stake Kelly recomendado: {stake}% del bankroll")
