import os, base64

from flask import Flask, jsonify, request, render_template, send_file, abort
from hashlib import md5

from PIL import Image, ImageFont, ImageDraw, ImageOps

from cStringIO import StringIO

import colorsys

from raven.contrib.flask import Sentry
from date_hash import date_hash
from werkzeug.contrib.profiler import ProfilerMiddleware

import ujson

import statsd

statsd_client = statsd.StatsClient(os.environ.get('STATSD_HOST', ''), 8125)
def incr_stat(name, count=1):
    statsd_client.incr('puzzle.captcha.%s' % name, count)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', None)
sentry = Sentry(app)

FONT = ImageFont.truetype(os.path.join(os.path.dirname(os.path.realpath(__file__)), 'fonts', 'ArchivoBlack-Regular.ttf'), size=24)

def calc_hash(string):
    m = md5()
    m.update(string)
    return m.hexdigest()

ALPHANUM = 'abcdefghijklmnopqrstuvwxyz0123456789'
IMAGE_SIZE = 100

def image_solution(h):
    result = ''
    for i in range(4):
        result += ALPHANUM[int(h[4*i:4*i+4], 16) % 36]
    return result

def real_image_solution(username, name):
    image_hash = calc_hash(username + name)
    return image_solution(image_hash)

def hsv_to_better_color(hsv):
    a, b, c = colorsys.hsv_to_rgb(*hsv)
    return (int(a*256), int(b*256), int(c*256))

def random_color(string):
    x = int(string, 16)
    y = float(x) / 16**len(string)
    hsv1 = (y, 1.0, 0.7)
    return hsv_to_better_color(hsv1)

def render_letter(letter):
    size = FONT.getsize(letter)
    txt=Image.new('L', size)
    d = ImageDraw.Draw(txt)
    d.text( (0, 0), letter,  font=FONT, fill=255)
    return txt

LETTER_CACHE = {}

def rotate_letter(letter_img, angle):
    return letter_img.rotate(angle, expand=1)

for letter in ALPHANUM:
    letter_img = render_letter(letter)
    for angle in range(361):
        k = (letter, angle)
        LETTER_CACHE[k] = rotate_letter(letter_img, angle)

def draw_rotated(draw, angle, letter, color, coordinate):
    k = (letter, angle)
    t = LETTER_CACHE.get(k)
    if not t:
        t = render_letter(letter)
        t = rotate_letter(t, angle)
        LETTER_CACHE[k] = t
    draw.bitmap(coordinate, t, fill=color)

def generate_image_base(username):
    user_hash = calc_hash(username)
    img = Image.new('RGB', (IMAGE_SIZE, 50))
    draw = ImageDraw.Draw(img)
    for i in range(4):
        a = random_color(user_hash[5+2*i:9+2*i])
        draw.rectangle(((i*IMAGE_SIZE/4, 0), ((i+1)*IMAGE_SIZE/4, 50)), fill=a)
    r = int(user_hash, 16)
    for i in range(4):
        color =  (255, 255, 255)
        r /= 2
        start_loc = (r % (IMAGE_SIZE/4), (r/64) % 50)
        r /= 256
        end_loc = ((r % (IMAGE_SIZE/4)) + 3*IMAGE_SIZE/4, (r/64) % 50)
        r /= 256
        draw.line((start_loc, end_loc), fill=color, width=2)
    return img

def generate_image(base, username, name):
    img = base.copy()
    image_hash = calc_hash(username + name)
    solution = image_solution(image_hash)
    draw = ImageDraw.Draw(img)
    rip = int(image_hash, 16)    
    for i, letter in enumerate(solution):
        offset_x = rip % ((IMAGE_SIZE/4 - 10) if i != 3 else 5)
        rip /= 64
        offset_y = rip % 20
        rip /= 64
        rotation = (rip % 60) - 30
        rip /= 64
        draw_rotated(draw, rotation, letter, (250, 250, 250), (i*IMAGE_SIZE/4 + offset_x, offset_y))
    return img

def random_image_from_base(base, username):
    name = os.urandom(16).encode('hex')
    return generate_image(base, username, name), name

def random_image(username):
    base = generate_image_base(username)
    return random_image_from_base(base, username)

def serve_pil_image(pil_img):
    img_io = StringIO()
    pil_img.save(img_io, 'JPEG', quality=50)
    img_io.seek(0)
    return send_file(img_io, mimetype='image/jpeg')

def pil_to_base64(pil_img):
    img_io = StringIO()
    pil_img.save(img_io, 'JPEG', quality=50)
    return base64.b64encode(img_io.getvalue())


@app.route('/u/<username>/solution', methods=['POST'])
def test_solution(username):
    correct = 0
    incorrect = 0
    content = request.get_json(force=True)
    if content.get('solutions') is None:
        abort(400)
        return
    if len(content['solutions']) > 15000:
        return jsonify({'error': "Too many answers submitted"})
    solved = set([])
    for solution in content['solutions']:
        if solution.get('name') is None or solution.get('solution') is None:
            abort(400)
            return
        if solution['name'] in solved:
            continue
        if solution['solution'] == real_image_solution(username, solution['name']):
            correct += 1
            solved.add(solution['name'])
        else:
            incorrect += 1
    incr_stat('correct_single_captcha', count=correct)
    incr_stat('incorrect_single_captcha', count=incorrect)
    if correct >= 10000:
        incr_stat('correct_solution')
        return jsonify({
            'message': "Congratulations! Marty and Doc are free. You are winrar.",
            'passcode': date_hash(app.secret_key, username)
        })
    else:
        incr_stat('wrong_solution')
        return jsonify({
            'error': "Too few correct solutions",
            'message': "Insufficient number of correct solutions"
        })

@app.route('/', methods=['GET'])
def index():
    return render_template('index.html')

@app.route('/u/<username>/', methods=['GET'])
def puzzle(username):
    return render_template('puzzle.html')

@app.route('/u/<username>/random_image', methods=['GET'])
def get_random_image(username):
    return serve_pil_image(random_image(username)[0])

@app.route('/u/<username>/image/<image_name>', methods=['GET'])
def get_image_name(username, image_name):
    base = generate_image_base(username)
    return serve_pil_image(generate_image(base, username, image_name))

@app.route('/u/<username>/challenge', methods=['GET'])
def get_challenge(username):
    incr_stat('challenge_requested')
    result = []
    base = generate_image_base(username)
    for i in range(1000):
        image, name = random_image_from_base(base, username)
        result.append({
            'jpg_base64': pil_to_base64(image),
            'name': name
        })
    return ujson.dumps({'images': result})

if __name__ == "__main__":
    app.wsgi_app = ProfilerMiddleware(app.wsgi_app)
    app.run(debug=True)
