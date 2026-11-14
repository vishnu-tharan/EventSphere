"""Meaningful ICA02 tests: 5 agents, overbooking, double scan, refund, persistence."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from agents import MessageBus, load_state
from database import Base
from models import (Booking,Event,Ticket,TicketCategory,User,Venue,WaitlistEntry,
                    AgentMessage,Notification,now_utc)
from security import hash_password, verify_password




def send(db,agent,action,**kwargs):
    return MessageBus(db).send('TestClient',agent,'REQUEST',dict(action=action,**kwargs))


def purchase(db,user_id=1,quantity=1,category_id=1):
    return send(db,'BookingAgent','book',user_id=user_id,category_id=category_id,quantity=quantity)


def ticket_for(db,booking_id):
    return db.scalar(select(Ticket).where(Ticket.booking_id==booking_id))


def test_password_hash_never_plain_text():
    password='VerySecret123!'; digest=hash_password(password)
    assert password not in digest
    assert verify_password(password,digest)
    assert not verify_password('wrong',digest)


def test_venue_agent_prevents_overlap_and_proposes_alternatives(setup_db):
    with setup_db() as db:
        e=db.get(Event,1)
        reply=send(db,'VenueAgent','check_event',venue_id=1,
                   start_at=(e.start_at+timedelta(minutes=10)).isoformat(),
                   end_at=(e.end_at+timedelta(minutes=10)).isoformat())
        assert reply['performative']=='PROPOSE'
        assert reply['content']['reason']
        assert reply['content']['alternatives']
        db.commit()


def test_venue_agent_hire_has_price_and_auto_confirms(setup_db):
    with setup_db() as db:
        start=now_utc()+timedelta(days=8)
        ans=send(db,'VenueAgent','request_hire',user_id=1,venue_id=2,
                 start_at=start.isoformat(),end_at=(start+timedelta(hours=2)).isoformat(),
                 purpose='Club practice',guest_count=8)
        assert ans['performative']=='AGREE'
        assert ans['content']['price']>=1000
        db.commit()


def test_booking_agent_sells_without_overselling(setup_db):
    with setup_db() as db:
        assert purchase(db,1,5)['performative']=='AGREE'
        assert purchase(db,2,1)['performative']=='REFUSE'
        assert db.get(TicketCategory,1).remaining==0
        db.commit()


def test_booking_limit_max_six(setup_db):
    with setup_db() as db:
        answer=purchase(db,1,7)
        assert answer['performative']=='REFUSE'
        assert 'reason' in answer['content']
        db.commit()


def test_simulated_payment_ref_and_unique_qr_tickets(setup_db):
    with setup_db() as db:
        answer=purchase(db,1,2);db.commit()
        booking=db.get(Booking,answer['content']['booking_id'])
        assert booking.payment_ref.startswith('SIM-')
        assert len(booking.tickets)==2
        assert booking.tickets[0].code!=booking.tickets[1].code
        assert all(len(t.code)>30 for t in booking.tickets)


def test_ticket_agent_admits_once_then_rejects_repeat(setup_db):
    with setup_db() as db:
        ans=purchase(db,1,1)
        ticket=ticket_for(db,ans['content']['booking_id'])
        event=db.get(Event,1)
        event.start_at=now_utc()-timedelta(minutes=15)
        event.end_at=now_utc()+timedelta(hours=1)
        admitted=send(db,'TicketAgent','checkin',code=ticket.code,event_id=1)
        repeat=send(db,'TicketAgent','checkin',code=ticket.code,event_id=1)
        assert admitted['performative']=='AGREE'
        assert repeat['performative']=='REFUSE'
        assert 'Already used' in repeat['content']['reason']
        assert load_state(db,'TicketAgent',f'rejected_{ticket.id}')['count']==1
        db.commit()


def test_ticket_agent_rejects_wrong_event(setup_db):
    with setup_db() as db:
        b=purchase(db,1,1);code=ticket_for(db,b['content']['booking_id']).code
        ans=send(db,'TicketAgent','checkin',code=code,event_id=999)
        assert ans['performative']=='REFUSE'
        assert 'Wrong event' in ans['content']['reason']
        db.commit()


def test_ticket_agent_rejects_outside_time(setup_db):
    with setup_db() as db:
        b=purchase(db,1,1);code=ticket_for(db,b['content']['booking_id']).code
        ans=send(db,'TicketAgent','checkin',code=code,event_id=1)
        assert ans['performative']=='REFUSE'
        assert 'Outside check-in time' in ans['content']['reason']
        db.commit()


def test_refund_after_cancellation_restores_seats(setup_db):
    with setup_db() as db:
        b=purchase(db,1,2)
        ans=send(db,'BookingAgent','cancel',booking_id=b['content']['booking_id'],user_id=1)
        assert ans['performative']=='AGREE'
        assert ans['content']['refund_amount']==200
        assert db.get(TicketCategory,1).remaining==5
        assert all(t.status=='cancelled' for t in db.get(Booking,b['content']['booking_id']).tickets)
        db.commit()


def test_waitlist_agent_allocates_expiring_hold(setup_db):
    with setup_db() as db:
        purchase(db,1,5)
        join=send(db,'WaitlistAgent','join',user_id=2,category_id=1,quantity=1)
        assert join['performative']=='AGREE'
        first=db.get(WaitlistEntry,join['content']['waitlist_id'])
        assert first.status=='waiting'
        sold=db.scalar(select(Booking).where(Booking.user_id==1))
        send(db,'BookingAgent','cancel',booking_id=sold.id,user_id=1)
        assert first.status=='offered'
        assert first.expires_at is not None
        db.commit()


def test_waitlist_agent_timer_expires_and_reoffers(setup_db):
    with setup_db() as db:
        purchase(db,1,1)
        purchase(db,2,4)
        send(db,'WaitlistAgent','join',user_id=2,category_id=1,quantity=1)
        next_offer = send(db, 'WaitlistAgent', 'join', user_id=1, category_id=1, quantity=1)
        sold=db.scalar(select(Booking).where(Booking.user_id==1))
        send(db,'BookingAgent','cancel',booking_id=sold.id,user_id=1)
        offer=db.scalar(select(WaitlistEntry).where(WaitlistEntry.user_id==2))
        offer.expires_at=now_utc()-timedelta(minutes=1)
        ans=send(db,'WaitlistAgent','sweep')
        assert ans['content']['expired']==1
        assert ans['content']['reoffered'] == 1
        assert offer.status=='expired'
        next_entry = db.get(WaitlistEntry, next_offer['content']['waitlist_id'])
        assert next_entry.status == 'offered'
        assert db.get(TicketCategory, 1).remaining == 0
        assert load_state(db,'WaitlistAgent','sweeper')['runs']==1
        db.commit()


def test_notification_agent_reacts_to_broadcast(setup_db):
    with setup_db() as db:
        MessageBus(db).send('BookingAgent','ALL','INFORM',{'user_id':1,'text':'Hello from agent'})
        db.commit()
        notes=db.scalars(select(Notification).where(Notification.user_id==1)).all()
        assert len(notes)==1 and 'Hello' in notes[0].message


def test_agent_messages_have_traceable_conversation_id(setup_db):
    with setup_db() as db:
        purchase(db,1,1)
        db.commit()
        messages=db.scalars(select(AgentMessage)).all()
        assert len(messages)>=6
        assert len(set(m.conversation_id for m in messages))==1
        assert any(m.receiver=='TicketAgent' for m in messages)
        assert any(m.receiver=='ALL' for m in messages)


def test_agent_state_survives_new_session(setup_db):
    with setup_db() as db:
        send(db,'WaitlistAgent','sweep');db.commit()
    with setup_db() as db:
        assert load_state(db,'WaitlistAgent','sweeper')['runs']==1


def test_concurrent_last_seat_only_one_booking(setup_db):
    with setup_db() as db:
        db.get(TicketCategory,1).remaining=1
        db.commit()
    barrier=Barrier(2)
    def book_thread(user_id):
        with setup_db() as db:
            barrier.wait(timeout=5)
            answer=purchase(db,user_id,1)
            db.commit()
            return answer['performative']
    with ThreadPoolExecutor(max_workers=2) as pool:
        a=pool.submit(book_thread,1);b=pool.submit(book_thread,2)
        statuses=[a.result(timeout=20),b.result(timeout=20)]
    assert sorted(statuses)==['AGREE','REFUSE']
    with setup_db() as db:
        assert db.get(TicketCategory,1).remaining==0
        assert len(db.scalars(select(Ticket)).all())==1


def test_concurrent_cancellation_returns_stock_once(setup_db):
    with setup_db() as db:
        booking_id = purchase(db, 1, 1)['content']['booking_id']
        db.commit()
    barrier = Barrier(2)

    def cancel():
        with setup_db() as db:
            barrier.wait(timeout=5)
            answer = send(db, 'BookingAgent', 'cancel', booking_id=booking_id, user_id=1)
            db.commit()
            return answer['performative']

    with ThreadPoolExecutor(max_workers=2) as pool:
        attempts = [pool.submit(cancel) for _ in range(2)]
        assert sorted(attempt.result(timeout=20) for attempt in attempts) == ['AGREE', 'REFUSE']
    with setup_db() as db:
        assert db.get(TicketCategory, 1).remaining == 5


def test_expired_hold_cannot_be_claimed_during_sweep(setup_db):
    with setup_db() as db:
        db.get(TicketCategory, 1).remaining = 0
        expired = WaitlistEntry(user_id=1, category_id=1, quantity=1, status='offered',
                                expires_at=now_utc()-timedelta(minutes=1))
        waiting = WaitlistEntry(user_id=2, category_id=1, quantity=1, status='waiting')
        db.add_all([expired, waiting])
        db.commit()
        expired_id, waiting_id = expired.id, waiting.id
    barrier = Barrier(2)

    def run(agent, action, **payload):
        with setup_db() as db:
            barrier.wait(timeout=5)
            result = send(db, agent, action, **payload)
            db.commit()
            return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        claim = pool.submit(run, 'BookingAgent', 'book', user_id=1, category_id=1,
                            quantity=1, hold_id=expired_id)
        sweep = pool.submit(run, 'WaitlistAgent', 'sweep')
        assert claim.result(timeout=20)['performative'] == 'REFUSE'
        assert sweep.result(timeout=20)['content']['reoffered'] == 1
    with setup_db() as db:
        assert db.get(WaitlistEntry, expired_id).status == 'expired'
        assert db.get(WaitlistEntry, waiting_id).status == 'offered'
        assert db.get(TicketCategory, 1).remaining == 0


def test_hire_proposes_first_open_slot_after_a_long_conflict(setup_db):
    with setup_db() as db:
        event = db.get(Event, 1)
        event.end_at = event.start_at + timedelta(days=10)
        db.get(Venue, 2).hire_open = False
        start = event.start_at + timedelta(hours=1)
        answer = send(db, 'VenueAgent', 'request_hire', user_id=1, venue_id=1,
                      start_at=start.isoformat(), end_at=(start + timedelta(hours=2)).isoformat(),
                      purpose='Club meeting', guest_count=5)
        assert answer['performative'] == 'PROPOSE'
        option, = answer['content']['alternatives']
        assert option['venue_id'] == 1
        assert option['start_at'] == (event.end_at + timedelta(minutes=30)).isoformat()


def test_hire_prices_weekends_at_twenty_percent_more(setup_db):
    with setup_db() as db:
        start = (now_utc() + timedelta(days=7)).replace(hour=10, minute=0, second=0, microsecond=0)
        saturday = start + timedelta(days=(5 - start.weekday()) % 7)
        for day, expected in ((saturday, 1200), (saturday + timedelta(days=2), 1000)):
            reply = send(db, 'VenueAgent', 'request_hire', user_id=1, venue_id=2,
                         start_at=day.isoformat(), end_at=(day + timedelta(hours=2)).isoformat(),
                         purpose='Club meeting', guest_count=5)
            assert reply['performative'] == 'AGREE'
            assert reply['content']['price'] == expected
