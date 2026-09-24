# CleanMyPlex

![CleanMyPlex Logo](https://github.com/jjtronics/CleanMyPlex/blob/main/static/logo.png)

CleanMyPlex est une application web permettant de gérer et nettoyer vos bibliothèques Plex. Elle permet également de faire une comparaison entre deux serveurs plex pour trouver les doublons.

## Table des matières
- [Fonctionnalités](#fonctionnalités)
- [Prérequis](#prérequis)
- [Installation](#installation)
- [MCP](#mcp)
- [Nettoyage contrôlé](#nettoyage-contrôlé)
- [Exploitation du déploiement actuel](#exploitation-du-déploiement-actuel)
- [Mise à jour](#mise-à-jour)
- [Scripts Systemd](#scripts-systemd)
- [Contribution](#contribution)

## Statut
![Version](https://img.shields.io/github/v/release/jjtronics/cleanmyplex)
![Issues](https://img.shields.io/github/issues/jjtronics/cleanmyplex)
![Forks](https://img.shields.io/github/forks/jjtronics/cleanmyplex)

## Fonctionnalités

- Nettoyage de vos films/séries
  - Scan des films et séries non visionnés selon des critères spécifiques.
  - Visualisation et gestion des données indexées en SQLite, avec export CSV si besoin.
  - Archivage ou suppression des éléments directement depuis l’interface web.
- Vérification des doublons entre serveurs Plex
  - Comparaison des bibliothèques de films et séries entre deux serveurs Plex.
  - Indexation des éléments en commun.
  - Calcul de l’espace disque pouvant être libéré en supprimant les doublons.
- Gestion des utilisateurs
  - Liste les users, leurs adresse email etc ...
- Configuration des paramètres
  - Interface web pour configurer les paramètres de l’application.
  - Mise à jour des informations de connexion au serveur Plex et des critères de nettoyage.
- MCP pour assistants IA
  - Exposition d’un serveur MCP HTTP pour lancer les scans, consulter les datasets, marquer les actions et suivre les jobs.

## Prérequis

- Python 3.7+
- Flask
- PlexAPI
- pandas

## Installation

1. Clonez le dépôt GitHub :
   ```sh
   git clone https://github.com/jjtronics/cleanmyplex.git /opt/cleanmyplex
   cd /opt/cleanmyplex

2. Créez un environnement virtuel et activez-le :

   ```sh
   python3 -m venv plex_env
   source plex_env/bin/activate

3. Installez les dépendances :
   ```bash
   pip install -r requirements.txt

4. Créer et Configurez les paramètres dans `config.json` :
   Vous pouvez saisir un token Plex existant ou renseigner username/password dans l'IHM. Si le MFA Plex est actif, la page Paramètres permet de saisir le code MFA pour récupérer et enregistrer automatiquement un nouveau token.
   ```json
   {
    "PLEX_URL": "http://127.0.0.1:32400",
    "PLEX_TOKEN": "your_plex_token",
    "PLEX_USERNAME": "your_plex_username",
    "PLEX_PASSWORD": "your_plex_password",
    "FRIEND_SERVER_NAME": "FriendServerName",
    "MCP_API_KEY": ""
   }

5. Créer un Utilisateur Basique Sans Home Directory ni Password :
   ```sh
   sudo useradd -r -s /usr/sbin/nologin cleanmyplex

6. Changer le Propriétaire du Répertoire du Projet :
   ```sh
   sudo chown -R cleanmyplex:cleanmyplex /opt/cleanmyplex

## MCP

CleanMyPlex expose un serveur MCP HTTP intégré à Flask :

- Découverte : `GET /.well-known/mcp`
- MCP streamable HTTP : `POST /mcp`
- Compatibilité API : `POST /api/mcp/initialize`, `GET /api/mcp/tools`, `POST /api/mcp/call`

Configurez `MCP_API_KEY` dans `config.json` ou `CLEANMYPLEX_MCP_API_KEY` dans l’environnement systemd. Si la valeur est vide, le MCP reste ouvert sur le réseau qui expose CleanMyPlex. Les clients MCP authentifiés doivent envoyer :

```sh
Authorization: Bearer votre_cle_mcp
```

Exemple :

```sh
curl -H "Authorization: Bearer votre_cle_mcp" http://127.0.0.1:5000/.well-known/mcp

curl -H "Authorization: Bearer votre_cle_mcp" http://127.0.0.1:5000/api/mcp/tools

curl -X POST http://127.0.0.1:5000/api/mcp/call \
  -H "Authorization: Bearer votre_cle_mcp" \
  -H "Content-Type: application/json" \
  -d '{"toolName":"cleanmyplex_status","arguments":{}}'
```

Outils disponibles : `cleanmyplex_status`, `list_libraries`, `list_datasets`, `query_dataset`, `set_dataset_actions`, `start_unwatched_scan`, `start_duplicate_scan`, `start_delete_marked_items`, `list_jobs`, `list_users`.

## Nettoyage contrôlé

Les actions enregistrées dans les datasets ont des effets différents :

- une action vide laisse l'élément inchangé ;
- `A` est un marqueur de conservation ou d'archivage manuel. CleanMyPlex ne déplace et ne supprime aucun fichier marqué `A` ;
- `D` autorise la suppression Plex lors du prochain traitement du dataset.

Le traitement d'un dataset supprime **toutes** ses lignes marquées `D`, y compris celles marquées lors d'une session précédente. Avant chaque lancement :

1. Actualisez le scan afin de travailler sur les chemins et tailles les plus récents.
2. Filtrez le dataset par action et examinez la liste complète des `D`.
3. Résolvez les conflits : un titre ne doit jamais être simultanément proposé en `A` et en `D`.
4. Pour libérer un volume précis, vérifiez que chaque chemin appartient bien à ce volume. La taille agrégée d'un titre peut inclure plusieurs versions stockées sur plusieurs disques.
5. Pour un doublon, confirmez l'existence de l'autre copie avant de supprimer la copie locale.
6. Faites valider explicitement la liste et son volume estimé par l'opérateur.
7. Lancez séparément les traitements films et séries, puis suivez les deux jobs jusqu'à un état terminal.
8. Contrôlez les erreurs du job et mesurez l'espace réellement récupéré avec `df`.

Une réponse indiquant que `plex` vaut `None` signifie que l'application a perdu sa connexion Plex. Aucun fichier concerné n'est alors supprimé. Rétablissez d'abord la connexion, contrôlez l'état Plex, puis relancez uniquement les lignes `D` autorisées. Ne contournez pas ce contrôle par une suppression directe des fichiers, sauf intervention d'administration explicitement validée.

## Exploitation du déploiement actuel

L'instance de production utilisée pour le nettoyage est hébergée sur `Plex-V2` :

- hôte SSH : `toxyk@192.168.1.113` ;
- application : `/opt/cleanmyplex` ;
- service : `cleanmyplex.service` ;
- point de montage du volume ciblé : `/mnt/net/STOCKAGE-HDD-RAID-01` ;
- partage correspondant : `//192.168.1.47/HDD-RAID-01`.

Commandes de contrôle non destructives :

```sh
ssh toxyk@192.168.1.113 'systemctl --no-pager --full status cleanmyplex.service'
ssh toxyk@192.168.1.113 'df -hT /mnt/net/STOCKAGE-HDD-RAID-01'
ssh toxyk@192.168.1.113 'curl -sSf http://127.0.0.1:5000/api/plex_status'
```

Lors de l'intervention du 24 septembre 2026, le nettoyage a combiné la suppression de copies redondantes validées et le traitement de sept titres explicitement marqués `D`. Les deux anciens marqueurs `D` hors sélection ont été retirés avant le traitement. Les jobs finaux se sont terminés sans erreur et le volume disposait de 305 Go libres après l'opération.

## Dépannage Plex : erreur 500 pendant une suppression

Si une suppression se termine avec une erreur Plex `500`, consultez d'abord le journal Plex Media Server. Une réponse `500` accompagnée de messages tels que `index corruption` ou `database disk image is malformed` indique une corruption de la base SQLite de Plex, et non un problème de sélection CleanMyPlex.

Dans ce cas :

1. Arrêtez les suppressions et les scans susceptibles de modifier la bibliothèque.
2. Sauvegardez intégralement le répertoire de données Plex avant toute intervention.
3. Réparez ou restaurez la base Plex conformément à la procédure Plex adaptée à votre système.
4. Relancez un scan complet dans CleanMyPlex avant de préparer une nouvelle sélection.

Ne relancez pas les suppressions en échec tant que l'intégrité de la base Plex n'a pas été rétablie : Plex peut supprimer une partie des métadonnées en mémoire puis échouer lors de l'écriture en base.

## Mise à jour

Pour mettre à jour une installation existante de CleanMyPlex via git :

1. Placez-vous dans le répertoire du projet :
   ```sh
   cd /opt/cleanmyplex

2. Récupérez la dernière version :
   ```sh
   git pull origin main

3. Réactivez l'environnement virtuel :
   ```sh
   source plex_env/bin/activate

4. Réinstallez les dépendances au cas où elles auraient changé :
   ```sh
   pip install -r requirements.txt

5. Si CleanMyPlex est lancé via systemd, redémarrez le service :
   ```sh
   sudo systemctl restart cleanmyplex.service

Si vous avez modifié des fichiers suivis par git localement, pensez à sauvegarder vos changements avant le `git pull` avec un commit ou un `git stash`.

## Scripts Systemd

Pour exécuter l’application automatiquement au démarrage, créez un script systemd :

1. Créez un fichier de service systemd :
   ```sh
   sudo nano /etc/systemd/system/cleanmyplex.service

2. Ajoutez le contenu suivant :

   ```ini
   [Unit]
   Description=CleanMyPlex Service
   After=network.target

   [Service]
   User=cleanmyplex
   WorkingDirectory=/opt/cleanmyplex
   ExecStart=/bin/bash -c 'source /opt/cleanmyplex/plex_env/bin/activate && exec python3 /opt/cleanmyplex/cleanmyplex.py'
   Restart=always

   [Install]
   WantedBy=multi-user.target

3. Rechargez systemd, activez et démarrez le service :

   ```sh
   sudo systemctl daemon-reload
   sudo systemctl enable cleanmyplex.service
   sudo systemctl start cleanmyplex.service

## Contribution

Les contributions sont les bienvenues ! Veuillez ouvrir une issue ou soumettre une pull request sur GitHub.
