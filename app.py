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

# Ensure directory
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

# ------------------ Facility Sections ------------------

always_visible_ford_zips = ['40202', '48134', '83000', '54800']

facility_zip_countries = {
    '40202': 'us', '48134': 'us', '83000': 'mx', '54800': 'mx'
}

always_visible_plants = [
    '95358', '25315', '76120', '78550', '40160',
    '28208', '30103', '17011', '48150',
    '54937', '55121', 'N3S 7P8'
]

facility_zip_plantcountries = {
    '95358': 'us', '25315': 'mx', '76120': 'mx',
    '78550': 'us', '40160': 'us', '28208': 'us',
    '30103': 'us', '17011': 'us', '48150': 'us',
    '54937': 'us', '55121': 'us', 'N3S 7P8': 'ca'
}

# ------------------ Helpers ------------------

zip_cache = {}

def clean_zip(z):
    return re.sub(r'\s+', ' ', z.strip().upper().replace('"','').replace("'",""))

def detect_country(zip_code):
    if zip_code == '25903':
        return "mx"
    if re.match(r'^[A-Z]\d[A-Z] ?\d[A-Z]\d$', zip_code):
        return "ca"
    elif re.match(r'^\d{5}$', zip_code):
        return "mx" if 1000 <= int(zip_code) <= 99998 else "us"
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
        if data:
            lat = float(data[0]['lat'])
            lon = float(data[0]['lon'])
            zip_cache[cleaned] = (lat, lon)
            return (lat, lon)
    except Exception as e:
        print(f"Geocode error: {zip_code} -> {e}")

    return None

# ------------------ Core ------------------

def generate_map(data):
    print("=== START MAP ===")
    routes = []
    seen = set()

    reader = csv.reader(StringIO(data), skipinitialspace=True)

    for row in reader:
        print("ROW:", row)

        if len(row) < 5:
            print("Skip: bad column count")
            continue

        origin_zip = clean_zip(row[0])
        dest_zip = clean_zip(row[1])
        delivery_number = row[2].strip()
        origin_country = row[3].strip().lower()
        dest_country = row[4].strip().lower()

        if not origin_zip or not dest_zip:
            print("Skip: missing zip")
            continue

        key = (origin_zip, dest_zip, delivery_number)
        if key in seen:
            continue
        seen.add(key)

        origin = get_coords(origin_zip, origin_country)
        dest = get_coords(dest_zip, dest_country)

        if not origin or not dest:
            print("Skip: bad coords")
            continue

        routes.append((origin, dest, delivery_number))

    m = folium.Map(location=[39.5, -98.35], zoom_start=4)

    # -------- FACILITY GROUPS ----------
    ford_group = folium.FeatureGroup(name="Ford Facilities")
    plant_group = folium.FeatureGroup(name="Plants")

    for z in always_visible_ford_zips:
        coords = get_coords(z, facility_zip_countries.get(z, 'us'))
        if coords:
            folium.Marker(
                location=coords,
                popup=f"Ford: {z}",
                icon=folium.Icon(color='blue', icon='truck', prefix='fa')
            ).add_to(ford_group)

    for z in always_visible_plants:
        coords = get_coords(z, facility_zip_plantcountries.get(z, 'us'))
        if coords:
            folium.Marker(
                location=coords,
                popup=f"Plant: {z}",
                icon=folium.Icon(color='gray', icon='building', prefix='fa')
            ).add_to(plant_group)

    ford_group.add_to(m)
    plant_group.add_to(m)

    # -------- ROUTES ----------
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

        group.add_child(folium.Marker(location=origin))
        group.add_child(folium.Marker(location=dest))

        line = folium.PolyLine([origin, dest], color='blue', weight=3)
        group.add_child(line)

        PolyLineTextPath(line, '➤', repeat=False).add_to(group)

    collection_group.add_to(m)
    delivery_group.add_to(m)
    stock_group.add_to(m)
    other_group.add_to(m)

    folium.LayerControl(collapsed=False).add_to(m)

    print("=== DONE ===")
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
