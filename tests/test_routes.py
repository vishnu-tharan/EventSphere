"""HTTP role checks and public mobile page smoke tests."""
from fastapi.testclient import TestClient
from datetime import timedelta
import re
import pytest
from sqlalchemy import select
from main import app, auth_signer, for_input
from database import get_db
from agents import MessageBus
from models import AgentMessage, Booking, Event, TicketCategory, User, WaitlistEntry, now_utc


def test_admin_and_scanner_refuse_wrong_roles(setup_db):
    # pytest fixture is defined in test_agents.py; local fixture import below in conftest.py
    with setup_db() as db:
        app.dependency_overrides[get_db]=lambda: db
        with TestClient(app) as client:
            client.cookies.set('session',auth_signer.dumps('1'))
            assert client.get('/admin').status_code==403
            assert client.get('/staff/scanner').status_code==403
            client.cookies.set('session',auth_signer.dumps('3'))
            assert client.get('/admin').status_code==200
            assert client.get('/staff/scanner').status_code==200
        app.dependency_overrides.clear()


def test_venue_request_returns_attendee_to_hire_after_sign_in(setup_db):
    with setup_db() as db:
        app.dependency_overrides[get_db] = lambda: db
        try:
            with TestClient(app) as client:
                response = client.get('/hire', follow_redirects=False)
                assert response.status_code == 303
                login_url = response.headers['location']
                sign_in = client.get(login_url)
                assert sign_in.status_code == 200
                assert 'name="venue_request" value="1"' in sign_in.text
                csrf = re.search(r'name="csrf" value="([^"]+)"', sign_in.text)[1]

                failed = client.post('/login', data={
                    'csrf': csrf, 'email': 'alice@test.com', 'password': 'wrong-password',
                    'venue_request': '1'})
                assert 'Incorrect email or password' in failed.text
                assert 'name="venue_request" value="1"' in failed.text

                for email, destination in (('admin@test.com', '/admin'), ('alice@test.com', '/hire')):
                    signed_in = client.post('/login', data={
                        'csrf': csrf, 'email': email, 'password': 'Pass12345', 'venue_request': '1'},
                        follow_redirects=False)
                    assert signed_in.status_code == 303
                    assert signed_in.headers['location'] == destination
                assert 'Hire a venue' in client.get('/hire').text
        finally:
            app.dependency_overrides.clear()


def test_public_api_lists_published_events(setup_db):
    with setup_db() as db:
        app.dependency_overrides[get_db]=lambda: db
        with TestClient(app) as client:
            response=client.get('/api/public/events')
            assert response.status_code==200
            assert response.json()[0]['title']=='Event A'
        app.dependency_overrides.clear()


def test_unused_ticket_shows_event_ended_after_check_in_closes(setup_db):
    with setup_db() as db:
        response = MessageBus(db).send('Website', 'BookingAgent', 'REQUEST', {
            'action': 'book', 'user_id': 1, 'category_id': 1, 'quantity': 3})
        booking = db.get(Booking, response['content']['booking_id'])
        booking.tickets[1].status = 'checked_in'
        booking.tickets[2].status = 'cancelled'
        db.commit()
        app.dependency_overrides[get_db] = lambda: db
        try:
            with TestClient(app) as client:
                client.cookies.set('session', auth_signer.dumps('1'))
                statuses = r'data-ticket-status>\s*([^<]+?)\s*</div>'
                upcoming = client.get('/bookings')
                assert upcoming.status_code == 200
                assert sorted(re.findall(statuses, upcoming.text)) == ['active', 'cancelled', 'checked_in']
                assert 'Cancel booking' not in upcoming.text

                booking.event.start_at = now_utc() - timedelta(hours=2)
                booking.event.end_at = now_utc() + timedelta(hours=1)
                db.commit()
                started = client.get('/bookings')
                assert started.status_code == 200
                assert 'Cancel booking' not in started.text
                assert 'Event ended' not in started.text

                booking.event.end_at = now_utc() - timedelta(hours=1)
                db.commit()
                ended = client.get('/bookings')
                assert ended.status_code == 200
                assert sorted(re.findall(statuses, ended.text)) == ['Event ended', 'cancelled', 'checked_in']
                assert 'Cancel booking' not in ended.text
                assert any(ticket.status == 'active' for ticket in booking.tickets)
        finally:
            app.dependency_overrides.clear()


def test_event_capacity_is_enforced_on_venue_edit_and_draft_publish(setup_db):
    with setup_db() as db:
        event = db.get(Event, 1)
        event.status = 'draft'
        db.commit()
        app.dependency_overrides[get_db] = lambda: db
        try:
            with TestClient(app) as client:
                client.cookies.set('session', auth_signer.dumps('3'))
                csrf = re.search(r'name="csrf" value="([^"]+)"', client.get('/admin').text)[1]
                response = client.post('/admin/venues/save', data={
                    'csrf': csrf, 'venue_id': 1, 'name': 'Hall', 'location': 'Campus', 'capacity': 4,
                    'hourly_rate': 1000, 'hire_open': 'true'})
                assert response.status_code == 409
                db.rollback()
                # Simulate a draft saved before the venue-edit protection existed.
                event.venue.capacity = 4
                db.commit()
                response = client.post('/admin/events/1/publish', data={'csrf': csrf})
                assert 'Ticket quantities exceed venue capacity' in response.text
                assert event.status == 'draft'
        finally:
            app.dependency_overrides.clear()


def test_conflicting_publish_is_logged_and_old_messages_are_accessible(setup_db):
    with setup_db() as db:
        event = db.get(Event, 1)
        app.dependency_overrides[get_db] = lambda: db
        try:
            with TestClient(app) as client:
                client.cookies.set('session', auth_signer.dumps('3'))
                csrf = re.search(r'name="csrf" value="([^"]+)"', client.get('/admin').text)[1]
                response = client.post('/admin/events/new', data={
                    'csrf': csrf, 'title': 'Conflict', 'category': 'Other', 'venue_id': 1,
                    'start_at': for_input(event.start_at), 'end_at': for_input(event.end_at),
                    'status': 'published', 'ticket1_name': 'Entry', 'ticket1_price': 0,
                    'ticket1_quantity': 1})
                assert response.status_code == 409
                assert db.scalar(select(Event).where(Event.title == 'Conflict')) is None
                messages = db.scalars(select(AgentMessage).order_by(AgentMessage.id)).all()
                assert [message.performative for message in messages] == ['REQUEST', 'PROPOSE']
                conversation_id = messages[0].conversation_id
                assert messages[1].conversation_id == conversation_id

                db.add_all(AgentMessage(sender='Test', receiver='VenueAgent', performative='REQUEST',
                                        content_json='{}', conversation_id='newer')
                           for _ in range(120))
                db.commit()
                first_page = client.get('/admin').text
                older_before = re.search(r'&amp;before=(\d+)', first_page)[1]
                assert conversation_id not in first_page
                assert conversation_id in client.get('/admin', params={'before': older_before}).text
                assert 'Conflict' in client.get('/admin', params={'conversation_id': conversation_id}).text
        finally:
            app.dependency_overrides.clear()


def test_attendee_can_claim_last_held_ticket_at_zero_public_stock(setup_db):
    with setup_db() as db:
        category = db.get(TicketCategory, 1)
        category.remaining = 0
        hold = WaitlistEntry(user_id=1, category_id=1, quantity=1, status='offered',
                             expires_at=now_utc() + timedelta(minutes=5))
        db.add(hold)
        db.commit()
        app.dependency_overrides[get_db] = lambda: db
        try:
            with TestClient(app) as client:
                client.cookies.set('session', auth_signer.dumps('2'))
                assert 'Claim 1 held ticket(s)' not in client.get('/events/1').text
                client.cookies.set('session', auth_signer.dumps('1'))
                event_page = client.get('/events/1').text
                assert 'Claim 1 held ticket(s)' in event_page
                assert 'Active waitlist hold!' in client.get('/notifications').text
                csrf = re.search(r'name="csrf" value="([^"]+)"', event_page)[1]
                form = {'csrf': csrf, 'quantity': 1, 'hold_id': hold.id}
                response = client.post('/book/1', data=form, follow_redirects=False)
                assert response.headers['location'].startswith('/bookings')
                assert hold.status == 'claimed'
                assert category.remaining == 0
                repeated = client.post('/book/1', data=form)
                assert 'This waitlist hold is no longer available' in repeated.text
        finally:
            app.dependency_overrides.clear()


def test_cancelling_event_refunds_bookings_and_restores_held_stock(setup_db):
    with setup_db() as db:
        bus = MessageBus(db)
        booking = bus.send('Website', 'BookingAgent', 'REQUEST', {
            'action': 'book', 'user_id': 1, 'category_id': 1, 'quantity': 4})
        booking_id = booking['content']['booking_id']
        category = db.get(TicketCategory, 1)
        category.remaining = 0
        hold = WaitlistEntry(user_id=2, category_id=1, quantity=1, status='offered',
                             expires_at=now_utc() + timedelta(minutes=5))
        db.add(hold)
        db.commit()
        app.dependency_overrides[get_db] = lambda: db
        try:
            with TestClient(app) as client:
                client.cookies.set('session', auth_signer.dumps('3'))
                csrf = re.search(r'name="csrf" value="([^"]+)"', client.get('/admin').text)[1]
                response = client.post('/admin/events/1/cancel', data={'csrf': csrf})
                assert 'full refunds: 1' in response.text
                cancelled = db.get(Booking, booking_id)
                assert cancelled.status == 'cancelled' and cancelled.refund_amount == cancelled.total
                assert all(ticket.status == 'cancelled' for ticket in cancelled.tickets)
                assert hold.status == 'expired'
                assert category.remaining == category.quantity
        finally:
            app.dependency_overrides.clear()


@pytest.fixture
def upcoming_booking(setup_db, monkeypatch):
    with setup_db() as db:
        event = db.get(Event, 1)
        event.start_at = now_utc() + timedelta(hours=1)
        event.end_at = event.start_at + timedelta(hours=2)
        booked = MessageBus(db).send('Website', 'BookingAgent', 'REQUEST', {
            'action': 'book', 'user_id': 1, 'category_id': 1, 'quantity': 2})
        db.commit()
        booking = db.get(Booking, booked['content']['booking_id'])
        monkeypatch.setitem(app.dependency_overrides, get_db, lambda: db)
        with TestClient(app) as client:
            yield db, client, booking


def test_early_checkin_prevents_whole_booking_cancellation(upcoming_booking):
    db, client, booking = upcoming_booking
    client.cookies.set('session', auth_signer.dumps('1'))
    page = client.get('/bookings')
    cancel_path = f'/bookings/{booking.id}/cancel'
    assert cancel_path in page.text
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]

    scanned = MessageBus(db).send('Scanner', 'TicketAgent', 'REQUEST', {
        'action': 'checkin', 'event_id': booking.event_id, 'code': booking.tickets[0].code})
    assert scanned['performative'] == 'AGREE'
    db.commit()
    assert cancel_path not in client.get('/bookings').text
    response = client.post(cancel_path, data={'csrf': csrf})
    assert 'Cannot cancel this booking after a ticket has been checked in' in response.text
    db.expire_all()
    assert booking.status == 'paid' and booking.refund_amount == 0
    assert sorted(t.status for t in booking.tickets) == ['active', 'checked_in']
    assert booking.category.remaining == 3


def test_ended_event_cannot_be_cancelled_or_refunded(upcoming_booking):
    db, client, booking = upcoming_booking
    client.cookies.set('session', auth_signer.dumps('3'))
    page = client.get('/admin')
    cancel_path = f'/admin/events/{booking.event_id}/cancel'
    assert cancel_path in page.text
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]

    booking.event.start_at = now_utc() - timedelta(hours=3)
    booking.event.end_at = now_utc() - timedelta(hours=1)
    db.commit()
    ended = client.get('/admin')
    assert 'Event ended' in ended.text and cancel_path not in ended.text
    assert client.post(cancel_path, data={'csrf': csrf}).status_code == 409
    db.expire_all()
    assert booking.event.status == 'published'
    assert booking.status == 'paid' and booking.refund_amount == 0
    assert booking.category.remaining == 3
    assert all(ticket.status == 'active' for ticket in booking.tickets)


def test_scanner_keeps_selected_event_and_allows_switching(upcoming_booking):
    db, client, booking = upcoming_booking
    db.get(User, 2).role = 'staff'
    other = Event(title='Campus lecture', category='Lecture', venue_id=2,
                  start_at=booking.event.start_at, end_at=booking.event.end_at, status='published')
    db.add(other)
    db.commit()
    client.cookies.set('session', auth_signer.dumps('2'))
    page = client.get('/staff/scanner')
    assert 'Event being entered' in page.text
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
    for event_id, expected in [(booking.event_id, 'ADMIT'), (booking.event_id, 'REJECT: Already used'),
                               (other.id, 'REJECT: Wrong event')]:
        response = client.post('/staff/checkin', data={
            'csrf': csrf, 'event_id': event_id, 'code': booking.tickets[0].code})
        assert expected in response.text
        assert 'Change event' in response.text
        assert f'<option value="{event_id}" selected>' in response.text
        assert response.text.count(' selected>') == 1
