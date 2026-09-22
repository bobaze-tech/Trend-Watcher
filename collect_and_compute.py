"""
TrendBoard — collecte multi-plateformes
------------------------------------------
Exécuté automatiquement toutes les 10 minutes par
.github/workflows/update.yml.

Plateformes couvertes (celles pour lesquelles des données fiables existent) :
- TikTok  : via l'endpoint interne du Creative Center (non officiel, peut
            casser si TikTok change son site — voir le warning plus bas)
- YouTube : via l'API Data v3 officielle de Google (fiable, nécessite une
            clé API gratuite — voir README.md)

Instagram a été volontairement exclu : Meta ne fournit aucune API publique
de hashtags/reels tendance, y compris pour les comptes business, donc
aucune donnée fiable n'est disponible côté Instagram.

Le fichier de sortie data.json contient un champ "platform" par entrée,
et le site filtre dessus selon l'onglet sélectionné.
"""

import json
import os
from datetime import datetime, timezone

import requests

HISTORY_PATH = "history.json"
DATA_PATH = "data.json"
MAX_HISTORY_POINTS = 288  # ~48h à raison d'un point / 10 min


# ---------------------------------------------------------------------
# TikTok — endpoint interne non officiel
# ---------------------------------------------------------------------
TIKTOK_ENDPOINT = "https://ads.tiktok.com/creative_radar_api/v1/popular_trend/hashtag/list"
TIKTOK_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Content-Type": "application/json",
}
TIKTOK_PARAMS = {"page": 1, "limit": 50, "period": 7, "country_code": "FR"}


def fetch_tiktok() -> list[dict]:
    """Retourne une liste de {tag, count} pour TikTok."""
    try:
        r = requests.get(TIKTOK_ENDPOINT, headers=TIKTOK_HEADERS, params=TIKTOK_PARAMS, timeout=15)
        print(f"[tiktok] code HTTP reçu : {r.status_code}")
    except requests.RequestException as exc:
        print(f"[tiktok] échec réseau : {exc}")
        return []

    if r.status_code != 200:
        # On log un extrait du corps de la réponse pour comprendre le blocage
        # (souvent un 403/429 avec une page HTML de type "access denied").
        print(f"[tiktok] réponse non-200, extrait du corps : {r.text[:300]!r}")
        return []

    try:
        payload = r.json()
    except ValueError:
        print(f"[tiktok] réponse non-JSON, extrait : {r.text[:300]!r}")
        return []

    items = payload.get("data", {}).get("list", [])
    if not items:
        # On log les clés de premier niveau pour voir si TikTok a changé
        # la structure de sa réponse (le chemin data.list ne serait alors
        # plus le bon).
        print(f"[tiktok] 0 item trouvé au chemin data.list — clés reçues : {list(payload.keys())}")
        print(f"[tiktok] extrait complet du payload : {str(payload)[:500]}")

    return [
        {"tag": it.get("hashtag_name"), "count": it.get("video_views") or it.get("video_count") or 0}
        for it in items
        if it.get("hashtag_name")
    ]


# ---------------------------------------------------------------------
# YouTube — API Data v3 officielle
# ---------------------------------------------------------------------
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "")
YOUTUBE_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"


def fetch_youtube() -> list[dict]:
    """
    Retourne une liste de {tag, count} pour YouTube, construite à partir
    des vidéos les plus populaires du jour (chart=mostPopular), filtrées
    sur les Shorts (durée <= 60s), en agrégeant les vues par hashtag
    présent dans les tags/le titre de la vidéo.
    """
    if not YOUTUBE_API_KEY:
        print("[youtube] YOUTUBE_API_KEY absente — collecte ignorée. Voir README.md.")
        return []

    try:
        r = requests.get(
            YOUTUBE_VIDEOS_URL,
            params={
                "part": "snippet,statistics,contentDetails",
                "chart": "mostPopular",
                "regionCode": "FR",
                "maxResults": 50,
                "key": YOUTUBE_API_KEY,
            },
            timeout=15,
        )
        r.raise_for_status()
        items = r.json().get("items", [])
    except requests.RequestException as exc:
        print(f"[youtube] échec de la collecte : {exc}")
        return []

    def is_short(duration_iso: str) -> bool:
        # Format ISO 8601 simplifié type "PT58S" ou "PT1M2S"
        if "H" in duration_iso:
            return False
        minutes, seconds = 0, 0
        num = ""
        for ch in duration_iso.replace("PT", ""):
            if ch.isdigit():
                num += ch
            elif ch == "M":
                minutes = int(num or 0)
                num = ""
            elif ch == "S":
                seconds = int(num or 0)
                num = ""
        return (minutes * 60 + seconds) <= 60

    tag_counts: dict[str, int] = {}
    for it in items:
        duration = it.get("contentDetails", {}).get("duration", "")
        if not is_short(duration):
            continue
        views = int(it.get("statistics", {}).get("viewCount", 0))
        tags = it.get("snippet", {}).get("tags", []) or []
        title_hashtags = [w[1:] for w in it.get("snippet", {}).get("title", "").split() if w.startswith("#")]
        for tag in (tags + title_hashtags):
            clean = tag.strip().lower().replace(" ", "")
            if not clean:
                continue
            tag_counts[clean] = tag_counts.get(clean, 0) + views

    return [{"tag": tag, "count": count} for tag, count in tag_counts.items()]


# ---------------------------------------------------------------------
# Historique, momentum, écriture
# ---------------------------------------------------------------------
def load_history() -> dict:
    if os.path.exists(HISTORY_PATH):
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_history(history: dict) -> None:
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False)


def compute_momentum(points: list[dict]) -> tuple[float, float, float]:
    counts = [p["v"] for p in points]
    if len(counts) < 3:
        return 0.0, 0.0, 0.0
    diffs = [counts[i + 1] - counts[i] for i in range(len(counts) - 1)]
    growth = sum(diffs) / len(diffs)
    accel_pts = [diffs[i + 1] - diffs[i] for i in range(len(diffs) - 1)]
    accel = sum(accel_pts) / len(accel_pts) if accel_pts else 0.0
    return growth, accel, growth + accel * 3


def main() -> None:
    history = load_history()
    now = datetime.now(timezone.utc).isoformat()

    # TikTok désactivé : l'endpoint testé (Ads Manager) renvoie
    # systématiquement "no permission" (code 40101) sans session
    # publicitaire authentifiée — ce n'est pas contournable proprement.
    # On garde fetch_tiktok() dans le fichier pour référence, mais on ne
    # l'appelle plus. Seul YouTube est collecté pour l'instant.
    platform_results = {
        "youtube": fetch_youtube(),
    }

    for platform, items in platform_results.items():
        for item in items:
            key = f"{platform}:{item['tag']}"
            series = history.setdefault(key, [])
            series.append({"t": now, "v": item["count"]})
            history[key] = series[-MAX_HISTORY_POINTS:]

    save_history(history)

    hashtags = []
    for key, points in history.items():
        if not points or ":" not in key:
            continue
        platform, tag = key.split(":", 1)
        growth, accel, score = compute_momentum(points)
        hashtags.append({
            "platform": platform,
            "tag": tag,
            "latest_count": points[-1]["v"],
            "growth": round(growth, 1),
            "accel": round(accel, 1),
            "score": round(score, 1),
            "history": points[-48:],
        })

    output = {"generated_at": now, "hashtags": hashtags}
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False)

    print(f"{len(hashtags)} hashtags écrits dans {DATA_PATH}")


if __name__ == "__main__":
    main()
