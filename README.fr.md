<p align="center">
  <img src="assets/logo.png" width="96" alt="logo nowplaying">
</p>

# nowplaying

<p align="center">
  <a href="https://cdn.jsdelivr.net/gh/mat-d3v/nowplaying@main/assets/nowplaying-presentation.mp4">
    <img src="assets/presentation-poster.jpg" width="800" alt="Voir la vidéo de présentation de NowPlaying">
  </a>
  <br>
  <sub>▶ <a href="https://cdn.jsdelivr.net/gh/mat-d3v/nowplaying@main/assets/nowplaying-presentation.mp4">Voir la vidéo de présentation</a> (52 s, avec le son, en anglais)</sub>
</p>

Page plein écran "En lecture" pour MPD. Affiche le titre en cours avec un dégradé de fond extrait de la pochette, des badges de qualité audio et la détection Hi-Res.

Pas besoin de nginx - le bridge sert tout sur le port 8766.

## Ce que ça affiche

- Dégradé de fond extrait des couleurs de la pochette
- Pochettes directement via mpd (tags embarqués ou cover du dossier), Last.fm en secours optionnel pour les streams et radios
- Badges FLAC / profondeur de bits / fréquence d'échantillonnage
- Logo Hi-Res Audio pour tout ce qui est >= 88,2 kHz ou >= 24 bits
- Barre de progression avec temps écoulé et total
- Défilement automatique pour les titres longs
- Assombrissement en pause
- Horloge en haut à droite
- Ligne « À suivre » avec le prochain morceau de la file
- Progression interpolée entre les polls (plus de sauts de 2 s)
- Indicateur « hors ligne » si le pont ne répond plus
- Mise en page portrait adaptée aux téléphones (iOS et Android) : pochette centrée en haut, horloge masquée
- Wake Lock (écran maintenu allumé) et plein écran via l'écran d'accueil, sur iOS comme sur Android

## Prérequis

- MPD
- Python 3
- Optionnel : une clé API Last.fm pour les pochettes des streams et radios - gratuite sur https://www.last.fm/api

## Démarrage
```bash
git clone https://github.com/mat-d3v/nowplaying.git
cd nowplaying
pip install python-mpd2 requests
export LASTFM_API_KEY=votre_clé   # optionnel, secours pochettes pour les streams
python mpd-bridge.py
```

Ouvrez ensuite http://localhost:8766 dans un navigateur.

Version française : http://localhost:8766/index.fr.html ou http://localhost:8766?lang=fr

## Variables d'environnement

| Variable | Défaut | Description |
|----------|--------|-------------|
| LASTFM_API_KEY | optionnel | Secours pochettes pour les streams et radios |
| ALSA_CARD | 0 | Numéro de carte ALSA pour la détection du format audio |
| MPD_HOST | 127.0.0.1 | Hôte MPD |
| MPD_PORT | 6600 | Port MPD |
| PORT | 8766 | Port HTTP du pont |

Copiez `.env.example` en `.env` et remplissez vos valeurs.

## Docker

Un `docker-compose.yml` est inclus. Renseignez éventuellement `LASTFM_API_KEY` dans la section environment puis :
docker compose up -d

## Licence

MIT
