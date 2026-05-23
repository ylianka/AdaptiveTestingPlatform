import json
import os
import random
import sqlite3
from datetime import datetime
from functools import wraps

from flask import Flask, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'database.sqlite3')

app = Flask(__name__)
app.config['SECRET_KEY'] = 'change-this-secret-key-for-production'

DIFFICULTIES = ['початковий', 'середній', 'достатній', 'високий']
POINTS_BY_DIFFICULTY = {
    'початковий': 3,
    'середній': 6,
    'достатній': 9,
    'високий': 12,
}
QUESTION_TYPES = [
    'single_choice',
    'multiple_choice',
    'matching',
    'text_input',
    'image_choice',
    'ordering',
    'numeric',
    'true_false',
    'fill_blank',
]
QUESTION_TYPE_LABELS = {
    'single_choice': 'Один варіант відповіді',
    'multiple_choice': 'Кілька варіантів відповіді',
    'matching': 'Встановлення відповідності',
    'text_input': 'Відкрита відповідь',
    'image_choice': 'Вибір за зображенням',
    'ordering': 'Порядок',
    'numeric': 'Числова відповідь',
    'true_false': 'Так / Ні',
    'fill_blank': 'Заповнення пропусків',
}


def normalize_text(value):
    return str(value or '').strip().lower()


def numbers_equal(left, right, tolerance=1e-9):
    try:
        return abs(float(str(left).replace(',', '.')) - float(str(right).replace(',', '.'))) <= tolerance
    except (TypeError, ValueError):
        return False


def insert_question_options(db, question_id, parsed, created_at):
    for item in parsed:
        order_value = item.get('order')
        match_key = item.get('match_key')
        if order_value is not None and match_key in (None, ''):
            match_key = str(order_value)
        db.execute(
            '''
            INSERT INTO question_options
            (question_id, option_text, is_correct, match_key, match_value, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ''',
            (
                question_id,
                item.get('text'),
                1 if item.get('correct', True) else 0,
                match_key,
                item.get('match_value'),
                created_at,
            )
        )


def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exception=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript("""
        PRAGMA foreign_keys = ON;

        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            full_name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('teacher', 'student')),
            class_id INTEGER,
            created_at TEXT NOT NULL,
            FOREIGN KEY (class_id) REFERENCES classes(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS classes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT,
            teacher_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (teacher_id) REFERENCES users(id) ON DELETE CASCADE,
            UNIQUE(name, teacher_id)
        );

        CREATE TABLE IF NOT EXISTS tests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            subject TEXT NOT NULL,
            description TEXT,
            created_by INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (created_by) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            test_id INTEGER NOT NULL,
            topic TEXT NOT NULL,
            prompt TEXT NOT NULL,
            question_type TEXT NOT NULL,
            difficulty TEXT NOT NULL,
            image_url TEXT,
            explanation TEXT,
            points INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (test_id) REFERENCES tests(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS question_options (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            question_id INTEGER NOT NULL,
            option_text TEXT,
            is_correct INTEGER DEFAULT 0,
            match_key TEXT,
            match_value TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (question_id) REFERENCES questions(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            test_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            total_score INTEGER NOT NULL DEFAULT 0,
            current_topic_index INTEGER NOT NULL DEFAULT 0,
            current_difficulty TEXT NOT NULL DEFAULT 'початковий',
            topics_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            completed_at TEXT,
            FOREIGN KEY (student_id) REFERENCES users(id),
            FOREIGN KEY (test_id) REFERENCES tests(id)
        );

        CREATE TABLE IF NOT EXISTS attempt_answers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            attempt_id INTEGER NOT NULL,
            question_id INTEGER NOT NULL,
            raw_answer TEXT,
            is_correct INTEGER NOT NULL,
            score_awarded INTEGER NOT NULL,
            feedback TEXT NOT NULL,
            review_status TEXT NOT NULL DEFAULT 'auto',
            teacher_comment TEXT,
            reviewed_by INTEGER,
            reviewed_at TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (attempt_id) REFERENCES attempts(id) ON DELETE CASCADE,
            FOREIGN KEY (question_id) REFERENCES questions(id)
        );
    """)
    # Міграція для старої бази: додаємо class_id, якщо проєкт уже запускався раніше.
    user_columns = [row[1] for row in db.execute("PRAGMA table_info(users)").fetchall()]
    if 'class_id' not in user_columns:
        db.execute('ALTER TABLE users ADD COLUMN class_id INTEGER')

    answer_columns = [row[1] for row in db.execute("PRAGMA table_info(attempt_answers)").fetchall()]
    if 'review_status' not in answer_columns:
        db.execute("ALTER TABLE attempt_answers ADD COLUMN review_status TEXT NOT NULL DEFAULT 'auto'")
    if 'teacher_comment' not in answer_columns:
        db.execute('ALTER TABLE attempt_answers ADD COLUMN teacher_comment TEXT')
    if 'reviewed_by' not in answer_columns:
        db.execute('ALTER TABLE attempt_answers ADD COLUMN reviewed_by INTEGER')
    if 'reviewed_at' not in answer_columns:
        db.execute('ALTER TABLE attempt_answers ADD COLUMN reviewed_at TEXT')
    db.commit()

    now = datetime.utcnow().isoformat()
    teacher = db.execute("SELECT id FROM users WHERE username = ?", ('teacher',)).fetchone()
    student = db.execute("SELECT id FROM users WHERE username = ?", ('student',)).fetchone()
    if teacher is None:
        db.execute(
            "INSERT INTO users (username, full_name, password_hash, role, created_at) VALUES (?, ?, ?, 'teacher', ?)",
            ('teacher', 'Вчитель Демонстраційний', generate_password_hash('teacher123'), now)
        )
    if student is None:
        db.execute(
            "INSERT INTO users (username, full_name, password_hash, role, created_at) VALUES (?, ?, ?, 'student', ?)",
            ('student', 'Учень Демонстраційний', generate_password_hash('student123'), now)
        )
    db.commit()

    teacher_row = db.execute("SELECT id FROM users WHERE username = ?", ('teacher',)).fetchone()
    student_row = db.execute("SELECT id FROM users WHERE username = ?", ('student',)).fetchone()
    if teacher_row:
        class_row = db.execute("SELECT id FROM classes WHERE teacher_id = ? AND name = ?", (teacher_row[0], '10-А')).fetchone()
        if class_row is None:
            cur = db.execute("INSERT INTO classes (name, description, teacher_id, created_at) VALUES (?, ?, ?, ?)", ('10-А', 'Демонстраційний клас', teacher_row[0], now))
            class_id = cur.lastrowid
        else:
            class_id = class_row[0]
        if student_row:
            db.execute("UPDATE users SET class_id = COALESCE(class_id, ?) WHERE id = ?", (class_id, student_row[0]))
    db.commit()

    has_tests = db.execute('SELECT COUNT(*) FROM tests').fetchone()[0]
    if has_tests == 0:
        seed_demo_data(db)
    db.commit()
    db.close()


def seed_demo_data(db):
    teacher_id = db.execute("SELECT id FROM users WHERE username='teacher'").fetchone()[0]
    now = datetime.utcnow().isoformat()
    cur = db.execute(
        "INSERT INTO tests (title, subject, description, created_by, created_at) VALUES (?, ?, ?, ?, ?)",
        (
            'Адаптивний тест з інформатики',
            'Інформатика',
            'Демонстраційний тест з адаптивною логікою, різними типами завдань та ваговою системою балів.',
            teacher_id,
            now,
        )
    )
    test_id = cur.lastrowid
    questions = [
        {'topic':'Алгоритми','prompt':'Що таке алгоритм?','question_type':'single_choice','difficulty':'початковий','image_url':'','explanation':'Початкове поняття алгоритму.','options':[{'text':'Послідовність дій для розв’язання задачі','correct':1},{'text':'Будь-який текст у комп’ютері','correct':0},{'text':'Тип файлу','correct':0}]},
        {'topic':'Алгоритми','prompt':'Оберіть усі властивості алгоритму.','question_type':'multiple_choice','difficulty':'середній','image_url':'','explanation':'Алгоритм має бути скінченним, визначеним і результативним.','options':[{'text':'Скінченність','correct':1},{'text':'Невизначеність','correct':0},{'text':'Результативність','correct':1},{'text':'Однозначність','correct':1}]},
        {'topic':'Алгоритми','prompt':'Установіть відповідність між терміном і визначенням.','question_type':'matching','difficulty':'достатній','image_url':'','explanation':'Відповідність понять алгоритмізації.','options':[{'match_key':'Лінійний алгоритм','match_value':'Команди виконуються послідовно'},{'match_key':'Розгалуження','match_value':'Вибір дії за умовою'},{'match_key':'Цикл','match_value':'Багаторазове повторення дій'}]},
        {'topic':'Алгоритми','prompt':'Впишіть слово: алгоритм, записаний зрозумілою людині структурованою мовою, називають _____.','question_type':'text_input','difficulty':'високий','image_url':'','explanation':'Очікувана відповідь: псевдокод.','options':[{'text':'псевдокод','correct':1}]},
        {'topic':'Scratch','prompt':'Який блок у Scratch запускає скрипт при натисканні зеленого прапорця?','question_type':'single_choice','difficulty':'початковий','image_url':'','explanation':'Стартовий блок події.','options':[{'text':'коли натиснуто прапорець','correct':1},{'text':'завжди','correct':0},{'text':'повторити 10','correct':0}]},
        {'topic':'Scratch','prompt':'Оберіть блоки керування в Scratch.','question_type':'multiple_choice','difficulty':'середній','image_url':'','explanation':'До керування належать цикли та умови.','options':[{'text':'повторити 10','correct':1},{'text':'якщо то','correct':1},{'text':'перемістити на 10 кроків','correct':0},{'text':'говорити','correct':0}]},
        {'topic':'Scratch','prompt':'Установіть відповідність між блоком і його призначенням.','question_type':'matching','difficulty':'достатній','image_url':'','explanation':'Блоки Scratch та їх роль.','options':[{'match_key':'перемістити на 10 кроків','match_value':'Рух спрайта'},{'match_key':'говорити','match_value':'Вивід репліки'},{'match_key':'якщо то','match_value':'Перевірка умови'}]},
        {'topic':'Scratch','prompt':'Погляньте на зображення та визначте, який блок належить до подій.','question_type':'image_choice','difficulty':'високий','image_url':'https://upload.wikimedia.org/wikipedia/commons/1/1a/Scratch_cat.png','explanation':'Тут використовується питання із зображенням.','options':[{'text':'коли натиснуто прапорець','correct':1},{'text':'сховатись','correct':0},{'text':'змінити розмір на 10','correct':0}]},
        {'topic':'Інтернет','prompt':'Що таке браузер?','question_type':'single_choice','difficulty':'початковий','image_url':'','explanation':'Браузер використовується для перегляду вебсторінок.','options':[{'text':'Програма для перегляду сайтів','correct':1},{'text':'Антивірус','correct':0},{'text':'Мова програмування','correct':0}]},
        {'topic':'Інтернет','prompt':'Оберіть безпечні дії в інтернеті.','question_type':'multiple_choice','difficulty':'середній','image_url':'','explanation':'Безпечна робота в мережі.','options':[{'text':'Не повідомляти пароль стороннім','correct':1},{'text':'Перевіряти адресу сайту','correct':1},{'text':'Завжди відкривати невідомі вкладення','correct':0},{'text':'Використовувати складні паролі','correct':1}]},
        {'topic':'Інтернет','prompt':'Установіть відповідність між терміном і значенням.','question_type':'matching','difficulty':'достатній','image_url':'','explanation':'Основні мережеві поняття.','options':[{'match_key':'URL','match_value':'Адреса ресурсу'},{'match_key':'IP-адреса','match_value':'Числовий мережевий ідентифікатор'},{'match_key':'HTTP','match_value':'Протокол передавання вебданих'}]},
        {'topic':'Інтернет','prompt':'Впишіть англійською скорочення: всесвітня мережа називається _____.','question_type':'text_input','difficulty':'високий','image_url':'','explanation':'Очікувана відповідь: WWW або Web.','options':[{'text':'www','correct':1},{'text':'web','correct':1}]},
    ]
    for item in questions:
        qcur = db.execute(
            "INSERT INTO questions (test_id, topic, prompt, question_type, difficulty, image_url, explanation, points, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (test_id, item['topic'], item['prompt'], item['question_type'], item['difficulty'], item['image_url'], item['explanation'], POINTS_BY_DIFFICULTY[item['difficulty']], now)
        )
        qid = qcur.lastrowid
        insert_question_options(db, qid, item['options'], now)


def login_required(role=None):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if 'user_id' not in session:
                return redirect(url_for('login'))
            if role and session.get('role') != role:
                flash('У вас немає доступу до цієї сторінки.', 'error')
                return redirect(url_for('index'))
            return view(*args, **kwargs)
        return wrapped
    return decorator


def get_current_user():
    if 'user_id' not in session:
        return None
    return get_db().execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()


def next_difficulty(current, was_correct):
    idx = DIFFICULTIES.index(current)
    return DIFFICULTIES[min(idx + 1, len(DIFFICULTIES) - 1)] if was_correct else DIFFICULTIES[max(idx - 1, 0)]


def find_question_for_topic(test_id, topic, target_difficulty, answered_ids, fallback='both'):
    db = get_db()
    def fetch_by_diff(diff):
        sql = "SELECT * FROM questions WHERE test_id = ? AND topic = ? AND difficulty = ?"
        params = [test_id, topic, diff]
        if answered_ids:
            placeholders = ','.join('?' for _ in answered_ids)
            sql += f" AND id NOT IN ({placeholders})"
            params.extend(answered_ids)
        sql += " ORDER BY RANDOM() LIMIT 1"
        return db.execute(sql, params).fetchone()
    direct = fetch_by_diff(target_difficulty)
    if direct:
        return direct
    idx = DIFFICULTIES.index(target_difficulty)
    order = []
    if fallback in ('lower', 'both'):
        order.extend(DIFFICULTIES[:idx][::-1])
    if fallback in ('higher', 'both'):
        order.extend(DIFFICULTIES[idx+1:])
    for diff in order:
        row = fetch_by_diff(diff)
        if row:
            return row
    return None


def get_question_options(question_id, shuffle=False):
    order = 'RANDOM()' if shuffle else 'id'
    return get_db().execute(f'SELECT * FROM question_options WHERE question_id = ? ORDER BY {order}', (question_id,)).fetchall()


def is_manual_review_type(question_type):
    return question_type in ('text_input', 'fill_blank')


def extract_short_answer(raw_answer):
    try:
        data = json.loads(raw_answer or '{}')
        value = data.get('text_input') or data.get('numeric_answer') or ['']
        if isinstance(value, list):
            return ', '.join(str(v) for v in value)
        return str(value)
    except Exception:
        return raw_answer or ''


def evaluate_answer(question, submitted):
    options = get_question_options(question['id'])
    qtype = question['question_type']

    if qtype in ('single_choice', 'image_choice'):
        correct_option = next((str(opt['id']) for opt in options if opt['is_correct']), None)
        return submitted.get('single_choice') == correct_option

    if qtype == 'multiple_choice':
        correct_set = sorted(str(opt['id']) for opt in options if opt['is_correct'])
        selected = sorted(submitted.getlist('multiple_choice'))
        return selected == correct_set

    if qtype in ('text_input', 'fill_blank'):
        accepted = [normalize_text(opt['option_text']) for opt in options if opt['is_correct']]
        answer = normalize_text(submitted.get('text_input', ''))
        return answer in accepted

    if qtype == 'numeric':
        accepted = [opt['option_text'] for opt in options if opt['is_correct']]
        answer = submitted.get('numeric_answer', '').strip()
        return any(numbers_equal(answer, correct) for correct in accepted)

    if qtype == 'true_false':
        correct = next((normalize_text(opt['option_text']) for opt in options if opt['is_correct']), None)
        answer = normalize_text(submitted.get('true_false', ''))
        return answer == correct

    if qtype == 'matching':
        for opt in options:
            if normalize_text(submitted.get(f'match_{opt["id"]}', '')) != normalize_text(opt['match_value'] or ''):
                return False
        return True

    if qtype == 'ordering':
        expected = [str(opt['id']) for opt in sorted(options, key=lambda item: int(item['match_key'] or 0))]
        submitted_pairs = []
        for opt in options:
            raw_position = submitted.get(f'order_{opt["id"]}', '').strip()
            if not raw_position.isdigit():
                return False
            submitted_pairs.append((int(raw_position), str(opt['id'])))
        positions = sorted(pos for pos, _ in submitted_pairs)
        if positions != list(range(1, len(options) + 1)):
            return False
        submitted_pairs.sort(key=lambda item: item[0])
        actual = [option_id for _, option_id in submitted_pairs]
        return actual == expected

    return False

def build_feedback(question, was_correct, next_level):
    diff = question['difficulty']
    topic = question['topic']
    if was_correct:
        if diff == 'високий':
            return f"Це була правильна відповідь на складне питання з теми «{topic}». Переходимо до супер-рівня і завершення теми."
        return f"Це була правильна відповідь на питання рівня «{diff}». Переходимо до рівня «{next_level}»."
    return f"Відповідь була неправильною. Система не карає, а допомагає: зараз буде простіше питання з тієї ж теми на рівні «{next_level}»."


def calculate_diagnostic(student_id):
    db = get_db()
    attempts = db.execute('''
        SELECT a.id, a.total_score, a.status, t.title, t.subject, a.created_at, a.completed_at
        FROM attempts a JOIN tests t ON t.id = a.test_id
        WHERE a.student_id = ? ORDER BY a.created_at DESC
    ''', (student_id,)).fetchall()
    completed = [a for a in attempts if a['status'] == 'completed']
    average = round(sum(a['total_score'] for a in completed) / len(completed), 2) if completed else 0
    topic_rows = db.execute('''
        SELECT q.topic,
               SUM(aa.score_awarded) AS earned,
               COUNT(aa.id) AS answered,
               SUM(CASE WHEN aa.is_correct = 1 THEN 1 ELSE 0 END) AS correct_answers
        FROM attempt_answers aa
        JOIN questions q ON q.id = aa.question_id
        JOIN attempts a ON a.id = aa.attempt_id
        WHERE a.student_id = ?
        GROUP BY q.topic ORDER BY q.topic
    ''', (student_id,)).fetchall()
    return attempts, average, topic_rows


def get_overall_diagnostic_dashboard(student_id):
    """Готує дані для нормальної сторінки діагностики учня: картки, діаграми, таблиця, рекомендації."""
    db = get_db()
    attempts, average, topic_rows = calculate_diagnostic(student_id)
    completed = [a for a in attempts if a['status'] == 'completed']

    total_answered = sum((row['answered'] or 0) for row in topic_rows)
    total_correct = sum((row['correct_answers'] or 0) for row in topic_rows)
    correct_percent = percent_or_zero(total_correct, total_answered)
    level_name = difficulty_by_percent(correct_percent)

    topics = []
    good_topics = []
    weak_topics = []
    for row in topic_rows:
        answered = row['answered'] or 0
        correct = row['correct_answers'] or 0
        percent = percent_or_zero(correct, answered)
        topic = dict(row)
        topic['percent'] = percent
        topics.append(topic)
        if percent >= 75:
            good_topics.append(row['topic'])
        elif answered and percent < 70:
            weak_topics.append(row['topic'])

    completed_rows = db.execute("""
        SELECT a.id, a.total_score, a.created_at, a.completed_at,
               COALESCE(SUM(q.points), 0) AS max_score
        FROM attempts a
        LEFT JOIN attempt_answers aa ON aa.attempt_id = a.id
        LEFT JOIN questions q ON q.id = aa.question_id
        WHERE a.student_id = ? AND a.status = 'completed'
        GROUP BY a.id
        ORDER BY a.completed_at DESC, a.created_at DESC
        LIMIT 5
    """, (student_id,)).fetchall()

    history = []
    for index, row in enumerate(reversed(completed_rows), start=1):
        history.append({
            'label': f'Тест {index}' + (' (останній)' if index == len(completed_rows) else ''),
            'percent': percent_or_zero(row['total_score'] or 0, row['max_score'] or 0),
        })

    distribution = [
        {'label': 'Відмінно (90-100%)', 'value': 0},
        {'label': 'Добре (75-89%)', 'value': 0},
        {'label': 'Задовільно (60-74%)', 'value': 0},
        {'label': 'Незадовільно (<60%)', 'value': 0},
    ]
    for row in db.execute("""
        SELECT a.id, a.total_score, COALESCE(SUM(q.points), 0) AS max_score
        FROM attempts a
        LEFT JOIN attempt_answers aa ON aa.attempt_id = a.id
        LEFT JOIN questions q ON q.id = aa.question_id
        WHERE a.student_id = ? AND a.status = 'completed'
        GROUP BY a.id
    """, (student_id,)).fetchall():
        p = percent_or_zero(row['total_score'] or 0, row['max_score'] or 0)
        if p >= 90:
            distribution[0]['value'] += 1
        elif p >= 75:
            distribution[1]['value'] += 1
        elif p >= 60:
            distribution[2]['value'] += 1
        else:
            distribution[3]['value'] += 1
    total_dist = sum(item['value'] for item in distribution)
    for item in distribution:
        item['percent'] = percent_or_zero(item['value'], total_dist)

    last = completed[0] if completed else None
    dashboard = {
        'average': round(average, 1),
        'correct_answers': total_correct,
        'total_answers': total_answered,
        'correct_percent': correct_percent,
        'level_points': difficulty_level_number(level_name),
        'level_name': level_name,
        'date': (last['completed_at'] or last['created_at'])[:10] if last else '—',
        'time': (last['completed_at'] or last['created_at'])[11:16] if last and 'T' in (last['completed_at'] or last['created_at']) else '—',
        'topics': topics,
        'history': history,
        'distribution': distribution,
        'good_topics': good_topics,
        'weak_topics': weak_topics,
        'attempts_count': len(completed),
        'card_percent': correct_percent,
        'recommended_level': difficulty_level_number(next_difficulty(level_name, correct_percent >= 75)),
    }
    return enrich_dashboard_common(dashboard)


def enrich_dashboard_common(dashboard):
    """Додає службові поля для відображення діаграм у шаблонах."""
    distribution = dashboard.get('distribution', [])
    cumulative = 0
    for item in distribution:
        item['start'] = cumulative
        cumulative += item.get('percent', 0) or 0
        item['end'] = cumulative
    dashboard['distribution'] = distribution
    dashboard['attempts_count'] = dashboard.get('attempts_count', 0)
    dashboard['card_percent'] = dashboard.get('card_percent', dashboard.get('correct_percent', dashboard.get('percent', 0)))
    return dashboard


def get_teacher_diagnostic_dashboard(class_id=None):
    """Загальна діагностика для вчителя: статистика класу або всіх завершених спроб."""
    db = get_db()
    class_filter = ''
    params = []
    if class_id:
        class_filter = ' AND u.class_id = ?'
        params.append(class_id)

    completed = db.execute(f"""
        SELECT a.id, a.total_score, a.created_at, a.completed_at, u.full_name,
               COALESCE(SUM(q.points), 0) AS max_score
        FROM attempts a
        JOIN users u ON u.id = a.student_id
        LEFT JOIN attempt_answers aa ON aa.attempt_id = a.id
        LEFT JOIN questions q ON q.id = aa.question_id
        WHERE a.status = 'completed' {class_filter}
        GROUP BY a.id
        ORDER BY a.completed_at DESC, a.created_at DESC
    """, params).fetchall()

    topic_rows = db.execute(f"""
        SELECT q.topic,
               SUM(aa.score_awarded) AS earned,
               COUNT(aa.id) AS answered,
               SUM(CASE WHEN aa.is_correct = 1 THEN 1 ELSE 0 END) AS correct_answers,
               SUM(q.points) AS max_points
        FROM attempt_answers aa
        JOIN questions q ON q.id = aa.question_id
        JOIN attempts a ON a.id = aa.attempt_id
        JOIN users u ON u.id = a.student_id
        WHERE a.status = 'completed' {class_filter}
        GROUP BY q.topic
        ORDER BY q.topic
    """, params).fetchall()

    total_answered = sum((row['answered'] or 0) for row in topic_rows)
    total_correct = sum((row['correct_answers'] or 0) for row in topic_rows)
    correct_percent = percent_or_zero(total_correct, total_answered)
    level_name = difficulty_by_percent(correct_percent)

    topics, good_topics, weak_topics = [], [], []
    for row in topic_rows:
        percent = percent_or_zero(row['earned'] or 0, row['max_points'] or 0)
        topic = dict(row)
        topic['percent'] = percent
        topics.append(topic)
        if percent >= 75:
            good_topics.append(row['topic'])
        elif (row['answered'] or 0) and percent < 70:
            weak_topics.append(row['topic'])

    distribution = [
        {'label': 'Відмінно (90-100%)', 'value': 0},
        {'label': 'Добре (75-89%)', 'value': 0},
        {'label': 'Задовільно (60-74%)', 'value': 0},
        {'label': 'Незадовільно (<60%)', 'value': 0},
    ]
    percents = []
    for row in completed:
        p = percent_or_zero(row['total_score'] or 0, row['max_score'] or 0)
        percents.append(p)
        if p >= 90:
            distribution[0]['value'] += 1
        elif p >= 75:
            distribution[1]['value'] += 1
        elif p >= 60:
            distribution[2]['value'] += 1
        else:
            distribution[3]['value'] += 1
    total_dist = sum(item['value'] for item in distribution)
    for item in distribution:
        item['percent'] = percent_or_zero(item['value'], total_dist)

    last_five = list(reversed(completed[:5]))
    history = []
    for index, row in enumerate(last_five, start=1):
        history.append({
            'label': f'Спроба {index}' + (' (остання)' if index == len(last_five) else ''),
            'percent': percent_or_zero(row['total_score'] or 0, row['max_score'] or 0),
        })

    last = completed[0] if completed else None
    average_percent = round(sum(percents) / len(percents)) if percents else 0
    dashboard = {
        'average': average_percent,
        'card_percent': average_percent,
        'correct_answers': total_correct,
        'total_answers': total_answered,
        'correct_percent': correct_percent,
        'attempts_count': len(completed),
        'level_points': difficulty_level_number(level_name),
        'level_name': level_name,
        'date': (last['completed_at'] or last['created_at'])[:10] if last else '—',
        'time': (last['completed_at'] or last['created_at'])[11:16] if last and 'T' in (last['completed_at'] or last['created_at']) else '—',
        'topics': topics,
        'history': history,
        'distribution': distribution,
        'good_topics': good_topics,
        'weak_topics': weak_topics,
        'recommended_level': difficulty_level_number(next_difficulty(level_name, correct_percent >= 75)),
    }
    return enrich_dashboard_common(dashboard)


def percent_or_zero(part, total):
    return round((part / total) * 100) if total else 0


def format_duration(start_value, end_value):
    try:
        start = datetime.fromisoformat(start_value)
        end = datetime.fromisoformat(end_value) if end_value else datetime.utcnow()
        seconds = max(0, int((end - start).total_seconds()))
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        secs = seconds % 60
        return f'{hours:02d}:{minutes:02d}:{secs:02d}'
    except Exception:
        return '00:00:00'


def difficulty_level_number(difficulty):
    return POINTS_BY_DIFFICULTY.get(difficulty or 'середній', 6)


def difficulty_by_percent(percent):
    if percent >= 90:
        return 'високий'
    if percent >= 75:
        return 'достатній'
    if percent >= 60:
        return 'середній'
    return 'початковий'


def get_result_dashboard_data(student_id, attempt, answers):
    db = get_db()
    total_answers = len(answers)
    correct_answers = sum(1 for item in answers if item['is_correct'])
    max_score = sum((item['points'] or 0) for item in answers)
    percent = percent_or_zero(attempt['total_score'], max_score)
    result_level = difficulty_by_percent(percent)
    recommended = difficulty_level_number(next_difficulty(result_level, percent >= 75))

    topic_rows = db.execute("""
        SELECT q.topic,
               COUNT(aa.id) AS answered,
               SUM(CASE WHEN aa.is_correct = 1 THEN 1 ELSE 0 END) AS correct_answers,
               SUM(aa.score_awarded) AS earned,
               SUM(q.points) AS max_points
        FROM attempt_answers aa
        JOIN questions q ON q.id = aa.question_id
        WHERE aa.attempt_id = ?
        GROUP BY q.topic
        ORDER BY q.topic
    """, (attempt['id'],)).fetchall()
    topics = []
    good_topics = []
    weak_topics = []
    for row in topic_rows:
        topic_percent = percent_or_zero(row['earned'] or 0, row['max_points'] or 0)
        topic = dict(row)
        topic['percent'] = topic_percent
        topics.append(topic)
        if topic_percent >= 75:
            good_topics.append(row['topic'])
        elif topic_percent < 70:
            weak_topics.append(row['topic'])

    completed_attempts = db.execute("""
        SELECT a.id, a.total_score, a.created_at, a.completed_at,
               COALESCE(SUM(q.points), 0) AS max_score
        FROM attempts a
        LEFT JOIN attempt_answers aa ON aa.attempt_id = a.id
        LEFT JOIN questions q ON q.id = aa.question_id
        WHERE a.student_id = ? AND a.status = 'completed'
        GROUP BY a.id
        ORDER BY a.completed_at DESC, a.created_at DESC
        LIMIT 5
    """, (student_id,)).fetchall()
    history = []
    for index, row in enumerate(reversed(completed_attempts), start=1):
        history.append({
            'label': f'Тест {index}' + (' (останній)' if row['id'] == attempt['id'] else ''),
            'percent': percent_or_zero(row['total_score'] or 0, row['max_score'] or 0),
        })

    distribution = [
        {'label': 'Відмінно (90-100%)', 'value': 0},
        {'label': 'Добре (75-89%)', 'value': 0},
        {'label': 'Задовільно (60-74%)', 'value': 0},
        {'label': 'Незадовільно (<60%)', 'value': 0},
    ]
    all_completed = db.execute("""
        SELECT a.id, a.total_score, COALESCE(SUM(q.points), 0) AS max_score
        FROM attempts a
        LEFT JOIN attempt_answers aa ON aa.attempt_id = a.id
        LEFT JOIN questions q ON q.id = aa.question_id
        WHERE a.status = 'completed'
        GROUP BY a.id
    """).fetchall()
    for row in all_completed:
        p = percent_or_zero(row['total_score'] or 0, row['max_score'] or 0)
        if p >= 90:
            distribution[0]['value'] += 1
        elif p >= 75:
            distribution[1]['value'] += 1
        elif p >= 60:
            distribution[2]['value'] += 1
        else:
            distribution[3]['value'] += 1
    total_dist = sum(item['value'] for item in distribution)
    for item in distribution:
        item['percent'] = percent_or_zero(item['value'], total_dist)

    dashboard = {
        'percent': percent,
        'correct_answers': correct_answers,
        'total_answers': total_answers,
        'duration': format_duration(attempt['created_at'], attempt['completed_at']),
        'level_points': difficulty_level_number(result_level),
        'level_name': result_level,
        'recommended_level': recommended,
        'date': (attempt['completed_at'] or attempt['created_at'])[:10],
        'time': (attempt['completed_at'] or attempt['created_at'])[11:16] if 'T' in (attempt['completed_at'] or attempt['created_at']) else '',
        'topics': topics,
        'history': history,
        'distribution': distribution,
        'good_topics': good_topics,
        'weak_topics': weak_topics,
        'card_percent': percent,
        'attempts_count': len(completed_attempts),
    }
    return enrich_dashboard_common(dashboard)


@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('teacher_dashboard' if session.get('role') == 'teacher' else 'student_dashboard'))
    return render_template('index.html')

@app.route('/choose-action/<role>')
def choose_action(role):
    if role not in ['teacher', 'student']:
        return redirect(url_for('index'))
    return render_template('choose_action.html', role=role)


@app.route('/login', methods=['GET', 'POST'])
def login():
    role = request.args.get('role', 'student')

    if request.method == 'POST':
        role = request.form.get('role_hint', role)

        user = get_db().execute('SELECT * FROM users WHERE username = ?',
                                (request.form.get('username', '').strip(),)).fetchone()
        if user and check_password_hash(user['password_hash'], request.form.get('password', '')):
            session.clear()
            session['user_id'] = user['id']
            session['role'] = user['role']
            flash('Вхід виконано успішно.', 'success')
            return redirect(url_for('index'))
        flash('Невірний логін або пароль.', 'error')

    return render_template('login.html', role=role)


@app.route('/register', methods=['GET', 'POST'])
def register():
    role = request.args.get('role', 'student')
    if 'user_id' in session:

        return redirect(url_for('index'))
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        full_name = request.form.get('full_name', '').strip()
        password = request.form.get('password', '')
        password2 = request.form.get('password2', '')
        role = request.form.get('role', 'student')
        if not username or not full_name or not password:
            flash('Усі поля є обов\'язковими.', 'error')
        elif role not in ('teacher', 'student'):
            flash('Недозволена роль.', 'error')
        elif password != password2:
            flash('Паролі не збігаються.', 'error')
        elif len(password) < 6:
            flash('Пароль має бути не менше 6 символів.', 'error')
        else:
            db = get_db()
            existing = db.execute('SELECT id FROM users WHERE username = ?', (username,)).fetchone()
            if existing:
                flash('Такий логін вже зайнятий.', 'error')
            else:
                db.execute(
                    'INSERT INTO users (username, full_name, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?)',
                    (username, full_name, generate_password_hash(password), role, datetime.utcnow().isoformat())
                )
                db.commit()
                flash('Реєстрація успішна! Тепер увійдіть у систему.', 'success')
                return redirect(url_for('login'))
    return render_template('register.html')


@app.route('/logout')
def logout():
    session.clear()
    flash('Ви вийшли із системи.', 'success')
    return redirect(url_for('login'))


@app.route('/teacher')
@login_required(role='teacher')
def teacher_dashboard():
    db = get_db()
    tests = db.execute('''
        SELECT t.*, COUNT(q.id) AS question_count
        FROM tests t LEFT JOIN questions q ON q.test_id = t.id
        GROUP BY t.id ORDER BY t.created_at DESC
    ''').fetchall()
    classes = db.execute("SELECT * FROM classes WHERE teacher_id = ? ORDER BY name", (session['user_id'],)).fetchall()
    selected_class_id = request.args.get('class_id', '').strip()
    student_sql = "SELECT u.*, c.name AS class_name FROM users u LEFT JOIN classes c ON c.id = u.class_id WHERE u.role = 'student'"
    student_params = []
    if selected_class_id:
        student_sql += " AND u.class_id = ?"
        student_params.append(selected_class_id)
    student_sql += " ORDER BY c.name, u.full_name"
    students = db.execute(student_sql, student_params).fetchall()
    diagnostics = []
    for student in students:
        attempts, average, topic_rows = calculate_diagnostic(student['id'])
        student_dashboard_data = get_overall_diagnostic_dashboard(student['id'])
        diagnostics.append({'student': student, 'attempts': attempts, 'average': average, 'topics': topic_rows, 'dashboard': student_dashboard_data})
    class_dashboard = get_teacher_diagnostic_dashboard(selected_class_id or None)
    return render_template(
        'teacher_dashboard.html',
        tests=tests,
        classes=classes,
        selected_class_id=selected_class_id,
        difficulties=DIFFICULTIES,
        question_types=QUESTION_TYPES,
        question_type_labels=QUESTION_TYPE_LABELS,
        diagnostics=diagnostics,
        class_dashboard=class_dashboard
    )



@app.route('/teacher/classes', methods=['GET', 'POST'])
@login_required(role='teacher')
def teacher_classes():
    db = get_db()
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'create_class':
            name = request.form.get('name', '').strip()
            description = request.form.get('description', '').strip()
            if not name:
                flash('Вкажіть назву класу.', 'error')
            else:
                try:
                    db.execute(
                        'INSERT INTO classes (name, description, teacher_id, created_at) VALUES (?, ?, ?, ?)',
                        (name, description, session['user_id'], datetime.utcnow().isoformat())
                    )
                    db.commit()
                    flash('Клас створено.', 'success')
                except sqlite3.IntegrityError:
                    flash('Такий клас уже існує.', 'error')
        elif action == 'add_student':
            full_name = request.form.get('full_name', '').strip()
            username = request.form.get('username', '').strip()
            password = request.form.get('password', '').strip()
            class_id = request.form.get('class_id', '').strip() or None
            if not full_name or not username or not password or not class_id:
                flash('Заповніть ім’я, логін, пароль і клас учня.', 'error')
            elif len(password) < 6:
                flash('Пароль має бути не менше 6 символів.', 'error')
            else:
                class_exists = db.execute('SELECT id FROM classes WHERE id = ? AND teacher_id = ?', (class_id, session['user_id'])).fetchone()
                if not class_exists:
                    flash('Оберіть коректний клас.', 'error')
                else:
                    try:
                        db.execute(
                            'INSERT INTO users (username, full_name, password_hash, role, class_id, created_at) VALUES (?, ?, ?, ?, ?, ?)',
                            (username, full_name, generate_password_hash(password), 'student', class_id, datetime.utcnow().isoformat())
                        )
                        db.commit()
                        flash('Учня зареєстровано і додано до класу.', 'success')
                    except sqlite3.IntegrityError:
                        flash('Такий логін уже зайнятий.', 'error')
        elif action == 'move_student':
            student_id = request.form.get('student_id')
            class_id = request.form.get('class_id') or None
            db.execute('UPDATE users SET class_id = ? WHERE id = ? AND role = ?', (class_id, student_id, 'student'))
            db.commit()
            flash('Клас учня оновлено.', 'success')
        elif action == 'delete_student':
            student_id = request.form.get('student_id')
            db.execute('DELETE FROM attempt_answers WHERE attempt_id IN (SELECT id FROM attempts WHERE student_id = ?)', (student_id,))
            db.execute('DELETE FROM attempts WHERE student_id = ?', (student_id,))
            db.execute('DELETE FROM users WHERE id = ? AND role = ?', (student_id, 'student'))
            db.commit()
            flash('Учня та його результати видалено.', 'success')
        return redirect(url_for('teacher_classes'))

    classes = db.execute("""
        SELECT c.*, COUNT(u.id) AS student_count
        FROM classes c
        LEFT JOIN users u ON u.class_id = c.id AND u.role = 'student'
        WHERE c.teacher_id = ?
        GROUP BY c.id
        ORDER BY c.name
    """, (session['user_id'],)).fetchall()
    students = db.execute("""
        SELECT u.*, c.name AS class_name
        FROM users u
        LEFT JOIN classes c ON c.id = u.class_id
        WHERE u.role = 'student'
        ORDER BY c.name, u.full_name
    """).fetchall()
    return render_template('teacher_classes.html', classes=classes, students=students)


@app.route('/teacher/tests/create', methods=['GET', 'POST'])
@login_required(role='teacher')
def create_test_page():
    db = get_db()
    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        subject = request.form.get('subject', '').strip()
        description = request.form.get('description', '').strip()
        if not title or not subject:
            flash('Назва та предмет є обов’язковими.', 'error')
        else:
            db.execute(
                'INSERT INTO tests (title, subject, description, created_by, created_at) VALUES (?, ?, ?, ?, ?)',
                (title, subject, description, session['user_id'], datetime.utcnow().isoformat())
            )
            db.commit()
            flash('Тест створено. Тепер можна додати питання.', 'success')
            return redirect(url_for('create_test_page'))
    tests = db.execute("""
        SELECT t.*, COUNT(q.id) AS question_count
        FROM tests t LEFT JOIN questions q ON q.test_id = t.id
        GROUP BY t.id ORDER BY t.created_at DESC
    """).fetchall()
    return render_template(
        'create_test.html',
        tests=tests,
        difficulties=DIFFICULTIES,
        question_types=QUESTION_TYPES,
        question_type_labels=QUESTION_TYPE_LABELS,
    )

@app.route('/teacher/create_test', methods=['POST'])
@login_required(role='teacher')
def create_test():
    title = request.form.get('title', '').strip()
    subject = request.form.get('subject', '').strip()
    description = request.form.get('description', '').strip()
    if not title or not subject:
        flash('Назва та предмет є обов’язковими.', 'error')
        return redirect(url_for('create_test_page'))
    db = get_db()
    db.execute('INSERT INTO tests (title, subject, description, created_by, created_at) VALUES (?, ?, ?, ?, ?)',
               (title, subject, description, session['user_id'], datetime.utcnow().isoformat()))
    db.commit()
    flash('Тест створено.', 'success')
    return redirect(url_for('create_test_page'))


@app.route('/teacher/edit_test/<int:test_id>', methods=['GET', 'POST'])
@login_required(role='teacher')
def edit_test(test_id):
    db = get_db()
    test = db.execute('SELECT * FROM tests WHERE id = ? AND created_by = ?', (test_id, session['user_id'])).fetchone()
    if not test:
        flash('Тест не знайдено.', 'error')
        return redirect(url_for('teacher_dashboard'))
    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        subject = request.form.get('subject', '').strip()
        description = request.form.get('description', '').strip()
        if not title or not subject:
            flash('Назва та предмет є обов\'язковими.', 'error')
        else:
            db.execute('UPDATE tests SET title=?, subject=?, description=? WHERE id=?',
                       (title, subject, description, test_id))
            db.commit()
            flash('Тест оновлено.', 'success')
            return redirect(url_for('teacher_dashboard'))
    questions = db.execute('SELECT * FROM questions WHERE test_id = ? ORDER BY difficulty, id', (test_id,)).fetchall()
    all_tests = db.execute('SELECT id, title FROM tests WHERE created_by = ? ORDER BY title', (session['user_id'],)).fetchall()
    return render_template('edit_test.html', test=test, questions=questions, all_tests=all_tests,
                           difficulties=DIFFICULTIES, question_types=QUESTION_TYPES,
                           question_type_labels=QUESTION_TYPE_LABELS)


@app.route('/teacher/delete_test/<int:test_id>', methods=['POST'])
@login_required(role='teacher')
def delete_test(test_id):
    db = get_db()
    test = db.execute('SELECT * FROM tests WHERE id = ? AND created_by = ?', (test_id, session['user_id'])).fetchone()
    if not test:
        flash('Тест не знайдено.', 'error')
    else:
        db.execute('DELETE FROM tests WHERE id = ?', (test_id,))
        db.commit()
        flash(f'Тест "{test["title"]}" видалено.', 'success')
    return redirect(url_for('teacher_dashboard'))


@app.route('/teacher/delete_question/<int:question_id>', methods=['POST'])
@login_required(role='teacher')
def delete_question(question_id):
    db = get_db()
    q = db.execute('SELECT questions.*, tests.created_by FROM questions JOIN tests ON tests.id = questions.test_id WHERE questions.id = ?', (question_id,)).fetchone()
    if not q or q['created_by'] != session['user_id']:
        flash('Питання не знайдено.', 'error')
        return redirect(url_for('teacher_dashboard'))
    test_id = q['test_id']
    db.execute('DELETE FROM questions WHERE id = ?', (question_id,))
    db.commit()
    flash('Питання видалено.', 'success')
    return redirect(url_for('edit_test', test_id=test_id))


@app.route('/teacher/copy_question/<int:question_id>', methods=['POST'])
@login_required(role='teacher')
def copy_question(question_id):
    db = get_db()
    target_test_id = request.form.get('target_test_id')
    q = db.execute('''
        SELECT q.*, t.created_by
        FROM questions q
        JOIN tests t ON t.id = q.test_id
        WHERE q.id = ?
    ''', (question_id,)).fetchone()
    target = db.execute('SELECT * FROM tests WHERE id = ? AND created_by = ?', (target_test_id, session['user_id'])).fetchone()
    if not q or q['created_by'] != session['user_id'] or not target:
        flash('Не вдалося скопіювати питання: тест або питання не знайдено.', 'error')
        return redirect(url_for('teacher_dashboard'))
    now = datetime.utcnow().isoformat()
    cur = db.execute('''
        INSERT INTO questions (test_id, topic, prompt, question_type, difficulty, image_url, explanation, points, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (target_test_id, q['topic'], q['prompt'], q['question_type'], q['difficulty'], q['image_url'], q['explanation'], q['points'], now))
    new_question_id = cur.lastrowid
    options = db.execute('SELECT * FROM question_options WHERE question_id = ? ORDER BY id', (question_id,)).fetchall()
    for opt in options:
        db.execute('''
            INSERT INTO question_options (question_id, option_text, is_correct, match_key, match_value, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (new_question_id, opt['option_text'], opt['is_correct'], opt['match_key'], opt['match_value'], now))
    db.commit()
    flash(f'Питання скопійовано в тест «{target["title"]}».', 'success')
    return redirect(url_for('edit_test', test_id=q['test_id']))


@app.route('/teacher/add_question', methods=['POST'])
@login_required(role='teacher')
def add_question():
    db = get_db()
    test_id = request.form.get('test_id')
    topic = request.form.get('topic', '').strip()
    prompt = request.form.get('prompt', '').strip()
    question_type = request.form.get('question_type')
    difficulty = request.form.get('difficulty') or 'початковий'
    if difficulty not in DIFFICULTIES:
        difficulty = 'початковий'
    image_url = request.form.get('image_url', '').strip()
    explanation = request.form.get('explanation', '').strip()
    points = int(request.form.get('points') or POINTS_BY_DIFFICULTY.get(difficulty, 3))
    raw_options = request.form.get('options_json', '').strip()
    if not all([test_id, topic, prompt, question_type, difficulty]):
        flash('Усі основні поля питання є обов’язковими.', 'error')
        return redirect(url_for('create_test_page'))
    try:
        parsed = json.loads(raw_options) if raw_options else []
    except json.JSONDecodeError:
        flash('Некоректний JSON у полі options_json.', 'error')
        return redirect(url_for('create_test_page'))
    qcur = db.execute('INSERT INTO questions (test_id, topic, prompt, question_type, difficulty, image_url, explanation, points, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                      (test_id, topic, prompt, question_type, difficulty, image_url or None, explanation, points, datetime.utcnow().isoformat()))
    qid = qcur.lastrowid
    now = datetime.utcnow().isoformat()
    insert_question_options(db, qid, parsed, now)
    db.commit()
    flash('Питання додано.', 'success')
    return redirect(url_for('create_test_page'))


@app.route('/student')
@login_required(role='student')
def student_dashboard():
    db = get_db()
    tests = db.execute('''
        SELECT t.*, COUNT(q.id) AS question_count
        FROM tests t LEFT JOIN questions q ON q.test_id = t.id
        GROUP BY t.id ORDER BY t.created_at DESC
    ''').fetchall()
    attempts, average, topic_rows = calculate_diagnostic(session['user_id'])
    dashboard = get_overall_diagnostic_dashboard(session['user_id'])
    return render_template('student_dashboard.html', tests=tests, attempts=attempts, average=average, topics=topic_rows, dashboard=dashboard)


@app.route('/student/start/<int:test_id>')
@login_required(role='student')
def start_test(test_id):
    db = get_db()
    topics = [row['topic'] for row in db.execute('SELECT DISTINCT topic FROM questions WHERE test_id = ? ORDER BY topic', (test_id,)).fetchall()]
    if not topics:
        flash('У тесті ще немає питань.', 'error')
        return redirect(url_for('student_dashboard'))
    active = db.execute('SELECT id FROM attempts WHERE student_id = ? AND test_id = ? AND status = ? ORDER BY id DESC LIMIT 1',
                        (session['user_id'], test_id, 'active')).fetchone()
    if active:
        return redirect(url_for('take_test', attempt_id=active['id']))
    cur = db.execute('INSERT INTO attempts (student_id, test_id, status, total_score, current_topic_index, current_difficulty, topics_json, created_at) VALUES (?, ?, ?, 0, 0, ?, ?, ?)',
                     (session['user_id'], test_id, 'active', 'початковий', json.dumps(topics, ensure_ascii=False), datetime.utcnow().isoformat()))
    db.commit()
    return redirect(url_for('take_test', attempt_id=cur.lastrowid))


def get_active_question(attempt):
    db = get_db()
    topics = json.loads(attempt['topics_json'])
    current_index = attempt['current_topic_index']
    answered_ids = [row['question_id'] for row in db.execute('SELECT question_id FROM attempt_answers WHERE attempt_id = ?', (attempt['id'],)).fetchall()]
    while current_index < len(topics):
        topic = topics[current_index]
        question = find_question_for_topic(attempt['test_id'], topic, attempt['current_difficulty'], answered_ids, fallback='both')
        if question:
            return question, topic, current_index
        current_index += 1
        db.execute('UPDATE attempts SET current_topic_index = ?, current_difficulty = ? WHERE id = ?', (current_index, 'початковий', attempt['id']))
        db.commit()
    db.execute('UPDATE attempts SET status = ?, completed_at = ? WHERE id = ?', ('completed', datetime.utcnow().isoformat(), attempt['id']))
    db.commit()
    return None, None, current_index


@app.route('/student/attempt/<int:attempt_id>', methods=['GET', 'POST'])
@login_required(role='student')
def take_test(attempt_id):
    db = get_db()
    attempt = db.execute('SELECT * FROM attempts WHERE id = ? AND student_id = ?', (attempt_id, session['user_id'])).fetchone()
    if not attempt:
        flash('Спробу не знайдено.', 'error')
        return redirect(url_for('student_dashboard'))
    if attempt['status'] == 'completed':
        return redirect(url_for('attempt_result', attempt_id=attempt_id))
    question, topic, topic_index = get_active_question(attempt)
    if question is None:
        flash('Тест завершено.', 'success')
        return redirect(url_for('attempt_result', attempt_id=attempt_id))
    if request.method == 'POST':
        was_correct = evaluate_answer(question, request.form)
        needs_review = is_manual_review_type(question['question_type']) and not was_correct
        next_level = next_difficulty(question['difficulty'], was_correct)
        feedback = build_feedback(question, was_correct, next_level)
        if needs_review:
            feedback += ' Відповідь також потрапила на ручну перевірку вчителю.'
        score = question['points'] if was_correct else 0
        raw_answer = json.dumps(request.form.to_dict(flat=False), ensure_ascii=False)
        review_status = 'pending' if needs_review else 'auto'
        db.execute('''INSERT INTO attempt_answers
                   (attempt_id, question_id, raw_answer, is_correct, score_awarded, feedback, review_status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                   (attempt_id, question['id'], raw_answer, 1 if was_correct else 0, score, feedback, review_status, datetime.utcnow().isoformat()))
        new_score = attempt['total_score'] + score
        answered_ids = [row['question_id'] for row in db.execute('SELECT question_id FROM attempt_answers WHERE attempt_id = ?', (attempt_id,)).fetchall()]
        new_topic_index = topic_index
        new_diff = next_level
        fallback_mode = 'higher' if was_correct else 'lower'
        next_same = find_question_for_topic(attempt['test_id'], topic, new_diff, answered_ids, fallback=fallback_mode)
        if was_correct and next_same is None:
            new_topic_index = topic_index + 1
            new_diff = 'початковий'
        elif not was_correct and next_same is None:
            another = find_question_for_topic(attempt['test_id'], topic, question['difficulty'], answered_ids, fallback='both')
            if another is None:
                new_topic_index = topic_index + 1
                new_diff = 'початковий'
        db.execute('UPDATE attempts SET total_score = ?, current_topic_index = ?, current_difficulty = ? WHERE id = ?',
                   (new_score, new_topic_index, new_diff, attempt_id))
        db.commit()
        flash(feedback, 'success' if was_correct else 'info')
        return redirect(url_for('take_test', attempt_id=attempt_id))
    options = get_question_options(question['id'], shuffle=True)
    test = db.execute('SELECT * FROM tests WHERE id = ?', (attempt['test_id'],)).fetchone()
    answered_count = db.execute('SELECT COUNT(*) FROM attempt_answers WHERE attempt_id = ?', (attempt_id,)).fetchone()[0]
    return render_template('take_test.html', attempt=attempt, test=test, question=question, options=options, answered_count=answered_count, topic=topic)


@app.route('/student/result/<int:attempt_id>')
@login_required(role='student')
def attempt_result(attempt_id):
    db = get_db()
    attempt = db.execute('SELECT a.*, t.title, t.subject FROM attempts a JOIN tests t ON t.id = a.test_id WHERE a.id = ? AND a.student_id = ?',
                         (attempt_id, session['user_id'])).fetchone()
    if not attempt:
        flash('Результат не знайдено.', 'error')
        return redirect(url_for('student_dashboard'))
    answers = db.execute('SELECT aa.*, q.topic, q.prompt, q.difficulty, q.points FROM attempt_answers aa JOIN questions q ON q.id = aa.question_id WHERE aa.attempt_id = ? ORDER BY aa.id',
                         (attempt_id,)).fetchall()
    dashboard = get_result_dashboard_data(session['user_id'], attempt, answers)
    return render_template('attempt_result.html', attempt=attempt, answers=answers, dashboard=dashboard)


@app.context_processor
def inject_globals():
    return {'current_user': get_current_user()}


@app.route('/teacher/edit_question/<int:question_id>', methods=['GET', 'POST'])
@login_required(role='teacher')
def edit_question(question_id):
    db = get_db()
    q = db.execute(
        '''
        SELECT q.*, t.created_by
        FROM questions q
        JOIN tests t ON t.id = q.test_id
        WHERE q.id = ?
        ''',
        (question_id,)
    ).fetchone()

    if not q or q['created_by'] != session['user_id']:
        flash('Питання не знайдено або доступ заборонено.', 'error')
        return redirect(url_for('teacher_dashboard'))

    if request.method == 'POST':
        topic = request.form.get('topic', '').strip()
        prompt = request.form.get('prompt', '').strip()
        question_type = request.form.get('question_type')
        difficulty = request.form.get('difficulty')
        image_url = request.form.get('image_url', '').strip()
        explanation = request.form.get('explanation', '').strip()
        points = int(request.form.get('points') or POINTS_BY_DIFFICULTY.get(difficulty, 3))
        raw_options = request.form.get('options_json', '').strip()

        if not all([topic, prompt, question_type, difficulty]):
            flash('Усі основні поля питання є обов’язковими.', 'error')
            return redirect(url_for('edit_question', question_id=question_id))

        try:
            parsed = json.loads(raw_options) if raw_options else []
        except json.JSONDecodeError:
            flash('Помилка у форматі відповідей.', 'error')
            return redirect(url_for('edit_question', question_id=question_id))

        db.execute(
            '''
            UPDATE questions
            SET topic=?, prompt=?, question_type=?, difficulty=?, image_url=?, explanation=?, points=?
            WHERE id=?
            ''',
            (topic, prompt, question_type, difficulty, image_url or None, explanation, points, question_id)
        )

        db.execute('DELETE FROM question_options WHERE question_id = ?', (question_id,))
        insert_question_options(db, question_id, parsed, datetime.utcnow().isoformat())
        db.commit()

        flash('Питання успішно оновлено.', 'success')
        return redirect(url_for('edit_test', test_id=q['test_id']))

    options = db.execute('SELECT * FROM question_options WHERE question_id = ? ORDER BY id', (question_id,)).fetchall()
    options_list = []
    for o in options:
        options_list.append({
            'id': o['id'],
            'text': o['option_text'],
            'correct': bool(o['is_correct']),
            'match_key': o['match_key'],
            'match_value': o['match_value'],
            'order': int(o['match_key']) if str(o['match_key'] or '').isdigit() else None,
        })

    return render_template(
        'edit_question.html',
        q=q,
        options_json=json.dumps(options_list, ensure_ascii=False),
        difficulties=DIFFICULTIES,
        question_types=QUESTION_TYPES,
        question_type_labels=QUESTION_TYPE_LABELS
    )



@app.route('/teacher/manual_review', methods=['GET', 'POST'])
@login_required(role='teacher')
def manual_review():
    db = get_db()
    if request.method == 'POST':
        answer_id = request.form.get('answer_id')
        decision = request.form.get('decision')
        comment = request.form.get('teacher_comment', '').strip()
        row = db.execute('''
            SELECT aa.*, a.id AS attempt_id, a.total_score, t.created_by, q.points
            FROM attempt_answers aa
            JOIN attempts a ON a.id = aa.attempt_id
            JOIN questions q ON q.id = aa.question_id
            JOIN tests t ON t.id = a.test_id
            WHERE aa.id = ?
        ''', (answer_id,)).fetchone()
        if not row or row['created_by'] != session['user_id']:
            flash('Відповідь не знайдено або доступ заборонено.', 'error')
            return redirect(url_for('manual_review'))
        old_score = row['score_awarded']
        if decision == 'approve':
            new_correct = 1
            new_score = row['points']
            status = 'approved'
            feedback = 'Відповідь зарахована вчителем.'
        else:
            new_correct = 0
            new_score = 0
            status = 'rejected'
            feedback = 'Відповідь не зарахована вчителем.'
        db.execute('''
            UPDATE attempt_answers
            SET is_correct=?, score_awarded=?, review_status=?, teacher_comment=?, reviewed_by=?, reviewed_at=?, feedback=?
            WHERE id=?
        ''', (new_correct, new_score, status, comment, session['user_id'], datetime.utcnow().isoformat(), feedback, answer_id))
        db.execute('UPDATE attempts SET total_score = total_score + ? WHERE id = ?', (new_score - old_score, row['attempt_id']))
        db.commit()
        flash('Ручну перевірку збережено.', 'success')
        return redirect(url_for('manual_review'))

    rows = db.execute('''
        SELECT aa.*, q.prompt, q.points, q.question_type, q.topic, a.id AS attempt_id,
               u.full_name AS student_name, t.title AS test_title
        FROM attempt_answers aa
        JOIN questions q ON q.id = aa.question_id
        JOIN attempts a ON a.id = aa.attempt_id
        JOIN users u ON u.id = a.student_id
        JOIN tests t ON t.id = a.test_id
        WHERE t.created_by = ? AND q.question_type IN ('text_input', 'fill_blank')
        ORDER BY CASE aa.review_status WHEN 'pending' THEN 0 ELSE 1 END, aa.created_at DESC
    ''', (session['user_id'],)).fetchall()
    review_items = []
    for row in rows:
        item = dict(row)
        item['student_answer'] = extract_short_answer(row['raw_answer'])
        review_items.append(item)
    return render_template('manual_review.html', review_items=review_items, question_type_labels=QUESTION_TYPE_LABELS)

@app.route('/teacher/reset_diagnostics', methods=['POST'])
@login_required(role='teacher')
def reset_diagnostics():
    db = get_db()

    student_id = request.form.get('student_id', '').strip()
    test_id = request.form.get('test_id', '').strip()

    filters = []
    params = []

    if student_id:
        filters.append('student_id = ?')
        params.append(student_id)

    if test_id:
        filters.append('test_id = ?')
        params.append(test_id)

    where_clause = ' AND '.join(filters) if filters else '1=1'


    db.execute(
        f'''
        DELETE FROM attempt_answers
        WHERE attempt_id IN (
            SELECT id FROM attempts WHERE {where_clause}
        )
        ''',
        params
    )
    cursor = db.execute(f'DELETE FROM attempts WHERE {where_clause}', params)
    db.commit()

    if student_id and test_id:
        flash('Результати обраного учня за обраний тест онулено.', 'success')
    elif student_id:
        flash('Усі результати обраного учня онулено.', 'success')
    elif test_id:
        flash('Результати всіх учнів за обраний тест онулено.', 'success')
    else:
        flash('Усю успішність учнів було онулено.', 'success')

    return redirect(url_for('teacher_dashboard'))

if __name__ == '__main__':
    init_db()
    app.run(debug=True)
