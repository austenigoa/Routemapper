# app.py
import os, re, csv, logging, requests
from io import StringIO
from flask import Flask, render_template_string, request, redirect, url_for, session, jsonify
from redis import Redis
from rq import Queue
from rq.job import Job
import folium
from folium.features import CustomIcon
from folium.plugins import PolyLineTextPath

app = Flask(__name__)
app.secret_key = 'your_secret_key'

logging.basicConfig(level=logging.INFO)
redis_conn = Redis()
q = Queue(connection=redis_conn)

USERNAME = 'admin'
PASSWORD = 'password'

login_template = """..."""  # same as your original
form_template = """..."""   # same as your original
map_template = """..."""    # same as your original
processing_template = """..."""  # same as your original

zip_cache = { '25298': (25.4383, -100.9737) }

def clean_zip(zip_code):
    zip_code = zip_code.strip().upper().replace('"', '').replace("'", '')
    zip_code = re.sub(r'\s+', ' ', zip_code)
    return zip_code

def detect_country(zip_code):
    if zip_code == '25903':
        return "mx"
    if re.match(r'^[A-Z]\d[A-Z] ?\d[A-Z]\d$', zip_code):
        return "ca"
    elif re.match(r'^\d{5}$', zip_code):
        zip_int = int(zip_code)
        if 1000 <= zip_int <= 99998:
            return "mx"
        else:
            return "us"
    return "us"

def get_coords(zip_code, country_hint=None):
    cleaned_zip = clean_zip(zip_code)
    if cleaned_zip in zip_cache:
        return zip_cache[cleaned_zip]
    if not country_hint:
        country_hint = detect_country(cleaned_zip)
    url = f"https://nominatim.openstreetmap.org/search?q={cleaned_zip}&countrycodes={country_hint}&format=json"
    headers = {'User-Agent': 'RouteMapper/1.0 (your@email.com)'}
    response = requests.get(url, headers=headers)
    if response.status_code == 200 and response.json():
        lat = float(response.json()[0]['lat'])
        lon = float(response.json()[0]['lon'])
        zip_cache[cleaned_zip] = (lat, lon)
        return (lat, lon)
    return None

def generate_map(data):
    collections, stock_orders, deliveries = [], [], []
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
            pair_key = (origin_zip, dest_zip, delivery_number)
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            origin_coords = get_coords(origin_zip, origin_country)
            dest_coords = get_coords(dest_zip, dest_country)
            if origin_coords and dest_coords:
                if delivery_number.startswith("37"):
                    collections.append((origin_coords, dest_coords, delivery_number))
                elif delivery_number.startswith("368"):
                    stock_orders.append((origin_coords, dest_coords, delivery_number))
                elif delivery_number.startswith("369") or delivery_number.startswith("34"):
                    deliveries.append((origin_coords, dest_coords, delivery_number))

    m = folium.Map(location=[39.5, -98.35], zoom_start=4)

    def add_routes(route_list, group_type):
        for origin, dest, delivery_number in route_list:
            group_div = folium.FeatureGroup(name=f"{group_type}", control=False)
            group_div.add_child(folium.Marker(location=origin, popup='Origin',
                icon=CustomIcon('https://raw.githubusercontent.com/pointhi/leaflet-color-markers/master/img/marker-icon-red.png', icon_size=(12, 20))))
            group_div.add_child(folium.Marker(location=dest, popup='Destination',
                icon=CustomIcon('https://raw.githubusercontent.com/pointhi/leaflet-color-markers/master/img/marker-icon-green.png', icon_size=(12, 20))))
            line = folium.PolyLine([origin, dest], color='blue', weight=3)
            folium.Popup(f'Delivery #: {delivery_number}', max_width=300).add_to(line)
            group_div.add_child(line)
            PolyLineTextPath(line, '➤', repeat=False, offset=7,
                attributes={'fill': 'blue', 'font-weight': 'bold', 'font-size': '16'}).add_to(group_div)
            group_div.add_child(folium.Element(f'<div class="route-group" data-type="{group_type}"></div>'))
            group_div.add_to(m)

    add_routes(deliveries, "delivery")
    add_routes(collections, "collection")
    add_routes(stock_orders, "stock")

    return m.get_root().render()

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
        data = request.form['data']
        job = q.enqueue(generate_map, data, job_timeout=20000)
        session['job_id'] = job.id
        return redirect(url_for('status'))
    return render_template_string(form_template)

@app.route('/status')
def status():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    job_id = session.get('job_id')
    if not job_id:
        return "<h2>No job found.</h2>"
    job = Job.fetch(job_id, connection=redis_conn)
    if job.is_finished:
        return render_template_string(map_template, map_html=job.result)
    else:
        return render_template_string(processing_template)

@app.route('/job_status')
def job_status():
    job_id = session.get('job_id')
    if not job_id:
        return jsonify({'status': 'none'})
    job = Job.fetch(job_id, connection=redis_conn)
    if job.is_finished:
        return jsonify({'status': 'finished'})
    elif job.is_failed:
        return jsonify({'status': 'failed'})
    else:
        return jsonify({'status': 'in_progress'})

if __name__ == '__main__':
    app.run(debug=True)

