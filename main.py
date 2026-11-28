"""EventSphere: FastAPI + five JSON-message agents + mobile-friendly website."""
import asyncio
import csv
import io
import json
import math
import os
import secrets
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import qrcode
from starlette.datastructures import URL
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, SignatureExpired, URLSafeSerializer, URLSafeTimedSerializer
from pydantic import BaseModel
from sqlalchemy import String, case, cast, func, or_, select
from sqlalchemy.orm import Session, contains_eager, joinedload, selectinload

from agents import BUFFER_MINUTES, MessageBus, venue_busy_periods
from calendar_file import event_calendar
from database import Base, engine, get_db, lock_row, SessionLocal
from models import (AdminActivity, AgentMessage, AgentState, Booking, Event, HireRequest, Notification,
                    Ticket, TicketCategory, User, Venue, WaitlistEntry, now_utc)
from security import hash_password, verify_password

SECRET_KEY = os.getenv('SECRET_KEY', 'LOCAL-ONLY-CHANGE-ME')
COOKIE_SECURE = os.getenv('COOKIE_SECURE', 'false').lower() == 'true'
LOCAL_TZ = ZoneInfo(os.getenv('APP_TIMEZONE', 'Asia/Colombo'))
auth_signer = URLSafeTimedSerializer(SECRET_KEY, salt='eventsphere-session')
csrf_signer = URLSafeSerializer(SECRET_KEY, salt='eventsphere-csrf')
scan_signer = URLSafeTimedSerializer(SECRET_KEY, salt='eventsphere-scan-result')
templates = Jinja2Templates(directory='templates')


def to_utc(form_time: str) -> datetime:
    """datetime-local is Sri Lanka wall time; store UTC naive in DB."""
    try:
        local = datetime.fromisoformat(form_time).replace(tzinfo=LOCAL_TZ)
    except ValueError as exc:
        raise HTTPException(400, 'Enter a valid date and time') from exc
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def as_local(utc_dt):
    if not utc_dt:
        return ''
    return utc_dt.replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ).strftime('%d %b %Y, %I:%M %p')


def for_input(utc_dt):
    if not utc_dt:
        return ''
    return utc_dt.replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ).strftime('%Y-%m-%dT%H:%M')


def get_user(request: Request, db: Session):
    token = request.cookies.get('session', '')
    try:
        user_id = int(auth_signer.loads(token, max_age=60 * 60 * 24 * 7))
        return db.get(User, user_id)
    except (BadSignature, SignatureExpired, ValueError, TypeError):
        return None


def require_role(request: Request, db: Session, *roles):
    user = get_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail='Please sign in')
    if user.role not in roles:
        raise HTTPException(status_code=403, detail='Forbidden: incorrect role')
    return user


def check_csrf(request: Request, csrf: str):
    try:
        if csrf_signer.loads(csrf) != request.state.visitor:
            raise HTTPException(status_code=403, detail='Invalid CSRF token')
    except BadSignature:
        raise HTTPException(status_code=403, detail='Missing/invalid CSRF token')


def page(request: Request, name: str, db: Session, **context):
    context.update({
        'request': request, 'user': get_user(request, db),
        'csrf': csrf_signer.dumps(request.state.visitor),
        'fmt': as_local, 'fmt_iso': lambda stamp: as_local(datetime.fromisoformat(stamp)), 'for_input': for_input,
        'now': now_utc(),
    })
    return templates.TemplateResponse(request, name, context)


def back(url: str, note: str | None = None):
    return RedirectResponse(URL(url).include_query_params(note=note) if note else url, status_code=303)


async def waitlist_loop():
    while True:
        await asyncio.sleep(30)
        try:
            with SessionLocal() as db:
                MessageBus(db).send('Scheduler','WaitlistAgent','REQUEST',{'action':'sweep'})
                db.commit()
        except Exception as exc:
            # The next sweep retries. In production, check the host logs.
            print('Waitlist sweep error:', type(exc).__name__, str(exc), flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if os.getenv('RENDER') and SECRET_KEY == 'LOCAL-ONLY-CHANGE-ME':
        raise RuntimeError('Set a strong SECRET_KEY environment variable on Render')
    Base.metadata.create_all(bind=engine)
    from seed import seed, update_seed_labels
    # Honor the previous setup setting on existing deployments.
    setup_accounts = os.getenv('SETUP_ACCOUNTS', os.getenv('SEED_DEMO', '')).strip() == '1'
    with SessionLocal() as db:
        if setup_accounts:
            seed(db, sample_data=os.getenv('SEED_SAMPLE_DATA', '').strip() == '1')
        else:
            update_seed_labels(db)
            db.commit()
    task = asyncio.create_task(waitlist_loop())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title='EventSphere Agent-Based Booking', version='1.0', lifespan=lifespan)
app.mount('/static', StaticFiles(directory='static'), name='static')


@app.middleware('http')
async def visitor_cookie(request: Request, call_next):
    visitor = request.cookies.get('visitor') or secrets.token_urlsafe(20)
    request.state.visitor = visitor
    response = await call_next(request)
    if not request.cookies.get('visitor'):
        response.set_cookie('visitor', visitor, max_age=86400 * 30, httponly=True,
                            secure=COOKIE_SECURE, samesite='lax')
    return response


@app.get('/healthz')
def health():
    return {'status':'ok', 'service':'EventSphere'}


class PublicEvent(BaseModel):
    id: int
    title: str
    category: str
    venue: str
    status: str


@app.get('/api/public/events', response_model=list[PublicEvent])
def api_events(db: Session=Depends(get_db)):
    return [PublicEvent(id=e.id,title=e.title,category=e.category,venue=e.venue.name,status=e.status)
            for e in db.scalars(select(Event).where(Event.status=='published')).all()]


@app.get('/', response_class=HTMLResponse)
def home(request: Request, q: str='', category: str='', note: str='', db: Session=Depends(get_db)):
    events=db.scalars(select(Event).where(Event.status=='published').order_by(Event.start_at)).all()
    if q: events=[e for e in events if q.lower() in (e.title + ' ' + e.description).lower()]
    if category: events=[e for e in events if e.category == category]
    cats=sorted(set(db.scalars(select(Event.category).where(Event.status=='published')).all()))
    return page(request,'home.html',db,events=events,categories=cats,q=q,category=category,note=note)


@app.get('/register')
def register_page(request: Request, db: Session=Depends(get_db)):
    return page(request,'auth.html',db,mode='register')


@app.post('/register')
def register(request: Request, name: str=Form(...), email: str=Form(...),
             password: str=Form(...), csrf: str=Form(...), db: Session=Depends(get_db)):
    check_csrf(request,csrf)
    email=email.strip().lower()
    if '@' not in email or len(password)<8 or not name.strip():
        return page(request,'auth.html',db,mode='register',error='Valid email, name and password (8+ chars) required')
    if db.scalar(select(User).where(User.email==email)):
        return page(request,'auth.html',db,mode='register',error='This email is already registered')
    user=User(name=name.strip(),email=email,password_hash=hash_password(password),role='attendee')
    db.add(user); db.commit()
    return back('/login','Account created; please sign in')


@app.get('/login')
def login_page(request: Request, note: str='', db: Session=Depends(get_db),
               venue_request: bool=False):
    return page(request,'auth.html',db,mode='login',note=note,venue_request=venue_request)


@app.post('/login')
def login(request: Request, email: str=Form(...), password: str=Form(...),
          csrf: str=Form(...), db: Session=Depends(get_db), venue_request: bool=Form(False)):
    check_csrf(request,csrf)
    user=db.scalar(select(User).where(User.email==email.strip().lower()))
    if not user or not verify_password(password,user.password_hash):
        return page(request,'auth.html',db,mode='login',error='Incorrect email or password',
                    venue_request=venue_request)
    if user.role == 'attendee' and venue_request:
        response = back('/hire')
    else:
        response=back('/admin' if user.role=='admin' else ('/staff/scanner' if user.role=='staff' else '/'))
    response.set_cookie('session',auth_signer.dumps(str(user.id)),max_age=7*86400,
                        httponly=True,secure=COOKIE_SECURE,samesite='lax')
    return response


@app.post('/logout')
def logout(request: Request, csrf: str=Form(...)):
    check_csrf(request,csrf)
    response=back('/')
    response.delete_cookie('session')
    return response


@app.get('/events/{event_id}')
def event_page(event_id: int, request: Request, note: str='', db: Session=Depends(get_db)):
    event=db.get(Event,event_id)
    if not event or event.status!='published':
        raise HTTPException(404,'Published event not found')
    user = get_user(request, db)
    waitlist = {}
    if user and user.role == 'attendee':
        entries = db.scalars(select(WaitlistEntry).join(TicketCategory).where(
            TicketCategory.event_id == event.id, WaitlistEntry.user_id == user.id,
            WaitlistEntry.status.in_(['waiting', 'offered']))
            .order_by(WaitlistEntry.created_at, WaitlistEntry.id))
        for entry in entries:
            waitlist.setdefault(entry.category_id, entry)
    return page(request, 'event.html', db, event=event, note=note, waitlist=waitlist)


@app.post('/book/{category_id}')
def book(category_id: int,request: Request,quantity: int=Form(...),csrf: str=Form(...),
         db: Session=Depends(get_db), hold_id: int | None=Form(None)):
    check_csrf(request,csrf)
    user=require_role(request,db,'attendee')
    cat=db.get(TicketCategory,category_id)
    if not cat: raise HTTPException(404,'Category not found')
    try:
        answer=MessageBus(db).send('Website','BookingAgent','REQUEST',{
            'action':'book','user_id':user.id,'category_id':category_id,'quantity':quantity,
            'hold_id':hold_id})
        db.commit()
    except Exception:
        db.rollback()
        raise
    if answer['performative']!='AGREE':
        return back(f'/events/{cat.event_id}',answer['content'].get('reason','Booking refused'))
    return back('/bookings',f"Booked! Simulated payment {answer['content']['payment_ref']}")


@app.get('/bookings')
def bookings_page(request: Request,note: str='',db: Session=Depends(get_db)):
    user=require_role(request,db,'attendee')
    bookings=db.scalars(select(Booking).where(Booking.user_id==user.id).order_by(Booking.id.desc())).all()
    return page(request,'bookings.html',db,bookings=bookings,note=note)


@app.get('/bookings/{booking_id}/calendar.ics')
def download_calendar(booking_id: int, request: Request, db: Session=Depends(get_db)):
    user = require_role(request, db, 'attendee')
    booking = db.scalar(select(Booking).join(Booking.event).where(
        Booking.id == booking_id, Booking.user_id == user.id, Booking.status == 'paid',
        Event.status == 'published', Event.end_at >= now_utc(),
    ).options(joinedload(Booking.event).joinedload(Event.venue)))
    if booking is None:
        raise HTTPException(404, 'Calendar download unavailable for this booking')
    return Response(event_calendar(booking.event), media_type='text/calendar', headers={
        'Content-Disposition': f'attachment; filename="event-{booking.event_id}.ics"',
        'Cache-Control': 'private, no-store',
    })


@app.post('/bookings/{booking_id}/cancel')
def cancel_booking(booking_id: int,request: Request,csrf: str=Form(...),db: Session=Depends(get_db)):
    check_csrf(request,csrf)
    user=require_role(request,db,'attendee')
    answer=MessageBus(db).send('Website','BookingAgent','REQUEST',{
        'action':'cancel','booking_id':booking_id,'user_id':user.id})
    db.commit()
    text=answer['content'].get('reason','Cancelled with simulated refund')
    if answer['performative']=='AGREE':text=f"Cancelled. Refund LKR {answer['content']['refund_amount']:,.2f}"
    return back('/bookings',text)


@app.get('/tickets/{ticket_id}/qr')
def ticket_qr(ticket_id: int,request: Request,db: Session=Depends(get_db)):
    user=require_role(request,db,'attendee','admin','staff')
    ticket=db.get(Ticket,ticket_id)
    if not ticket: raise HTTPException(404,'Ticket not found')
    if user.role=='attendee' and ticket.booking.user_id!=user.id:
        raise HTTPException(403,'Cannot see another attendee ticket')
    image=qrcode.make(ticket.code)
    output=io.BytesIO(); image.save(output,format='PNG'); output.seek(0)
    return StreamingResponse(output,media_type='image/png')


@app.post('/waitlist/{category_id}')
def join_waitlist(category_id: int,request: Request,quantity: int=Form(1),csrf: str=Form(...),
                  db: Session=Depends(get_db)):
    check_csrf(request,csrf)
    user=require_role(request,db,'attendee')
    cat=db.get(TicketCategory,category_id)
    if not cat:raise HTTPException(404,'Category not found')
    answer=MessageBus(db).send('Website','WaitlistAgent','REQUEST',{
        'action':'join','user_id':user.id,'category_id':category_id,'quantity':quantity})
    db.commit()
    return back(f'/events/{cat.event_id}',answer['content'].get('reason','Waitlist updated'))


@app.post('/waitlist/{entry_id}/leave')
def leave_waitlist(entry_id: int, request: Request, csrf: str=Form(...), db: Session=Depends(get_db)):
    check_csrf(request, csrf)
    user = require_role(request, db, 'attendee')
    entry = db.get(WaitlistEntry, entry_id)
    if entry is None or entry.user_id != user.id:
        raise HTTPException(404, 'Waitlist entry not found')
    event = entry.category.event
    answer = MessageBus(db).send('Website', 'WaitlistAgent', 'REQUEST', {
        'action': 'leave', 'entry_id': entry_id, 'user_id': user.id,
    })
    db.commit()
    target = f'/events/{event.id}' if event.status == 'published' else '/notifications'
    return back(target, answer['content']['reason'])


@app.get('/notifications')
def notifications(request: Request, db: Session=Depends(get_db)):
    user=require_role(request,db,'attendee','admin','staff')
    rows=db.scalars(select(Notification).where(Notification.user_id==user.id)
                    .order_by(Notification.id.desc()).limit(100)).all()
    instant = now_utc()
    offers = db.scalars(select(WaitlistEntry).join(TicketCategory).join(Event).where(
        WaitlistEntry.user_id == user.id, WaitlistEntry.status == 'offered',
        WaitlistEntry.expires_at > instant, Event.status == 'published',
        Event.start_at > instant)).all() if user.role == 'attendee' else []
    return page(request,'notifications.html',db,notifications=rows,offers=offers)


@app.get('/hire')
def hire_page(request: Request,note: str='',db: Session=Depends(get_db)):
    if get_user(request, db) is None:
        return back('/login?venue_request=1')
    user=require_role(request,db,'attendee')
    venues=db.scalars(select(Venue).where(Venue.hire_open.is_(True))).all()
    hires=db.scalars(select(HireRequest).where(HireRequest.user_id==user.id)
                     .order_by(HireRequest.id.desc())).all()
    return page(request,'hire.html',db,venues=venues,hires=hires,note=note,json_loads=json.loads)


@app.get('/hire/availability')
def hire_availability(request: Request, venue_id: int | None=None, day: date | None=None,
                      db: Session=Depends(get_db)):
    if get_user(request, db) is None:
        return back('/login?venue_request=1')
    require_role(request, db, 'attendee')
    venues = db.scalars(select(Venue).where(Venue.hire_open.is_(True)).order_by(Venue.name, Venue.id)).all()
    venue = next((room for room in venues if room.id == venue_id), None) if venue_id is not None else (
        venues[0] if venues else None)
    if venue_id is not None and venue is None:
        raise HTTPException(404, 'Venue not available for private hire')
    instant = now_utc()
    today = instant.replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ).date()
    day = day or today
    if day < today or day == date.max:
        return page(request, 'hire_availability.html', db, venues=venues, venue=venue,
                    day=day, today=today, slots=[], error='Choose today or a future date.')
    slots = []
    if venue is not None:
        start = max(to_utc(datetime.combine(day, time.min).isoformat()), instant)
        end = to_utc(datetime.combine(day + timedelta(days=1), time.min).isoformat())
        buffer = timedelta(minutes=BUFFER_MINUTES)
        occupied = venue_busy_periods(venue.id, start - buffer, end + buffer).order_by('start_at')
        for busy_start, busy_end in db.execute(occupied):
            busy_start = max(start, busy_start - buffer)
            busy_end = min(end, busy_end + buffer)
            if slots and busy_start <= slots[-1][1]:
                slots[-1] = (slots[-1][0], max(slots[-1][1], busy_end), 'Busy')
            else:
                slots.append((busy_start, busy_end, 'Busy'))
        timeline = []
        for busy_start, busy_end, status in slots:
            if start < busy_start:
                timeline.append((start, busy_start, 'Available'))
            timeline.append((busy_start, busy_end, status))
            start = busy_end
        if start < end:
            timeline.append((start, end, 'Available'))
        slots = timeline
    return page(request, 'hire_availability.html', db, venues=venues, venue=venue, day=day,
                today=today, slots=slots, buffer_minutes=BUFFER_MINUTES)


@app.post('/hire')
def request_hire(request: Request,venue_id: int=Form(...),start_at: str=Form(...),
                 end_at: str=Form(...),purpose: str=Form(...),guest_count: int=Form(...),
                 csrf: str=Form(...),db: Session=Depends(get_db)):
    check_csrf(request,csrf)
    user=require_role(request,db,'attendee')
    answer=MessageBus(db).send('Website','VenueAgent','REQUEST',{
        'action':'request_hire','user_id':user.id,'venue_id':venue_id,
        'start_at':to_utc(start_at).isoformat(),'end_at':to_utc(end_at).isoformat(),
        'purpose':purpose,'guest_count':guest_count})
    db.commit()
    txt=answer['content'].get('reason') or ('Hire confirmed' if answer['performative']=='AGREE' else 'Decision saved')
    return back('/hire',txt)


@app.post('/hire/{hire_id}/accept/{index}')
def accept_hire(hire_id: int,index: int,request: Request,csrf: str=Form(...),
                db: Session=Depends(get_db)):
    check_csrf(request,csrf)
    user=require_role(request,db,'attendee')
    hire=lock_row(db, HireRequest, hire_id)
    if not hire or hire.user_id!=user.id or hire.status!='proposed':
        raise HTTPException(404,'Proposal not found')
    options=json.loads(hire.alternatives_json)
    if index<0 or index>=len(options):raise HTTPException(400,'Invalid proposal')
    option=options[index]
    answer=MessageBus(db).send('Website','VenueAgent','REQUEST',{
        'action':'accept_alternative','hire_id':hire.id,'user_id':user.id,
        'venue_id':option['venue_id'],'start_at':option['start_at'],
        'end_at':option['end_at']})
    db.commit()
    return back('/hire',answer['content'].get('reason') or f"Hire status: {answer['content'].get('status','updated')}")


@app.get('/staff/scanner')
def scanner(request: Request, db: Session=Depends(get_db), event_id: int | None=None, result: str=''):
    user = require_role(request,db,'staff','admin')
    events=db.scalars(select(Event).where(Event.status=='published').order_by(Event.start_at)).all()
    scan_result = None
    if result:
        try:
            user_id, scanned_event, admitted, reason = scan_signer.loads(result, max_age=120)
            if user_id == user.id and scanned_event == event_id:
                scan_result = {'admitted': admitted, 'reason': reason}
        except BadSignature:
            pass
    return page(request, 'scanner.html', db, events=events, selected_event_id=event_id,
                scan_result=scan_result)


@app.post('/staff/checkin')
def checkin(request: Request,event_id: int=Form(...),code: str=Form(...),
            csrf: str=Form(...),db: Session=Depends(get_db), camera: bool=Form(False)):
    check_csrf(request,csrf)
    user = require_role(request,db,'staff','admin')
    answer=MessageBus(db).send('Scanner','TicketAgent','REQUEST',{
        'action':'checkin','event_id':event_id,'code':code})
    db.commit()
    admitted = answer['performative'] == 'AGREE'
    reason = 'Valid ticket' if admitted else answer['content'].get('reason', 'Ticket could not be checked in')
    if camera:
        return JSONResponse({'admitted': admitted, 'reason': reason}, headers={'Cache-Control': 'no-store'})
    result = scan_signer.dumps([user.id, event_id, admitted, reason])
    location = URL('/staff/scanner').include_query_params(event_id=event_id, result=result)
    return RedirectResponse(location, status_code=303, headers={'Cache-Control': 'no-store'})


def event_status_at(instant: datetime):
    return case(
        (Event.status == 'draft', 'draft'),
        (Event.status == 'cancelled', 'cancelled'),
        (Event.end_at < instant, 'ended'),
        (Event.start_at <= instant, 'in-progress'),
        else_='upcoming',
    )


@app.get('/admin')
def admin(request: Request, db: Session=Depends(get_db), note: str='',
          conversation_id: str='', before: int | None=None, q: str='', status: str='',
          activity: bool=False):
    require_role(request, db, 'admin')
    event_status = event_status_at(now_utc())
    sold = case((Booking.status == 'paid', Booking.quantity), else_=0)
    sales = select(
        Booking.event_id,
        func.sum(sold).label('sold'),
        func.sum(Booking.total - Booking.refund_amount).label('revenue'),
    ).group_by(Booking.event_id).subquery()
    attendance = select(
        Booking.event_id, func.count(Ticket.id).label('checkins'),
    ).join(Ticket).where(Ticket.status == 'checked_in').group_by(Booking.event_id).subquery()
    events = (
        select(Event, event_status.label('status'), func.coalesce(sales.c.sold, 0).label('sold'),
               func.coalesce(sales.c.revenue, 0).label('revenue'),
               func.coalesce(attendance.c.checkins, 0).label('checkins'))
        .options(joinedload(Event.venue))
        .outerjoin(sales, sales.c.event_id == Event.id)
        .outerjoin(attendance, attendance.c.event_id == Event.id)
        .order_by(Event.start_at.desc(), Event.id.desc())
    )
    q = q.strip()
    if q:
        events = events.where(Event.title.icontains(q, autoescape=True))
    if status:
        events = events.where(event_status == status)
    stats = [{'event': event, 'status': state, 'sold': sold_count, 'revenue': revenue, 'checkins': checkins}
             for event, state, sold_count, revenue, checkins in db.execute(events)]
    pending_hires = db.scalar(select(func.count()).select_from(HireRequest).where(HireRequest.status == 'pending'))
    log_query = select(AgentMessage)
    if conversation_id:
        log_query = log_query.where(AgentMessage.conversation_id == conversation_id)
    if before is not None:
        log_query = log_query.where(AgentMessage.id < before)
    messages = db.scalars(log_query.order_by(AgentMessage.id.desc()).limit(121)).all()
    older_than = messages[119].id if len(messages) > 120 else None
    states=db.scalars(select(AgentState).order_by(AgentState.agent,AgentState.key).limit(100)).all()
    return page(request, 'admin.html', db, stats=stats, messages=messages[:120], states=states,
                note=note, conversation_id=conversation_id, before=before, older_than=older_than,
                q=q, status=status, pending_hires=pending_hires,
                show_activity=activity or bool(conversation_id) or before is not None)


@app.get('/admin/activity')
def admin_activity(request: Request, db: Session=Depends(get_db), before: int | None=None):
    require_role(request, db, 'admin')
    query = select(AdminActivity).options(joinedload(AdminActivity.admin))
    if before is not None:
        query = query.where(AdminActivity.id < before)
    entries = db.scalars(query.order_by(AdminActivity.id.desc()).limit(31)).all()
    response = page(request, 'admin_activity.html', db, entries=entries[:30], before=before,
                    older_than=entries[29].id if len(entries) > 30 else None)
    response.headers['Cache-Control'] = 'private, no-store'
    return response


@app.get('/admin/events/{event_id}/manage')
def manage_event(event_id: int, request: Request, db: Session=Depends(get_db), q: str='',
                 before: int | None=None, note: str=''):
    require_role(request, db, 'admin')
    instant = now_utc()
    event_row = db.execute(
        select(Event, event_status_at(instant)).where(Event.id == event_id)
        .options(joinedload(Event.venue), selectinload(Event.ticket_categories))
    ).one_or_none()
    if event_row is None:
        raise HTTPException(404, 'Event not found')
    event, status = event_row
    checked_in = (
        select(Ticket.booking_id, func.count(Ticket.id).label('count'))
        .join(Ticket.booking)
        .where(Booking.event_id == event.id, Booking.status == 'paid', Ticket.status == 'checked_in')
        .group_by(Ticket.booking_id)
        .subquery()
    )
    checkins_per_booking = func.coalesce(checked_in.c.count, 0)
    category_sales = {
        row.category_id: row for row in db.execute(
            select(Booking.category_id,
                   func.sum(case((Booking.status == 'paid', Booking.quantity), else_=0)).label('sold'),
                   func.sum(checkins_per_booking).label('checkins'),
                   func.sum(Booking.refund_amount).label('refunds'),
                   func.sum(Booking.total - Booking.refund_amount).label('revenue'))
            .outerjoin(checked_in, checked_in.c.booking_id == Booking.id)
            .where(Booking.event_id == event.id).group_by(Booking.category_id)
        )
    }
    bookings = (
        select(Booking, checkins_per_booking).join(Booking.user)
        .outerjoin(checked_in, checked_in.c.booking_id == Booking.id)
        .where(Booking.event_id == event.id)
        .options(contains_eager(Booking.user), joinedload(Booking.category))
    )
    q = q.strip()
    if q:
        bookings = bookings.where(or_(User.name.icontains(q, autoescape=True),
                                      Booking.payment_ref.icontains(q, autoescape=True),
                                      cast(Booking.id, String) == q.removeprefix('#')))
    if before is not None:
        bookings = bookings.where(Booking.id < before)
    rows = db.execute(bookings.order_by(Booking.id.desc()).limit(31)).all()
    entries = []
    if status == 'upcoming':
        entries = db.scalars(
            select(WaitlistEntry).join(WaitlistEntry.category)
            .where(TicketCategory.event_id == event.id,
                   or_(WaitlistEntry.status == 'waiting',
                       (WaitlistEntry.status == 'offered') & (WaitlistEntry.expires_at > instant)))
            .options(joinedload(WaitlistEntry.user), contains_eager(WaitlistEntry.category))
            .order_by(WaitlistEntry.created_at, WaitlistEntry.id)
        ).all()
    response = page(
        request, 'admin_event.html', db, event=event, event_status=status, category_sales=category_sales,
        categories=sorted(event.ticket_categories, key=lambda category: category.id),
        total_checkins=sum(row.checkins for row in category_sales.values()),
        total_refunds=sum(row.refunds for row in category_sales.values()),
        net_revenue=sum(row.revenue for row in category_sales.values()),
        bookings=rows[:30], waitlist=entries, q=q, before=before,
        older_than=rows[29][0].id if len(rows) > 30 else None, note=note,
    )
    response.headers['Cache-Control'] = 'private, no-store'
    return response


def csv_text(value: str) -> str:
    """Keep spreadsheet programs from interpreting text fields as formulas."""
    if value.startswith(('\t', '\r', '\n')) or value.lstrip().startswith(('=', '+', '-', '@', '＝', '＋', '－', '＠')):
        return "'" + value
    return value


@app.get('/admin/events/{event_id}/attendance.csv')
def download_attendance(event_id: int, request: Request, db: Session=Depends(get_db)):
    require_role(request, db, 'admin')
    event = db.get(Event, event_id)
    if event is None or event.status != 'published':
        raise HTTPException(404, 'Published event not found')
    rows = db.execute(
        select(Ticket.id, User.name, TicketCategory.name, Ticket.status, Ticket.checked_in_at)
        .select_from(Ticket).join(Ticket.booking).join(Booking.user).join(Booking.category)
        .where(Booking.event_id == event.id, Booking.status == 'paid')
        .order_by(Ticket.id)
    )
    output = io.StringIO(newline='')
    writer = csv.writer(output)
    writer.writerow(['Ticket number', 'Booked by', 'Ticket category', 'Attendance', f'Check-in time ({LOCAL_TZ.key})'])
    for ticket_id, name, category, status, checked_in_at in rows:
        timestamp = checked_in_at.replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ) if checked_in_at else None
        writer.writerow([
            ticket_id, csv_text(name), csv_text(category),
            'Checked in' if status == 'checked_in' else 'Not checked in',
            timestamp.isoformat(timespec='seconds') if timestamp else '',
        ])
    return Response(output.getvalue().encode('utf-8-sig'), media_type='text/csv', headers={
        'Content-Disposition': f'attachment; filename="event-{event.id}-attendance.csv"',
        'Cache-Control': 'private, no-store',
    })


@app.get('/admin/venues')
def venues_admin(request: Request,db: Session=Depends(get_db)):
    require_role(request,db,'admin')
    return page(request,'venues.html',db,venues=db.scalars(select(Venue).order_by(Venue.id)).all())


@app.get('/admin/venues/new')
def venue_create_page(request: Request,db: Session=Depends(get_db)):
    require_role(request,db,'admin')
    return page(request,'venue_form.html',db,venue=None)


@app.get('/admin/venues/{venue_id}/edit')
def venue_edit_page(venue_id: int,request: Request,db: Session=Depends(get_db)):
    require_role(request,db,'admin')
    venue=db.get(Venue,venue_id)
    if not venue:raise HTTPException(404,'Venue not found')
    return page(request,'venue_form.html',db,venue=venue)


@app.post('/admin/venues/save')
def venue_save(request: Request,venue_id: int=Form(0),name: str=Form(...),
               location: str=Form(...),capacity: int=Form(...),description: str=Form(''),
               image_url: str=Form(''),hourly_rate: float=Form(...),hire_open: bool=Form(False),
               csrf: str=Form(...),db: Session=Depends(get_db)):
    check_csrf(request,csrf)
    require_role(request,db,'admin')
    if capacity<1 or hourly_rate<0:raise HTTPException(400,'Invalid capacity or hourly price')
    venue=lock_row(db, Venue, venue_id) if venue_id else Venue()
    if venue_id and not venue:raise HTTPException(404,'Venue not found')
    if venue_id:
        over_capacity = db.scalar(select(Event.id).join(TicketCategory).where(
            Event.venue_id == venue.id, Event.status.in_(['draft', 'published']))
            .group_by(Event.id).having(func.sum(TicketCategory.quantity) > capacity).limit(1))
        if over_capacity is not None:
            raise HTTPException(409, 'Capacity is below the ticket allocation of an existing event')
        over_capacity_hire = db.scalar(select(HireRequest.id).where(
            HireRequest.venue_id == venue.id, HireRequest.status.in_(['confirmed', 'pending']),
            HireRequest.end_at > now_utc(), HireRequest.guest_count > capacity).limit(1))
        if over_capacity_hire is not None:
            raise HTTPException(409, 'Capacity is below the guest count of an existing hire')
    if not venue_id:db.add(venue)
    venue.name=name.strip();venue.location=location.strip();venue.capacity=capacity
    venue.description=description;venue.image_url=image_url;venue.hourly_rate=hourly_rate
    venue.hire_open=hire_open
    db.commit()
    return back('/admin/venues')


@app.get('/admin/events/{event_id}/edit')
@app.get('/admin/events/new')
def event_form(request: Request, db: Session=Depends(get_db), event_id: int | None=None):
    require_role(request, db, 'admin')
    event = None
    tickets = []
    if event_id is not None:
        event = db.get(Event, event_id)
        if event is None or event.status != 'draft':
            raise HTTPException(404, 'Draft event not found')
        tickets = sorted(event.ticket_categories, key=lambda ticket: ticket.id)
    venues = db.scalars(select(Venue).order_by(Venue.name, Venue.id)).all()
    return page(request, 'event_form.html', db, venues=venues, event=event, tickets=tickets)


@app.post('/admin/events/{event_id}/edit')
@app.post('/admin/events/new')
def event_save(request: Request,title: str=Form(...),category: str=Form(...),
                 venue_id: int=Form(...),start_at: str=Form(...),end_at: str=Form(...),
                 description: str=Form(''),status: str=Form('draft'),
                 ticket1_name: str=Form(...),ticket1_price: float=Form(...),ticket1_quantity: int=Form(...),
                 ticket2_name: str=Form(''),ticket2_price: float=Form(0),ticket2_quantity: int=Form(0),
                 csrf: str=Form(...),db: Session=Depends(get_db), event_id: int | None=None):
    check_csrf(request,csrf)
    require_role(request,db,'admin')
    event = None
    if event_id is not None:
        event = lock_row(db, Event, event_id)
        if event is None or event.status != 'draft':
            raise HTTPException(404, 'Draft event not found')
        if status != 'draft':
            raise HTTPException(400, 'Save changes before publishing the draft')
    existing = sorted(event.ticket_categories, key=lambda ticket: ticket.id) if event else []
    if len(existing) > 2:
        raise HTTPException(409, 'This draft has more than two ticket categories')
    venue=lock_row(db, Venue, venue_id)
    if not venue:raise HTTPException(404,'Venue not found')
    if status not in ('draft','published'):raise HTTPException(400,'Incorrect event status')
    starts=to_utc(start_at);ends=to_utc(end_at)
    if ends<=starts:raise HTTPException(400,'End time must be later')
    if (ticket1_quantity <= 0 or ticket2_quantity < 0
            or not all(math.isfinite(price) and price >= 0 for price in (ticket1_price, ticket2_price))):
        raise HTTPException(400,'Invalid ticket quantity/price')
    title, category = title.strip(), category.strip()
    if not 1 <= len(title) <= 180 or not 1 <= len(category) <= 80:
        raise HTTPException(400, 'Enter a title and category within the allowed lengths')
    allocations = [(ticket1_name.strip(), ticket1_price, ticket1_quantity)]
    if ticket2_quantity > 0:
        allocations.append((ticket2_name.strip(), ticket2_price, ticket2_quantity))
    if any(not 1 <= len(name) <= 100 for name, _, _ in allocations):
        raise HTTPException(400, 'Enter a name of up to 100 characters for each ticket category')
    ticket_quantity = ticket1_quantity + ticket2_quantity
    if ticket_quantity > venue.capacity:
        raise HTTPException(400,'Ticket quantities exceed venue capacity')
    if status == 'published':
        decision = MessageBus(db).send('Admin', 'VenueAgent', 'REQUEST', {
            'action': 'check_event', 'venue_id': venue_id, 'title': title,
            'start_at': starts.isoformat(), 'end_at': ends.isoformat(),
            'ticket_quantity': ticket_quantity})
        if decision['performative'] != 'AGREE':
            db.commit()
            raise HTTPException(409, decision['content'].get('reason', 'Venue conflict'))
    if event is None:
        event = Event(status=status)
        db.add(event)
    event.title = title
    event.category = category
    event.venue_id = venue.id
    event.start_at, event.end_at = starts, ends
    event.description = description
    for index, (name, price, quantity) in enumerate(allocations):
        if index < len(existing):
            ticket = existing[index]
        else:
            ticket = TicketCategory()
            event.ticket_categories.append(ticket)
        ticket.name, ticket.price = name, price
        ticket.quantity = ticket.remaining = quantity
    for ticket in existing[len(allocations):]:
        event.ticket_categories.remove(ticket)
    db.commit()
    if event_id is not None:
        return back(f'/admin/events/{event.id}/manage', 'Draft event updated')
    return back('/admin', 'Event created as ' + status)


@app.post('/admin/events/{event_id}/publish')
def event_publish(event_id: int, request: Request, csrf: str=Form(...), db: Session=Depends(get_db),
                  from_manage: bool=Form(False)):
    check_csrf(request,csrf)
    require_role(request,db,'admin')
    event=lock_row(db, Event, event_id)
    if not event or event.status!='draft':raise HTTPException(400,'Draft event not found')
    target = f'/admin/events/{event_id}/manage' if from_manage else '/admin'
    decision=MessageBus(db).send('Admin','VenueAgent','REQUEST',{
        'action':'check_event','event_id':event.id,'venue_id':event.venue_id,
        'start_at':event.start_at.isoformat(),'end_at':event.end_at.isoformat(),
        'ticket_quantity':sum(category.quantity for category in event.ticket_categories)})
    if decision['performative']!='AGREE':
        db.commit();return back(target,decision['content'].get('reason','Venue conflict'))
    event.status='published';db.commit()
    return back(target,'Event published')


def schedule_version(event: Event) -> str:
    return f'{event.venue_id}/{event.start_at.isoformat()}/{event.end_at.isoformat()}'


@app.api_route('/admin/events/{event_id}/schedule', methods=['GET', 'POST'], include_in_schema=False)
def event_schedule(event_id: int, request: Request, db: Session=Depends(get_db), note: str='',
                   csrf: str=Form(''), venue_id: int=Form(0), start_at: str=Form(''),
                   end_at: str=Form(''), version: str=Form('')):
    admin_user = require_role(request, db, 'admin')
    saving = request.method == 'POST'
    if saving:
        check_csrf(request, csrf)
    event = lock_row(db, Event, event_id) if saving else db.get(Event, event_id)
    if event is None or event.status != 'published':
        raise HTTPException(404, 'Published event not found')
    manage_url = f'/admin/events/{event.id}/manage'
    instant = now_utc()
    has_checkins = db.scalar(select(Ticket.id).join(Ticket.booking).where(
        Booking.event_id == event.id, Ticket.status == 'checked_in').limit(1))
    if event.start_at <= instant or has_checkins is not None:
        return back(manage_url, 'Schedule changes close when the event starts or the first ticket is checked in.')
    current_version = schedule_version(event)
    if not saving:
        venues = db.scalars(select(Venue).order_by(Venue.name, Venue.id)).all()
        return page(request, 'event_schedule.html', db, event=event, venues=venues,
                    version=current_version, note=note)

    form_url = f'/admin/events/{event.id}/schedule'
    if version != current_version:
        return back(form_url, 'The schedule changed while you were editing. Review the latest details.')
    starts = event.start_at if start_at == for_input(event.start_at) else to_utc(start_at)
    ends = event.end_at if end_at == for_input(event.end_at) else to_utc(end_at)
    if (venue_id, starts, ends) == (event.venue_id, event.start_at, event.end_at):
        return back(manage_url, 'The event schedule is already up to date.')
    if starts <= instant or ends <= starts:
        return back(form_url, 'Choose a future start time and a later end time.')
    hold_until = db.scalar(select(func.max(WaitlistEntry.expires_at)).join(WaitlistEntry.category).where(
        TicketCategory.event_id == event.id, WaitlistEntry.status == 'offered'))
    if hold_until and starts <= hold_until:
        return back(form_url, 'Choose a start time after the current ticket holds expire.')

    bus = MessageBus(db)
    decision = bus.send('Admin', 'VenueAgent', 'REQUEST', {
        'action': 'check_event', 'event_id': event.id, 'venue_id': venue_id,
        'start_at': starts.isoformat(), 'end_at': ends.isoformat(),
        'ticket_quantity': sum(ticket.quantity for ticket in event.ticket_categories),
    })
    if decision['performative'] != 'AGREE':
        db.commit()
        return back(form_url, decision['content'].get('reason', 'Venue unavailable'))
    if min(event.start_at, starts) <= now_utc():
        return back(manage_url, 'The event start time has arrived. Its schedule can no longer be changed.')

    venue = db.get(Venue, venue_id)
    old_schedule = f'{event.venue.name}, {event.venue.location} from {as_local(event.start_at)} to {as_local(event.end_at)}'
    new_schedule = f'{venue.name}, {venue.location} from {as_local(starts)} to {as_local(ends)}'
    event.venue = venue
    event.start_at, event.end_at = starts, ends
    recipients = db.scalars(select(Booking.user_id).where(
        Booking.event_id == event.id, Booking.status == 'paid').distinct()).all()
    for user_id in recipients:
        bus.send('Admin', 'ALL', 'INFORM', {
            'user_id': user_id,
            'text': f'{event.title} schedule changed. Previous: {old_schedule}. New: {new_schedule}. Times are in Sri Lanka.',
        })
    db.add(AdminActivity(admin_id=admin_user.id, action='Updated event schedule',
                         details=f'{event.title} (event #{event.id}). Previous: {old_schedule}. New: {new_schedule}.'))
    db.commit()
    return back(manage_url, f'Event schedule updated. {len(recipients)} attendee(s) notified.')


@app.post('/admin/events/{event_id}/cancel')
def event_cancel(event_id: int, request: Request, csrf: str=Form(...), db: Session=Depends(get_db),
                 from_manage: bool=Form(False)):
    check_csrf(request,csrf)
    admin_user = require_role(request,db,'admin')
    event=lock_row(db, Event, event_id)
    if not event or event.status!='published':raise HTTPException(400,'Published event not found')
    if now_utc() > event.end_at:
        raise HTTPException(409, 'Cannot cancel an event that has ended')
    event.status='cancelled'
    answer=MessageBus(db).send('Admin','BookingAgent','REQUEST',
                               {'action':'event_cancelled','event_id':event_id})
    refunds = answer['content']['refunded_bookings']
    db.add(AdminActivity(admin_id=admin_user.id, action='Cancelled event',
                         details=f'{event.title} (event #{event.id}). Simulated refunds issued: {refunds}.'))
    db.commit()
    target = f'/admin/events/{event.id}/manage' if from_manage else '/admin'
    return back(target, f'Event cancelled; full refunds: {refunds}')


@app.get('/admin/hires')
def admin_hires(request: Request, db: Session=Depends(get_db), status: str=''):
    require_role(request,db,'admin')
    query = select(HireRequest).order_by(HireRequest.id.desc())
    if status == 'pending':
        query = query.where(HireRequest.status == 'pending')
    return page(request, 'admin_hires.html', db, hires=db.scalars(query).all(), pending_only=status == 'pending')


@app.post('/admin/hires/{hire_id}/{decision}')
def decide_hire(hire_id: int,decision: str,request: Request,csrf: str=Form(...),
                db: Session=Depends(get_db)):
    check_csrf(request,csrf)
    admin_user = require_role(request,db,'admin')
    hire=lock_row(db, HireRequest, hire_id)
    if not hire or hire.status!='pending' or decision not in ('approve','reject'):
        raise HTTPException(400,'Invalid pending request/decision')
    hire.status='confirmed' if decision=='approve' else 'rejected'
    MessageBus(db).send('Admin','ALL','INFORM',{
        'user_id':hire.user_id,'text':f'Private hire #{hire.id} {hire.status} by admin.'})
    action = 'Approved venue hire' if decision == 'approve' else 'Rejected venue hire'
    db.add(AdminActivity(admin_id=admin_user.id, action=action,
                         details=f'Hire #{hire.id} at {hire.venue.name} on {as_local(hire.start_at)}.'))
    db.commit()
    return back('/admin/hires')
