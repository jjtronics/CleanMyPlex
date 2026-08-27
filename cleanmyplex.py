from flask import Flask, render_template, request, redirect, url_for, flash, send_from_directory, jsonify, Response, send_file
import requests
from plexapi.server import PlexServer
from plexapi.myplex import MyPlexAccount
import json
import os
import re
import sqlite3
import threading
import time
from urllib.parse import urljoin
from html import escape
from uuid import uuid4

app = Flask(__name__)
app.secret_key = 'supersecretkey'

# Charger la configuration depuis le fichier config.json
def load_config():
    default_config = {
        'PLEX_URL': '',
        'PLEX_TOKEN': '',
        'PLEX_USERNAME': '',
        'PLEX_PASSWORD': '',
        'FRIEND_SERVER_NAME': ''
    }

    config_paths = ['config.json', 'config.json.example']
    for config_path in config_paths:
        if os.path.exists(config_path):
            with open(config_path) as config_file:
                loaded_config = json.load(config_file)
            default_config.update(loaded_config)
            return default_config

    return default_config

config = load_config()

PLEX_URL = config.get('PLEX_URL', '')
PLEX_TOKEN = config.get('PLEX_TOKEN', '')
PLEX_USERNAME = config.get('PLEX_USERNAME', '')
PLEX_PASSWORD = config.get('PLEX_PASSWORD', '')
FRIEND_SERVER_NAME = config.get('FRIEND_SERVER_NAME', '')
CSV_FILE_FILMS = 'unwatched_movies.csv'
CSV_FILE_SERIES = 'unwatched_series.csv'
CSV_FILE_COMMON_MOVIES = 'common_movies.csv'
CSV_FILE_COMMON_SERIES = 'common_series.csv'
SQLITE_DB_FILE = 'cleanmyplex.sqlite3'
VALID_CSV_FILES = [CSV_FILE_FILMS, CSV_FILE_SERIES, CSV_FILE_COMMON_MOVIES, CSV_FILE_COMMON_SERIES]
HIDDEN_CSV_COLUMNS = ['poster_url', 'summary', 'genres', 'directors', 'actors']

plex = None
account = None
pd = None
connection_status = {
    'plex_error': None,
    'account_error': None,
    'account_configured': bool(PLEX_TOKEN or (PLEX_USERNAME and PLEX_PASSWORD)),
    'refreshing': False,
}
connection_lock = threading.Lock()

# Variables globales pour suivre les tâches en arrière-plan
tasks = {}
tasks_lock = threading.Lock()

# Assurez-vous que le répertoire de cache des affiches existe
if not os.path.exists('static/poster_cache'):
    os.makedirs('static/poster_cache')


def get_pandas():
    global pd
    if pd is None:
        import pandas as pandas
        pd = pandas
    return pd


def is_missing_value(value):
    return value is None or value != value


def get_db_connection():
    conn = sqlite3.connect(SQLITE_DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_sqlite_store():
    with get_db_connection() as conn:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA synchronous=NORMAL')
        conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS csv_datasets (
                csv_file TEXT PRIMARY KEY,
                columns_json TEXT NOT NULL,
                source_mtime REAL NOT NULL,
                row_count INTEGER NOT NULL DEFAULT 0,
                imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            '''
        )
        conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS csv_rows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                csv_file TEXT NOT NULL,
                row_index INTEGER NOT NULL,
                data_json TEXT NOT NULL,
                title TEXT,
                rating_key TEXT,
                action TEXT,
                library TEXT,
                local_path TEXT,
                added_at TEXT,
                release_date TEXT,
                rating REAL,
                plex_rating REAL,
                view_count REAL,
                file_size REAL,
                local_file_size REAL,
                remote_file_size REAL,
                largest_file_size REAL,
                UNIQUE(csv_file, row_index)
            )
            '''
        )
        conn.execute('CREATE INDEX IF NOT EXISTS idx_csv_rows_file ON csv_rows(csv_file)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_csv_rows_title ON csv_rows(csv_file, title)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_csv_rows_action ON csv_rows(csv_file, action)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_csv_rows_library ON csv_rows(csv_file, library)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_csv_rows_local_path ON csv_rows(csv_file, local_path)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_csv_rows_added_at ON csv_rows(csv_file, added_at)')


def parse_size_gb(value):
    if is_missing_value(value):
        return None
    match = re.search(r'-?\d+(?:[.,]\d+)?', str(value))
    if not match:
        return None
    return float(match.group(0).replace(',', '.'))


def parse_float_value(value):
    if is_missing_value(value):
        return None
    try:
        return float(str(value).replace(',', '.').replace(' Go', '').strip())
    except ValueError:
        return None


def normalize_csv_cell(value):
    if is_missing_value(value):
        return 'N/A'
    if hasattr(value, 'strftime'):
        return value.strftime('%Y-%m-%d')
    text = str(value)
    return 'N/A' if text == 'nan' else text


def load_csv_dataframe(csv_file):
    pd = get_pandas()
    df = pd.read_csv(csv_file)
    df = df.fillna('N/A')
    if 'ratingKey' not in df.columns:
        df['ratingKey'] = 'N/A'
    return df


def import_csv_to_sqlite(csv_file, force=False):
    if csv_file not in VALID_CSV_FILES or not os.path.exists(csv_file):
        return None

    source_mtime = os.path.getmtime(csv_file)
    with get_db_connection() as conn:
        dataset = conn.execute(
            'SELECT source_mtime, columns_json, row_count FROM csv_datasets WHERE csv_file = ?',
            (csv_file,)
        ).fetchone()
        if dataset and not force and float(dataset['source_mtime']) == float(source_mtime):
            return {
                'columns': json.loads(dataset['columns_json']),
                'row_count': dataset['row_count'],
                'source_mtime': dataset['source_mtime']
            }

    df = load_csv_dataframe(csv_file)
    columns = [str(column) for column in df.columns]
    rows = []
    for row_index, row in df.iterrows():
        data = {column: normalize_csv_cell(row.get(column, 'N/A')) for column in columns}
        rows.append((
            csv_file,
            int(row_index),
            json.dumps(data, ensure_ascii=False),
            data.get('title'),
            data.get('ratingKey'),
            data.get('Action', ''),
            data.get('Bibliothèque'),
            data.get('local_path'),
            data.get('added_at'),
            data.get('release_date'),
            parse_float_value(data.get('rating')),
            parse_float_value(data.get('plex_rating')),
            parse_float_value(data.get('view_count')),
            parse_size_gb(data.get('file_size')),
            parse_size_gb(data.get('local_file_size')),
            parse_size_gb(data.get('remote_file_size')),
            parse_size_gb(data.get('largest_file_size')),
        ))

    with get_db_connection() as conn:
        conn.execute('DELETE FROM csv_rows WHERE csv_file = ?', (csv_file,))
        conn.executemany(
            '''
            INSERT INTO csv_rows (
                csv_file, row_index, data_json, title, rating_key, action, library, local_path,
                added_at, release_date, rating, plex_rating, view_count, file_size,
                local_file_size, remote_file_size, largest_file_size
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            rows
        )
        conn.execute(
            '''
            INSERT INTO csv_datasets (csv_file, columns_json, source_mtime, row_count, imported_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(csv_file) DO UPDATE SET
                columns_json = excluded.columns_json,
                source_mtime = excluded.source_mtime,
                row_count = excluded.row_count,
                imported_at = CURRENT_TIMESTAMP
            ''',
            (csv_file, json.dumps(columns, ensure_ascii=False), source_mtime, len(rows))
        )

    return {'columns': columns, 'row_count': len(rows), 'source_mtime': source_mtime}


def get_sqlite_dataset(csv_file):
    imported = import_csv_to_sqlite(csv_file)
    if imported is None:
        return None
    return imported


def export_sqlite_to_csv(csv_file):
    pd = get_pandas()
    dataset = get_sqlite_dataset(csv_file)
    if dataset is None:
        return False
    columns = dataset['columns']
    with get_db_connection() as conn:
        rows = conn.execute(
            'SELECT data_json FROM csv_rows WHERE csv_file = ? ORDER BY row_index ASC',
            (csv_file,)
        ).fetchall()
    data = [json.loads(row['data_json']) for row in rows]
    df = pd.DataFrame(data, columns=columns)
    df.to_csv(csv_file, index=False)
    import_csv_to_sqlite(csv_file, force=True)
    return True


SQL_COLUMN_BY_CSV_COLUMN = {
    'title': 'title',
    'ratingKey': 'rating_key',
    'rating': 'rating',
    'plex_rating': 'plex_rating',
    'view_count': 'view_count',
    'local_path': 'local_path',
    'added_at': 'added_at',
    'release_date': 'release_date',
    'file_size': 'file_size',
    'local_file_size': 'local_file_size',
    'remote_file_size': 'remote_file_size',
    'largest_file_size': 'largest_file_size',
    'Bibliothèque': 'library',
    'Action': 'action',
}

NUMERIC_FILTER_COLUMNS = {
    'rating', 'plex_rating', 'view_count', 'file_size',
    'local_file_size', 'remote_file_size', 'largest_file_size',
    'number_of_local_episodes', 'number_of_remote_episodes'
}
DATE_FILTER_COLUMNS = {'added_at', 'release_date'}
EXACT_FILTER_COLUMNS = {'Bibliothèque', 'Action'}


def get_distinct_csv_values(csv_file, sql_column):
    with get_db_connection() as conn:
        rows = conn.execute(
            f'''
            SELECT DISTINCT {sql_column} AS value
            FROM csv_rows
            WHERE csv_file = ? AND {sql_column} IS NOT NULL AND {sql_column} != ''
            ORDER BY {sql_column} COLLATE NOCASE
            ''',
            (csv_file,)
        ).fetchall()
    return [row['value'] for row in rows]


def data_json_value_expr(column_name):
    return "json_extract(data_json, '$.' || ?)", [column_name]


def add_column_filter(where_clauses, params, column_name, raw_value):
    value = (raw_value or '').strip()
    if not value:
        return

    sql_column = SQL_COLUMN_BY_CSV_COLUMN.get(column_name)
    if sql_column:
        expr = sql_column
        expr_params = []
    else:
        expr, expr_params = data_json_value_expr(column_name)

    operator_match = re.match(r'^(<=|>=|=|<|>)(.*)$', value)
    if operator_match:
        operator = operator_match.group(1)
        filter_value = operator_match.group(2).strip()
        if column_name in DATE_FILTER_COLUMNS:
            where_clauses.append(f'{expr} {operator} ?')
            params.extend(expr_params)
            params.append(filter_value)
            return

        if column_name in NUMERIC_FILTER_COLUMNS or sql_column in {
            'rating', 'plex_rating', 'view_count', 'file_size',
            'local_file_size', 'remote_file_size', 'largest_file_size'
        }:
            where_clauses.append(f'COALESCE({expr}, 0) {operator} ?')
            params.extend(expr_params)
            params.append(parse_float_value(filter_value) or 0)
            return

    if column_name in EXACT_FILTER_COLUMNS:
        where_clauses.append(f'COALESCE(CAST({expr} AS TEXT), "") = ?')
        params.extend(expr_params)
        params.append(value)
        return

    where_clauses.append(f'LOWER(COALESCE(CAST({expr} AS TEXT), "")) LIKE ?')
    params.extend(expr_params)
    params.append(f'%{value.lower()}%')


def build_csv_where_clause(csv_file, visible_columns, request_args):
    where_clauses = ['csv_file = ?']
    params = [csv_file]

    global_search = request_args.get('search[value]', '').strip()
    if global_search:
        search_clauses = []
        for column_name in visible_columns:
            sql_column = SQL_COLUMN_BY_CSV_COLUMN.get(column_name)
            if sql_column:
                search_clauses.append(f'LOWER(COALESCE(CAST({sql_column} AS TEXT), "")) LIKE ?')
                params.append(f'%{global_search.lower()}%')
            else:
                search_clauses.append('LOWER(COALESCE(CAST(json_extract(data_json, ?) AS TEXT), "")) LIKE ?')
                params.extend((f'$.{column_name}', f'%{global_search.lower()}%'))
        if search_clauses:
            where_clauses.append(f"({' OR '.join(search_clauses)})")

    for column_name in visible_columns:
        add_column_filter(
            where_clauses,
            params,
            column_name,
            request_args.get(f'column_filter_{column_name}', '')
        )

    return ' AND '.join(where_clauses), params


def get_order_clause(visible_columns, request_args):
    order_column_index = request_args.get('order[0][column]')
    order_direction = request_args.get('order[0][dir]', 'asc')
    direction = 'DESC' if order_direction == 'desc' else 'ASC'

    try:
        column_index = int(order_column_index)
    except (TypeError, ValueError, IndexError):
        return 'row_index ASC'

    column_name = request_args.get(f'columns[{column_index}][name]', '')
    if not column_name or column_name in ['select', 'miniature']:
        return 'row_index ASC'
    if column_name not in visible_columns:
        return 'row_index ASC'

    sql_column = SQL_COLUMN_BY_CSV_COLUMN.get(column_name)
    if sql_column:
        return f'{sql_column} {direction}, row_index ASC'

    if not re.match(r'^[A-Za-z0-9_]+$', column_name):
        return 'row_index ASC'

    return f"json_extract(data_json, '$.{column_name}') COLLATE NOCASE {direction}, row_index ASC"


def update_csv_actions_from_request(csv_file, form_data):
    action_updates = []
    for key, value in form_data.items():
        if not key.startswith('action_') or value not in ['A', 'D']:
            continue
        try:
            row_id = int(key.replace('action_', '', 1))
        except ValueError:
            continue
        action_updates.append((row_id, value))

    if not action_updates:
        return 0

    with get_db_connection() as conn:
        for row_id, action in action_updates:
            row = conn.execute(
                'SELECT data_json FROM csv_rows WHERE id = ? AND csv_file = ?',
                (row_id, csv_file)
            ).fetchone()
            if not row:
                continue
            data = json.loads(row['data_json'])
            data['Action'] = action
            conn.execute(
                'UPDATE csv_rows SET data_json = ?, action = ? WHERE id = ? AND csv_file = ?',
                (json.dumps(data, ensure_ascii=False), action, row_id, csv_file)
            )

    export_sqlite_to_csv(csv_file)
    return len(action_updates)


def render_csv_cell(row_id, data, column_name):
    value = normalize_csv_cell(data.get(column_name, 'N/A'))
    safe_value = escape(value)

    if column_name == '__select__':
        return f'<input type="checkbox" class="row-select" name="selected_rows" value="{row_id}">'

    if column_name == '__poster__':
        poster_url = data.get('poster_url', 'N/A')
        if poster_url and poster_url != 'N/A':
            return (
                f'<img src="/static/placeholder.jpg" data-actualsrc="{escape(str(poster_url), quote=True)}" '
                'alt="Affiche" class="lazy-image">'
            )
        return '<img src="/static/no_image_available.jpg" alt="Pas d image">'

    if column_name == 'title':
        attrs = {
            'poster-url': data.get('poster_url', 'N/A'),
            'summary': data.get('summary', 'N/A'),
            'release-date': data.get('release_date', 'N/A'),
            'rating': data.get('rating', 'N/A'),
            'plex-rating': data.get('plex_rating', 'N/A'),
            'view-count': data.get('view_count', 'N/A'),
            'genres': data.get('genres', 'N/A'),
            'directors': data.get('directors', 'N/A'),
            'actors': data.get('actors', 'N/A'),
        }
        data_attrs = ' '.join(
            f'data-{key}="{escape(str(attr_value), quote=True)}"'
            for key, attr_value in attrs.items()
        )
        return f'<a href="#" class="item-title" {data_attrs}>{safe_value}</a>'

    return safe_value


@app.route('/api/csv/<path:csv_file>/rows')
def csv_rows_api(csv_file):
    dataset = get_sqlite_dataset(csv_file)
    if dataset is None:
        return jsonify({'draw': int(request.args.get('draw', 0)), 'recordsTotal': 0, 'recordsFiltered': 0, 'data': []}), 404

    columns = dataset['columns']
    visible_columns = [column for column in columns if column not in HIDDEN_CSV_COLUMNS]
    draw = int(request.args.get('draw', 0))
    start = max(int(request.args.get('start', 0)), 0)
    length = int(request.args.get('length', 30))
    if length < 0:
        length = 500
    length = min(max(length, 1), 500)

    where_clause, params = build_csv_where_clause(csv_file, visible_columns, request.args)
    order_clause = get_order_clause(visible_columns, request.args)

    with get_db_connection() as conn:
        records_total = conn.execute(
            'SELECT COUNT(*) AS count FROM csv_rows WHERE csv_file = ?',
            (csv_file,)
        ).fetchone()['count']
        records_filtered = conn.execute(
            f'SELECT COUNT(*) AS count FROM csv_rows WHERE {where_clause}',
            params
        ).fetchone()['count']
        rows = conn.execute(
            f'''
            SELECT id, data_json
            FROM csv_rows
            WHERE {where_clause}
            ORDER BY {order_clause}
            LIMIT ? OFFSET ?
            ''',
            params + [length, start]
        ).fetchall()

    data_rows = []
    for row in rows:
        row_data = json.loads(row['data_json'])
        rendered = [
            render_csv_cell(row['id'], row_data, '__select__'),
            render_csv_cell(row['id'], row_data, '__poster__'),
        ]
        rendered.extend(render_csv_cell(row['id'], row_data, column) for column in visible_columns)
        data_rows.append(rendered)

    return jsonify({
        'draw': draw,
        'recordsTotal': records_total,
        'recordsFiltered': records_filtered,
        'data': data_rows
    })


init_sqlite_store()


def connect_to_plex(plex_url, plex_token):
    if not plex_url or not plex_token:
        return None, 'URL ou token Plex manquant.'

    try:
        plex_server = PlexServer(plex_url, plex_token, timeout=10)
        plex_server.library.sections()
        return plex_server, None
    except Exception as e:
        app.logger.warning("Connexion au serveur Plex impossible: %s", e)
        return None, str(e)


def connect_to_account(username='', password='', token=''):
    if token:
        try:
            plex_account = MyPlexAccount(token=token)
            plex_account.resources()
            return plex_account, None
        except Exception as e:
            app.logger.warning("Connexion au compte Plex par token impossible: %s", e)
            return None, str(e)

    if not username and not password:
        return None, None

    if not username or not password:
        return None, "Le nom d'utilisateur et le mot de passe Plex doivent être renseignés ensemble."

    try:
        plex_account = MyPlexAccount(username, password)
        plex_account.resources()
        return plex_account, None
    except Exception as e:
        app.logger.warning("Connexion au compte Plex impossible: %s", e)
        return None, str(e)


def refresh_connections():
    global plex, account

    with connection_lock:
        connection_status['refreshing'] = True

    next_plex, plex_error = connect_to_plex(PLEX_URL, PLEX_TOKEN)
    next_account, account_error = connect_to_account(PLEX_USERNAME, PLEX_PASSWORD, PLEX_TOKEN)

    with connection_lock:
        plex = next_plex
        account = next_account
        connection_status['plex_error'] = plex_error
        connection_status['account_error'] = account_error
        connection_status['account_configured'] = bool(PLEX_TOKEN or (PLEX_USERNAME and PLEX_PASSWORD))
        connection_status['refreshing'] = False


def refresh_connections_async():
    threading.Thread(target=refresh_connections, daemon=True).start()


def ensure_required_connections(require_plex=False, require_account=False):
    errors = []

    if require_plex and plex is None:
        if connection_status['refreshing']:
            errors.append("Connexion au serveur Plex en cours d'initialisation.")
        else:
            errors.append(f"Connexion au serveur Plex indisponible : {connection_status['plex_error']}")

    if require_account and account is None:
        if connection_status['refreshing']:
            errors.append("Connexion au compte Plex en cours d'initialisation.")
        elif connection_status['account_configured']:
            errors.append(f"Connexion au compte Plex indisponible : {connection_status['account_error']}")
        else:
            errors.append("Cette fonctionnalité nécessite un token Plex ou les identifiants Plex.")

    if errors:
        for error_message in errors:
            flash(f"{error_message} Rendez-vous dans les paramètres pour corriger la configuration.", 'warning')
        return False

    return True


def get_friend_server_names():
    if account is None:
        return []

    try:
        return [resource.name for resource in account.resources()]
    except Exception as e:
        app.logger.warning("Impossible de récupérer la liste des serveurs amis: %s", e)
        return []


def probe_plex_server():
    if not PLEX_URL or not PLEX_TOKEN:
        return False, 'URL ou token Plex manquant.'

    try:
        response = requests.get(
            urljoin(PLEX_URL.rstrip('/') + '/', 'identity'),
            headers={'X-Plex-Token': PLEX_TOKEN},
            timeout=2
        )
        response.raise_for_status()
        return True, None
    except Exception as e:
        return False, str(e)


@app.route('/api/plex_status')
def plex_status_api():
    if connection_status['refreshing']:
        return jsonify({'status': 'checking', 'label': 'Connexion Plex...'})

    is_available, error_message = probe_plex_server()
    if is_available:
        return jsonify({'status': 'ok', 'label': 'Plex connecté'})

    return jsonify({'status': 'down', 'label': 'Plex hors ligne', 'error': error_message})


@app.context_processor
def inject_connection_status():
    return {
        'connection_status': {
            'plex_error': connection_status['plex_error'],
            'account_error': connection_status['account_error'],
            'account_configured': connection_status['account_configured'],
            'refreshing': connection_status['refreshing'],
            'has_issues': bool(connection_status['plex_error'] or connection_status['account_error'])
        }
    }


refresh_connections_async()

def get_active_sessions():
    try:
        sessions = plex.sessions()
        session_data = []

        for session in sessions:
            last_active = session.startedAt.strftime('%Y-%m-%d %H:%M:%S') if hasattr(session, 'startedAt') and session.startedAt else 'Inconnu'
            player = session.players[0] if session.players else None
            session_info = {
                'username': session.usernames[0] if session.usernames else 'Inconnu',
                'publicAddress': getattr(player, 'address', 'N/A') if player else 'N/A',
                'last_active': last_active,
                'media_title': getattr(session, 'title', 'N/A'),
                'media_type': getattr(session, 'type', 'N/A'),
                'grandparent_title': getattr(session, 'grandparentTitle', 'N/A'),
                'parent_title': getattr(session, 'parentTitle', 'N/A'),
                'year': getattr(session, 'year', 'N/A'),
                'player_title': getattr(player, 'title', 'N/A') if player else 'N/A',
                'player_product': getattr(player, 'product', 'N/A') if player else 'N/A',
                'player_platform': getattr(player, 'platform', 'N/A') if player else 'N/A',
                'player_state': getattr(player, 'state', 'N/A') if player else 'N/A',
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


def safe_user_attr(user, *names, default='N/A'):
    for name in names:
        if hasattr(user, name):
            value = getattr(user, name)
            if value not in [None, '']:
                return value
    return default


def display_value(value):
    if value in [None, '']:
        return 'N/A'
    if isinstance(value, (list, tuple, set)):
        return ', '.join(str(item) for item in value) if value else 'N/A'
    if isinstance(value, dict):
        return ', '.join(f'{key}: {val}' for key, val in value.items()) if value else 'N/A'
    return str(value)


def format_datetime(value):
    if hasattr(value, 'strftime'):
        return value.strftime('%Y-%m-%d %H:%M:%S')
    return display_value(value)


def history_item_matches_user(item, user):
    account_id = str(safe_user_attr(user, 'id', default='')).strip()
    username = str(safe_user_attr(user, 'username', default='')).strip()
    item_account_id = str(getattr(item, 'accountID', '') or getattr(item, 'accountId', '')).strip()
    item_username = str(getattr(item, 'username', '') or getattr(item, 'user', '')).strip()

    if account_id and item_account_id:
        return item_account_id == account_id
    if username and item_username:
        return item_username == username
    return False


def build_history_title(item):
    title = display_value(getattr(item, 'title', 'N/A'))
    grandparent_title = display_value(getattr(item, 'grandparentTitle', 'N/A'))
    parent_title = display_value(getattr(item, 'parentTitle', 'N/A'))

    if grandparent_title != 'N/A':
        parts = [grandparent_title]
        if parent_title != 'N/A':
            parts.append(parent_title)
        if title != 'N/A':
            parts.append(title)
        return ' - '.join(parts)

    return title


def fetch_full_media_from_history(item):
    rating_key = getattr(item, 'ratingKey', None)
    if not rating_key:
        return None

    try:
        return plex.fetchItem(rating_key)
    except Exception as e:
        app.logger.warning("Impossible de récupérer le média complet Plex %s: %s", rating_key, e)
        return None


def get_user_last_played(user):
    account_id = str(safe_user_attr(user, 'id', default='')).strip()

    try:
        try:
            history = plex.history(maxresults=200, accountID=account_id) if account_id else plex.history(maxresults=200)
            history_is_user_scoped = bool(account_id)
        except TypeError:
            history = plex.history(maxresults=200)
            history_is_user_scoped = False

        latest_item = None
        latest_viewed_at = None
        for item in history:
            if not history_is_user_scoped and not history_item_matches_user(item, user):
                continue

            viewed_at = getattr(item, 'viewedAt', None)
            if latest_item is None or (viewed_at and (latest_viewed_at is None or viewed_at > latest_viewed_at)):
                latest_item = item
                latest_viewed_at = viewed_at

        if latest_item is None:
            return None

        full_media = fetch_full_media_from_history(latest_item)
        media_for_details = full_media or latest_item

        return {
            'title': build_history_title(latest_item),
            'type': display_value(getattr(media_for_details, 'type', getattr(latest_item, 'type', 'N/A'))),
            'year': display_value(getattr(media_for_details, 'year', getattr(latest_item, 'year', 'N/A'))),
            'library': display_value(getattr(media_for_details, 'librarySectionTitle', getattr(latest_item, 'librarySectionTitle', 'N/A'))),
            'viewed_at': format_datetime(latest_viewed_at),
        }
    except Exception as e:
        app.logger.warning("Impossible de récupérer le dernier média lu pour %s: %s", user.username, e)
        return None


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
    pd = get_pandas()
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

                # Gestion de l'affiche
                if hasattr(local_item, 'thumb'):
                    poster_filename = f"poster_{local_item.ratingKey}.jpg"
                    poster_filepath = os.path.join('static', 'poster_cache', poster_filename)
                    poster_url = f"/static/poster_cache/{poster_filename}"

                    # Télécharger et stocker l'image si elle n'existe pas
                    if not os.path.exists(poster_filepath):
                        poster_url_full = plex.url(local_item.thumb)
                        headers = {'X-Plex-Token': PLEX_TOKEN}
                        response = requests.get(poster_url_full, headers=headers, stream=True)
                        if response.status_code == 200:
                            with open(poster_filepath, 'wb') as f:
                                for chunk in response.iter_content(1024):
                                    f.write(chunk)
                        else:
                            poster_url = '/static/no_image_available.jpg'
                else:
                    poster_url = '/static/no_image_available.jpg'

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
    pd = get_pandas()
    print(f"generate_csv appelé avec library_names={library_names}, csv_file='{csv_file}', media_type='{media_type}'")
    if os.path.exists(csv_file):
        print(f"Le fichier CSV '{csv_file}' existe déjà. Chargement des données existantes.")
        existing_df = pd.read_csv(csv_file, encoding='utf-8', delimiter=',', quotechar='"')
        existing_df['file_size'] = existing_df['file_size'].replace('N/A', '0')
        existing_df['file_size'] = existing_df['file_size'].str.replace(' Go', '', regex=False).astype(float)
    else:
        print(f"Le fichier CSV '{csv_file}' n'existe pas. Création d'un nouveau DataFrame.")
        columns = [
            'title', 'ratingKey', 'rating', 'plex_rating', 'view_count', 'local_path',
            'added_at', 'release_date', 'file_size', 'Bibliothèque', 'Action',
            'poster_url', 'summary', 'genres', 'directors', 'actors'
        ]
        existing_df = pd.DataFrame(columns=columns)

    # Déterminer les bibliothèques à traiter
    if "ALL" in library_names:
        if media_type == 'movie':
            libraries = [section.title for section in plex.library.sections() if section.type == 'movie']
        elif media_type == 'show':
            libraries = [section.title for section in plex.library.sections() if section.type == 'show']
        else:
            libraries = []
    else:
        libraries = library_names

    new_items = []
    total_libraries = len(libraries)
    for lib_idx, library_name in enumerate(libraries):
        print(f"Traitement de la bibliothèque {lib_idx + 1}/{total_libraries} : '{library_name}'")
        try:
            library = plex.library.section(library_name)
        except Exception as e:
            flash(f"Erreur lors de l'accès à la bibliothèque '{library_name}': {e}", 'danger')
            print(f"Erreur lors de l'accès à la bibliothèque '{library_name}': {e}")
            continue

        try:
            all_items = library.all()
        except Exception as e:
            flash(f"Erreur lors de la récupération des éléments de la bibliothèque '{library_name}': {e}", 'danger')
            print(f"Erreur lors de la récupération des éléments de la bibliothèque '{library_name}': {e}")
            continue

        total_items = len(all_items)
        for idx, item in enumerate(all_items):
            print(f"Traitement de l'élément {idx + 1}/{total_items} : '{item.title}'")
            try:
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

                # Gestion de l'affiche
                if hasattr(item, 'thumb'):
                    poster_filename = f"poster_{item.ratingKey}.jpg"
                    poster_filepath = os.path.join('static', 'poster_cache', poster_filename)
                    poster_url = f"/static/poster_cache/{poster_filename}"

                    # Télécharger et stocker l'image si elle n'existe pas
                    if not os.path.exists(poster_filepath):
                        poster_url_full = plex.url(item.thumb)
                        headers = {'X-Plex-Token': PLEX_TOKEN}
                        try:
                            response = requests.get(poster_url_full, headers=headers, stream=True, timeout=10)
                            if response.status_code == 200:
                                with open(poster_filepath, 'wb') as f:
                                    for chunk in response.iter_content(1024):
                                        f.write(chunk)
                            else:
                                print(f"Erreur lors du téléchargement de l'image pour '{item.title}': {response.status_code}")
                                poster_url = '/static/no_image_available.jpg'
                        except Exception as e:
                            print(f"Exception lors du téléchargement de l'image pour '{item.title}': {e}")
                            poster_url = '/static/no_image_available.jpg'
                else:
                    poster_url = '/static/no_image_available.jpg'

                if item.TYPE == 'movie':
                    view_count = item.viewCount if hasattr(item, 'viewCount') else 0
                    local_path = item.media[0].parts[0].file if item.media and item.media[0].parts else 'N/A'
                    file_size_gb = sum(media_part.size for media in item.media for media_part in media.parts) / (1024 ** 3)
                elif item.TYPE == 'show':
                    view_count = sum(
                        episode.viewCount for episode in item.episodes() if hasattr(episode, 'viewCount')
                    )
                    local_path = 'N/A'
                    file_size_gb = sum(
                        media_part.size
                        for episode in item.episodes()
                        for media in episode.media
                        for media_part in media.parts
                    ) / (1024 ** 3)
                else:
                    view_count = 0
                    local_path = 'N/A'
                    file_size_gb = 0.0

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

            except Exception as e:
                print(f"Erreur lors du traitement de l'élément '{item.title}': {e}")
                continue

    new_df = pd.DataFrame(new_items)
    print("Tous les éléments ont été traités. Création du DataFrame.")

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
    print(f"Écriture du DataFrame dans le fichier CSV '{csv_file}'.")
    combined_df.to_csv(csv_file, index=False)
    import_csv_to_sqlite(csv_file, force=True)
    print(f"CSV '{csv_file}' généré avec succès.")
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
        pd = get_pandas()
        if not os.path.exists(csv_file):
            with tasks_lock:
                tasks[task_id]['status'] = 'failed'
                tasks[task_id]['message'] = f"Le fichier CSV {csv_file} n'existe pas."
            return

        df = pd.read_csv(csv_file)
        items_to_delete = df[df['Action'] == 'D']

        total_items = len(items_to_delete)
        deleted_items = 0
        already_absent_items = 0

        for index, row in items_to_delete.iterrows():
            try:
                rating_key = row.get('ratingKey')
                if not is_missing_value(rating_key):
                    try:
                        item = plex.fetchItem(int(rating_key))
                    except Exception as fetch_error:
                        local_path = row.get('local_path')
                        path_is_missing = (
                            is_missing_value(local_path)
                            or str(local_path).strip() in ['', 'N/A']
                            or not os.path.exists(str(local_path))
                        )

                        if path_is_missing:
                            df.drop(index, inplace=True)
                            already_absent_items += 1
                            with tasks_lock:
                                tasks[task_id]['progress'] = (
                                    f"{deleted_items} supprimés, {already_absent_items} déjà absents "
                                    f"sur {total_items} éléments."
                                )
                            continue

                        with tasks_lock:
                            tasks[task_id]['errors'].append(
                                f"{row['title']} introuvable dans Plex avec ratingKey {rating_key}, "
                                f"mais le fichier existe encore ({local_path}) : {fetch_error}"
                            )
                        continue

                    item.delete()
                    df.drop(index, inplace=True)
                    deleted_items += 1

                    with tasks_lock:
                        tasks[task_id]['progress'] = (
                            f"{deleted_items} supprimés, {already_absent_items} déjà absents "
                            f"sur {total_items} éléments."
                        )
                else:
                    with tasks_lock:
                        tasks[task_id]['errors'].append(f"Clé de notation invalide pour {row['title']}.")
            except Exception as e:
                with tasks_lock:
                    tasks[task_id]['errors'].append(f"Erreur lors de la suppression de {row['title']}: {e}")

        df.to_csv(csv_file, index=False)
        import_csv_to_sqlite(csv_file, force=True)

        with tasks_lock:
            if tasks[task_id]['errors']:
                tasks[task_id]['status'] = 'completed_with_errors'
                tasks[task_id]['message'] = (
                    f"Suppression terminée avec des erreurs. {deleted_items} supprimés, "
                    f"{already_absent_items} déjà absents sur {total_items} éléments."
                )
            else:
                tasks[task_id]['status'] = 'completed'
                tasks[task_id]['message'] = (
                    f"Suppression terminée avec succès. {deleted_items} supprimés, "
                    f"{already_absent_items} déjà absents."
                )

    except Exception as e:
        with tasks_lock:
            tasks[task_id]['status'] = 'failed'
            tasks[task_id]['message'] = f"Erreur lors de la suppression : {e}"

def compare_libraries_thread(local_library_names, friend_library_names, media_type, task_id):
    try:
        df, output_file, num_items, total_space_saved_gb = compare_libraries(local_library_names, friend_library_names, media_type)
        import_csv_to_sqlite(output_file, force=True)
        with tasks_lock:
            tasks[task_id]['status'] = 'completed'
            tasks[task_id]['message'] = f"CSV {output_file} généré avec succès."
    except Exception as e:
        with tasks_lock:
            tasks[task_id]['status'] = 'failed'
            tasks[task_id]['message'] = f"Erreur lors de la comparaison des bibliothèques : {e}"

@app.route('/test_token', methods=['POST'])
def test_token():
    plex_url = request.form.get('PLEX_URL', config.get('PLEX_URL', ''))
    test_token = request.form['PLEX_TOKEN']
    _, server_error = connect_to_plex(plex_url, test_token)
    _, account_error = connect_to_account(token=test_token)

    if server_error or account_error:
        errors = []
        if server_error:
            errors.append(f'Serveur Plex : {server_error}')
        if account_error:
            errors.append(f'Compte Plex : {account_error}')
        return jsonify({'status': 'error', 'message': 'Erreur : ' + ' | '.join(errors)})

    return jsonify({'status': 'success', 'message': 'Connexion serveur et compte réussie avec ce token !'})

@app.route('/test_login', methods=['POST'])
def test_login():
    plex_username = request.form['PLEX_USERNAME']
    plex_password = request.form['PLEX_PASSWORD']
    _, error_message = connect_to_account(plex_username, plex_password)

    if error_message:
        return jsonify({'status': 'error', 'message': f'Erreur : {error_message}'})

    return jsonify({'status': 'success', 'message': 'Connexion réussie avec ces identifiants !'})

@app.route('/manage_users')
def manage_users():
    if not ensure_required_connections(require_plex=True, require_account=True):
        return redirect(url_for('settings'))

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
    if not ensure_required_connections(require_plex=True, require_account=True):
        return redirect(url_for('settings'))

    try:
        user = next((u for u in account.users() if u.username == username), None)
        if not user:
            flash(f"Utilisateur {username} non trouvé", 'danger')
            return redirect(url_for('manage_users'))

        sessions = get_active_sessions()
        session_info = next((session for session in sessions if session['username'] == user.username), None)
        last_played = get_user_last_played(user)

        user_info = {
            'username': user.username,
            'email': safe_user_attr(user, 'email'),
            'title': safe_user_attr(user, 'title'),
            'userID': safe_user_attr(user, 'id'),
            'uuid': safe_user_attr(user, 'uuid'),
            'homeUser': 'Oui' if safe_user_attr(user, 'home', default=False) else 'Non',
            'restricted': 'Oui' if safe_user_attr(user, 'restricted', default=False) else 'Non',
            'allowSync': 'Oui' if safe_user_attr(user, 'allowSync', default=False) else 'Non',
            'allowChannels': 'Oui' if safe_user_attr(user, 'allowChannels', default=False) else 'Non',
            'allowCameraUpload': 'Oui' if safe_user_attr(user, 'allowCameraUpload', default=False) else 'Non',
            'filterAll': display_value(safe_user_attr(user, 'filterAll')),
            'filterMovies': display_value(safe_user_attr(user, 'filterMovies')),
            'filterMusic': display_value(safe_user_attr(user, 'filterMusic')),
            'filterPhotos': display_value(safe_user_attr(user, 'filterPhotos')),
            'filterTelevision': display_value(safe_user_attr(user, 'filterTelevision')),
            'publicAddress': session_info['publicAddress'] if session_info else 'N/A',
            'subscriptionType': safe_user_attr(user, 'subscriptionType', default='Gratuit'),
            'is_active': "Oui" if session_info else "Non",
            'session': session_info,
            'last_played': last_played,
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
            with get_db_connection() as conn:
                conn.execute('DELETE FROM csv_rows WHERE csv_file = ?', (csv_file,))
                conn.execute('DELETE FROM csv_datasets WHERE csv_file = ?', (csv_file,))
            flash(f"Fichier {csv_file} supprimé avec succès.", 'success')
        else:
            flash(f"Le fichier {csv_file} n'existe pas.", 'danger')
    else:
        flash(f"Le fichier {csv_file} spécifié est invalide.", 'danger')
    return redirect(url_for('index'))

@app.route('/clean', methods=['GET', 'POST'])
def clean():
    if not ensure_required_connections(require_plex=True):
        return redirect(url_for('settings'))

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
    if not ensure_required_connections(require_plex=True, require_account=True):
        return redirect(url_for('settings'))

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

@app.route('/view_csv/<path:csv_file>', methods=['GET', 'POST'])
def view_csv(csv_file):
    dataset = get_sqlite_dataset(csv_file)
    if dataset is None:
        flash('Le fichier CSV spécifié n\'existe pas.', 'danger')
        return redirect(url_for('index'))

    if request.method == 'POST':
        updated_count = update_csv_actions_from_request(csv_file, request.form)
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({
                'status': 'success',
                'message': 'CSV mis à jour avec succès.',
                'updated': updated_count,
            })
        flash('CSV mis à jour avec succès.', 'success')
        return redirect(url_for('view_csv', csv_file=csv_file))

    columns = dataset['columns']
    unique_libraries = get_distinct_csv_values(csv_file, 'library') if 'Bibliothèque' in columns else []
    unique_actions = get_distinct_csv_values(csv_file, 'action') if 'Action' in columns else []

    return render_template(
        'view_csv.html',
        row_count=dataset['row_count'],
        titles=columns,
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
    export_sqlite_to_csv(csv_file)
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

@app.route('/download/<path:csv_file>')
def download_csv(csv_file):
    export_sqlite_to_csv(csv_file)
    return send_from_directory(directory=os.getcwd(), path=csv_file, as_attachment=True)

@app.route('/settings', methods=['GET', 'POST'])
def settings():
    global PLEX_URL, PLEX_TOKEN, PLEX_USERNAME, PLEX_PASSWORD
    global FRIEND_SERVER_NAME

    if request.method == 'POST':
        plex_url = request.form.get('PLEX_URL', '').strip()
        plex_token = request.form.get('PLEX_TOKEN', '').strip()
        plex_username = request.form.get('PLEX_USERNAME', '').strip()
        plex_password = request.form.get('PLEX_PASSWORD', '').strip()
        friend_server_name = request.form.get('friend_server_name', '').strip()

        if not plex_token and not (plex_username and plex_password):
            flash('Renseignez soit un token Plex, soit un couple username/password.', 'warning')
            return redirect(url_for('settings'))

        config['PLEX_URL'] = plex_url
        config['PLEX_TOKEN'] = plex_token
        config['PLEX_USERNAME'] = plex_username
        config['PLEX_PASSWORD'] = plex_password
        config['FRIEND_SERVER_NAME'] = friend_server_name

        with open('config.json', 'w') as config_file:
            json.dump(config, config_file, indent=4)

        PLEX_URL = config['PLEX_URL']
        PLEX_TOKEN = config['PLEX_TOKEN']
        PLEX_USERNAME = config['PLEX_USERNAME']
        PLEX_PASSWORD = config['PLEX_PASSWORD']
        FRIEND_SERVER_NAME = config['FRIEND_SERVER_NAME']

        refresh_connections()

        if plex is None:
            flash(f"Connexion au serveur Plex invalide : {connection_status['plex_error']}", 'warning')

        if connection_status['account_error']:
            flash(f"Connexion au compte Plex invalide : {connection_status['account_error']}", 'warning')

        if plex is None:
            flash('Paramètres enregistrés, mais la connexion au serveur Plex est encore invalide.', 'warning')
            return redirect(url_for('settings'))

        if connection_status['account_error']:
            flash('Paramètres enregistrés. Les fonctions qui dépendent du compte Plex resteront indisponibles tant que les identifiants ne sont pas corrigés.', 'warning')
            return redirect(url_for('settings'))

        flash('Paramètres mis à jour avec succès.', 'success')
        return redirect(url_for('index'))

    return render_template(
        'settings.html',
        config=config,
        local_movie_libraries=get_library_sections(plex, 'movie') if plex else [],
        local_show_libraries=get_library_sections(plex, 'show') if plex else [],
        friend_server_names=get_friend_server_names()
    )

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)
