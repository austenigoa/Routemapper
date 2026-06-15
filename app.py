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

# Ensure map folder exists
map_dir = os.path.join("static", "maps")
os.makedirs(map_dir, exist_ok=True)

USERNAME = 'admin'
PASSWORD = 'password'

# -------------------------
# ✅ ZIP HANDLING (FIXED)
# -------------------------

zip_cache = {
    '25298': (25.4383, -100.9737),
    '25903': (26.0056, -101.0053)
}

mx_overrides = {'25903', '25298'}
ca_pattern = r'^[A-Z]\d[A-Z] ?\d[A-Z]\d$'


def clean_zip(zip_code):
    zip_code = zip_code.strip().upper().replace('"', '').replace("'", '')
    zip_code = re.sub(r'\s+', ' ', zip_code)
    return zip_code


def detect_country(zip_code):
    zip_code = zip_code.strip().upper()

    if zip_code in mx_overrides:
        return "mx"

    if re.match(ca_pattern, zip_code):
        return "ca"

    if re.match(r'^\d{5}$', zip_code):
        return "us"

    return "us"


def get_coords(zip_code, country_hint=None):
    cleaned_zip = clean_zip(zip_code)

    # ✅ Cache
    if cleaned_zip in zip_cache:
        print(f"CACHE HIT: {cleaned_zip}")
        return zip_cache[cleaned_zip]

    detected_country = detect_country(cleaned_zip)

    # ✅ Override bad country inputs
    if country_hint:
        if country_hint != detected_country:
            print(f"WARNING: overriding {country_hint} -> {detected_country} for {cleaned_zip}")
            country_hint = detected_country
    else:
        country_hint = detected_country

    headers = {'User-Agent': 'RouteMapper/1.0 (your@email.com)'}

    params = {
        "postalcode": cleaned_zip,
        "countrycodes": country_hint,
        "format": "json",
        "limit": 1
    }

    print(f"Geocode attempt: {cleaned_zip} ({country_hint})")

    response = requests.get(
        "https://nominatim.openstreetmap.org/search",
        headers=headers,
        params=params
    )

    if response.status_code == 200 and response.json():
        data = response.json()[0]

        # ✅ FIXED validation logic
        display_name = data.get("display_name", "").lower()

        valid = False
        if country_hint == "us" and "united states" in display_name:
            valid = True
        elif country_hint == "mx" and ("mexico" in display_name or "méxico" in display_name):
            valid = True
        elif country_hint == "ca" and "canada" in display_name:
            valid = True

        if not valid:
            print(f"REJECTED (wrong country): {cleaned_zip} -> {data.get('display_name')}")
            return None

        lat = float(data['lat'])
        lon = float(data['lon'])

        print(f"RESULT: {cleaned_zip} -> {lat}, {lon} | {data.get('display_name')}")

        zip_cache[cleaned_zip] = (lat, lon)
        return (lat, lon)

    print(f"FAILED TO GEOCODE: {cleaned_zip}")
    return None


# -------------------------
# ✅ FACILITY MARKERS (RESTORED)
# -------------------------

always_visible_zips = [
    '95358', '25315', '76120', '78550', '40160',
    '28208', '30103', '17011', '48150',
    '54937', '55121', 'N3S 7P8'
]

facility_zip_countries = {
    '95358': 'us', '25315': 'mx', '76120': 'mx',
    '35403': 'us', '78550': 'us', '40160': 'us',
    '28208': 'us', '30103': 'us', '18640': 'us',
    '37122': 'us', '17011': 'us', '48150': 'us',
    '54937': 'us', '55121': 'us', 'N3S 7P8': 'ca'
}


# -------------------------
# UI TEMPLATES
# -------------------------

login_template = """
<!doctype html>
<title>Login</title>
<h2>Login</h2>
<form method='post'>
  Username: <input type='text' name='username'><br>
  Password: <input type='password' name='password'><br>
  <input type='submit' value='Login'>
</form>
"""

form_template = """
<!doctype html>
<title>Paste ZIP Code Data</title>
<h2>Paste ZIP Code Data</h2>
<form method='post'>
  <textarea name='data' rows='10' cols='70'></textarea><br>
  <input type='submit' value='Generate Map'>
</form>
"""

processing_template = """
<!doctype html>
<title>Processing</title>
<h2>Map is processing...</h2>

<div style="width:100%;background:#eee;">
  <div id="progress" style="width:0%;background:#4CAF50;color:white;padding:5px;">0%</div>
</div>

<script>
let progress = 0;
function updateProgressBar(){
    if(progress < 90){
        progress += 10;
        document.getElementById("progress").style.width = progress + "%";
        document.getElementById("progress").innerText = progress + "%";
    }
}

function checkStatus() {
    fetch("/job_status")
    .then(r => r.json())
    .then(data => {
        if(data.status === "finished"){
            document.getElementById("progress").style.width = "100%";
            document.getElementById("progress").innerText = "100%";
            window.location.href = "/status";
        } else if(data.status === "failed"){
            alert("Failed"); window.location.href = "/form";
        } else {
            updateProgressBar();
            setTimeout(checkStatus, 1000);
        }
    });
}

checkStatus();
</script>
"""


# -------------------------
# ✅ MAP GENERATION
# -------------------------

def generate_map(data):
    routes = []
    seen_pairs = set()

    f = StringIO(data)
    reader = csv.reader(f)

    for row in reader:
        if len(row) >= 3:
            origin_zip = clean_zip(row[0])
            dest_zip = clean_zip(row[1])
            delivery_number = row[2].strip()

            origin_country = row[3].strip().lower() if len(row) > 3 else None
            dest_country = row[4].strip().lower() if len(row) > 4 else None

            key = (origin_zip, dest_zip, delivery_number)
            if key in seen_pairs:
                continue
            seen_pairs.add(key)

            origin_coords = get_coords(origin_zip, origin_country)
            dest_coords = get_coords(dest_zip, dest_country)

            if origin_coords and dest_coords:
                routes.append((origin_coords, dest_coords, delivery_number))

    m = folium.Map(location=[39.5, -98.35], zoom_start=4)

    for zip_code in always_visible_zips:
        cleaned_zip = clean_zip(zip_code)
        country_hint = facility_zip_countries.get(cleaned_zip, detect_country(cleaned_zip))

        coords = get_coords(cleaned_zip, country_hint)

        if coords:
            folium.Marker(
                location=coords,
                popup=f'Facility: {cleaned_zip}',
                icon=folium.Icon(color='gray', icon='building', prefix='fa')
            ).add_to(m)

    delivery_group = folium.FeatureGroup(name="Delivery")
    collection_group = folium.FeatureGroup(name="Collection")
    stock_group = folium.FeatureGroup(name="Stock Order")
    other_group = folium.FeatureGroup(name="Other")

    for origin, dest, delivery_number in routes:
        if delivery_number.startswith("37"):
            group = collection_group
        elif delivery_number.startswith("368"):
            group = stock_group
        elif delivery_number.startswith("369") or delivery_number.startswith("34"):
            group = delivery_group
        else:
            group = other_group

        origin_icon = CustomIcon(
            icon_image='https://raw.githubusercontent.com/pointhi/leaflet-color-markers/master/img/marker-icon-red.png',
            icon_size=(12, 20)
        )
        dest_icon = CustomIcon(
            icon_image='https://raw.githubusercontent.com/pointhi/leaflet-color-markers/master/img/marker-icon-green.png',
            icon_size=(12, 20)
        )

        group.add_child(folium.Marker(location=origin, popup='Origin', icon=origin_icon))
        group.add_child(folium.Marker(location=dest, popup='Destination', icon=dest_icon))

        line = folium.PolyLine([origin, dest], color='blue', weight=3)
        folium.Popup(f'Delivery #: {delivery_number}').add_to(line)
        group.add_child(line)

        PolyLineTextPath(line, '➤', repeat=False, offset=7).add_to(group)

    collection_group.add_to(m)
    delivery_group.add_to(m)
    stock_group.add_to(m)
    other_group.add_to(m)

    folium.LayerControl(collapsed=False).add_to(m)

    return m.get_root().render()


# -------------------------
# ROUTES
# -------------------------

@app.route('/', methods=['GET','POST'])
def login():
    if request.method=='POST':
        if request.form['username']==USERNAME and request.form['password']==PASSWORD:
            session['logged_in']=True
            return redirect(url_for('form'))
    return render_template_string(login_template)


@app.route('/form', methods=['GET','POST'])
def form():
    if not session.get('logged_in'):
        return redirect(url_for('login'))

    if request.method=='POST':
        data=request.form['data']
        job=q.enqueue(generate_map,data,job_timeout=20000)
        session['job_id']=job.id
        return redirect(url_for('status'))

    return render_template_string(form_template)


@app.route('/status')
def status():
    job_id=session.get('job_id')
    if not job_id:
        return "No job"

    job=Job.fetch(job_id,connection=redis_conn)

    if job.is_failed:
        return f"<pre>{job.exc_info}</pre>"

    if job.is_finished:
        return f"<div>{job.result}</div>"

    return render_template_string(processing_template)


@app.route('/job_status')
def job_status():
    job_id=session.get('job_id')
    job=Job.fetch(job_id,connection=redis_conn)

    if job.is_finished:
        return jsonify({'status':'finished'})
    elif job.is_failed:
        return jsonify({'status':'failed'})
    return jsonify({'status':'in_progress'})


if __name__=='__main__':
    app.run(debug=True)
