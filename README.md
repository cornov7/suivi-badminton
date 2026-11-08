# Suivi Swiss Badminton — collecte des données

Chaque nuit, GitHub Actions lit les pages publiques de swiss-badminton.ch
(Match Center interclub, liste des tournois, sélections nationales) et enregistre
le résultat dans `data/`. Une tâche Claude planifiée recopie ensuite ces données
dans la page « Suivi Swiss Badminton ».

- `scraper/sb_refresh.py` : le programme de collecte
- `.github/workflows/actualiser.yml` : le lancement quotidien (onglet **Actions** pour lancer à la main avec **Run workflow**)
- `data/summary.json` : le bilan de la dernière collecte (contrôles par partie)
- `data/last_run.log` : le journal de la dernière collecte

Si une partie échoue (par exemple si le site change de mise en page), ses données
précédentes sont conservées et le bilan l'indique.
