from flask import Flask, render_template, request, redirect, url_for, flash, send_from_directory
import requests
import pandas as pd
from plexapi.server import PlexServer
from plexapi.myplex import MyPlexAccount
import json
import os
import threading
import time
import codecs

app = Flask(__name__)
app.secret_key = 'supersecretkey'

# Charger la configuration depuis le fichier config.json
def load_config():
    with open('config.json') as config_file:
        return json.load(config_file)

config = load_config()

PLEX_URL = config['PLEX_URL']
PLEX_TOKEN = config['PLEX_TOKEN']
PLEX_USERNAME = config['PLEX_USERNAME']
PLEX_PASSWORD = config['PLEX_PASSWORD']
FRIEND_SERVER_NAME = config['FRIEND_SERVER_NAME']
LOG_FILE_PATH = config['LOG_FILE_PATH']
CSV_FILE_FILMS = 'unwatched_movies.csv'
CSV_FILE_SERIES = 'unwatched_series.csv'
CSV_FILE_COMMON_MOVIES = 'common_movies.csv'
CSV_FILE_COMMON_SERIES = 'common_series.csv'

plex = PlexServer(PLEX_URL, PLEX_TOKEN)
account = MyPlexAccount(PLEX_USERNAME, PLEX_PASSWORD)

def is_hardware_acceleration_enabled():
    response = requests.get(f'{PLEX_URL}/:/prefs', headers={'X-Plex-Token': PLEX_TOKEN})
    if response.status_code == 200 and response.text:
        try:
            prefs = response.json()
            return prefs.get('HardwareDecodingEnabled', False)
        except ValueError:
            return False
    else:
        return False

def check_hardware_acceleration_logs(log_file_path):
    try:
        with codecs.open(log_file_path, 'r', 'latin1') as log_file:
            log_content = log_file.read()
            return 'hardware transcoding' in log_content
    except IOError:
        return False

# Fonction pour récupérer dynamiquement les sections de bibliothèque
def get_library_sections(plex_server, media_type):
    """
    Récupère les noms des sections de bibliothèque correspondant au type de média spécifié.

    Args:
        plex_server (PlexServer): Instance du serveur Plex.
        media_type (str): Type de média ('movie' ou 'show').

    Returns:
        list: Liste des noms des sections de bibliothèque.
    """
    sections = plex_server.library.sections()
    if media_type == 'movie':
        return [section.title for section in sections if section.type == 'movie']
    elif media_type == 'show':
        return [section.title for section in sections if section.type == 'show']
    else:
        return []

# Fonction pour comparer les bibliothèques et générer un CSV des doublons
def compare_libraries(library_names_local, library_names_friend, media_type):
    friend_server = account.resource(FRIEND_SERVER_NAME).connect()
    
    # Récupérer les bibliothèques sélectionnées ou toutes
    if "ALL" in library_names_friend:
        friend_libraries = friend_server.library.sections()
        if media_type == 'movie':
            friend_library_list = [lib for lib in friend_libraries if lib.type == 'movie']
        elif media_type == 'show':
            friend_library_list = [lib for lib in friend_libraries if lib.type == 'show']
    else:
        try:
            friend_library_list = [friend_server.library.section(name) for name in library_names_friend]
        except Exception as e:
            flash(f"Erreur lors de l'accès à une bibliothèque de l'ami : {e}", 'danger')
            friend_library_list = []

    if "ALL" in library_names_local:
        local_libraries = plex.library.sections()
        if media_type == 'movie':
            local_library_list = [lib for lib in local_libraries if lib.type == 'movie']
        elif media_type == 'show':
            local_library_list = [lib for lib in local_libraries if lib.type == 'show']
    else:
        try:
            local_library_list = [plex.library.section(name) for name in library_names_local]
        except Exception as e:
            flash(f"Erreur lors de l'accès à une de vos bibliothèques : {e}", 'danger')
            local_library_list = []

    # Créer des dictionnaires pour un accès rapide par titre
    friend_items = {}
    for lib in friend_library_list:
        for item in lib.all():
            friend_items[item.title] = item

    duplicates = []

    for lib in local_library_list:
        for local_item in lib.all():
            title = local_item.title
            if title in friend_items:
                friend_item = friend_items[title]

                # Récupérer les tailles de fichiers
                if media_type == 'movie':
                    # Taille locale
                    local_file_size_gb = sum(
                        media_part.size for media in local_item.media for media_part in media.parts
                    ) / (1024 ** 3)
                    # Taille distante
                    remote_file_size_gb = sum(
                        media_part.size for media in friend_item.media for media_part in media.parts
                    ) / (1024 ** 3)
                    # Pas de nombre d'épisodes pour les films
                    number_of_local_episodes = None
                    number_of_remote_episodes = None
                elif media_type == 'show':
                    # Taille locale
                    local_file_size_gb = sum(
                        media_part.size
                        for episode in local_item.episodes()
                        for media in episode.media
                        for media_part in media.parts
                    ) / (1024 ** 3)
                    # Taille distante
                    remote_file_size_gb = sum(
                        media_part.size
                        for episode in friend_item.episodes()
                        for media in episode.media
                        for media_part in media.parts
                    ) / (1024 ** 3)
                    # Nombre d'épisodes
                    number_of_local_episodes = len(local_item.episodes())
                    number_of_remote_episodes = len(friend_item.episodes())
                else:
                    continue  # Type de média inconnu

                # Déterminer la plus grande taille de fichier
                largest_file_size_gb = max(local_file_size_gb, remote_file_size_gb)

                # Récupérer d'autres informations
                added_at = local_item.addedAt.strftime('%Y-%m-%d') if local_item.addedAt else 'N/A'
                release_date = local_item.originallyAvailableAt.strftime('%Y-%m-%d') if local_item.originallyAvailableAt else 'N/A'
                rating = local_item.audienceRating if local_item.audienceRating else 0
                plex_rating = local_item.userRating if local_item.userRating else local_item.rating if local_item.rating else 0

                # Créer l'entrée dupliquée
                duplicate_entry = {
                    'title': title,
                    'rating': rating,
                    'plex_rating': plex_rating,
                    'key': local_item.key,
                    'added_at': added_at,
                    'release_date': release_date,
                    'local_file_size': f"{local_file_size_gb:.2f} Go",
                    'remote_file_size': f"{remote_file_size_gb:.2f} Go",
                    'largest_file_size': f"{largest_file_size_gb:.2f} Go",
                    'Bibliothèque': lib.title,
                    'Action': ''
                }

                # Inclure les nombres d'épisodes uniquement pour les séries
                if media_type == 'show':
                    duplicate_entry['number_of_local_episodes'] = number_of_local_episodes
                    duplicate_entry['number_of_remote_episodes'] = number_of_remote_episodes

                duplicates.append(duplicate_entry)

    df = pd.DataFrame(duplicates)
    output_file = CSV_FILE_COMMON_MOVIES if media_type == 'movie' else CSV_FILE_COMMON_SERIES

    # Définir l'ordre des colonnes
    if media_type == 'movie':
        columns_order = [
            'title', 'rating', 'plex_rating', 'key', 'added_at', 'release_date',
            'local_file_size', 'remote_file_size', 'largest_file_size', 'Bibliothèque', 'Action'
        ]
    else:  # Pour les séries
        columns_order = [
            'title', 'rating', 'plex_rating', 'key', 'added_at', 'release_date',
            'local_file_size', 'remote_file_size', 'largest_file_size', 'Bibliothèque',
            'number_of_local_episodes', 'number_of_remote_episodes', 'Action'
        ]
    df = df[columns_order]

    # Calculer l'espace total économisé
    total_space_saved = sum(float(size.replace(' Go', '')) for size in df['largest_file_size'])

    df.to_csv(output_file, index=False)
    return df, output_file, len(duplicates), total_space_saved

# Fonction pour générer le CSV
def generate_csv(library_names, csv_file, media_type):
    if os.path.exists(csv_file):
        existing_df = pd.read_csv(csv_file, encoding='utf-8', delimiter=',', quotechar='"')
        # Convertir 'file_size' en numérique en supprimant ' Go'
        existing_df['file_size'] = existing_df['file_size'].replace('N/A', '0')
        existing_df['file_size'] = existing_df['file_size'].str.replace(' Go', '', regex=False).astype(float)
    else:
        if media_type == 'movie':
            columns = ['title', 'rating', 'plex_rating', 'key', 'added_at', 'release_date', 'file_size', 'Bibliothèque', 'Action']
        else:
            columns = ['title', 'rating', 'plex_rating', 'key', 'added_at', 'release_date', 'file_size', 'Bibliothèque', 'Action']
        existing_df = pd.DataFrame(columns=columns)

    # Récupérer toutes les bibliothèques sélectionnées
    libraries = []
    if "ALL" in library_names:
        if media_type == 'movie':
            libraries = [section.title for section in plex.library.sections() if section.type == 'movie']
        elif media_type == 'show':
            libraries = [section.title for section in plex.library.sections() if section.type == 'show']
    else:
        libraries = library_names

    new_items = []
    for library_name in libraries:
        try:
            library = plex.library.section(library_name)
        except Exception as e:
            flash(f"Erreur lors de l'accès à la bibliothèque '{library_name}': {e}", 'danger')
            continue

        for item in library.all():
            release_date = item.originallyAvailableAt if item.originallyAvailableAt else None
            rating = item.audienceRating if item.audienceRating else 0

            plex_rating = item.userRating if hasattr(item, 'userRating') and item.userRating else item.rating if item.rating else 0

            # Films
            if item.TYPE == 'movie':
                file_size_gb = sum(media_part.size for media in item.media for media_part in media.parts) / (1024 ** 3)
                new_items.append({
                    'title': item.title,
                    'rating': rating,
                    'plex_rating': plex_rating,
                    'key': item.key,
                    'added_at': item.addedAt.strftime('%Y-%m-%d') if item.addedAt else 'N/A',
                    'release_date': release_date.strftime('%Y-%m-%d') if release_date else 'N/A',
                    'file_size': file_size_gb,
                    'Bibliothèque': library_name,
                    'Action': ''
                })

            # Séries
            elif item.TYPE == 'show':
                file_size_gb = sum(
                    media_part.size
                    for episode in item.episodes()
                    for media in episode.media
                    for media_part in media.parts
                ) / (1024 ** 3)
                new_items.append({
                    'title': item.title,
                    'rating': rating,
                    'plex_rating': plex_rating,
                    'key': item.key,
                    'added_at': item.addedAt.strftime('%Y-%m-%d') if item.addedAt else 'N/A',
                    'release_date': release_date.strftime('%Y-%m-%d') if release_date else 'N/A',
                    'file_size': file_size_gb,
                    'Bibliothèque': library_name,
                    'Action': ''
                })

    new_df = pd.DataFrame(new_items)

    combined_df = pd.concat([existing_df, new_df]).drop_duplicates(subset='key', keep='first').reset_index(drop=True)
    combined_df['Action'] = combined_df['Action'].fillna('')
    combined_df['file_size'] = combined_df['file_size'].fillna(0.00)
    combined_df['file_size'] = pd.to_numeric(combined_df['file_size'], errors='coerce').fillna(0)
    combined_df = combined_df.sort_values(by=['added_at'], ascending=True)

    # Formater 'file_size' avant d'écrire dans le CSV
    combined_df['file_size'] = combined_df['file_size'].apply(lambda x: f"{x:.2f} Go")

    columns_order = ['title', 'rating', 'plex_rating', 'key', 'added_at', 'release_date', 'file_size', 'Bibliothèque', 'Action']
    combined_df = combined_df[columns_order]

    combined_df.to_csv(csv_file, index=False)
    return combined_df, csv_file

# Fonction de génération de CSV en arrière-plan avec thread
def generate_csv_thread(library_names, csv_file, media_type):
    try:
        generate_csv(library_names, csv_file, media_type)
        print(f"CSV {csv_file} généré avec succès.")
    except Exception as e:
        print(f"Erreur lors de la génération du CSV : {e}")

def delete_items_from_csv(csv_file):
    if not os.path.exists(csv_file):
        flash('Le fichier CSV spécifié n\'existe pas.', 'danger')
        return

    df = pd.read_csv(csv_file)
    items_to_delete = df[df['Action'] == 'D']

    for index, row in items_to_delete.iterrows():
        try:
            plex.fetchItem(row['key']).delete()
            flash(f"{row['title']} supprimé avec succès.", 'success')
            df.drop(index, inplace=True)
        except Exception as e:
            flash(f"Erreur lors de la suppression de {row['title']}: {e}", 'danger')

    df.to_csv(csv_file, index=False)

@app.route('/')
def index():
    films_csv_exists = os.path.exists(CSV_FILE_FILMS)
    series_csv_exists = os.path.exists(CSV_FILE_SERIES)
    common_movies_csv_exists = os.path.exists(CSV_FILE_COMMON_MOVIES)
    common_series_csv_exists = os.path.exists(CSV_FILE_COMMON_SERIES)
    return render_template('index.html', 
                           films_csv_exists=films_csv_exists, 
                           series_csv_exists=series_csv_exists, 
                           common_movies_csv_exists=common_movies_csv_exists, 
                           common_series_csv_exists=common_series_csv_exists)

@app.route('/delete_csv', methods=['POST'])
def delete_csv():
    csv_file = request.form.get('csv_file')
    if not csv_file:
        flash(f"Aucun fichier spécifié pour la suppression.", 'danger')
        return redirect(url_for('index'))

    valid_files = [CSV_FILE_FILMS, CSV_FILE_SERIES, CSV_FILE_COMMON_MOVIES, CSV_FILE_COMMON_SERIES]
    if csv_file in valid_files:
        file_path = os.path.join(os.getcwd(), csv_file)
        if os.path.isfile(file_path):
            os.remove(csv_file)
            flash(f"Fichier {csv_file} supprimé avec succès.", 'success')
        else:
            flash(f"Le fichier {csv_file} n'existe pas.", 'danger')
    else:
        flash(f"Le fichier {csv_file} spécifié est invalide.", 'danger')
    return redirect(url_for('index'))

@app.route('/clean', methods=['GET', 'POST'])
def clean():
    # Récupérer dynamiquement les noms des bibliothèques
    local_movie_libraries = get_library_sections(plex, 'movie')
    local_show_libraries = get_library_sections(plex, 'show')

    # Ajouter l'option "ALL" si elle n'existe pas déjà
    if "ALL" not in local_movie_libraries:
        local_movie_libraries.insert(0, "ALL")
    if "ALL" not in local_show_libraries:
        local_show_libraries.insert(0, "ALL")

    # Obtenir les dates de modification
    films_csv_mtime = time.ctime(os.path.getmtime(CSV_FILE_FILMS)) if os.path.exists(CSV_FILE_FILMS) else None
    series_csv_mtime = time.ctime(os.path.getmtime(CSV_FILE_SERIES)) if os.path.exists(CSV_FILE_SERIES) else None

    if request.method == 'POST':
        selected_movie_libraries = request.form.getlist('library_names_films')
        selected_series_libraries = request.form.getlist('library_names_series')

        if selected_movie_libraries:
            csv_file = CSV_FILE_FILMS
            media_type = 'movie'
            threading.Thread(target=generate_csv_thread, args=(selected_movie_libraries, csv_file, media_type)).start()
            flash('La génération du CSV des films a démarré en arrière-plan.', 'info')

        if selected_series_libraries:
            csv_file = CSV_FILE_SERIES
            media_type = 'show'
            threading.Thread(target=generate_csv_thread, args=(selected_series_libraries, csv_file, media_type)).start()
            flash('La génération du CSV des séries a démarré en arrière-plan.', 'info')

        return redirect(url_for('index'))

    return render_template(
        'clean.html',
        local_movie_libraries=local_movie_libraries,
        local_show_libraries=local_show_libraries,
        films_csv_exists=os.path.exists(CSV_FILE_FILMS),
        series_csv_exists=os.path.exists(CSV_FILE_SERIES),
        films_csv_mtime=films_csv_mtime,
        series_csv_mtime=series_csv_mtime
    )

@app.route('/duplicates', methods=['GET', 'POST'])
def duplicates():
    # Récupérer dynamiquement les noms des bibliothèques
    try:
        friend_server = account.resource(FRIEND_SERVER_NAME).connect()
        friend_movie_libraries = get_library_sections(friend_server, 'movie')
        friend_show_libraries = get_library_sections(friend_server, 'show')
    except Exception as e:
        flash(f"Erreur lors de la connexion au serveur de l'ami : {e}", 'danger')
        friend_movie_libraries = []
        friend_show_libraries = []

    # Récupérer les bibliothèques locales
    local_movie_libraries = get_library_sections(plex, 'movie')
    local_show_libraries = get_library_sections(plex, 'show')

    # Ajouter l'option "ALL" si elle n'existe pas déjà
    for lib_list in [friend_movie_libraries, friend_show_libraries, local_movie_libraries, local_show_libraries]:
        if "ALL" not in lib_list:
            lib_list.insert(0, "ALL")

    common_movies_csv_exists = os.path.exists(CSV_FILE_COMMON_MOVIES)
    common_series_csv_exists = os.path.exists(CSV_FILE_COMMON_SERIES)

    # Obtenir les dates de modification
    common_movies_csv_mtime = time.ctime(os.path.getmtime(CSV_FILE_COMMON_MOVIES)) if common_movies_csv_exists else None
    common_series_csv_mtime = time.ctime(os.path.getmtime(CSV_FILE_COMMON_SERIES)) if common_series_csv_exists else None

    if request.method == 'POST':
        selected_local_movie_libraries = request.form.getlist('local_library_movies')
        selected_friend_movie_libraries = request.form.getlist('friend_library_movies')
        selected_local_series_libraries = request.form.getlist('local_library_series')
        selected_friend_series_libraries = request.form.getlist('friend_library_series')

        if selected_local_movie_libraries and selected_friend_movie_libraries:
            csv_file = CSV_FILE_COMMON_MOVIES
            media_type = 'movie'
            threading.Thread(target=compare_libraries_thread, args=(selected_local_movie_libraries, selected_friend_movie_libraries, media_type)).start()
            flash('La comparaison des films a démarré en arrière-plan.', 'info')

        if selected_local_series_libraries and selected_friend_series_libraries:
            csv_file = CSV_FILE_COMMON_SERIES
            media_type = 'show'
            threading.Thread(target=compare_libraries_thread, args=(selected_local_series_libraries, selected_friend_series_libraries, media_type)).start()
            flash('La comparaison des séries a démarré en arrière-plan.', 'info')

        return redirect(url_for('index'))

    return render_template(
        'duplicates.html',
        friend_movie_libraries=friend_movie_libraries,
        friend_show_libraries=friend_show_libraries,
        local_movie_libraries=local_movie_libraries,
        local_show_libraries=local_show_libraries,
        common_movies_csv_exists=common_movies_csv_exists,
        common_series_csv_exists=common_series_csv_exists,
        common_movies_csv_mtime=common_movies_csv_mtime,
        common_series_csv_mtime=common_series_csv_mtime
    )

def compare_libraries_thread(local_library_names, friend_library_names, media_type):
    try:
        df, output_file, num_items, total_space_saved_gb = compare_libraries(local_library_names, friend_library_names, media_type)
        print(f"CSV {output_file} généré avec succès.")
    except Exception as e:
        print(f"Erreur lors de la comparaison des bibliothèques : {e}")

@app.route('/hardware', methods=['GET'])
def hardware():
    hw_accel_enabled = is_hardware_acceleration_enabled()
    if hw_accel_enabled:
        hw_accel_logs = check_hardware_acceleration_logs(LOG_FILE_PATH)
        if hw_accel_logs:
            flash("L'encodage matériel est activé et utilisé dans les sessions en cours.", 'success')
        else:
            flash("L'encodage matériel est activé mais aucune session ne l'utilise actuellement.", 'info')
    else:
        flash("L'encodage matériel n'est pas activé sur le serveur Plex.", 'danger')
    return redirect(url_for('index'))

@app.route('/view_csv/<csv_file>', methods=['GET', 'POST'])
def view_csv(csv_file):
    if os.path.exists(csv_file):
        df = pd.read_csv(csv_file)
        df = df.fillna('N/A')  # Remplacer les NaN par 'N/A'
    else:
        flash('Le fichier CSV spécifié n\'existe pas.', 'danger')
        return redirect(url_for('index'))

    # Convertir les colonnes de date en datetime, si elles existent
    date_columns = ['added_at', 'release_date']
    for col in date_columns:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors='coerce')

    if request.method == 'POST':
        # Mise à jour des actions (A pour Archiver, D pour Supprimer)
        for index, row in df.iterrows():
            action = request.form.get(f'action_{index}')
            if action in ['A', 'D']:
                df.at[index, 'Action'] = action
        df.to_csv(csv_file, index=False)
        flash('CSV mis à jour avec succès.', 'success')
        return redirect(url_for('view_csv', csv_file=csv_file))

    return render_template('view_csv.html', df=df, titles=df.columns.values, csv_file=csv_file)

@app.route('/view_existing_csv/<library>')
def view_existing_csv(library):
    if library == 'films':
        csv_file = CSV_FILE_FILMS
    elif library == 'series':
        csv_file = CSV_FILE_SERIES
    elif library == 'common_movies':
        csv_file = CSV_FILE_COMMON_MOVIES
    elif library == 'common_series':
        csv_file = CSV_FILE_COMMON_SERIES

    return redirect(url_for('view_csv', csv_file=csv_file))

@app.route('/process_csv/<csv_file>', methods=['POST'])
def process_csv(csv_file):
    delete_items_from_csv(csv_file)
    flash('Le CSV a été traité avec succès.', 'success')
    return redirect(url_for('view_csv', csv_file=csv_file))

@app.route('/download/<csv_file>')
def download_csv(csv_file):
    return send_from_directory(directory=os.getcwd(), path=csv_file, as_attachment=True)

@app.route('/settings', methods=['GET', 'POST'])
def settings():
    global PLEX_URL, PLEX_TOKEN, PLEX_USERNAME, PLEX_PASSWORD
    global FRIEND_SERVER_NAME, LOG_FILE_PATH
    global plex, account  # Pour réinitialiser les instances PlexServer et MyPlexAccount

    # Récupérer les noms des bibliothèques depuis votre serveur Plex
    local_movie_libraries = get_library_sections(plex, 'movie')
    local_show_libraries = get_library_sections(plex, 'show')

    # Récupérer les noms des ressources (serveurs) depuis votre compte MyPlex
    try:
        resources = account.resources()
        friend_server_names = [resource.name for resource in resources]
    except Exception as e:
        flash(f"Erreur lors de la récupération des ressources : {e}", 'danger')
        friend_server_names = []

    # Ajouter l'option "ALL" si elle n'existe pas déjà
    for lib_list in [local_movie_libraries, local_show_libraries]:
        if "ALL" not in lib_list:
            lib_list.insert(0, "ALL")

    if request.method == 'POST':
        config['PLEX_URL'] = request.form['PLEX_URL']
        config['PLEX_TOKEN'] = request.form['PLEX_TOKEN']
        config['PLEX_USERNAME'] = request.form['PLEX_USERNAME']
        config['PLEX_PASSWORD'] = request.form['PLEX_PASSWORD']
        config['FRIEND_SERVER_NAME'] = request.form['friend_server_name']  # Sélection dynamique

        # Récupérer les bibliothèques sélectionnées (maintenant gérées via /clean et /duplicates)
        config['LOG_FILE_PATH'] = request.form['LOG_FILE_PATH']

        with open('config.json', 'w') as config_file:
            json.dump(config, config_file, indent=4)

        # Mettre à jour les variables globales avec les nouvelles valeurs
        PLEX_URL = config['PLEX_URL']
        PLEX_TOKEN = config['PLEX_TOKEN']
        PLEX_USERNAME = config['PLEX_USERNAME']
        PLEX_PASSWORD = config['PLEX_PASSWORD']
        FRIEND_SERVER_NAME = config['FRIEND_SERVER_NAME']
        LOG_FILE_PATH = config['LOG_FILE_PATH']

        # Réinitialiser les instances PlexServer et MyPlexAccount avec les nouvelles configurations
        plex = PlexServer(PLEX_URL, PLEX_TOKEN)
        account = MyPlexAccount(PLEX_USERNAME, PLEX_PASSWORD)

        flash('Paramètres mis à jour avec succès.', 'success')
        return redirect(url_for('index'))

    return render_template(
        'settings.html',
        config=config,
        local_movie_libraries=local_movie_libraries,
        local_show_libraries=local_show_libraries,
        friend_server_names=friend_server_names
    )

if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=True)
