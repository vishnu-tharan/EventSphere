"""Optional demo-data seed: do not use fixed demo credentials for non-demo sites."""
import os
from datetime import timedelta
from sqlalchemy import select
from database import Base, SessionLocal, engine
from models import User,Venue,Event,TicketCategory,now_utc
from security import hash_password


def seed(db):
    specs=[
        ('Admin Demo','admin@eventsphere.test','admin','DEMO_ADMIN_PASSWORD','AdminDemo!2026'),
        ('Gate Staff','staff@eventsphere.test','staff','DEMO_STAFF_PASSWORD','StaffDemo!2026'),
        ('Student Demo','attendee@eventsphere.test','attendee','DEMO_ATTENDEE_PASSWORD','StudentDemo!2026'),
    ]
    for name,email,role,env,default in specs:
        if not db.scalar(select(User).where(User.email==email)):
            db.add(User(name=name,email=email,role=role,password_hash=hash_password(os.getenv(env,default))))
    db.flush()
    if not db.scalar(select(Venue).limit(1)):
        db.add_all([
            Venue(name='University Main Auditorium',location='Colombo Campus',capacity=300,
                  description='Large indoor hall for concerts and conferences',
                  hourly_rate=8000,hire_open=True),
            Venue(name='Innovation Seminar Room',location='Block B, Floor 2',capacity=80,
                  description='Classroom and seminar venue',hourly_rate=2500,hire_open=True),
        ])
        db.flush()
    if not db.scalar(select(Event).limit(1)):
        venue=db.scalar(select(Venue).order_by(Venue.id).limit(1))
        soon=now_utc()+timedelta(days=4)
        event=Event(title='Campus Music Night',category='Concert',venue_id=venue.id,
                    start_at=soon,end_at=soon+timedelta(hours=3),
                    description='Demo concert with Standard and VIP tickets',status='published')
        db.add(event);db.flush()
        db.add_all([
            TicketCategory(event_id=event.id,name='Standard',price=1000,quantity=200,remaining=200),
            TicketCategory(event_id=event.id,name='VIP',price=2500,quantity=50,remaining=50),
        ])
        # A second event is currently in the valid scanner window, for gate demonstration.
        started=now_utc()+timedelta(hours=1)
        live=Event(title='Live QR Check-in Demo',category='Workshop',venue_id=venue.id,
                   start_at=started,end_at=started+timedelta(hours=2),
                   description='Staff can show QR check-in for this event',status='published')
        db.add(live);db.flush()
        db.add(TicketCategory(event_id=live.id,name='Entry',price=0,quantity=30,remaining=30))
    db.commit()


if __name__=='__main__':
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as session:
        seed(session)
    print('Demo data seeded. Change demo passwords before deploying publicly.')
