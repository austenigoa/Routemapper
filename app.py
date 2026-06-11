from flask import Flask, render_template_string, request, redirect, url_for, session, jsonify
import folium
import csv
from io import StringIO
import requests
from folium.plugins import PolyLineTextPath
from folium.features import CustomIcon
import re
from rq import Queue
from redis import Redis
from rq.job import Job
import os

app = Flask(__name__)
app.secret_key = 'your_secret_key'

# Redis setup
redis_url = os.getenv('REDIS_URL', 'redis://red-d302k12dbo4c73b72nt0:6379')
redis_conn = Redis.from_url(redis_url)
q = Queue(connection=redis_conn)

# Ensure map directory exists
os.makedirs(os.path.join("static", "maps"), exist_ok=True)

USERNAME = 'admin'
PASSWORD = 'password'

# ------------------ Templates ------------------

login_template = """<!doctype html>
<title>Login</title>
<h2>Login</h2>
<form method='post'>
Username: <input type='text' name='username'><br>
Password: <input type='password' name='password'><br>
<input type='submit' value='Login'>
</form>
"""

form_template = """<!doctype html>
<title>Paste ZIP Code Data</title>
<h2>Paste ZIP Code Data</h2>
<form method='post'>
<textarea name='data' rows='10' cols='70'></textarea><br>
<input type='submit' value='Generate Map'>
</form>
"""

processing_template = """<!doctype html>
<title>Processing</title>
<h2>Map is processing...</h2>
<div id="progress-bar" style="width: 100%; background-color: #f3f3f3">
<div id="progress" style="width: 0%; height: 30px; background-color: #4CAF50; text-align: center; line-height: 30px; color: white">0%</div>
</div>

<script>
let progress = 0
function updateProgressBar() {
    if (progress < 90) {
        progress += 10
        document.getElementById("progress").style.width = progress + "%"
        document.getElementById("progress").innerText = progress + "%"
    }
}

function checkStatus() {
    fetch("/job_status")
        .then(r => r.json())
        .then(data => {
            if (data.status === 'finished') {
                document.getElementById("progress").style.width = "100%"
                document.getElementById("progress").innerText = "100%"
                window.location.href = "/status"
            } else if (data.status === 'failed') {
                alert("Task failed.")
                window.location.href = "/form"
            } else {
                updateProgressBar()
                setTimeout(checkStatus, 1000)
            }
        })
}
checkStatus()
</script>
"""

# ------------------ Helpers ------------------

zip_cache = {}

def clean_zip(z):
    return re.sub(r'\s+', ' ', z.strip().upper().replace('"','').replace("'",""))

def detect_country(zip_code):
    # Canada pattern
    if re.match(r'^[A-Z]\d[A-Z] ?\d[A-Z]\d$', zip_code):
        return "ca"
    # Default all 5-digit to US
    elif re.match(r'^\d{5}$', zip_code):
        return "us"
    return "us"

def get_coords(zip_code, country_hint=None):
    cleaned = clean_zip(zip_code)

    if cleaned in zip_cache:
        return zip_cache[cleaned]

    if not country_hint:
        country_hint = detect_country(cleaned)

    url = f"https://nominatim.openstreetmap.org/search?q={cleaned}&countrycodes={country_hint}&format=json"

    try:
        res = requests.get(url, headers={'User-Agent': 'RouteMapper'}, timeout=5)
        res.raise_for_status()
        data = res.json()

        if not data:
            print(f"[GEOCODE FAILED] {cleaned} with hint={country_hint}")
            return None

        lat = float(data[0]['lat'])
        lon = float(data[0]['lon'])

        zip_cache[cleaned] = (lat, lon)
        return (lat, lon)

    except Exception as e:
        print(f"[GEOCODE ERROR] {cleaned} -> {e}")
        return None

def safe_get_coords(zip_code, country_hint=None):
    # Try with hint first
    coords = get_coords(zip_code, country_hint)
    if coords:
        return coords

    # Fallback without hint
    print(f"[RETRY] {zip_code} without country hint")
    return get_coords(zip_code)

# ------------------ Map Generation ------------------

def generate_map(data):
    print("=== START MAP ===")
    routes = []
    seen = set()
    skipped = 0

    reader = csv.reader(StringIO(data), skipinitialspace=True)

    for row in reader:
        print("ROW:", row)

        if len(row) < 5:
            print("[ROW SKIPPED] Bad column count")
            continue

        origin_zip = clean_zip(row[0])
        dest_zip = clean_zip(row[1])
        delivery_number = row[2].strip()
        origin_country = row[3].strip().lower()
        dest_country = row[4].strip().lower()

        key = (origin_zip, dest_zip, delivery_number)
        if key in seen:
            continue
        seen.add(key)

        # ✅ FIXED HERE
        origin = safe_get_coords(origin_zip, origin_country)
        dest = safe_get_coords(dest_zip, dest_country)

        if not origin or not dest:
            skipped += 1
            print(f"[ROUTE SKIPPED] {origin_zip} -> {dest_zip}")
            continue

        routes.append((origin, dest, delivery_number))

    print(f"[SUMMARY] Valid routes: {len(routes)}, Skipped: {skipped}")

    m = folium.Map(location=[39.5, -98.35], zoom_start=4)

    collection_group = folium.FeatureGroup(name="Collection")
    delivery_group = folium.FeatureGroup(name="Delivery")
    stock_group = folium.FeatureGroup(name="Stock Order")
    other_group = folium.FeatureGroup(name="Other")

    for origin, dest, delivery_number in routes:

        if delivery_number.startswith("368"):
            group = stock_group
        elif delivery_number.startswith("37"):
            group = collection_group
        elif delivery_number.startswith("369") or delivery_number.startswith("34"):
            group = delivery_group
        else:
            group = other_group

        origin_icon = CustomIcon(
            icon_image='https://raw.githubusercontent.com/pointhi/leaflet-color-markers/master/img/marker-icon-red.png',
            icon_size=(10, 16)
        )

        dest_icon = CustomIcon(
            icon_image='https://raw.githubusercontent.com/pointhi/leaflet-color-markers/master/img/marker-icon-green.png',
            icon_size=(10, 16)
        )

        group.add_child(folium.Marker(location=origin, icon=origin_icon))
        group.add_child(folium.Marker(location=dest, icon=dest_icon))

        line = folium.PolyLine([origin, dest], color='blue', weight=3)
        group.add_child(line)

        PolyLineTextPath(line, '➤', repeat=False, offset=7).add_to(group)

    collection_group.add_to(m)
    delivery_group.add_to(m)
    stock_group.add_to(m)
    other_group.add_to(m)

    folium.LayerControl(collapsed=False).add_to(m)

    print("=== MAP COMPLETE ===")
    return m.get_root().render()

# ------------------ Routes ------------------

@app.route('/', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form['username'] == USERNAME and request.form['password'] == PASSWORD:
            session['logged_in'] = True
            return redirect(url_for('form'))
    return render_template_string(login_template)

@app.route('/form', methods=['GET', 'POST'])
def form():
    if not session.get('logged_in'):
        return redirect(url_for('login'))

    if request.method == 'POST':
        job = q.enqueue(generate_map, request.form['data'], job_timeout=20000)
        session['job_id'] = job.id
        return redirect(url_for('status'))

    return render_template_string(form_template)

@app.route('/status')
def status():
    if not session.get('logged_in'):
        return redirect(url_for('login'))

    job = Job.fetch(session.get('job_id'), connection=redis_conn)

    if job.is_failed:
        return f"<pre>{job.exc_info}</pre>"

    if job.is_finished:
        return f"<div>{job.result}</div><br><a href='/form'>Back</a>"

    return render_template_string(processing_template)

@app.route('/job_status')
def job_status():
    job = Job.fetch(session.get('job_id'), connection=redis_conn)

    if job.is_finished:
        return jsonify({'status': 'finished'})
    elif job.is_failed:
        return jsonify({'status': 'failed'})
    return jsonify({'status': 'in_progress'})

# ------------------

if __name__ == '__main__':
    app.run(debug=True)
