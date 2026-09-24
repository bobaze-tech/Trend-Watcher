"""
TrendBoard — collecte YouTube Shorts (v2)
------------------------------------------
Exécuté toutes les 10 minutes par .github/workflows/update.yml.

Ce que fait le script à chaque passage :
1. Récupère les classements "populaires" de YouTube en France : le
   classement général (200 vidéos) + un classement par catégorie.
   Coût : environ 12 unités de quota par passage (~1 700/jour sur 10 000).
2. Garde les Shorts (vidéos de 3 minutes ou moins).
3. Extrait les hashtags de chaque vidéo (tags, titre, description), les
   normalise pour éviter les doublons (#Foot, #foot, #Foot! = un seul tag)
   et écarte les tags génériques (#shorts, #viral, #fyp…).
4. Mesure la VITESSE : vues gagnées par heure par les vidéos de chaque
   hashtag, en comparant chaque vidéo avec son passage précédent. C'est cette
   vitesse qui classe les tendances, et non plus la somme brute des vues
   (qui sautait dès qu'une vidéo entrait ou sortait du classement).
5. Écrit history.json (mémoire) et data.json (ce que le site affiche).

Instagram et TikTok ne sont pas collectés (pas d'accès public fiable).
"""

import json
import os
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

import requests

API_KEY = os.environ.get("YOUTUBE_API_KEY", "")
VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
REGION = "FR"
SHORT_MAX_SECONDS = 180          # les Shorts peuvent durer jusqu'à 3 minutes
GENERAL_PAGES = 4                # 4 x 50 vidéos du classement général

HISTORY_PATH = "history.json"
DATA_PATH = "data.json"

VIDEO_TTL = timedelta(hours=24)       # on oublie une vidéo absente depuis 24 h
TAG_TTL = timedelta(hours=48)         # on oublie un tag absent depuis 48 h
MAX_GAP = timedelta(hours=3)          # au-delà, l'écart entre 2 mesures n'est pas fiable
SERIES_WINDOW = timedelta(hours=24)   # durée affichée dans les graphiques
SERIES_POINTS = 36                    # points max par graphique (poids du fichier)

CATEGORY_NAMES = {
    "1": "Films et animation", "2": "Auto et moto", "10": "Musique",
    "15": "Animaux", "17": "Sport", "19": "Voyages", "20": "Jeux vidéo",
    "22": "Vlogs", "23": "Humour", "24": "Divertissement", "25": "Actualités",
    "26": "Tutos et style", "27": "Éducation", "28": "Science et tech",
    "29": "Associatif",
}
CHART_CATEGORIES = ["10", "17", "20", "22", "23", "24", "26", "28"]

# Tags qui décrivent la plateforme ou l'algorithme, pas un sujet.
GENERIC_TAGS = {
    "shorts", "short", "youtubeshorts", "youtubeshort", "shortsvideo", "shortvideo",
    "shortsfeed", "shortsyoutube", "ytshorts", "ytshort", "youtube", "yt", "video",
    "videos", "viral", "viralshorts", "viralvideo", "viralvideos", "trending",
    "trend", "trendingshorts", "tendance", "fyp", "foryou", "foryoupage", "pourtoi",
    "explore", "explorepage", "subscribe", "abonnetoi", "abonnezvous", "like",
    "follow", "new", "tiktok", "reels", "instagram", "funny", "fun", "lol",
    "memes", "meme", "fr", "france", "french", "francais",
}

HASHTAG_RE = re.compile(r"#([^\s#.,!?;:()\[\]{}\"'«»]+)")


# ---------------------------------------------------------------------
# Normalisation des hashtags
# ---------------------------------------------------------------------
def tag_key(raw: str) -> str:
    """Clé de dédoublonnage : minuscules, sans accents, sans espaces ni ponctuation."""
    s = unicodedata.normalize("NFKD", raw.strip().lstrip("#").lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[\W_]+", "", s)


def tag_label(raw: str) -> str:
    """Forme affichée : minuscules, accents conservés, sans espaces ni ponctuation."""
    s = unicodedata.normalize("NFKC", raw.strip().lstrip("#").lower())
    return re.sub(r"[\W_]+", "", s)


def is_useful(key: str) -> bool:
    return 2 <= len(key) <= 40 and not key.isdigit() and key not in GENERIC_TAGS


def extract_tags(snippet: dict) -> dict:
    """Retourne {clé: libellé} pour une vidéo, chaque tag compté une seule fois."""
    raws = list(snippet.get("tags") or [])
    raws += HASHTAG_RE.findall(snippet.get("title", ""))
    raws += HASHTAG_RE.findall(snippet.get("description", ""))
    found = {}
    for raw in raws:
        key = tag_key(raw)
        if is_useful(key) and key not in found:
            found[key] = tag_label(raw) or key
    return found


# ---------------------------------------------------------------------
# Appels YouTube
# ---------------------------------------------------------------------
def duration_seconds(iso: str) -> int:
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not m:
        return 10 ** 6
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def fetch_chart(category: str | None = None, pages: int = 1) -> list[dict]:
    items, token = [], None
    for _ in range(pages):
        params = {
            "part": "snippet,statistics,contentDetails",
            "chart": "mostPopular",
            "regionCode": REGION,
            "maxResults": 50,
            "key": API_KEY,
        }
        if category:
            params["videoCategoryId"] = category
        if token:
            params["pageToken"] = token
        try:
            r = requests.get(VIDEOS_URL, params=params, timeout=20)
        except requests.RequestException as exc:
            print(f"[youtube] échec réseau ({category or 'général'}) : {exc}")
            break
        if r.status_code != 200:
            # Certaines catégories n'ont pas de classement en France : on continue.
            print(f"[youtube] classement {category or 'général'} indisponible (HTTP {r.status_code})")
            break
        payload = r.json()
        items += payload.get("items", [])
        token = payload.get("nextPageToken")
        if not token:
            break
    return items


def fetch_shorts() -> tuple[dict, int]:
    """Retourne ({id: vidéo}, nombre total de vidéos vues) pour les Shorts dédoublonnés."""
    raw = fetch_chart(None, GENERAL_PAGES)
    for cat in CHART_CATEGORIES:
        raw += fetch_chart(cat, 1)
    unique = {it["id"]: it for it in raw if it.get("id")}
    shorts = {
        vid: it for vid, it in unique.items()
        if duration_seconds(it.get("contentDetails", {}).get("duration", "")) <= SHORT_MAX_SECONDS
    }
    return shorts, len(unique)


# ---------------------------------------------------------------------
# Historique
# ---------------------------------------------------------------------
def parse_time(s: str) -> datetime:
    return datetime.fromisoformat(s)


def load_state() -> dict:
    if os.path.exists(HISTORY_PATH):
        try:
            with open(HISTORY_PATH, "r", encoding="utf-8") as f:
                state = json.load(f)
            if state.get("version") == 2:
                return state
            print("[historique] ancien format détecté : nouvel historique démarré.")
        except (ValueError, OSError):
            print("[historique] fichier illisible : nouvel historique démarré.")
    return {"version": 2, "videos": {}, "tags": {}}


def downsample(points: list, limit: int) -> list:
    if len(points) <= limit:
        return points
    step = (len(points) - 1) / (limit - 1)
    return [points[round(i * step)] for i in range(limit)]


def mean(values: list) -> float:
    return sum(values) / len(values) if values else 0.0


# ---------------------------------------------------------------------
# Programme principal
# ---------------------------------------------------------------------
def main() -> None:
    if not API_KEY:
        print("[youtube] YOUTUBE_API_KEY absente : collecte ignorée.")
        return

    now = datetime.now(timezone.utc).replace(microsecond=0)
    now_s = now.isoformat()
    state = load_state()
    videos_state, tags_state = state["videos"], state["tags"]

    shorts, total_videos = fetch_shorts()
    print(f"[youtube] {total_videos} vidéos récupérées, dont {len(shorts)} Shorts")
    if not shorts:
        print("[youtube] aucun Short récupéré : données inchangées.")
        return

    # --- Vitesse par vidéo -------------------------------------------------
    video_tags: dict[str, dict] = {}
    video_vph: dict[str, float | None] = {}
    video_cat: dict[str, str] = {}
    for vid, it in shorts.items():
        snippet = it.get("snippet", {})
        views = int(it.get("statistics", {}).get("viewCount", 0))
        prev = videos_state.get(vid)
        vph = None
        if prev:
            dt = now - parse_time(prev["t"])
            if timedelta(minutes=3) <= dt <= MAX_GAP and views >= prev["v"]:
                vph = (views - prev["v"]) / (dt.total_seconds() / 3600)
        video_vph[vid] = vph
        video_tags[vid] = extract_tags(snippet)
        video_cat[vid] = CATEGORY_NAMES.get(str(snippet.get("categoryId", "")), "Autre")
        videos_state[vid] = {
            "v": views, "t": now_s,
            "title": snippet.get("title", "")[:120],
            "ch": snippet.get("channelTitle", "")[:60],
            "first": prev["first"] if prev else now_s,
        }

    # --- Agrégation par hashtag -------------------------------------------
    tag_videos: dict[str, list] = defaultdict(list)
    label_votes: dict[str, Counter] = defaultdict(Counter)
    for vid, tags in video_tags.items():
        for key, label in tags.items():
            tag_videos[key].append(vid)
            label_votes[key][label] += 1

    for key, vids in tag_videos.items():
        views_total = sum(videos_state[v]["v"] for v in vids)
        known = [video_vph[v] for v in vids if video_vph[v] is not None]
        entry = tags_state.setdefault(key, {"first": now_s, "points": []})
        entry["label"] = label_votes[key].most_common(1)[0][0]
        entry["points"].append({
            "t": now_s, "views": views_total, "n": len(vids),
            "vph": round(sum(known)) if known else None,
        })

    # --- Nettoyage -----------------------------------------------------------
    for vid in [v for v, d in videos_state.items() if now - parse_time(d["t"]) > VIDEO_TTL]:
        del videos_state[vid]
    for key in list(tags_state):
        pts = [p for p in tags_state[key]["points"] if now - parse_time(p["t"]) <= TAG_TTL]
        if pts:
            tags_state[key]["points"] = pts
        else:
            del tags_state[key]

    state["updated"] = now_s
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, separators=(",", ":"))

    # --- Tags associés (co-occurrence dans les vidéos actuelles) -------------
    cooc: dict[str, Counter] = defaultdict(Counter)
    for tags in video_tags.values():
        keys = list(tags)
        for a in keys:
            for b in keys:
                if a != b:
                    cooc[a][b] += 1

    # --- Regroupement des tags qui ont exactement les mêmes vidéos ----------
    # (sinon une seule vidéo virale remplit le classement avec ses 15 tags,
    #  tous avec les mêmes chiffres)
    group_of: dict[str, str] = {}
    by_signature: dict[frozenset, list] = defaultdict(list)
    for key, vids in tag_videos.items():
        by_signature[frozenset(vids)].append(key)
    for keys in by_signature.values():
        leader = max(keys, key=lambda k: (sum(label_votes[k].values()), -len(k)))
        for k in keys:
            group_of[k] = leader

    # --- Sortie pour le site ---------------------------------------------------
    out_tags = []
    for key, entry in tags_state.items():
        pts = entry["points"]
        last = pts[-1]
        active = last["t"] == now_s
        recent_vph = [p["vph"] for p in pts if p["vph"] is not None
                      and now - parse_time(p["t"]) <= timedelta(minutes=35)]
        before_vph = [p["vph"] for p in pts if p["vph"] is not None
                      and timedelta(hours=1) <= now - parse_time(p["t"]) <= timedelta(hours=3)]
        vph_now = mean(recent_vph[-2:]) if active else 0.0
        vph_before = mean(before_vph)
        trend = None
        if active and before_vph and vph_before > 0:
            trend = (vph_now - vph_before) / vph_before

        series = [[p["t"], p["vph"]] for p in pts
                  if p["vph"] is not None and now - parse_time(p["t"]) <= SERIES_WINDOW]
        vids = sorted(tag_videos.get(key, []), key=lambda v: (video_vph[v] or 0, videos_state[v]["v"]), reverse=True)
        cats = Counter(video_cat[v] for v in vids)

        item = {
            "key": key,
            "label": entry.get("label", key),
            "active": active,
            "first_seen": entry["first"],
            "last_seen": last["t"],
            "n": last["n"] if active else 0,
            "views": last["views"],
            "vph": round(vph_now),
            "trend": round(trend, 3) if trend is not None else None,
            "cat": cats.most_common(1)[0][0] if cats else None,
            "group": group_of.get(key, key),
        }
        if active:
            item["series"] = downsample(series, SERIES_POINTS)
            item["videos"] = [
                {"id": v, "title": videos_state[v]["title"], "ch": videos_state[v]["ch"],
                 "views": videos_state[v]["v"],
                 "vph": round(video_vph[v]) if video_vph[v] is not None else None}
                for v in vids[:3]
            ]
            item["related"] = [k for k, _ in cooc[key].most_common(6)]
        out_tags.append(item)

    output = {
        "version": 2,
        "generated_at": now_s,
        "region": REGION,
        "videos_analyzed": total_videos,
        "shorts_analyzed": len(shorts),
        "tags": out_tags,
    }
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, separators=(",", ":"))

    active_count = sum(1 for t in out_tags if t["active"])
    print(f"{active_count} hashtags actifs ({len(out_tags)} en mémoire) écrits dans {DATA_PATH}")


if __name__ == "__main__":
    main()
