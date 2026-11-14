"""Create the initial role accounts and optional sample events."""
import argparse
import os
from datetime import timedelta
from getpass import getpass
from sqlalchemy import select
from database import Base, SessionLocal, engine
from models import User,Venue,Event,TicketCategory,now_utc
from security import hash_password

CAMPUS_LOCATION = 'Pampaimadu, Vavuniya, Sri Lanka'
TECHNOLOGY_HALL = 'TLH2, Faculty of Technological Studies'
APPLIED_SCIENCE_HALL = 'Smart Lecture Hall, Faculty of Applied Science'
BUSINESS_STUDIES_HALL = 'LH 6 Hall, Faculty of Business Studies'
ORIENTATION_DESCRIPTION = 'Sample orientation listing for incoming students at the Faculty of Technological Studies.'
ICT_WORKSHOP_DESCRIPTION = 'Sample hands-on workshop on campus computing and software development.'


def localize_sample_venues(db):
    """Update the original sample listings while preserving their bookings and dates."""
    auditorium = db.scalar(select(Venue).where(
        Venue.name == 'University Main Auditorium', Venue.location == 'Colombo Campus',
        Venue.description == 'Large indoor hall for concerts and conferences'))
    if auditorium is not None:
        auditorium.name = TECHNOLOGY_HALL
        auditorium.location = CAMPUS_LOCATION
        auditorium.description = 'Faculty of Technological Studies lecture hall. Capacity and hire rate are sample values.'
        for event in db.scalars(select(Event).where(Event.venue_id == auditorium.id)):
            if event.title == 'Campus Music Night' and event.description == 'Campus concert with Standard and VIP tickets':
                event.title = 'Freshers’ Orientation'
                event.category = 'Other'
                event.description = ORIENTATION_DESCRIPTION
            elif event.title == 'QR Check-in Workshop' and event.description == 'Workshop with QR ticket check-in':
                event.title = 'ICT Skills Workshop'
                event.description = ICT_WORKSHOP_DESCRIPTION

    seminar_room = db.scalar(select(Venue).where(
        Venue.name == 'Innovation Seminar Room', Venue.location == 'Block B, Floor 2',
        Venue.description == 'Classroom and seminar venue'))
    if seminar_room is not None:
        seminar_room.name = APPLIED_SCIENCE_HALL
        seminar_room.location = CAMPUS_LOCATION
        seminar_room.description = 'Faculty of Applied Science lecture hall. Capacity and hire rate are sample values.'


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
    localize_sample_venues(db)
    venues = db.scalars(select(Venue).where(
        Venue.location == CAMPUS_LOCATION,
        Venue.name.in_([TECHNOLOGY_HALL, APPLIED_SCIENCE_HALL, BUSINESS_STUDIES_HALL]),
        Venue.description == 'Capacity and hire rate are sample values for this coursework project.'))
    for venue in venues:
        venue.description = 'Capacity and hire rate are illustrative.'


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
    instant = now_utc()
    listings = (
        (TECHNOLOGY_HALL, 150, 4000, 'Freshers’ Orientation', 'Other',
         ORIENTATION_DESCRIPTION, timedelta(hours=1), 100),
        (APPLIED_SCIENCE_HALL, 80, 2500, 'Research Grants Workshop', 'Workshop',
         'Sample workshop on applying for university research grants.', timedelta(days=4), 50),
        (BUSINESS_STUDIES_HALL, 60, 2000, 'Communication Skills Workshop', 'Workshop',
         'Sample workshop on communication skills for campus and professional settings.', timedelta(days=6), 40),
    )
    for venue_name, capacity, rate, title, category, description, starts_in, tickets in listings:
        venue = Venue(name=venue_name, location=CAMPUS_LOCATION, capacity=capacity, hourly_rate=rate,
                      hire_open=True, description='Capacity and hire rate are illustrative.')
        start = instant + starts_in
        event = Event(venue=venue, title=title, category=category, description=description, status='published',
                      start_at=start, end_at=start + timedelta(hours=2))
        db.add(TicketCategory(event=event, name='General admission', price=0, quantity=tickets, remaining=tickets))


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
