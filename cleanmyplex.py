from flask import Flask, render_template, request, redirect, url_for, flash, send_from_directory, jsonify
import requests
import pandas as pd
from plexapi.server import PlexServer
from plexapi.myplex import MyPlexAccount
from datetime import datetime, timedelta
import json
import os
import threading
import time
import operator

app = Flask(__name__)
app.secret_key = 'supersecretkey'

# Charger la configuration depuis le fichier config.json
with open('config.json') as config_file:
    config = json.load(config_file)

PLEX_URL = config['PLEX_URL']
PLEX_TOKEN = config['PLEX_TOKEN']
PLEX_USERNAME = config['PLEX_USERNAME']
PLEX_PASSWORD = config['PLEX_PASSWORD']
FRIEND_SERVER_NAME = config['FRIEND_SERVER_NAME']
FRIEND_LIBRARY_NAME_FILMS = config['FRIEND_LIBRARY_NAME_FILMS']
FRIEND_LIBRARY_NAME_SERIES = config['FRIEND_LIBRARY_NAME_SERIES']
FILM_LIBRARY_NAME = config['FILM_LIBRARY_NAME']
SERIES_LIBRARY_NAME = config['SERIES_LIBRARY_NAME']
LOG_FILE_PATH = config['LOG_FILE_PATH']
DAYS_TO_IGNORE = config['DAYS_TO_IGNORE']
RELEASE_DATE_LIMIT = datetime.strptime(config['RELEASE_DATE_LIMIT'], '%Y-%m-%d')
RATING_LIMIT = config['RATING_LIMIT']
INCLUDE_UNRATED = config['INCLUDE_UNRATED']

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
        with open(log_file_path, 'r', encoding='latin1') as log_file:
            log_content = log_file.read()
            return 'hardware transcoding' in log_content
    except FileNotFoundError:
        return False

# Fonction pour comparer les bibliothèques et générer un CSV des doublons
def compare_libraries(library_name_local, library_name_friend, media_type):
    friend_server = account.resource(FRIEND_SERVER_NAME).connect()
    friend_library = friend_server.library.section(library_name_friend)
    local_library = plex.library.section(library_name_local)

    friend_titles = {item.title for item in friend_library.all()}
    duplicates = []
    total_space_saved = 0

    for item in local_library.all():
        if item.title in friend_titles:
            file_size_gb = 0
            if media_type == 'movie':
                file_size_gb = sum(media_part.size for media in item.media for media_part in media.parts) / (1024 ** 3)
            elif media_type == 'show':
                file_size_gb = sum(media_part.size for episode in item.episodes() for media in episode.media for media_part in media.parts) / (1024 ** 3)
            duplicates.append({
                'title': item.title,
                'key': item.key,
                'file_size': f"{file_size_gb:.2f} Go",
                'Action': ''
            })
            total_space_saved += file_size_gb

    df = pd.DataFrame(duplicates)
    output_file = CSV_FILE_COMMON_MOVIES if media_type == 'movie' else CSV_FILE_COMMON_SERIES
    df.to_csv(output_file, index=False)

    return df, output_file, len(duplicates), total_space_saved

# Fonction pour générer le CSV
def generate_csv(library_name, csv_file):
    if os.path.exists(csv_file):
        existing_df = pd.read_csv(csv_file, encoding='utf-8', delimiter=',', quotechar='"')
    else:
        existing_df = pd.DataFrame(columns=['title', 'rating', 'plex_rating', 'key', 'added_at', 'release_date', 'file_size', 'server', 'Action'])

    items = plex.library.section(library_name).all()

    new_items = []
    for item in items:
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
                'added_at': item.addedAt.strftime('%Y-%m-%d'),
                'release_date': release_date.strftime('%Y-%m-%d') if release_date else 'N/A',
                'file_size': file_size_gb,
                'server': library_name,
                'Action': ''
            })

        # Séries
        elif item.TYPE == 'show':
            file_size_gb = sum(media_part.size for episode in item.episodes() for media in episode.media for media_part in media.parts) / (1024 ** 3)
            new_items.append({
                'title': item.title,
                'rating': rating,
                'plex_rating': plex_rating,
                'key': item.key,
                'added_at': item.addedAt.strftime('%Y-%m-%d'),
                'release_date': release_date.strftime('%Y-%m-%d') if release_date else 'N/A',
                'file_size': file_size_gb,
                'server': library_name,
                'Action': ''
            })

    new_df = pd.DataFrame(new_items)

    combined_df = pd.concat([existing_df, new_df]).drop_duplicates(subset='key', keep='first').reset_index(drop=True)
    combined_df['Action'] = combined_df['Action'].fillna('')
    combined_df['file_size'] = combined_df['file_size'].fillna(0.00)
    combined_df['file_size'] = pd.to_numeric(combined_df['file_size'], errors='coerce').fillna(0)
    combined_df['file_size'] = combined_df['file_size'].apply(lambda x: f"{x:.2f} Go")
    combined_df = combined_df.sort_values(by=['added_at'], ascending=True)

    columns_order = ['title', 'rating', 'plex_rating', 'key', 'added_at', 'release_date', 'file_size', 'server', 'Action']
    combined_df = combined_df[columns_order]

    combined_df.to_csv(csv_file, index=False)
    return combined_df, csv_file

# Fonction de génération de CSV en arrière-plan avec thread
def generate_csv_thread(library_name, csv_file):
    try:
        generate_csv(library_name, csv_file)
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

# Fonction pour appliquer un filtre numérique
def apply_numeric_filter(df, column_name, filter_str):
    ops = {
        '>=': operator.ge,
        '<=': operator.le,
        '>': operator.gt,
        '<': operator.lt,
        '==': operator.eq,
        '!=': operator.ne,
    }
    for op_str, op_func in ops.items():
        if filter_str.startswith(op_str):
            try:
                value = float(filter_str[len(op_str):].strip())
                return df[op_func(df[column_name].astype(float), value)]
            except ValueError:
                flash(f"Valeur numérique invalide pour le filtre sur {column_name}.", 'danger')
                break
    return df

# Fonction pour appliquer un filtre de date
def apply_date_filter(df, column_name, filter_str):
    ops = {
        '>=': operator.ge,
        '<=': operator.le,
        '>': operator.gt,
        '<': operator.lt,
        '==': operator.eq,
        '!=': operator.ne,
    }
    for op_str, op_func in ops.items():
        if filter_str.startswith(op_str):
            date_str = filter_str[len(op_str):].strip()
            try:
                date_value = pd.to_datetime(date_str, errors='coerce')
                if pd.isnull(date_value):
                    flash(f"Date invalide pour le filtre sur {column_name}.", 'danger')
                    break
                return df[op_func(df[column_name], date_value)]
            except ValueError:
                flash(f"Erreur lors de l'analyse de la date pour le filtre sur {column_name}.", 'danger')
                break
    return df

@app.route('/')
def index():
    films_csv_exists = os.path.exists(CSV_FILE_FILMS)
    series_csv_exists = os.path.exists(CSV_FILE_SERIES)
    common_movies_csv_exists = os.path.exists(CSV_FILE_COMMON_MOVIES)
    common_series_csv_exists = os.path.exists(CSV_FILE_COMMON_SERIES)
    return render_template('index.html', films_csv_exists=films_csv_exists, series_csv_exists=series_csv_exists, common_movies_csv_exists=common_movies_csv_exists, common_series_csv_exists=common_series_csv_exists)

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
            os.remove(file_path)
            flash(f"Fichier {csv_file} supprimé avec succès.", 'success')
        else:
            flash(f"Le fichier {csv_file} n'existe pas.", 'danger')
    else:
        flash(f"Le fichier {csv_file} spécifié est invalide.", 'danger')
    return redirect(url_for('index'))

@app.route('/clean', methods=['GET', 'POST'])
def clean():
    if request.method == 'POST':
        library = request.form.get('library')
        if library == 'films':
            csv_file = CSV_FILE_FILMS
            threading.Thread(target=generate_csv_thread, args=(FILM_LIBRARY_NAME, csv_file)).start()
        else:
            csv_file = CSV_FILE_SERIES
            threading.Thread(target=generate_csv_thread, args=(SERIES_LIBRARY_NAME, csv_file)).start()
        flash('La génération du CSV a démarré en arrière-plan.', 'info')
        return redirect(url_for('index'))
    return render_template('clean.html', films_csv_exists=os.path.exists(CSV_FILE_FILMS), series_csv_exists=os.path.exists(CSV_FILE_SERIES))

@app.route('/duplicates', methods=['GET', 'POST'])
def duplicates():
    if request.method == 'POST':
        library = request.form.get('library')
        if library == 'films':
            df, output_file, num_items, total_space_saved_gb = compare_libraries(FILM_LIBRARY_NAME, FRIEND_LIBRARY_NAME_FILMS, 'movie')
        else:
            df, output_file, num_items, total_space_saved_gb = compare_libraries(SERIES_LIBRARY_NAME, FRIEND_LIBRARY_NAME_SERIES, 'show')
        flash(f"Nombre d'éléments en commun : {num_items}", 'success')
        flash(f"Espace disque total gagné en supprimant les plus gros doublons : {total_space_saved_gb:.2f} Go", 'info')
        return redirect(url_for('view_csv', csv_file=output_file))
    return render_template('duplicates.html', common_movies_csv_exists=os.path.exists(CSV_FILE_COMMON_MOVIES), common_series_csv_exists=os.path.exists(CSV_FILE_COMMON_SERIES))

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
    else:
        flash('Le fichier CSV spécifié n\'existe pas.', 'danger')
        return redirect(url_for('index'))

    # Convertir les colonnes de date en datetime
    df['added_at'] = pd.to_datetime(df['added_at'], errors='coerce')
    df['release_date'] = pd.to_datetime(df['release_date'], errors='coerce')

    if request.method == 'POST':
        # Mise à jour des actions (A pour Archiver, D pour Supprimer)
        for index, row in df.iterrows():
            action = request.form.get(f'action_{index}')
            if action in ['A', 'D']:
                df.at[index, 'Action'] = action
        df.to_csv(csv_file, index=False)
        flash('CSV mis à jour avec succès.', 'success')
        return redirect(url_for('view_csv', csv_file=csv_file))

    # Récupérer les filtres depuis les paramètres de requête
    rating_filter = request.args.get('rating_filter')
    plex_rating_filter = request.args.get('plex_rating_filter')
    added_at_filter = request.args.get('added_at_filter')
    release_date_filter = request.args.get('release_date_filter')

    # Appliquer les filtres
    if rating_filter:
        df = apply_numeric_filter(df, 'rating', rating_filter)
    if plex_rating_filter:
        df = apply_numeric_filter(df, 'plex_rating', plex_rating_filter)
    if added_at_filter:
        df = apply_date_filter(df, 'added_at', added_at_filter)
    if release_date_filter:
        df = apply_date_filter(df, 'release_date', release_date_filter)

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
    if request.method == 'POST':
        config['PLEX_URL'] = request.form['PLEX_URL']
        config['PLEX_TOKEN'] = request.form['PLEX_TOKEN']
        config['PLEX_USERNAME'] = request.form['PLEX_USERNAME']
        config['PLEX_PASSWORD'] = request.form['PLEX_PASSWORD']
        config['FRIEND_SERVER_NAME'] = request.form['FRIEND_SERVER_NAME']
        config['FRIEND_LIBRARY_NAME_FILMS'] = request.form['FRIEND_LIBRARY_NAME_FILMS']
        config['FRIEND_LIBRARY_NAME_SERIES'] = request.form['FRIEND_LIBRARY_NAME_SERIES']
        config['FILM_LIBRARY_NAME'] = request.form['FILM_LIBRARY_NAME']
        config['SERIES_LIBRARY_NAME'] = request.form['SERIES_LIBRARY_NAME']
        config['LOG_FILE_PATH'] = request.form['LOG_FILE_PATH']
        config['DAYS_TO_IGNORE'] = int(request.form['DAYS_TO_IGNORE'])
        config['RELEASE_DATE_LIMIT'] = request.form['RELEASE_DATE_LIMIT']
        config['RATING_LIMIT'] = float(request.form['RATING_LIMIT'])
        config['INCLUDE_UNRATED'] = request.form.get('INCLUDE_UNRATED') == 'on'

        with open('config.json', 'w') as config_file:
            json.dump(config, config_file, indent=4)

        flash('Paramètres mis à jour avec succès.', 'success')
        return redirect(url_for('index'))

    return render_template('settings.html', config=config)

if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=True)
