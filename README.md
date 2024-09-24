# CleanMyPlex

![CleanMyPlex Logo](https://github.com/jjtronics/CleanMyPlex/blob/main/static/logo.png)

CleanMyPlex est une application web permettant de gérer et nettoyer vos bibliothèques Plex. Elle permet également de faire une comparaison entre deux serveurs plex pour trouver les plus gros doublons.

## Fonctionnalités

- Nettoyage de vos films/séries
  - Génération de fichiers CSV des films et séries non visionnés selon des critères spécifiques.
  - Visualisation et gestion des fichiers CSV générés.
  - Archivage ou suppression des éléments directement depuis l’interface web.
- Vérification des doublons entre serveurs Plex
  - Comparaison des bibliothèques de films et séries entre deux serveurs Plex.
  - Génération de fichiers CSV des éléments en commun.
  - Calcul de l’espace disque pouvant être libéré en supprimant les doublons.
- Vérification de l’encodage matériel
  - Vérification de l’état de l’encodage matériel sur votre serveur Plex.
  - Affichage de l’utilisation de l’encodage matériel dans les sessions en cours.
- Configuration des paramètres
  - Interface web pour configurer les paramètres de l’application.
  - Mise à jour des informations de connexion au serveur Plex et des critères de nettoyage.

## Prérequis

- Python 3.7+
- Flask
- PlexAPI
- pandas

## Installation

1. Clonez le dépôt GitHub :
   ```sh
   git clone https://github.com/votre-utilisateur/cleanmyplex.git /opt/cleanmyplex
   cd /opt/cleanmyplex

2. Créez un environnement virtuel et activez-le :

   ```sh
   python3 -m venv plex_env
   source plex_env/bin/activate

3. Installez les dépendances :
   ```bash
   pip install -r requirements.txt

4. Créer et Configurez les paramètres dans `config.json` :
   Pour récupérer votre token vous pouvez suivre la doc https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/
   ```json
   {
    "PLEX_URL": "http://127.0.0.1:32400",
    "PLEX_TOKEN": "your_plex_token",
    "PLEX_USERNAME": "your_plex_username",
    "PLEX_PASSWORD": "your_plex_password",
    "FRIEND_SERVER_NAME": "FriendServerName",
    "LOG_FILE_PATH": "/path/to/plex/logs/Plex Media Server.log"
   }

5. Créer un Utilisateur Basique Sans Home Directory ni Password :
   ```sh
   sudo useradd -r -s /usr/sbin/nologin cleanmyplex

6. Changer le Propriétaire du Répertoire du Projet :
   ```sh
   sudo chown -R cleanmyplex:cleanmyplex /opt/cleanmyplex

Scripts Systemd

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

Contribution

Les contributions sont les bienvenues ! Veuillez ouvrir une issue ou soumettre une pull request sur GitHub.
