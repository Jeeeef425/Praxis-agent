import os, json, redis, phonenumbers
from flask import Flask, request, render_template_string
from twilio.twiml.voice_response import VoiceResponse, Gather
from twilio.rest import Client as TwilioClient
from openai import OpenAI
from datetime import datetime, timedelta
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from sqlalchemy import create_engine, Column, String, DateTime, Boolean
from sqlalchemy.orm import declarative_base, sessionmaker

app = Flask(__name__)

# ---------- CONFIG ----------
app.config.from_mapping(
    OPENAI_API_KEY=os.getenv("OPENAI_API_KEY"),
    TWILIO_SID=os.getenv("TWILIO_ACCOUNT_SID"),
    TWILIO_TOKEN=os.getenv("TWILIO_AUTH_TOKEN"),
    TWILIO_NUMBER=os.getenv("TWILIO_NUMBER"),
    REDIS_URL=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
    CALENDAR_ID=os.getenv("CALENDAR_ID"),
    GOOGLE_SA=json.loads(os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")),
)
r = redis.from_url(app.config["REDIS_URL"])
engine = create_engine(os.getenv("DATABASE_URL", "sqlite:///data/app.db"), pool_pre_ping=True)
Session = sessionmaker(bind=engine)
Base = declarative_base()

# ---------- DB MODEL ----------
class Call(Base):
    __tablename__ = "calls"
    id = Column(String, primary_key=True)
    name = Column(String)
    phone = Column(String)
    date = Column(String)
    time = Column(String)
    consent = Column(Boolean, default=False)
    created = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(engine)

# ---------- CLIENTS ----------
openai_client = OpenAI(api_key=app.config["OPENAI_API_KEY"])
twilio_client = TwilioClient(app.config["TWILIO_SID"], app.config["TWILIO_TOKEN"])
creds = Credentials.from_service_account_info(app.config["GOOGLE_SA"])
cal = build("calendar", "v3", credentials=creds)

# ---------- HELPERS ----------
def gpt(prompt):
    return openai_client.chat.completions.create(
        model="gpt-3.5-turbo", messages=[{"role": "user", "content": prompt}]
    ).choices[0].message.content

def free_slots(date_str):
    # 2 Dummy-Slots
    return ["09:30", "10:15"]

def book_cal(date, time, name):
    start = f"{date}T{time}:00"
    end = (datetime.fromisoformat(start) + timedelta(minutes=15)).isoformat()
    event = {"summary": f"Termin: {name}", "start": {"dateTime": start, "timeZone": "Europe/Berlin"}, "end": {"dateTime": end, "timeZone": "Europe/Berlin"}}
    cal.events().insert(calendarId=app.config["CALENDAR_ID"], body=event).execute()

def send_sms(to, name, date, time):
    twilio_client.messages.create(
        body=f"Hallo {name}, Ihr Termin am {date} um {time} Uhr ist gebucht.",
        from_=app.config["TWILIO_NUMBER"], to=to
    )

# ---------- ROUTES ----------
@app.route("/voice", methods=["POST"])
def voice():
    call_id = request.form["CallSid"]
    r.hset(call_id, mapping={"step": "name"})
    resp = VoiceResponse()
    gather = Gather(input="speech", action="/handle", timeout=3)
    gather.say("Guten Tag, Praxis Dr. Müller. Darf ich Ihren Namen erfahren?")
    resp.append(gather)
    return str(resp)

@app.route("/handle", methods=["POST"])
def handle():
    speech = request.form.get("SpeechResult", "")
    call_id = request.form["CallSid"]
    step = r.hget(call_id, "step").decode()

    resp = VoiceResponse()

    if step == "name":
        r.hset(call_id, mapping={"name": speech, "step": "phone"})
        gather = Gather(input="speech", action="/handle", timeout=3)
        gather.say("Danke. Ihre Telefonnummer bitte.")
        resp.append(gather)
        return str(resp)

    if step == "phone":
        try:
            num = phonenumbers.parse(speech, "DE")
            phone = phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)
        except:
            phone = speech
        r.hset(call_id, mapping={"phone": phone, "step": "date"})
        gather = Gather(input="speech", action="/handle", timeout=3)
        gather.say("Für wann möchten Sie einen Termin?")
        resp.append(gather)
        return str(resp)

    if step == "date":
        date = gpt(f"Extrahiere das Datum aus: {speech}. Antworte nur JJJJ-MM-TT.")
        slots = free_slots(date)
        r.hset(call_id, mapping={"date": date, "slots": ",".join(slots), "step": "time"})
        gather = Gather(input="speech", action="/handle", timeout=3)
        gather.say(f"Ich habe am {date} um {slots[0]} oder {slots[1]} Uhr frei. Welche Zeit passt?")
        resp.append(gather)
        return str(resp)

    if step == "time":
        time = request.form["SpeechResult"]
        mapping = r.hgetall(call_id)
        name = mapping[b"name"].decode()
        phone = mapping[b"phone"].decode()
        date = mapping[b"date"].decode()
        book_cal(date, time, name)
        send_sms(phone, name, date, time)
        with Session() as s:
            s.add(Call(id=call_id, name=name, phone=phone, date=date, time=time, consent=True))
            s.commit()
        resp.say(f"Termin am {date} um {time} Uhr ist gebucht. Sie erhalten gleich eine SMS.")
        return str(resp)

    resp.say("Ich habe Sie nicht verstanden.")
    return str(resp)

# ---------- SIMPLE DASHBOARD ----------
DASH_HTML = """
<!doctype html>
<title>Praxis-Agent Dashboard</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
<div class="container py-4">
  <h2>Termine heute</h2>
  <table class="table table-sm">
    <thead><tr><th>Name</th><th>Telefon</th><th>Datum</th><th>Zeit</th></tr></thead>
    <tbody>
      {% for c in calls %}
        <tr>
          <td>{{ c.name }}</td><td>{{ c.phone }}</td><td>{{ c.date }}</td><td>{{ c.time }}</td>
        </tr>
      {% endfor %}
    </tbody>
  </table>
</div>
"""

@app.route("/dashboard")
def dashboard():
    with Session() as s:
        calls = s.query(Call).filter(Call.date == datetime.today().strftime("%Y-%m-%d")).all()
    return render_template_string(DASH_HTML, calls=calls)
