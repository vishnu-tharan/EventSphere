from datetime import timedelta
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from database import Base
from models import User,Venue,Event,TicketCategory,now_utc
from security import hash_password

@pytest.fixture
def setup_db(tmp_path):
    url='sqlite:///' + str(tmp_path/'tests.db')
    engine=create_engine(url,connect_args={'check_same_thread':False,'timeout':30})
    Base.metadata.create_all(engine)
    Sessions=sessionmaker(bind=engine,expire_on_commit=False)
    with Sessions() as db:
        db.add_all([
            User(name='Alice',email='alice@test.com',role='attendee',password_hash=hash_password('Pass12345')),
            User(name='Bob',email='bob@test.com',role='attendee',password_hash=hash_password('Pass12345')),
            User(name='Admin',email='admin@test.com',role='admin',password_hash=hash_password('Pass12345')),
            Venue(name='Hall',location='Campus',capacity=20,description='Demo',hourly_rate=1000,hire_open=True),
            Venue(name='Room',location='Campus',capacity=40,description='Demo',hourly_rate=500,hire_open=True),
        ])
        db.flush()
        start=now_utc()+timedelta(days=4)
        db.add(Event(title='Event A',category='Concert',venue_id=1,start_at=start,
                     end_at=start+timedelta(hours=2),description='Demo',status='published'))
        db.flush()
        db.add(TicketCategory(event_id=1,name='Standard',price=100,quantity=5,remaining=5))
        db.commit()
    yield Sessions
    engine.dispose()

