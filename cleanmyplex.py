from flask import Flask, render_template, request, redirect, url_for, flash, send_from_directory, jsonify, Response, send_file
import requests
import pandas as pd
from plexapi.server import PlexServer
from plexapi.myplex import MyPlexAccount
import json
import os
import threading
import time
import codecs
from uuid import uuid4
from io import BytesIO

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
CSV_FILE_FILMS = 'unwatched_movies.csv'
CSV_FILE_SERIES = 'unwatched_series.csv'
CSV_FILE_COMMON_MOVIES = 'common_movies.csv'
CSV_FILE_COMMON_SERIES = 'common_series.csv'

plex = PlexServer(PLEX_URL, PLEX_TOKEN, timeout=300)
account = MyPlexAccount(PLEX_USERNAME, PLEX_PASSWORD)

# Variables globales pour suivre les tâches en arrière-plan
tasks = {}
tasks_lock = threading.Lock()

def get_active_sessions():
    try:
        sessions = plex.sessions()
        session_data = []

        for session in sessions:
            last_active = session.startedAt.strftime('%Y-%m-%d %H:%M:%S') if hasattr(session, 'startedAt') and session.startedAt else 'Inconnu'
            session_info = {
                'username': session.usernames[0] if session.usernames else 'Inconnu',
                'publicAddress': session.players[0].address if session.players else 'N/A',
                'last_active': last_active
            }
            session_data.append(session_info)

        return session_data
    except Exception as e:
        flash(f"Erreur lors de la récupération des sessions actives : {e}", 'danger')
        return []

def get_view_history(user):
    try:
        view_history = []
        for section in plex.library.sections():
            for item in section.all():
                if item.viewCount > 0:
                    view_history.append({
                        'title': item.title,
                        'watched': item.viewCount > 0,
                        'user': user.username
                    })
        return view_history
    except Exception as e:
        flash(f"Erreur lors de la récupération de l'historique de lecture : {e}", 'danger')
        return []

def get_last_activity(user):
    try:
        last_activity = None

        for section in plex.library.sections():
            for item in section.all():
                history = item.history()
                for view in history:
                    if view.user and view.user.username == user.username:
                        if not last_activity or view.viewedAt > last_activity:
                            last_activity = view.viewedAt

        return last_activity.strftime('%Y-%m-%d %H:%M:%S') if last_activity else 'Inconnu'
    except Exception as e:
        flash(f"Erreur lors de la récupération de la dernière activité pour l'utilisateur {user.username} : {e}", 'danger')
        return 'Inconnu'

# Fonction pour récupérer dynamiquement les sections de bibliothèque
def get_library_sections(plex_server, media_type):
    sections = plex_server.library.sections()
    if media_type == 'movie':
        return [section.title for section in sections if section.type == 'movie']
    elif media_type == 'show':
        return [section.title for section in sections if section.type == 'show']
    else:
        return []

def compare_libraries(library_names_local, library_names_friend, media_type):
    friend_server = account.resource(FRIEND_SERVER_NAME).connect()

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

                if media_type == 'movie':
                    local_file_size_gb = sum(
                        media_part.size for media in local_item.media for media_part in media.parts
                    ) / (1024 ** 3)
                    remote_file_size_gb = sum(
                        media_part.size for media in friend_item.media for media_part in media.parts
                    ) / (1024 ** 3)
                    number_of_local_episodes = None
                    number_of_remote_episodes = None
                elif media_type == 'show':
                    local_file_size_gb = sum(
                        media_part.size
                        for episode in local_item.episodes()
                        for media in episode.media
                        for media_part in media.parts
                    ) / (1024 ** 3)
                    remote_file_size_gb = sum(
                        media_part.size
                        for episode in friend_item.episodes()
                        for media in episode.media
                        for media_part in media.parts
                    ) / (1024 ** 3)
                    number_of_local_episodes = len(local_item.episodes())
                    number_of_remote_episodes = len(friend_item.episodes())
                else:
                    continue

                largest_file_size_gb = max(local_file_size_gb, remote_file_size_gb)

                added_at = local_item.addedAt.strftime('%Y-%m-%d') if local_item.addedAt else 'N/A'
                release_date = local_item.originallyAvailableAt.strftime('%Y-%m-%d') if local_item.originallyAvailableAt else 'N/A'
                rating = local_item.audienceRating if local_item.audienceRating else 0
                if local_item.userRating is not None:
                    plex_rating = local_item.userRating
                elif local_item.rating is not None:
                    plex_rating = local_item.rating
                else:
                    plex_rating = 0

                if media_type == 'movie':
                    view_count = local_item.viewCount if hasattr(local_item, 'viewCount') else 0
                    local_file_path = local_item.media[0].parts[0].file if local_item.media and local_item.media[0].parts else 'N/A'
                elif media_type == 'show':
                    view_count = sum(episode.viewCount for episode in local_item.episodes() if hasattr(episode, 'viewCount'))
                    local_file_path = 'N/A'
                else:
                    view_count = 0
                    local_file_path = 'N/A'

                summary = local_item.summary if local_item.summary else 'N/A'
                genres = ', '.join([genre.tag for genre in local_item.genres]) if hasattr(local_item, 'genres') else 'N/A'
                directors = ', '.join([director.tag for director in local_item.directors]) if hasattr(local_item, 'directors') else 'N/A'
                actors = ', '.join([actor.tag for actor in local_item.actors]) if hasattr(local_item, 'actors') else 'N/A'

                if hasattr(local_item, 'thumb'):
                    poster_url = f"/poster/{local_item.ratingKey}"
                else:
                    poster_url = 'N/A'

                duplicate_entry = {
                    'title': title,
                    'rating': rating,
                    'plex_rating': plex_rating,
                    'view_count': view_count,
                    'local_path': local_file_path,
                    'added_at': added_at,
                    'release_date': release_date,
                    'local_file_size': f"{local_file_size_gb:.2f} Go",
                    'remote_file_size': f"{remote_file_size_gb:.2f} Go",
                    'largest_file_size': f"{largest_file_size_gb:.2f} Go",
                    'Bibliothèque': lib.title,
                    'Action': '',
                    'poster_url': poster_url,
                    'summary': summary,
                    'genres': genres,
                    'directors': directors,
                    'actors': actors
                }

                if media_type == 'show':
                    duplicate_entry['number_of_local_episodes'] = number_of_local_episodes
                    duplicate_entry['number_of_remote_episodes'] = number_of_remote_episodes

                duplicates.append(duplicate_entry)

    df = pd.DataFrame(duplicates)
    output_file = CSV_FILE_COMMON_MOVIES if media_type == 'movie' else CSV_FILE_COMMON_SERIES

    if media_type == 'movie':
        columns_order = [
            'title', 'rating', 'plex_rating', 'view_count', 'local_path',
            'added_at', 'release_date',
            'local_file_size', 'remote_file_size', 'largest_file_size', 'Bibliothèque', 'Action',
            'poster_url',
            'summary', 'genres', 'directors', 'actors'
        ]
    else:
        columns_order = [
            'title', 'rating', 'plex_rating', 'view_count', 'local_path',
            'added_at', 'release_date',
            'local_file_size', 'remote_file_size', 'largest_file_size', 'Bibliothèque',
            'number_of_local_episodes', 'number_of_remote_episodes', 'Action',
            'poster_url',
            'summary', 'genres', 'directors', 'actors'
        ]
    df = df[columns_order]

    total_space_saved = sum(float(size.replace(' Go', '')) for size in df['largest_file_size'])

    df.to_csv(output_file, index=False)
    return df, output_file, len(duplicates), total_space_saved

def generate_csv(library_names, csv_file, media_type):
    if os.path.exists(csv_file):
        existing_df = pd.read_csv(csv_file, encoding='utf-8', delimiter=',', quotechar='"')
        existing_df['file_size'] = existing_df['file_size'].replace('N/A', '0')
        existing_df['file_size'] = existing_df['file_size'].str.replace(' Go', '', regex=False).astype(float)
    else:
        columns = [
            'title', 'ratingKey', 'rating', 'plex_rating', 'view_count', 'local_path',
            'added_at', 'release_date', 'file_size', 'Bibliothèque', 'Action',
            'poster_url', 'summary', 'genres', 'directors', 'actors'
        ]
        existing_df = pd.DataFrame(columns=columns)

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

            if item.userRating is not None:
                plex_rating = item.userRating
            elif item.rating is not None:
                plex_rating = item.rating
            else:
                plex_rating = 0

            summary = item.summary if item.summary else 'N/A'
            genres = ', '.join([genre.tag for genre in item.genres]) if hasattr(item, 'genres') else 'N/A'
            directors = ', '.join([director.tag for director in item.directors]) if hasattr(item, 'directors') else 'N/A'
            actors = ', '.join([actor.tag for actor in item.actors]) if hasattr(item, 'actors') else 'N/A'

            if hasattr(item, 'thumb'):
                poster_url = f"/poster/{item.ratingKey}"
            else:
                poster_url = 'N/A'

            if item.TYPE == 'movie':
                view_count = item.viewCount if hasattr(item, 'viewCount') else 0
                local_path = item.media[0].parts[0].file if item.media and item.media[0].parts else 'N/A'
                file_size_gb = sum(media_part.size for media in item.media for media_part in media.parts) / (1024 ** 3)
                new_items.append({
                    'title': item.title,
                    'ratingKey': item.ratingKey,
                    'rating': rating,
                    'plex_rating': plex_rating,
                    'view_count': view_count,
                    'local_path': local_path,
                    'added_at': item.addedAt.strftime('%Y-%m-%d') if item.addedAt else 'N/A',
                    'release_date': release_date.strftime('%Y-%m-%d') if release_date else 'N/A',
                    'file_size': file_size_gb,
                    'Bibliothèque': library_name,
                    'Action': '',
                    'poster_url': poster_url,
                    'summary': summary,
                    'genres': genres,
                    'directors': directors,
                    'actors': actors
                })

            elif item.TYPE == 'show':
                view_count = sum(episode.viewCount for episode in item.episodes() if hasattr(episode, 'viewCount'))
                local_path = 'N/A'
                file_size_gb = sum(
                    media_part.size
                    for episode in item.episodes()
                    for media in episode.media
                    for media_part in media.parts
                ) / (1024 ** 3)
                new_items.append({
                    'title': item.title,
                    'ratingKey': item.ratingKey,
                    'rating': rating,
                    'plex_rating': plex_rating,
                    'view_count': view_count,
                    'local_path': local_path,
                    'added_at': item.addedAt.strftime('%Y-%m-%d') if item.addedAt else 'N/A',
                    'release_date': release_date.strftime('%Y-%m-%d') if release_date else 'N/A',
                    'file_size': file_size_gb,
                    'Bibliothèque': library_name,
                    'Action': '',
                    'poster_url': poster_url,
                    'summary': summary,
                    'genres': genres,
                    'directors': directors,
                    'actors': actors
                })

    new_df = pd.DataFrame(new_items)

    if not existing_df.empty and not new_df.empty:
        combined_df = pd.concat([existing_df, new_df]).drop_duplicates(subset='title', keep='first').reset_index(drop=True)
    elif not existing_df.empty:
        combined_df = existing_df
    elif not new_df.empty:
        combined_df = new_df
    else:
        combined_df = pd.DataFrame(columns=[
            'title', 'ratingKey', 'rating', 'plex_rating', 'view_count', 'local_path',
            'added_at', 'release_date', 'file_size', 'Bibliothèque', 'Action',
            'poster_url', 'summary', 'genres', 'directors', 'actors'
        ])

    combined_df['Action'] = combined_df['Action'].fillna('')
    combined_df['file_size'] = pd.to_numeric(combined_df['file_size'], errors='coerce').fillna(0)
    combined_df = combined_df.sort_values(by=['added_at'], ascending=True)

    combined_df['file_size'] = combined_df['file_size'].apply(lambda x: f"{x:.2f} Go")

    columns_order = [
        'title', 'ratingKey', 'rating', 'plex_rating', 'view_count', 'local_path',
        'added_at', 'release_date', 'file_size', 'Bibliothèque', 'Action',
        'poster_url', 'summary', 'genres', 'directors', 'actors'
    ]
    combined_df = combined_df[columns_order]
    combined_df.to_csv(csv_file, index=False)
    return combined_df, csv_file

# Fonction de génération de CSV en arrière-plan avec thread
def generate_csv_thread(library_names, csv_file, media_type, task_id):
    try:
        generate_csv(library_names, csv_file, media_type)
        with tasks_lock:
            tasks[task_id]['status'] = 'completed'
            tasks[task_id]['message'] = f"CSV {csv_file} généré avec succès."
        print(f"CSV {csv_file} généré avec succès.")
    except Exception as e:
        with tasks_lock:
            tasks[task_id]['status'] = 'failed'
            tasks[task_id]['message'] = f"Erreur lors de la génération du CSV : {e}"
        print(f"Erreur lors de la génération du CSV : {e}")

# Nouvelle fonction pour la suppression en tâche de fond
def delete_items_from_csv_thread(csv_file, task_id):
    try:
        if not os.path.exists(csv_file):
            with tasks_lock:
                tasks[task_id]['status'] = 'failed'
                tasks[task_id]['message'] = f"Le fichier CSV {csv_file} n'existe pas."
            return

        df = pd.read_csv(csv_file)
        items_to_delete = df[df['Action'] == 'D']

        total_items = len(items_to_delete)
        deleted_items = 0

        for index, row in items_to_delete.iterrows():
            try:
                rating_key = row.get('ratingKey')
                if pd.notna(rating_key):
                    item = plex.fetchItem(int(rating_key))
                    item.delete()
                    df.drop(index, inplace=True)
                    deleted_items += 1

                    with tasks_lock:
                        tasks[task_id]['progress'] = f"{deleted_items}/{total_items} éléments supprimés."
                else:
                    with tasks_lock:
                        tasks[task_id]['errors'].append(f"Clé de notation invalide pour {row['title']}.")
            except Exception as e:
                with tasks_lock:
                    tasks[task_id]['errors'].append(f"Erreur lors de la suppression de {row['title']}: {e}")

        df.to_csv(csv_file, index=False)

        with tasks_lock:
            if tasks[task_id]['errors']:
                tasks[task_id]['status'] = 'completed_with_errors'
                tasks[task_id]['message'] = f"Suppression terminée avec des erreurs. {deleted_items}/{total_items} éléments supprimés."
            else:
                tasks[task_id]['status'] = 'completed'
                tasks[task_id]['message'] = f"Suppression terminée avec succès. {deleted_items} éléments supprimés."

    except Exception as e:
        with tasks_lock:
            tasks[task_id]['status'] = 'failed'
            tasks[task_id]['message'] = f"Erreur lors de la suppression : {e}"

def compare_libraries_thread(local_library_names, friend_library_names, media_type, task_id):
    try:
        df, output_file, num_items, total_space_saved_gb = compare_libraries(local_library_names, friend_library_names, media_type)
        with tasks_lock:
            tasks[task_id]['status'] = 'completed'
            tasks[task_id]['message'] = f"CSV {output_file} généré avec succès."
    except Exception as e:
        with tasks_lock:
            tasks[task_id]['status'] = 'failed'
            tasks[task_id]['message'] = f"Erreur lors de la comparaison des bibliothèques : {e}"

@app.route('/test_token', methods=['POST'])
def test_token():
    try:
        test_token = request.form['PLEX_TOKEN']
        plex_test = PlexServer(config['PLEX_URL'], test_token)
        plex_test.library.sections()
        return jsonify({'status': 'success', 'message': 'Connexion réussie avec ce token !'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'Erreur : {str(e)}'})

@app.route('/test_login', methods=['POST'])
def test_login():
    try:
        plex_username = request.form['PLEX_USERNAME']
        plex_password = request.form['PLEX_PASSWORD']
        account_test = MyPlexAccount(plex_username, plex_password)
        account_test.devices()
        return jsonify({'status': 'success', 'message': 'Connexion réussie avec ces identifiants !'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'Erreur : {str(e)}'})

@app.route('/manage_users')
def manage_users():
    try:
        users = account.users()
        sessions = get_active_sessions()
        user_data = []

        for user in users:
            session_info = next((session for session in sessions if session['username'] == user.username), None)
            is_active = "Oui" if session_info else "Non"

            user_info = {
                'username': user.username,
                'email': user.email if hasattr(user, 'email') else 'N/A',
                'title': user.title if hasattr(user, 'title') else 'N/A',
                'userID': user.id,
                'homeUser': 'Oui' if user.home else 'Non',
                'role': 'Invité',
                'is_active': is_active,
                'publicAddress': session_info['publicAddress'] if session_info else 'N/A',
            }
            user_data.append(user_info)

        return render_template('manage_users.html', users=user_data)
    except Exception as e:
        flash(f"Erreur lors de la récupération des utilisateurs : {e}", 'danger')
        return redirect(url_for('index'))

@app.route('/user_details/<username>')
def user_details(username):
    try:
        user = next((u for u in account.users() if u.username == username), None)
        if not user:
            flash(f"Utilisateur {username} non trouvé", 'danger')
            return redirect(url_for('manage_users'))

        sessions = get_active_sessions()
        session_info = next((session for session in sessions if session['username'] == user.username), None)

        user_info = {
            'username': user.username,
            'email': user.email if hasattr(user, 'email') else 'N/A',
            'title': user.title if hasattr(user, 'title') else 'N/A',
            'userID': user.id,
            'homeUser': 'Oui' if user.home else 'Non',
            'publicAddress': session_info['publicAddress'] if session_info else 'N/A',
            'subscriptionType': user.subscriptionType if hasattr(user, 'subscriptionType') else 'Gratuit',
            'is_active': "Oui" if session_info else "Non"
        }

        return render_template('user_details.html', user=user_info)
    except Exception as e:
        flash(f"Erreur lors de la récupération des détails de l'utilisateur {username} : {e}", 'danger')
        return redirect(url_for('manage_users'))

@app.route('/')
def index():
    films_csv_exists = os.path.exists(CSV_FILE_FILMS)
    series_csv_exists = os.path.exists(CSV_FILE_SERIES)
    common_movies_csv_exists = os.path.exists(CSV_FILE_COMMON_MOVIES)
    common_series_csv_exists = os.path.exists(CSV_FILE_COMMON_SERIES)
    tasks_list = []
    if 'tasks' in request.args:
        tasks_list = request.args.get('tasks').split(',')
    return render_template('index.html',
                           films_csv_exists=films_csv_exists,
                           series_csv_exists=series_csv_exists,
                           common_movies_csv_exists=common_movies_csv_exists,
                           common_series_csv_exists=common_series_csv_exists,
                           tasks_list=tasks_list)

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
    local_movie_libraries = get_library_sections(plex, 'movie')
    local_show_libraries = get_library_sections(plex, 'show')

    if "ALL" not in local_movie_libraries:
        local_movie_libraries.insert(0, "ALL")
    if "ALL" not in local_show_libraries:
        local_show_libraries.insert(0, "ALL")

    films_csv_mtime = time.ctime(os.path.getmtime(CSV_FILE_FILMS)) if os.path.exists(CSV_FILE_FILMS) else None
    series_csv_mtime = time.ctime(os.path.getmtime(CSV_FILE_SERIES)) if os.path.exists(CSV_FILE_SERIES) else None

    if request.method == 'POST':
        selected_movie_libraries = request.form.getlist('library_names_films')
        selected_series_libraries = request.form.getlist('library_names_series')
        tasks_list = []
        if selected_movie_libraries:
            csv_file = CSV_FILE_FILMS
            media_type = 'movie'
            task_id = str(uuid4())
            with tasks_lock:
                tasks[task_id] = {'status': 'running', 'message': 'La génération du CSV des films a démarré.'}
            threading.Thread(target=generate_csv_thread, args=(selected_movie_libraries, csv_file, media_type, task_id)).start()
            tasks_list.append(task_id)
            flash('La génération du CSV des films a démarré en arrière-plan.', 'info')

        if selected_series_libraries:
            csv_file = CSV_FILE_SERIES
            media_type = 'show'
            task_id = str(uuid4())
            with tasks_lock:
                tasks[task_id] = {'status': 'running', 'message': 'La génération du CSV des séries a démarré.'}
            threading.Thread(target=generate_csv_thread, args=(selected_series_libraries, csv_file, media_type, task_id)).start()
            tasks_list.append(task_id)
            flash('La génération du CSV des séries a démarré en arrière-plan.', 'info')

        return redirect(url_for('index', tasks=','.join(tasks_list)))

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
    try:
        friend_server = account.resource(FRIEND_SERVER_NAME).connect()
        friend_movie_libraries = get_library_sections(friend_server, 'movie')
        friend_show_libraries = get_library_sections(friend_server, 'show')
    except Exception as e:
        flash(f"Erreur lors de la connexion au serveur de l'ami : {e}", 'danger')
        friend_movie_libraries = []
        friend_show_libraries = []

    local_movie_libraries = get_library_sections(plex, 'movie')
    local_show_libraries = get_library_sections(plex, 'show')

    for lib_list in [friend_movie_libraries, friend_show_libraries, local_movie_libraries, local_show_libraries]:
        if "ALL" not in lib_list:
            lib_list.insert(0, "ALL")

    common_movies_csv_exists = os.path.exists(CSV_FILE_COMMON_MOVIES)
    common_series_csv_exists = os.path.exists(CSV_FILE_COMMON_SERIES)

    common_movies_csv_mtime = time.ctime(os.path.getmtime(CSV_FILE_COMMON_MOVIES)) if common_movies_csv_exists else None
    common_series_csv_mtime = time.ctime(os.path.getmtime(CSV_FILE_COMMON_SERIES)) if common_series_csv_exists else None

    if request.method == 'POST':
        selected_local_movie_libraries = request.form.getlist('local_library_movies')
        selected_friend_movie_libraries = request.form.getlist('friend_library_movies')
        selected_local_series_libraries = request.form.getlist('local_library_series')
        selected_friend_series_libraries = request.form.getlist('friend_library_series')
        tasks_list = []
        if selected_local_movie_libraries and selected_friend_movie_libraries:
            media_type = 'movie'
            task_id = str(uuid4())
            with tasks_lock:
                tasks[task_id] = {'status': 'running', 'message': 'La comparaison des films a démarré.'}
            threading.Thread(target=compare_libraries_thread, args=(selected_local_movie_libraries, selected_friend_movie_libraries, media_type, task_id)).start()
            tasks_list.append(task_id)
            flash('La comparaison des films a démarré en arrière-plan.', 'info')

        if selected_local_series_libraries and selected_friend_series_libraries:
            media_type = 'show'
            task_id = str(uuid4())
            with tasks_lock:
                tasks[task_id] = {'status': 'running', 'message': 'La comparaison des séries a démarré.'}
            threading.Thread(target=compare_libraries_thread, args=(selected_local_series_libraries, selected_friend_series_libraries, media_type, task_id)).start()
            tasks_list.append(task_id)
            flash('La comparaison des séries a démarré en arrière-plan.', 'info')

        return redirect(url_for('index', tasks=','.join(tasks_list)))

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

@app.route('/task_status/<task_id>')
def task_status(task_id):
    with tasks_lock:
        status = tasks.get(task_id, {'status': 'unknown', 'message': 'Tâche inconnue.'})
    return jsonify(status)

@app.route('/view_csv/<csv_file>', methods=['GET', 'POST'])
def view_csv(csv_file):
    if os.path.exists(csv_file):
        df = pd.read_csv(csv_file)
        df = df.fillna('N/A')
    else:
        flash('Le fichier CSV spécifié n\'existe pas.', 'danger')
        return redirect(url_for('index'))

    if 'ratingKey' not in df.columns:
        df['ratingKey'] = 'N/A'

    date_columns = ['added_at', 'release_date']
    for col in date_columns:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], format='%Y-%m-%d', errors='coerce')

    unique_libraries = sorted(df['Bibliothèque'].unique().tolist()) if 'Bibliothèque' in df.columns else []
    unique_actions = sorted(df['Action'].dropna().unique().tolist()) if 'Action' in df.columns else []

    if request.method == 'POST':
        for index, row in df.iterrows():
            action = request.form.get(f'action_{index}')
            if action in ['A', 'D']:
                df.at[index, 'Action'] = action
        df.to_csv(csv_file, index=False)
        flash('CSV mis à jour avec succès.', 'success')
        return redirect(url_for('view_csv', csv_file=csv_file))

    return render_template(
        'view_csv.html',
        df=df,
        titles=df.columns.values,
        csv_file=csv_file,
        unique_libraries=unique_libraries,
        unique_actions=unique_actions
    )

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
    else:
        flash("Bibliothèque CSV inconnue.", 'danger')
        return redirect(url_for('index'))

    return redirect(url_for('view_csv', csv_file=csv_file))

@app.route('/process_csv/<path:csv_file>', methods=['POST'])
def process_csv(csv_file):
    task_id = str(uuid4())
    with tasks_lock:
        tasks[task_id] = {
            'status': 'running',
            'message': 'La suppression a démarré.',
            'progress': '0%',
            'errors': []
        }
    threading.Thread(target=delete_items_from_csv_thread, args=(csv_file, task_id)).start()
    flash('La suppression a démarré en arrière-plan.', 'info')
    return redirect(url_for('index', tasks=task_id))
    pass

@app.route('/download/<path:csv_file>')
def download_csv(csv_file):
    return send_from_directory(directory=os.getcwd(), path=csv_file, as_attachment=True)

@app.route('/settings', methods=['GET', 'POST'])
def settings():
    global PLEX_URL, PLEX_TOKEN, PLEX_USERNAME, PLEX_PASSWORD
    global FRIEND_SERVER_NAME
    global plex, account

    if request.method == 'POST':
        config['PLEX_URL'] = request.form.get('PLEX_URL')
        config['PLEX_TOKEN'] = request.form.get('PLEX_TOKEN')
        config['PLEX_USERNAME'] = request.form.get('PLEX_USERNAME')
        config['PLEX_PASSWORD'] = request.form.get('PLEX_PASSWORD')
        config['FRIEND_SERVER_NAME'] = request.form.get('friend_server_name', '')

        with open('config.json', 'w') as config_file:
            json.dump(config, config_file, indent=4)

        PLEX_URL = config['PLEX_URL']
        PLEX_TOKEN = config['PLEX_TOKEN']
        PLEX_USERNAME = config['PLEX_USERNAME']
        PLEX_PASSWORD = config['PLEX_PASSWORD']
        FRIEND_SERVER_NAME = config['FRIEND_SERVER_NAME']

        plex = PlexServer(PLEX_URL, PLEX_TOKEN)
        account = MyPlexAccount(PLEX_USERNAME, PLEX_PASSWORD)

        flash('Paramètres mis à jour avec succès.', 'success')
        return redirect(url_for('index'))

    return render_template(
        'settings.html',
        config=config,
        local_movie_libraries=get_library_sections(plex, 'movie'),
        local_show_libraries=get_library_sections(plex, 'show'),
        friend_server_names=[resource.name for resource in account.resources()]
    )

@app.route('/poster/<int:rating_key>')
def get_poster(rating_key):
    try:
        item = plex.fetchItem(rating_key)
        if item.thumb:
            poster_url = plex.url(item.thumb)
            headers = {'X-Plex-Token': PLEX_TOKEN}
            response = requests.get(poster_url, headers=headers, stream=True)
            return Response(response.content, mimetype='image/jpeg')
        else:
            return send_file('static/no_image_available.jpg', mimetype='image/jpeg')
    except Exception as e:
        print(f"Erreur lors de la récupération de la jaquette : {e}")
        return send_file('static/no_image_available.jpg', mimetype='image/jpeg')

if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=True)
