"""HTTP role checks and public mobile page smoke tests."""
import csv
import io
from fastapi.testclient import TestClient
from datetime import timedelta
import re
import pytest
from sqlalchemy import select
from main import app, auth_signer, for_input, to_utc
from database import get_db
from agents import MessageBus
from models import AdminActivity, AgentMessage, Booking, Event, HireRequest, Notification, TicketCategory, User, WaitlistEntry, now_utc


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
    manage_path = f'/admin/events/{booking.event_id}/manage'
    page = client.get(manage_path)
    cancel_path = f'/admin/events/{booking.event_id}/cancel'
    assert cancel_path in page.text
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]

    booking.event.start_at = now_utc() - timedelta(hours=3)
    booking.event.end_at = now_utc() - timedelta(hours=1)
    db.commit()
    ended = client.get(manage_path)
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
    for event_id, verdict, reason in [(booking.event_id, 'ADMIT', 'Valid ticket'),
                                      (booking.event_id, 'REJECT', 'Already used'),
                                      (other.id, 'REJECT', 'Wrong event')]:
        response = client.post('/staff/checkin', data={
            'csrf': csrf, 'event_id': event_id, 'code': booking.tickets[0].code})
        assert verdict in response.text and reason in response.text
        assert 'Change event' in response.text
        assert f'<option value="{event_id}" selected>' in response.text
        assert response.text.count(' selected>') == 1


def test_camera_checkin_handles_multiple_tickets_in_same_session(upcoming_booking):
    db, client, booking = upcoming_booking
    db.get(User, 2).role = 'staff'
    db.commit()
    client.cookies.set('session', auth_signer.dumps('2'))
    csrf = re.search(r'name="csrf" value="([^"]+)"', client.get('/staff/scanner').text)[1]
    form = {'csrf': csrf, 'event_id': booking.event_id, 'camera': 'true'}
    first, second = [ticket.code for ticket in booking.tickets]
    checks = [(first, True), (first, False), (second, True)]
    for code, admitted in checks:
        response = client.post('/staff/checkin', data={**form, 'code': code})
        assert response.status_code == 200 and response.json()['admitted'] is admitted
    assert client.post('/staff/checkin', data={**form, 'code': second, 'csrf': 'invalid'}).status_code == 403
    db.expire_all()
    assert all(ticket.status == 'checked_in' for ticket in booking.tickets)


def test_dashboard_filters_event_statuses_and_opens_pending_hires(upcoming_booking):
    db, client, booking = upcoming_booking
    start = booking.event.start_at
    end = booking.event.end_at
    states = [
        ('Active workshop', 'published', start - timedelta(hours=2), end),
        ('Past workshop', 'published', start - timedelta(hours=3), start - timedelta(hours=2)),
        ('Draft workshop', 'draft', start, end),
        ('Cancelled workshop', 'cancelled', start, end),
    ]
    for title, state, starts, ends in states:
        db.add(Event(title=title, category='Workshop', venue_id=1, start_at=starts,
                     end_at=ends, status=state))
    for state in ('pending', 'confirmed'):
        db.add(HireRequest(user_id=1, venue_id=1, start_at=start, end_at=end,
                           purpose=f'{state.title()} request', guest_count=10, status=state))
    booking.tickets[0].status = 'checked_in'
    db.commit()

    client.cookies.set('session', auth_signer.dumps('3'))
    dashboard = client.get('/admin').text
    assert '1 of 2 tickets checked in' in dashboard
    assert 'text-bg-light">1</span>' in dashboard
    assert 'View agent logs' in dashboard
    assert '<details id="agent-activity" class="card mt-4 p-3">' in dashboard
    for state, title in [('upcoming', 'Event A'), ('in-progress', 'Active workshop'),
                         ('ended', 'Past workshop'), ('draft', 'Draft workshop'),
                         ('cancelled', 'Cancelled workshop')]:
        response = client.get('/admin', params={'status': state})
        assert response.status_code == 200
        assert title in response.text
        assert sum(name in response.text for name in ['Event A', *(item[0] for item in states)]) == 1

    filtered = client.get('/admin', params={'q': ' dRaFt ', 'status': 'draft', 'activity': 'true'})
    assert 'Draft workshop' in filtered.text and 'Past workshop' not in filtered.text
    assert '<details id="agent-activity" class="card mt-4 p-3" open>' in filtered.text
    assert 'No events match your search.' in client.get('/admin', params={'q': '%'}).text
    pending = client.get('/admin/hires', params={'status': 'pending'}).text
    assert 'Pending request' in pending and 'Confirmed request' not in pending


def test_draft_edit_prefills_and_saves_details_before_publish(setup_db, monkeypatch):
    with setup_db() as db:
        event = db.get(Event, 1)
        event.status = 'draft'
        event.category = 'Campus lecture'
        event.ticket_categories.append(TicketCategory(name='VIP', price=500, quantity=3, remaining=3))
        db.commit()
        monkeypatch.setitem(app.dependency_overrides, get_db, lambda: db)

        with TestClient(app) as client:
            client.cookies.set('session', auth_signer.dumps('3'))
            edit_path = '/admin/events/1/edit'
            assert edit_path in client.get('/admin/events/1/manage').text
            edit = client.get(edit_path)
            assert edit.status_code == 200
            assert 'value="Event A"' in edit.text and 'value="VIP"' in edit.text
            assert '<option selected>Campus lecture</option>' in edit.text
            assert for_input(event.start_at) in edit.text
            csrf = re.search(r'name="csrf" value="([^"]+)"', edit.text)[1]
            start = event.start_at + timedelta(days=1)
            end = event.end_at + timedelta(days=1)
            form = {
                'csrf': csrf, 'title': 'Research workshop', 'category': 'Workshop',
                'venue_id': 2, 'start_at': for_input(start), 'end_at': for_input(end),
                'description': 'Updated workshop', 'ticket1_name': 'Student',
                'ticket1_price': 300, 'ticket1_quantity': 25, 'ticket2_quantity': 0,
            }
            invalid = client.post(edit_path, data={**form, 'ticket1_quantity': 41})
            assert invalid.status_code == 400
            db.rollback()
            assert event.title == 'Event A'
            assert 'Draft event updated' in client.post(edit_path, data=form).text
            db.expire_all()
            assert (event.title, event.category, event.venue_id, event.description) == (
                'Research workshop', 'Workshop', 2, 'Updated workshop')
            assert event.start_at == start.replace(second=0, microsecond=0)
            assert event.end_at == end.replace(second=0, microsecond=0)
            assert [(t.id, t.name, t.price, t.quantity, t.remaining) for t in event.ticket_categories] == [
                (1, 'Student', 300, 25, 25)]

            assert 'Event published' in client.post('/admin/events/1/publish', data={'csrf': csrf}).text
            assert client.get(edit_path).status_code == 404
            assert client.post(edit_path, data=form).status_code == 404
            client.cookies.set('session', auth_signer.dumps('1'))
            assert client.get(edit_path).status_code == 403


def test_attendance_export_is_event_specific_and_admin_only(upcoming_booking):
    db, client, booking = upcoming_booking
    bus = MessageBus(db)
    cancelled = bus.send('Website', 'BookingAgent', 'REQUEST', {
        'action': 'book', 'user_id': 2, 'category_id': 1, 'quantity': 1})
    assert bus.send('Website', 'BookingAgent', 'REQUEST', {
        'action': 'cancel', 'user_id': 2, 'booking_id': cancelled['content']['booking_id'],
    })['performative'] == 'AGREE'
    db.get(User, 1).name = '=Vishnu'
    booking.category.name = 'தமிழ், Entry'
    assert bus.send('Scanner', 'TicketAgent', 'REQUEST', {
        'action': 'checkin', 'event_id': booking.event_id, 'code': booking.tickets[0].code,
    })['performative'] == 'AGREE'
    other = Event(title='Another workshop', category='Workshop', venue_id=2,
                  start_at=booking.event.start_at, end_at=booking.event.end_at, status='published')
    db.add(other)
    db.commit()
    export_path = f'/admin/events/{booking.event_id}/attendance.csv'
    client.cookies.set('session', auth_signer.dumps('1'))
    assert client.get(export_path).status_code == 403
    client.cookies.set('session', auth_signer.dumps('3'))
    assert export_path in client.get('/admin').text
    response = client.get(export_path)
    assert response.status_code == 200
    assert response.headers['content-type'].startswith('text/csv')
    assert response.headers['content-disposition'].endswith('event-1-attendance.csv"')
    assert response.headers['cache-control'] == 'private, no-store'
    report = response.content.decode('utf-8-sig')
    rows = list(csv.reader(io.StringIO(report)))
    assert rows[0][:4] == ['Ticket number', 'Booked by', 'Ticket category', 'Attendance']
    assert rows[1][:4] == [str(booking.tickets[0].id), "'=Vishnu", 'தமிழ், Entry', 'Checked in']
    assert rows[1][4].endswith('+05:30')
    assert rows[2][3:] == ['Not checked in', '']
    assert len(rows) == 3 and booking.tickets[0].code not in report
    empty = client.get(f'/admin/events/{other.id}/attendance.csv')
    assert len(empty.content.decode('utf-8-sig').splitlines()) == 1


def test_calendar_download_uses_booked_event_details_and_utc(upcoming_booking):
    db, client, booking = upcoming_booking
    event = booking.event
    event.title = 'தமிழ் இலக்கியச் சந்திப்பு, மாணவர் நிகழ்வு'
    event.description = 'First line\r\nSecond line; bring a notebook'
    event.venue.location = 'Pampaimadu, Vavuniya'
    db.commit()
    path = f'/bookings/{booking.id}/calendar.ics'

    client.cookies.set('session', auth_signer.dumps('2'))
    assert client.get(path).status_code == 404
    client.cookies.set('session', auth_signer.dumps('1'))
    assert path in client.get('/bookings').text
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers['content-type'].startswith('text/calendar')
    assert response.headers['content-disposition'].endswith('event-1.ics"')
    assert response.content.endswith(b'END:VCALENDAR\r\n')
    assert max(map(len, response.content.split(b'\r\n'))) <= 75
    details = response.content.replace(b'\r\n ', b'').decode()
    assert f'DTSTART:{event.start_at:%Y%m%dT%H%M%SZ}\r\n' in details
    assert f'DTEND:{event.end_at:%Y%m%dT%H%M%SZ}\r\n' in details
    assert 'SUMMARY:' + event.title.replace(',', r'\,') + '\r\n' in details
    assert r'LOCATION:Hall\, Pampaimadu\, Vavuniya' in details
    assert r'DESCRIPTION:First line\nSecond line\; bring a notebook' in details

    booking.status = 'cancelled'
    db.commit()
    assert path not in client.get('/bookings').text
    assert client.get(path).status_code == 404


def test_published_schedule_checks_conflicts_and_notifies_each_attendee_once(upcoming_booking):
    db, client, booking = upcoming_booking
    event = booking.event
    start = event.start_at + timedelta(hours=3)
    end = event.end_at + timedelta(hours=3)
    other = Event(title='Room already booked', category='Workshop', venue_id=2,
                  start_at=start, end_at=end, status='published')
    db.add(other)
    bus = MessageBus(db)
    assert bus.send('Website', 'BookingAgent', 'REQUEST', {
        'action': 'book', 'user_id': 1, 'category_id': 1, 'quantity': 1,
    })['performative'] == 'AGREE'
    db.commit()

    path = f'/admin/events/{event.id}/schedule'
    client.cookies.set('session', auth_signer.dumps('1'))
    assert client.get(path).status_code == 403
    client.cookies.set('session', auth_signer.dumps('3'))
    manage_path = f'/admin/events/{event.id}/manage'
    assert path in client.get(manage_path).text
    page = client.get(path).text
    form = {
        'csrf': re.search(r'name="csrf" value="([^"]+)"', page)[1],
        'version': re.search(r'name="version" value="([^"]+)"', page)[1],
        'venue_id': 2, 'start_at': for_input(start), 'end_at': for_input(end),
    }
    assert 'conflicting activity' in client.post(path, data=form).text
    assert event.venue_id == 1

    other.status = 'cancelled'
    db.commit()
    updated = client.post(path, data=form)
    assert '1 attendee(s) notified' in updated.text
    assert event.venue_id == 2 and event.status == 'published'
    assert event.start_at == start.replace(second=0, microsecond=0)
    assert booking.status == 'paid'
    notices = db.scalars(select(Notification).where(Notification.message.contains('schedule changed'))).all()
    assert len(notices) == 1 and notices[0].user_id == 1
    assert 'Previous: Hall' in notices[0].message and 'New: Room' in notices[0].message
    assert 'schedule changed while you were editing' in client.post(path, data=form).text

    booking.tickets[0].status = 'checked_in'
    db.commit()
    assert 'Schedule changes close' in client.post(path, data=form).text
    assert path not in client.get(manage_path).text
    history = client.get('/admin/activity').text
    assert history.count('Updated event schedule') == 1
    assert 'By Admin' in history and 'Previous: Hall' in history and 'New: Room' in history


def test_leaving_waitlist_releases_hold_once_and_offers_it_to_next_attendee(upcoming_booking):
    db, client, booking = upcoming_booking
    booking.category.remaining = 0
    held = WaitlistEntry(user_id=1, category_id=1, quantity=1, status='offered',
                         expires_at=now_utc() + timedelta(minutes=5))
    queued = WaitlistEntry(user_id=2, category_id=1, quantity=1, status='waiting')
    db.add_all([held, queued])
    db.commit()
    client.cookies.set('session', auth_signer.dumps('2'))
    page = client.get('/events/1').text
    assert 'You’re on the waitlist' in page
    csrf = re.search(r'name="csrf" value="([^"]+)"', page)[1]
    assert client.post(f'/waitlist/{held.id}/leave', data={'csrf': csrf}).status_code == 404

    client.cookies.set('session', auth_signer.dumps('1'))
    response = client.post(f'/waitlist/{held.id}/leave', data={'csrf': csrf})
    assert 'Your ticket hold was released' in response.text
    db.expire_all()
    assert (held.status, queued.status, booking.category.remaining) == ('withdrawn', 'offered', 0)
    assert 'no longer active' in client.post(f'/waitlist/{held.id}/leave', data={'csrf': csrf}).text
    assert booking.category.remaining == 0

    client.cookies.set('session', auth_signer.dumps('2'))
    assert 'Claim 1 held ticket(s)' in client.get('/events/1').text
    assert 'You left the waitlist' in client.post(f'/waitlist/{queued.id}/leave', data={'csrf': csrf}).text
    db.expire_all()
    assert queued.status == 'withdrawn' and booking.category.remaining == 1


def test_venue_availability_merges_buffers_without_showing_booking_details(upcoming_booking):
    db, client, booking = upcoming_booking
    day = (now_utc().date() + timedelta(days=4)).isoformat()
    db.add(Event(title='Hidden event name', category='Workshop', venue_id=1,
                 start_at=to_utc(day + 'T09:00'), end_at=to_utc(day + 'T10:00'),
                 status='published'))
    for state, start, end in [('pending', '10:45', '11:00'), ('rejected', '13:00', '14:00')]:
        db.add(HireRequest(user_id=2, venue_id=1, start_at=to_utc(f'{day}T{start}'),
                           end_at=to_utc(f'{day}T{end}'), purpose=f'Private {state} purpose',
                           guest_count=10, status=state))
    db.commit()
    client.cookies.set('session', auth_signer.dumps('1'))
    response = client.get('/hire/availability', params={'venue_id': 1, 'day': day})
    assert response.status_code == 200
    assert response.text.count('>Busy</span>') == 1 and response.text.count('>Available</span>') == 2
    assert '08:30 AM' in response.text and '11:30 AM' in response.text
    assert 'Hidden event name' not in response.text and 'Private pending purpose' not in response.text
    assert '01:00 PM' not in response.text
    assert client.get('/hire/availability', params={'venue_id': 99, 'day': day}).status_code == 404


def test_manage_event_limits_search_and_stats_to_selected_event(upcoming_booking):
    db, client, booking = upcoming_booking
    booking.tickets[0].status = 'checked_in'
    booking.event.start_at += timedelta(days=3)
    booking.event.end_at += timedelta(days=3)
    bus = MessageBus(db)
    response = bus.send('Website', 'BookingAgent', 'REQUEST', {
        'action': 'book', 'user_id': 2, 'category_id': 1, 'quantity': 1})
    other_booking = db.get(Booking, response['content']['booking_id'])
    bus.send('Website', 'BookingAgent', 'REQUEST', {
        'action': 'cancel', 'user_id': 2, 'booking_id': other_booking.id})
    db.add(WaitlistEntry(user_id=2, category_id=1, quantity=1, status='offered',
                         expires_at=now_utc() + timedelta(minutes=5)))
    unrelated = Event(title='Another event', category='Workshop', venue_id=2,
                      start_at=booking.event.start_at, end_at=booking.event.end_at, status='published')
    category = TicketCategory(event=unrelated, name='Private category', price=100, quantity=5, remaining=5)
    db.add_all([unrelated, category])
    db.flush()
    booked_elsewhere = bus.send('Website', 'BookingAgent', 'REQUEST', {
        'action': 'book', 'user_id': 2, 'category_id': category.id, 'quantity': 1})
    unrelated_ref = booked_elsewhere['content']['payment_ref']
    db.commit()

    url = f'/admin/events/{booking.event_id}/manage'
    client.cookies.set('session', auth_signer.dumps('1'))
    assert client.get(url).status_code == 403
    client.cookies.set('session', auth_signer.dumps('3'))
    assert url in client.get('/admin').text
    page = client.get(url)
    assert page.status_code == 200 and page.headers['cache-control'] == 'private, no-store'
    assert booking.payment_ref in page.text and other_booking.payment_ref in page.text
    assert 'Active hold' in page.text and '1 checked in' in page.text
    assert 'Simulated refunds <strong>LKR 100.00</strong>' in page.text
    assert 'Net simulated revenue <strong>LKR 200.00</strong>' in page.text
    assert unrelated_ref not in page.text and booking.tickets[0].code not in page.text
    filtered = client.get(url, params={'q': ' aLiCe '}).text
    assert booking.payment_ref in filtered and other_booking.payment_ref not in filtered
    filtered = client.get(url, params={'q': other_booking.payment_ref}).text
    assert booking.payment_ref not in filtered and other_booking.payment_ref in filtered
    filtered = client.get(url, params={'q': f'#{booking.id}'}).text
    assert booking.payment_ref in filtered and other_booking.payment_ref not in filtered
    assert 'No bookings match' in client.get(url, params={'q': unrelated_ref}).text


def test_admin_activity_records_hire_decisions_and_event_cancellation(upcoming_booking):
    db, client, booking = upcoming_booking
    start = booking.event.end_at + timedelta(days=1)
    hires = [HireRequest(user_id=1, venue_id=2, start_at=start, end_at=start+timedelta(hours=1),
                         purpose='Venue request', guest_count=30, status='pending') for _ in range(2)]
    db.add_all(hires)
    db.commit()
    client.cookies.set('session', auth_signer.dumps('1'))
    history_path = '/admin/activity'
    assert client.get(history_path).status_code == 403
    client.cookies.set('session', auth_signer.dumps('3'))
    history = client.get(history_path)
    assert 'No admin actions' in history.text
    csrf = re.search(r'name="csrf" value="([^"]+)"', history.text)[1]
    for hire, decision in zip(hires, ('approve', 'reject')):
        assert client.post(f'/admin/hires/{hire.id}/{decision}', data={'csrf': csrf}).status_code == 200
    manage = f'/admin/events/{booking.event_id}/manage'
    cancelled = client.post(f'/admin/events/{booking.event_id}/cancel',
                            data={'csrf': csrf, 'from_manage': 'true'})
    assert cancelled.url.path == manage and 'full refunds: 1' in cancelled.text
    assert 'Cancel this event' not in cancelled.text
    records = db.scalars(select(AdminActivity).order_by(AdminActivity.id)).all()
    assert [row.action for row in records] == ['Approved venue hire', 'Rejected venue hire', 'Cancelled event']
    assert all(row.admin_id == 3 and row.created_at for row in records)
    assert 'Simulated refunds issued: 1.' in client.get(history_path).text
