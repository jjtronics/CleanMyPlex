from flask import Flask, render_template, request, redirect, url_for, flash, send_from_directory
import requests
import pandas as pd
from plexapi.server import PlexServer
from plexapi.myplex import MyPlexAccount
from datetime import datetime, timedelta
import json
import os

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

def generate_csv(library_name, csv_file):
    if os.path.exists(csv_file):
        existing_df = pd.read_csv(csv_file)
    else:
        existing_df = pd.DataFrame(columns=['title', 'rating', 'key', 'added_at', 'release_date', 'file_size', 'server', 'Action'])

    items = plex.library.section(library_name).all()
    existing_keys = [item.key for item in items]
    existing_df = existing_df[existing_df['key'].isin(existing_keys)]

    date_limit = datetime.now() - timedelta(days=DAYS_TO_IGNORE)
    new_items = []
    for item in items:
        release_date = item.originallyAvailableAt if item.originallyAvailableAt else None
        rating = item.audienceRating if item.audienceRating else 0

        # Filtrage des films
        if item.TYPE == 'movie':
            if (item.viewCount is None or item.viewCount == 0) and (item.addedAt < date_limit):
                if release_date and release_date <= RELEASE_DATE_LIMIT:
                    if (rating < RATING_LIMIT) and (INCLUDE_UNRATED or rating != 0):
                        file_size_gb = sum(media_part.size for media in item.media for media_part in media.parts) / (1024 ** 3)
                        new_items.append({
                            'title': item.title,
                            'rating': rating,
                            'key': item.key,
                            'added_at': item.addedAt.strftime('%Y-%m-%d'),
                            'release_date': release_date.strftime('%Y-%m-%d') if release_date else 'N/A',
                            'file_size': file_size_gb,
                            'server': library_name,
                            'Action': ''
                        })

        # Filtrage des séries
        elif item.TYPE == 'show':
            if (item.viewCount is None or item.viewCount == 0) and (item.addedAt < date_limit):
                if release_date and release_date <= RELEASE_DATE_LIMIT:
                    if (rating < RATING_LIMIT) and (INCLUDE_UNRATED or rating != 0):
                        file_size_gb = sum(media_part.size for episode in item.episodes() for media in episode.media for media_part in media.parts) / (1024 ** 3)
                        new_items.append({
                            'title': item.title,
                            'rating': rating,
                            'key': item.key,
                            'added_at': item.addedAt.strftime('%Y-%m-%d'),
                            'release_date': release_date.strftime('%Y-%m-%d') if release_date else 'N/A',
                            'file_size': file_size_gb,
                            'server': library_name,
                            'Action': ''
                        })

    new_df = pd.DataFrame(new_items)
    if not existing_df.empty and not new_df.empty:
        combined_df = pd.concat([existing_df, new_df]).drop_duplicates(subset='key', keep='first').reset_index(drop=True)
    elif not existing_df.empty:
        combined_df = existing_df
    else:
        combined_df = new_df

    combined_df['Action'] = combined_df['Action'].fillna('')
    combined_df['file_size'] = combined_df['file_size'].fillna(0.00)
    combined_df['file_size'] = pd.to_numeric(combined_df['file_size'], errors='coerce').fillna(0)
    combined_df = combined_df.infer_objects()
    combined_df['server'] = combined_df['server'].fillna('')
    combined_df['file_size'] = combined_df['file_size'].apply(lambda x: f"{x:.2f} Go")
    combined_df = combined_df.sort_values(by=['Action', 'added_at'], ascending=[False, True])
    columns_order = ['title', 'rating', 'key', 'added_at', 'release_date', 'file_size', 'server', 'Action']
    combined_df = combined_df[columns_order]
    combined_df.to_csv(csv_file, index=False)
    return combined_df, csv_file

def compare_libraries(library_name, friend_library_name, item_type):
    friend_server = account.resource(FRIEND_SERVER_NAME).connect()
    friend_library = friend_server.library.section(friend_library_name)
    my_library = plex.library.section(library_name)

    my_items = {item.title: item for item in my_library.all()}
    friend_items = {item.title: item for item in friend_library.all()}

    common_items = []
    total_size_gained = 0
    for title in my_items:
        if title in friend_items:
            if item_type == 'movie':
                my_size = sum(media_part.size for media in my_items[title].media for media_part in media.parts)
                friend_size = sum(media_part.size for media in friend_items[title].media for media_part in media.parts)
                larger_size = max(my_size, friend_size)
                server = 'BOTH' if my_size == friend_size else 'Mine' if my_size > friend_size else 'Friend'
            else:  # item_type == 'show'
                my_size = sum(media_part.size for episode in my_items[title].episodes() for media in episode.media for media_part in media.parts)
                friend_size = sum(media_part.size for episode in friend_items[title].episodes() for media in episode.media for media_part in media.parts)
                larger_size = max(my_size, friend_size)
                server = 'BOTH' if my_size == friend_size else 'Mine' if my_size > friend_size else 'Friend'
                my_episodes_count = len(my_items[title].episodes())
                friend_episodes_count = len(friend_items[title].episodes())
            total_size_gained += larger_size / (1024 ** 3)  # Convertir en Go
            common_item = {
                'title': title,
                'rating': my_items[title].audienceRating if my_items[title].audienceRating else 0,
                'key': my_items[title].key,
                'added_at': my_items[title].addedAt.strftime('%Y-%m-%d'),
                'release_date': my_items[title].originallyAvailableAt.strftime('%Y-%m-%d') if my_items[title].originallyAvailableAt else 'N/A',
                'local_file_size': f"{my_size / (1024 ** 3):.2f} Go",  # Convertir en Go
                'remote_file_size': f"{friend_size / (1024 ** 3):.2f} Go",  # Convertir en Go
                'largest_file_size': f"{larger_size / (1024 ** 3):.2f} Go",  # Convertir en Go
                'server': server
            }
            if item_type == 'show':
                common_item['number_of_local_episodes'] = my_episodes_count
                common_item['number_of_remote_episodes'] = friend_episodes_count
            common_items.append(common_item)

    common_df = pd.DataFrame(common_items)
    output_file = CSV_FILE_COMMON_MOVIES if item_type == 'movie' else CSV_FILE_COMMON_SERIES
    common_df.to_csv(output_file, index=False)
    return common_df, output_file, len(common_items), total_size_gained

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
    # Check if CSV files exist
    films_csv_exists = os.path.exists(CSV_FILE_FILMS)
    series_csv_exists = os.path.exists(CSV_FILE_SERIES)
    common_movies_csv_exists = os.path.exists(CSV_FILE_COMMON_MOVIES)
    common_series_csv_exists = os.path.exists(CSV_FILE_COMMON_SERIES)
    return render_template('index.html', films_csv_exists=films_csv_exists, series_csv_exists=series_csv_exists, common_movies_csv_exists=common_movies_csv_exists, common_series_csv_exists=common_series_csv_exists)

@app.route('/delete_csv/<csv_file>', methods=['POST'])
def delete_csv(csv_file):
    file_path = os.path.join(os.getcwd(), csv_file)
    if os.path.exists(file_path):
        os.remove(file_path)
        flash(f"Fichier {csv_file} supprimé avec succès.", 'success')
    else:
        flash(f"Le fichier {csv_file} n'existe pas.", 'danger')
    return redirect(url_for('index'))

@app.route('/clean', methods=['GET', 'POST'])
def clean():
    if request.method == 'POST':
        library = request.form.get('library')
        if library == 'films':
            df, csv_file = generate_csv(FILM_LIBRARY_NAME, CSV_FILE_FILMS)
        else:
            df, csv_file = generate_csv(SERIES_LIBRARY_NAME, CSV_FILE_SERIES)
        flash('CSV généré avec succès.', 'success')
        return redirect(url_for('view_csv', csv_file=csv_file))
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

    if request.method == 'POST':
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
