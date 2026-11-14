"""Create the initial role accounts and optional sample events."""
import argparse
import os
from datetime import timedelta
from getpass import getpass
from sqlalchemy import select
from database import Base, SessionLocal, engine
from models import User,Venue,Event,TicketCategory,now_utc
from security import hash_password


def update_seed_labels(db):
    """Update labels created by earlier versions while preserving records and passwords."""
    for email, role, old_name, name in (
        ('admin@eventsphere.test', 'admin', 'Admin Demo', 'Administrator'),
        ('attendee@eventsphere.test', 'attendee', 'Student Demo', 'Attendee'),
    ):
        user = db.scalar(select(User).where(User.email == email, User.role == role, User.name == old_name))
        if user is not None:
            user.name = name
    events = db.scalars(select(Event).where(
        Event.title == 'Live QR Check-in Demo',
        Event.description == 'Staff can show QR check-in for this event')).all()
    for event in events:
        event.title = 'QR Check-in Workshop'
        event.description = 'Workshop with QR ticket check-in'
    concerts = db.scalars(select(Event).where(
        Event.title == 'Campus Music Night',
        Event.description == 'Demo concert with Standard and VIP tickets')).all()
    for event in concerts:
        event.description = 'Campus concert with Standard and VIP tickets'


def seed(db, *, sample_data=False, prompt=False):
    specs = (
        ('Administrator', 'admin@eventsphere.test', 'admin'),
        ('Gate Staff', 'staff@eventsphere.test', 'staff'),
        ('Attendee', 'attendee@eventsphere.test', 'attendee'),
    )
    for name, email, role in specs:
        existing = db.scalar(select(User).where(User.email == email))
        if existing is not None:
            if existing.role != role:
                raise ValueError(f'{email} already exists with a different role')
            continue
        key = f'INITIAL_{role.upper()}_PASSWORD'
        # Existing deployments can switch to the new names at their own pace.
        password = os.getenv(key) or os.getenv(f'DEMO_{role.upper()}_PASSWORD')
        if not password and prompt:
            password = getpass(f'Choose a password for {email} (at least 8 characters): ')
        if not password or len(password) < 8:
            raise ValueError(f'Set {key} to a password of at least 8 characters')
        db.add(User(name=name, email=email, role=role, password_hash=hash_password(password)))
    db.flush()
    update_seed_labels(db)
    if sample_data:
        add_sample_data(db)
    db.commit()


def add_sample_data(db):
    if db.scalar(select(Venue.id).limit(1)) is not None:
        return
    auditorium = Venue(name='University Main Auditorium', location='Colombo Campus', capacity=300,
                       description='Large indoor hall for concerts and conferences',
                       hourly_rate=8000, hire_open=True)
    db.add_all([
        auditorium,
        Venue(name='Innovation Seminar Room', location='Block B, Floor 2', capacity=80,
              description='Classroom and seminar venue', hourly_rate=2500, hire_open=True),
    ])
    db.flush()
    instant = now_utc()
    concert_start = instant + timedelta(days=4)
    workshop_start = instant + timedelta(hours=1)
    concert = Event(title='Campus Music Night', category='Concert', venue_id=auditorium.id,
                    start_at=concert_start, end_at=concert_start + timedelta(hours=3),
                    description='Campus concert with Standard and VIP tickets', status='published')
    workshop = Event(title='QR Check-in Workshop', category='Workshop', venue_id=auditorium.id,
                     start_at=workshop_start, end_at=workshop_start + timedelta(hours=2),
                     description='Workshop with QR ticket check-in', status='published')
    db.add_all([concert, workshop])
    db.flush()
    db.add_all([
        TicketCategory(event_id=concert.id, name='Standard', price=1000, quantity=200, remaining=200),
        TicketCategory(event_id=concert.id, name='VIP', price=2500, quantity=50, remaining=50),
        TicketCategory(event_id=workshop.id, name='Entry', price=0, quantity=30, remaining=30),
    ])


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sample-data', action='store_true', help='Create sample venues and events')
    args = parser.parse_args()
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as session:
        try:
            seed(session, sample_data=args.sample_data, prompt=not os.getenv('RENDER'))
        except ValueError as exc:
            parser.exit(1, f'{exc}\n')
    print('Initial accounts are ready.')
