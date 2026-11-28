"""EventSphere: FastAPI + five JSON-message agents + mobile-friendly website."""
import asyncio
import io
import json
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

import qrcode
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, SignatureExpired, URLSafeSerializer, URLSafeTimedSerializer
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agents import MessageBus
from database import Base, engine, get_db, lock_row, SessionLocal
from models import (AgentMessage, AgentState, Booking, Event, HireRequest, Notification,
                    Ticket, TicketCategory, User, Venue, WaitlistEntry, now_utc)
from security import hash_password, verify_password

SECRET_KEY = os.getenv('SECRET_KEY', 'LOCAL-ONLY-CHANGE-ME')
COOKIE_SECURE = os.getenv('COOKIE_SECURE', 'false').lower() == 'true'
LOCAL_TZ = ZoneInfo(os.getenv('APP_TIMEZONE', 'Asia/Colombo'))
auth_signer = URLSafeTimedSerializer(SECRET_KEY, salt='eventsphere-session')
csrf_signer = URLSafeSerializer(SECRET_KEY, salt='eventsphere-csrf')
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
    return RedirectResponse(url + ('?note=' + quote(note) if note else ''), status_code=303)


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
    holds = {}
    if user and user.role == 'attendee':
        offers = db.scalars(select(WaitlistEntry).join(TicketCategory).where(
            TicketCategory.event_id == event.id, WaitlistEntry.user_id == user.id,
            WaitlistEntry.status == 'offered', WaitlistEntry.expires_at > now_utc())
            .order_by(WaitlistEntry.created_at, WaitlistEntry.id))
        for offer in offers:
            holds.setdefault(offer.category_id, offer)
    return page(request, 'event.html', db, event=event, note=note, holds=holds)


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
def scanner(request: Request,note: str='',db: Session=Depends(get_db)):
    require_role(request,db,'staff','admin')
    events=db.scalars(select(Event).where(Event.status=='published').order_by(Event.start_at)).all()
    return page(request,'scanner.html',db,events=events,note=note)


@app.post('/staff/checkin')
def checkin(request: Request,event_id: int=Form(...),code: str=Form(...),
            csrf: str=Form(...),db: Session=Depends(get_db)):
    check_csrf(request,csrf)
    require_role(request,db,'staff','admin')
    answer=MessageBus(db).send('Scanner','TicketAgent','REQUEST',{
        'action':'checkin','event_id':event_id,'code':code})
    db.commit()
    label='ADMIT' if answer['performative']=='AGREE' else 'REJECT'
    return back('/staff/scanner',label+': '+answer['content'].get('reason','Check complete'))


@app.get('/admin')
def admin(request: Request, db: Session=Depends(get_db), note: str='',
          conversation_id: str='', before: int | None=None):
    require_role(request,db,'admin')
    events=db.scalars(select(Event).order_by(Event.id.desc())).all()
    stats=[]
    for event in events:
        bookings=db.scalars(select(Booking).where(Booking.event_id==event.id)).all()
        tickets=db.scalars(select(Ticket).join(Booking).where(Booking.event_id==event.id)).all()
        stats.append({'event':event,
                      'sold':sum(b.quantity for b in bookings if b.status=='paid'),
                      'revenue':sum(b.total-b.refund_amount for b in bookings),
                      'checkins':sum(1 for t in tickets if t.status=='checked_in'),
                      'capacity':event.venue.capacity})
    log_query = select(AgentMessage)
    if conversation_id:
        log_query = log_query.where(AgentMessage.conversation_id == conversation_id)
    if before is not None:
        log_query = log_query.where(AgentMessage.id < before)
    messages = db.scalars(log_query.order_by(AgentMessage.id.desc()).limit(121)).all()
    older_than = messages[119].id if len(messages) > 120 else None
    states=db.scalars(select(AgentState).order_by(AgentState.agent,AgentState.key).limit(100)).all()
    return page(request, 'admin.html', db, stats=stats, messages=messages[:120], states=states,
                note=note, conversation_id=conversation_id, before=before, older_than=older_than)


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


@app.get('/admin/events/new')
def event_create_page(request: Request,db: Session=Depends(get_db)):
    require_role(request,db,'admin')
    venues=db.scalars(select(Venue)).all()
    return page(request,'event_form.html',db,venues=venues)


@app.post('/admin/events/new')
def event_create(request: Request,title: str=Form(...),category: str=Form(...),
                 venue_id: int=Form(...),start_at: str=Form(...),end_at: str=Form(...),
                 description: str=Form(''),status: str=Form('draft'),
                 ticket1_name: str=Form(...),ticket1_price: float=Form(...),ticket1_quantity: int=Form(...),
                 ticket2_name: str=Form(''),ticket2_price: float=Form(0),ticket2_quantity: int=Form(0),
                 csrf: str=Form(...),db: Session=Depends(get_db)):
    check_csrf(request,csrf)
    require_role(request,db,'admin')
    venue=lock_row(db, Venue, venue_id)
    if not venue:raise HTTPException(404,'Venue not found')
    if status not in ('draft','published'):raise HTTPException(400,'Incorrect event status')
    starts=to_utc(start_at);ends=to_utc(end_at)
    if ends<=starts:raise HTTPException(400,'End time must be later')
    if ticket1_quantity<=0 or ticket1_price<0 or ticket2_quantity<0 or ticket2_price<0:
        raise HTTPException(400,'Invalid ticket quantity/price')
    if not ticket1_name.strip() or (ticket2_quantity > 0 and not ticket2_name.strip()):
        raise HTTPException(400, 'Enter a name for each ticket category')
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
    event=Event(title=title,category=category,venue_id=venue_id,start_at=starts,end_at=ends,
                description=description,status=status)
    db.add(event);db.flush()
    db.add(TicketCategory(event_id=event.id,name=ticket1_name,price=ticket1_price,
                          quantity=ticket1_quantity,remaining=ticket1_quantity))
    if ticket2_name.strip() and ticket2_quantity>0:
        db.add(TicketCategory(event_id=event.id,name=ticket2_name,price=ticket2_price,
                              quantity=ticket2_quantity,remaining=ticket2_quantity))
    db.commit()
    return back('/admin','Event created as '+status)


@app.post('/admin/events/{event_id}/publish')
def event_publish(event_id: int,request: Request,csrf: str=Form(...),db: Session=Depends(get_db)):
    check_csrf(request,csrf)
    require_role(request,db,'admin')
    event=lock_row(db, Event, event_id)
    if not event or event.status!='draft':raise HTTPException(400,'Draft event not found')
    decision=MessageBus(db).send('Admin','VenueAgent','REQUEST',{
        'action':'check_event','event_id':event.id,'venue_id':event.venue_id,
        'start_at':event.start_at.isoformat(),'end_at':event.end_at.isoformat(),
        'ticket_quantity':sum(category.quantity for category in event.ticket_categories)})
    if decision['performative']!='AGREE':
        db.commit();return back('/admin',decision['content'].get('reason','Venue conflict'))
    event.status='published';db.commit()
    return back('/admin','Event published')


@app.post('/admin/events/{event_id}/cancel')
def event_cancel(event_id: int,request: Request,csrf: str=Form(...),db: Session=Depends(get_db)):
    check_csrf(request,csrf)
    require_role(request,db,'admin')
    event=lock_row(db, Event, event_id)
    if not event or event.status!='published':raise HTTPException(400,'Published event not found')
    if now_utc() > event.end_at:
        raise HTTPException(409, 'Cannot cancel an event that has ended')
    event.status='cancelled'
    answer=MessageBus(db).send('Admin','BookingAgent','REQUEST',
                               {'action':'event_cancelled','event_id':event_id})
    db.commit()
    return back('/admin',f"Event cancelled; full refunds: {answer['content'].get('refunded_bookings',0)}")


@app.get('/admin/hires')
def admin_hires(request: Request,db: Session=Depends(get_db)):
    require_role(request,db,'admin')
    hires=db.scalars(select(HireRequest).order_by(HireRequest.id.desc())).all()
    return page(request,'admin_hires.html',db,hires=hires)


@app.post('/admin/hires/{hire_id}/{decision}')
def decide_hire(hire_id: int,decision: str,request: Request,csrf: str=Form(...),
                db: Session=Depends(get_db)):
    check_csrf(request,csrf)
    require_role(request,db,'admin')
    hire=lock_row(db, HireRequest, hire_id)
    if not hire or hire.status!='pending' or decision not in ('approve','reject'):
        raise HTTPException(400,'Invalid pending request/decision')
    hire.status='confirmed' if decision=='approve' else 'rejected'
    MessageBus(db).send('Admin','ALL','INFORM',{
        'user_id':hire.user_id,'text':f'Private hire #{hire.id} {hire.status} by admin.'})
    db.commit()
    return back('/admin/hires')
