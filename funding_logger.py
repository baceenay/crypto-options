"""
Historique du funding BTC-PERPETUAL (Deribit, API publique).

Backfill complet puis top-up incrémental : à chaque run, ne télécharge que ce qui
manque depuis le dernier point stocké. Écrit funding_history.jsonl (1 ligne = 1h)
avec le taux horaire (interest_1h) et le taux 8h glissant (interest_8h).

Le funding est payé par les longs aux shorts quand il est positif : un short perp
(notre hedge, ou une jambe cash-and-carry) ENCAISSE interest_1h × notionnel chaque
heure. Annualisé ≈ interest_1h × 24 × 365.

Usage :
    python funding_logger.py             # backfill/top-up (défaut 4 ans)
    python funding_logger.py --years 2
    python funding_logger.py --stats     # affiche le rendement annualisé par année
"""
import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

HERE       = Path(__file__).parent
OUT        = HERE / "funding_history.jsonl"
BASE       = "https://www.deribit.com/api/v2/public"
INSTRUMENT = "BTC-PERPETUAL"
CHUNK_H    = 720   # heures par requête (~1 mois)


def _get(method: str, params: dict) -> dict:
    r = requests.get(f"{BASE}/{method}", params=params, timeout=15, verify=False)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"{method}: {data['error']}")
    return data["result"]


def load_existing() -> list[dict]:
    if not OUT.exists():
        return []
    rows = []
    for line in OUT.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def fetch_range(start_ms: int, end_ms: int) -> list[dict]:
    """Télécharge le funding horaire par chunks (l'API limite la taille de fenêtre)."""
    rows = []
    t = start_ms
    while t < end_ms:
        t2 = min(t + CHUNK_H * 3600 * 1000, end_ms)
        res = _get("get_funding_rate_history", {
            "instrument_name": INSTRUMENT,
            "start_timestamp": t,
            "end_timestamp":   t2,
        })
        for e in res:
            rows.append({
                "ts":          e["timestamp"],
                "date":        datetime.fromtimestamp(e["timestamp"] / 1000, tz=timezone.utc)
                                       .strftime("%Y-%m-%d %H:%M"),
                "interest_1h": e.get("interest_1h"),
                "interest_8h": e.get("interest_8h"),
                "index_price": e.get("index_price"),
            })
        t = t2
        time.sleep(0.25)   # politesse rate-limit
    return rows


def top_up(years: float) -> list[dict]:
    existing = load_existing()
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    if existing:
        start_ms = existing[-1]["ts"] + 1
        print(f"[funding] {len(existing)} points existants, top-up depuis {existing[-1]['date']}")
    else:
        start_ms = now_ms - int(years * 365 * 24 * 3600 * 1000)
        print(f"[funding] backfill complet sur {years:.1f} ans")
    if start_ms >= now_ms - 3600 * 1000:
        print("[funding] deja a jour")
        return existing
    new = fetch_range(start_ms, now_ms)
    with OUT.open("a", encoding="utf-8") as fh:
        for r in new:
            fh.write(json.dumps(r) + "\n")
    print(f"[funding] +{len(new)} points -> {len(existing) + len(new)} au total")
    return existing + new


def stats(rows: list[dict]):
    """Rendement annualisé du funding encaissé par un short perp, par année."""
    if not rows:
        print("Aucune donnée.")
        return
    by_year: dict[str, list[float]] = {}
    for r in rows:
        v = r.get("interest_1h")
        if v is None:
            continue
        by_year.setdefault(r["date"][:4], []).append(float(v))
    print(f"\n{'annee':>6} {'n_heures':>9} {'ann. moyen':>11} {'% h positives':>14} {'pire mois ann.':>15}")
    for y in sorted(by_year):
        vs = by_year[y]
        ann = sum(vs) / len(vs) * 24 * 365 * 100
        pos = sum(1 for v in vs if v > 0) / len(vs) * 100
        # pire mois : moyenne mensuelle annualisée minimale
        by_month: dict[str, list[float]] = {}
        for r in rows:
            if r["date"][:4] == y and r.get("interest_1h") is not None:
                by_month.setdefault(r["date"][:7], []).append(float(r["interest_1h"]))
        worst = min((sum(m) / len(m) * 24 * 365 * 100) for m in by_month.values())
        print(f"{y:>6} {len(vs):>9} {ann:>10.2f}% {pos:>13.1f}% {worst:>14.2f}%")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=float, default=4.0)
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args()
    rows = top_up(args.years) if not args.stats else load_existing()
    if args.stats or rows:
        stats(rows)
