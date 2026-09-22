# TrendBoard — déploiement

Ce dossier est un site complet et autonome. Une fois en ligne, les visiteurs
n'installent rien : ils ouvrent le lien, c'est tout.

## État actuel : YouTube uniquement

TikTok a été mis en pause : l'endpoint testé appartient à TikTok Ads
Manager et renvoie systématiquement une erreur "no permission" sans session
publicitaire authentifiée — ce n'est pas quelque chose qu'on peut
contourner proprement avec un simple script. Le code de collecte TikTok
reste dans `collect_and_compute.py` (fonction `fetch_tiktok`, non appelée)
si une vraie source de données TikTok est trouvée plus tard (API
officielle si elle s'ouvre un jour, ou un fournisseur tiers comme Exolyt).

## Mise en place (une seule fois)

1. Crée un compte GitHub si tu n'en as pas (gratuit).
2. Crée un nouveau repository (public), par exemple `trendboard`.
3. Mets tous les fichiers de ce dossier dedans.
4. Dans le repo → **Settings → Pages** → Source : `Deploy from a branch`,
   branche `main`, dossier `/ (root)`. Tu obtiens une URL du type
   `https://tonpseudo.github.io/trendboard/` — c'est le lien à partager.
5. Dans **Settings → Actions → General**, vérifie que les workflows sont
   autorisés à écrire ("Read and write permissions").

## Clé YouTube (nécessaire pour que la collecte fonctionne)

Sans cette clé, le workflow tourne toutes les 10 minutes mais n'écrit
aucune donnée (0 hashtags), comme actuellement.

1. Va sur https://console.cloud.google.com/ (compte Google gratuit)
2. Crée un projet, puis "APIs & Services" → "Library" → active
   **"YouTube Data API v3"**
3. "Credentials" → "Create credentials" → "API key" → copie la clé générée
4. Dans ton repo GitHub → **Settings → Secrets and variables → Actions**
   → "New repository secret" → nom `YOUTUBE_API_KEY`, valeur = la clé copiée
5. Le quota gratuit (10 000 unités/jour) suffit largement pour une collecte
   toutes les 10 minutes sur cette API.

Point à savoir : Google demande une carte bancaire pour créer le projet,
même en restant sur le quota gratuit — c'est une vérification anti-abus,
aucun prélèvement tant que tu restes sous 10 000 unités/jour (on en
consomme ~144/jour ici). Cette étape peut être faite plus tard ; le reste
du site fonctionne déjà sans elle, juste sans données à afficher.

## Lancer une collecte manuellement

Onglet **Actions** du repo → "Mise à jour des données TrendBoard" →
"Run workflow".

## Ce qui se passe ensuite, automatiquement

- Toutes les 10 minutes, GitHub Actions collecte YouTube
- `history.json` (mémoire complète) et `data.json` (ce que le site affiche)
  sont régénérés et poussés automatiquement dans le repo
- GitHub Pages republie le site en quelques secondes
- Le site revérifie `data.json` chaque minute pour se rafraîchir tout seul

## Limites à connaître

- GitHub exécute les workflows planifiés "au mieux" : un passage peut être
  retardé de quelques minutes en cas de forte charge sur leurs serveurs —
  fréquent et gratuit, donc pas garanti à la seconde près.
- YouTube : l'API ne propose pas de filtre officiel "Shorts tendance" — le
  script part des vidéos les plus populaires du jour en France et ne garde
  que celles de 60 secondes ou moins, ce qui reste une bonne approximation
  sans être un classement Shorts à 100% natif.
- Instagram n'est pas couvert : Meta ne fournit aucune API publique de
  hashtags ou reels tendance, y compris pour les comptes business.
