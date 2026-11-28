"""Five real agents using JSON message passing, persistent DB state, and decision rules.

Agents exchange messages through the message bus.
"""
import json
import math
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from database import lock_row
from models import (
    AgentMessage, AgentState, Booking, Event, HireRequest, Notification,
    Ticket, TicketCategory, User, Venue, WaitlistEntry, now_utc,
)

PERFORMATIVES = {'REQUEST', 'AGREE', 'REFUSE', 'PROPOSE', 'INFORM', 'FAILURE'}
BUFFER_MINUTES = 30
HOLD_MINUTES = 5
MAX_TICKETS = 6
RESERVED_HIRE_STATUSES = ('pending', 'confirmed')


def result(performative: str, **content):
    return {'performative': performative, 'content': content}


def parse_dt(value):
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))


def has_waiting_entries(db: Session, category_id: int) -> bool:
    return db.scalar(select(WaitlistEntry.id).where(
        WaitlistEntry.category_id == category_id, WaitlistEntry.status == 'waiting'
    ).limit(1)) is not None


class BookingInput(BaseModel):
    user_id: int = Field(gt=0)
    category_id: int = Field(gt=0)
    quantity: int = Field(ge=1, le=MAX_TICKETS)
    hold_id: int | None = Field(default=None, gt=0)


class HireInput(BaseModel):
    user_id: int = Field(gt=0)
    venue_id: int = Field(gt=0)
    start_at: datetime
    end_at: datetime
    purpose: str = Field(min_length=3, max_length=250)
    guest_count: int = Field(gt=0)


class MessageBus:
    """Central mailbox. Each send creates a JSON log row and dispatches by receiver."""
    def __init__(self, db: Session):
        self.db = db
        self.handlers = {
            'VenueAgent': VenueAgent(),
            'BookingAgent': BookingAgent(),
            'TicketAgent': TicketAgent(),
            'WaitlistAgent': WaitlistAgent(),
            'NotificationAgent': NotificationAgent(),
        }

    def _record(self, sender, receiver, performative, content, conversation_id):
        message = json.loads(json.dumps({
            'sender': sender, 'receiver': receiver, 'performative': performative,
            'content': content, 'conversation_id': conversation_id,
        }, default=str, ensure_ascii=False))
        self.db.add(AgentMessage(
            sender=sender, receiver=receiver, performative=performative,
            content_json=json.dumps(message['content'], default=str, ensure_ascii=False),
            conversation_id=conversation_id,
        ))
        self.db.flush()
        return message

    def send(self, sender: str, receiver: str, performative: str,
             content: dict[str, Any], conversation_id: str | None = None) -> dict:
        conversation_id = conversation_id or str(uuid.uuid4())
        if performative not in PERFORMATIVES:
            raise ValueError('Invalid performative')
        message = self._record(sender, receiver, performative, content, conversation_id)
        if receiver == 'ALL':
            # Broadcast INFORM is delivered to a subscriber, not by a direct agent call.
            if performative != 'INFORM':
                reply = result('FAILURE', reason='Broadcast requires INFORM')
                responder = 'MessageBus'
            else:
                responder = 'NotificationAgent'
                reply = self.handlers[responder].handle(message, self)
        elif receiver in self.handlers:
            responder = receiver
            reply = self.handlers[receiver].handle(message, self)
        else:
            responder = 'MessageBus'
            reply = result('FAILURE', reason='Unknown agent')
        return self._record(responder, sender, reply['performative'], reply['content'], conversation_id)


def load_state(db: Session, agent: str, key: str, default=None):
    row = db.scalar(select(AgentState).where(AgentState.agent == agent, AgentState.key == key))
    return json.loads(row.value_json) if row else default


def save_state(db: Session, agent: str, key: str, value):
    row = db.scalar(select(AgentState).where(AgentState.agent == agent, AgentState.key == key))
    if row is None:
        row = AgentState(agent=agent, key=key)
        db.add(row)
    row.value_json = json.dumps(value, default=str)
    db.flush()


def venue_busy_periods(venue_id, after, before=None, exclude_event=None):
    """Return the published activities and reserved hires that occupy the venue."""
    events = select(Event.start_at, Event.end_at).where(
        Event.venue_id == venue_id, Event.status == 'published', Event.end_at > after)
    hires = select(HireRequest.start_at, HireRequest.end_at).where(
        HireRequest.venue_id == venue_id, HireRequest.status.in_(RESERVED_HIRE_STATUSES),
        HireRequest.end_at > after)
    if before is not None:
        events = events.where(Event.start_at < before)
        hires = hires.where(HireRequest.start_at < before)
    if exclude_event is not None:
        events = events.where(Event.id != exclude_event)
    return events.union_all(hires)


class VenueAgent:
    """Goal: prevent venue clashes, calculate and decide whole-venue hire."""
    def _clashes(self, db, venue_id, start, end, exclude_event=None):
        buffer = timedelta(minutes=BUFFER_MINUTES)
        occupied = venue_busy_periods(venue_id, start - buffer, end + buffer, exclude_event)
        return db.execute(occupied.limit(1)).first() is not None

    def _alternatives(self, db, venue, start, end, guest_count, *, for_hire=False,
                      exclude_event=None):
        duration = end - start
        buffer = timedelta(minutes=BUFFER_MINUTES)
        eligible = select(Venue).where(Venue.capacity >= guest_count)
        if for_hire:
            eligible = eligible.where(Venue.hire_open.is_(True))
        options = []
        for room in db.scalars(eligible.order_by(Venue.id)):
            occupied = venue_busy_periods(room.id, start - buffer, exclude_event=exclude_event)
            proposed_start = start
            for busy_start, busy_end in db.execute(occupied.order_by('start_at')):
                if proposed_start + duration + buffer <= busy_start:
                    break
                proposed_start = max(proposed_start, busy_end + buffer)
            options.append((proposed_start, room.id != venue.id, room))

        options.sort(key=lambda option: (option[0], option[1], option[2].id))
        return [
            {'venue_id': room.id, 'venue_name': room.name, 'start_at': proposed_start.isoformat(),
             'end_at': (proposed_start + duration).isoformat()}
            for proposed_start, _, room in options[:3]
        ]

    def handle(self, message, bus):
        p = message['content']; action = p.get('action'); db = bus.db; cid = message['conversation_id']
        if action == 'check_event':
            venue = lock_row(db, Venue, int(p['venue_id']))
            if not venue:
                return result('REFUSE', reason='Venue not found')
            start = parse_dt(p['start_at']); end = parse_dt(p['end_at'])
            if end <= start:
                return result('REFUSE', reason='End time must be after start time')
            quantity = p.get('ticket_quantity', venue.capacity)
            if quantity > venue.capacity:
                return result('REFUSE', reason='Ticket quantities exceed venue capacity')
            if self._clashes(db, venue.id, start, end, exclude_event=p.get('event_id')):
                alt = self._alternatives(db, venue, start, end, quantity, exclude_event=p.get('event_id'))
                if not alt:
                    return result('REFUSE', reason='Venue has a conflict and no suitable free alternative')
                return result('PROPOSE', reason='Venue has a conflicting activity or 30-minute buffer', alternatives=alt)
            return result('AGREE', reason='Venue available including 30-minute turnaround buffer')
        if action == 'cancel_hire':
            hire = lock_row(db, HireRequest, int(p['hire_id']))
            if hire is None or hire.user_id != int(p['user_id']):
                return result('REFUSE', reason='Venue hire not found')
            if hire.status not in RESERVED_HIRE_STATUSES:
                return result('REFUSE', reason='Only pending or confirmed hires can be cancelled')
            if hire.start_at <= now_utc():
                return result('REFUSE', reason='Venue hires can only be cancelled before they start')
            hire.status = 'cancelled'
            bus.send('VenueAgent', 'ALL', 'INFORM', {
                'user_id': hire.user_id,
                'text': f'Venue hire #{hire.id} for {hire.venue.name} was cancelled. The time slot has been released.',
            }, cid)
            return result('AGREE', reason='Venue hire cancelled. The time slot has been released.')
        if action in ['request_hire', 'accept_alternative']:
            existing = lock_row(db, HireRequest, int(p['hire_id'])) if action == 'accept_alternative' else None
            if action == 'accept_alternative' and (not existing or existing.status != 'proposed' or existing.user_id != int(p.get('user_id', 0))):
                return result('REFUSE', reason='No valid proposed hire belongs to this user')
            try:
                candidate = HireInput.model_validate({
                    'user_id': p.get('user_id', existing.user_id if existing else None),
                    'venue_id': p['venue_id'], 'start_at': p['start_at'], 'end_at': p['end_at'],
                    'purpose': p.get('purpose', existing.purpose if existing else ''),
                    'guest_count': p.get('guest_count', existing.guest_count if existing else 0),
                })
            except ValidationError as exc:
                return result('REFUSE', reason='Invalid hire details: ' + str(exc.errors()[0]['msg']))
            venue = lock_row(db, Venue, candidate.venue_id)
            if not venue:
                return result('REFUSE', reason='Venue not found')
            instant = now_utc()
            if candidate.end_at <= candidate.start_at:
                return result('REFUSE', reason='End time must be after start time')
            if existing is None and candidate.start_at <= instant:
                return result('REFUSE', reason='Choose a future start time')
            reason = None
            if candidate.start_at <= instant:
                reason = 'The proposed start time has passed'
            elif not venue.hire_open:
                reason = 'Venue not available for private hire'
            elif candidate.guest_count > venue.capacity:
                reason = 'Guest count exceeds this venue capacity'
            elif self._clashes(db, venue.id, candidate.start_at, candidate.end_at):
                reason = 'Venue is busy at that time (includes 30-minute buffer)'
            if reason:
                search_start = max(candidate.start_at, instant + timedelta(minutes=BUFFER_MINUTES))
                search_end = search_start + (candidate.end_at - candidate.start_at)
                options = self._alternatives(db, venue, search_start, search_end,
                                             candidate.guest_count, for_hire=True)
                if not options:
                    return result('REFUSE', reason=reason + '; no suitable venue is available')

            if existing is None:
                existing = HireRequest(user_id=candidate.user_id, venue_id=venue.id,
                    start_at=candidate.start_at, end_at=candidate.end_at,
                    purpose=candidate.purpose, guest_count=candidate.guest_count)
                db.add(existing)
                db.flush()
            if reason:
                existing.status = 'proposed'
                existing.alternatives_json = json.dumps(options)
                bus.send('VenueAgent', 'ALL', 'INFORM', {
                    'user_id': existing.user_id, 'text': f'Private hire requested: alternatives available for {venue.name}.'}, cid)
                return result('PROPOSE', reason=reason,
                              hire_id=existing.id, alternatives=options)
            hours = (candidate.end_at - candidate.start_at).total_seconds() / 3600
            # Optional advanced rule: weekend +20% using local Sri Lanka date.
            local_start = candidate.start_at.replace(tzinfo=timezone.utc).astimezone(ZoneInfo('Asia/Colombo'))
            multiplier = 1.2 if local_start.weekday() >= 5 else 1.0
            price = round(hours * venue.hourly_rate * multiplier, 2)
            existing.venue_id = venue.id
            existing.start_at = candidate.start_at
            existing.end_at = candidate.end_at
            existing.price = price
            existing.alternatives_json = '[]'
            existing.status = 'pending' if candidate.guest_count > math.floor(venue.capacity * 0.7) else 'confirmed'
            perf = 'AGREE'  # AGREE acknowledges escalation, not final confirmation
            bus.send('VenueAgent', 'ALL', 'INFORM', {
                'user_id': existing.user_id, 'text': f'Hire #{existing.id}: {existing.status}. Price: LKR {price:,.2f}'}, cid)
            if existing.status == 'pending':
                return result(perf, reason='Large gathering escalated to admin for final approval',
                              hire_id=existing.id, price=price, status='pending')
            return result(perf, hire_id=existing.id, price=price, status='confirmed')
        return result('FAILURE', reason='Unknown VenueAgent action')


class BookingAgent:
    """Goal: sell fairly, never oversell, apply a documented refund policy."""
    def handle(self, message, bus):
        p=message['content']; action=p.get('action'); db=bus.db; cid=message['conversation_id']
        if action == 'book':
            try:
                data = BookingInput.model_validate(p)
            except ValidationError as exc:
                return result('REFUSE', reason='1 to 6 tickets per booking; valid user/category required')
            user = db.get(User, data.user_id)
            cat = db.get(TicketCategory, data.category_id)
            if not user or not cat:
                return result('REFUSE', reason='User or ticket category not found')
            event = lock_row(db, Event, cat.event_id)
            if event.status != 'published' or event.start_at <= now_utc():
                return result('REFUSE', reason='Event is not published or booking has closed')
            if user.role != 'attendee':
                return result('REFUSE', reason='Only attendees can book tickets')
            # Offered waitlist stock was reserved in advance; do not decrement twice.
            offer_query = select(WaitlistEntry).where(
                WaitlistEntry.user_id == data.user_id,
                WaitlistEntry.category_id == data.category_id,
                WaitlistEntry.quantity == data.quantity,
                WaitlistEntry.status == 'offered',
                WaitlistEntry.expires_at > now_utc(),
            )
            if data.hold_id is not None:
                offer_query = offer_query.where(WaitlistEntry.id == data.hold_id)
            offered = db.scalar(offer_query.order_by(WaitlistEntry.created_at, WaitlistEntry.id)
                                .limit(1).execution_options(populate_existing=True))
            if offered is None:
                if data.hold_id is not None:
                    return result('REFUSE', reason='This waitlist hold is no longer available')
                if has_waiting_entries(db, data.category_id):
                    return result('REFUSE', reason='The waitlist has priority for this ticket category')
                # Single atomic UPDATE is safe under concurrent PostgreSQL transactions.
                # SQLite serialises writers; rowcount guarantees only one wins the last seat.
                changed = db.execute(update(TicketCategory).where(
                    TicketCategory.id == data.category_id,
                    TicketCategory.remaining >= data.quantity,
                ).values(remaining=TicketCategory.remaining - data.quantity))
                if changed.rowcount != 1:
                    return result('REFUSE', reason='Not enough tickets; please join the waitlist',
                                  remaining=max(0, cat.remaining))
            else:
                offered.status = 'claimed'
            price = round(cat.price * data.quantity, 2)
            booking = Booking(user_id=data.user_id, event_id=event.id, category_id=cat.id,
                quantity=data.quantity, total=price, payment_ref='SIM-' + uuid.uuid4().hex[:18].upper(), status='paid')
            db.add(booking); db.flush()
            ticket_answer = bus.send('BookingAgent', 'TicketAgent', 'REQUEST',
                {'action':'issue', 'booking_id': booking.id, 'quantity': data.quantity}, cid)
            if ticket_answer['performative'] != 'AGREE':
                raise RuntimeError('Ticket issuance failed; transaction must be rolled back')
            bus.send('BookingAgent', 'ALL', 'INFORM', {
                'user_id': data.user_id,
                'text': f'Booking #{booking.id} confirmed. Payment {booking.payment_ref}; {data.quantity} ticket(s).'}, cid)
            return result('AGREE', booking_id=booking.id, payment_ref=booking.payment_ref, total=price)
        if action == 'cancel':
            booking = db.get(Booking, int(p['booking_id']))
            if not booking or booking.user_id != int(p['user_id']):
                return result('REFUSE', reason='Booking not found for this account')
            event = lock_row(db, Event, booking.event_id)
            db.refresh(booking)
            if booking.status != 'paid':
                return result('REFUSE', reason='Booking already cancelled')
            if event.start_at <= now_utc() and event.status != 'cancelled':
                return result('REFUSE', reason='Cannot cancel after event starts')
            checked_in_ticket = db.scalar(select(Ticket.id).where(
                Ticket.booking_id == booking.id, Ticket.status == 'checked_in').limit(1))
            if checked_in_ticket is not None:
                return result('REFUSE', reason='Cannot cancel this booking after a ticket has been checked in')
            hours_left = (event.start_at - now_utc()).total_seconds() / 3600
            fraction = 1.0 if event.status == 'cancelled' or hours_left >= 48 else (0.5 if hours_left >= 24 else 0.0)
            refund = round(booking.total * fraction, 2)
            booking.status = 'cancelled'; booking.refund_amount = refund
            db.execute(update(TicketCategory).where(TicketCategory.id == booking.category_id)
                       .values(remaining=TicketCategory.remaining + booking.quantity))
            bus.send('BookingAgent', 'TicketAgent', 'REQUEST', {'action':'cancel', 'booking_id':booking.id}, cid)
            bus.send('BookingAgent', 'ALL', 'INFORM', {
                'user_id':booking.user_id, 'text':f'Booking #{booking.id} cancelled. Simulated refund LKR {refund:,.2f}.'}, cid)
            if event.status == 'published':
                bus.send('BookingAgent', 'WaitlistAgent', 'INFORM',
                    {'action':'seats_freed', 'category_id':booking.category_id}, cid)
            return result('AGREE', refund_amount=refund, reason='Refund: 48h+=100%, 24-48h=50%, less=0%; cancelled event=100%')
        if action == 'event_cancelled':
            event = lock_row(db, Event, int(p['event_id']))
            if not event or event.status != 'cancelled':
                return result('REFUSE', reason='Event must already be cancelled by admin')
            count=0
            for booking in db.scalars(select(Booking).where(Booking.event_id==event.id, Booking.status=='paid')).all():
                booking.status='cancelled'; booking.refund_amount=booking.total
                db.execute(update(TicketCategory).where(TicketCategory.id == booking.category_id)
                           .values(remaining=TicketCategory.remaining + booking.quantity))
                bus.send('BookingAgent', 'TicketAgent', 'REQUEST',
                         {'action':'cancel', 'booking_id':booking.id}, cid)
                bus.send('BookingAgent', 'ALL', 'INFORM', {'user_id':booking.user_id,
                    'text':f'Event {event.title} cancelled. Full simulated refund LKR {booking.total:,.2f}.'}, cid)
                count+=1
            bus.send('BookingAgent', 'WaitlistAgent', 'INFORM',
                     {'action': 'event_cancelled', 'event_id': event.id}, cid)
            return result('AGREE', refunded_bookings=count)
        return result('FAILURE', reason='Unknown BookingAgent action')


class TicketAgent:
    """Goal: issue unguessable unique codes, admit each ticket at most once."""
    def handle(self, message, bus):
        p=message['content']; action=p.get('action'); db=bus.db
        if action == 'issue':
            booking=db.get(Booking,int(p['booking_id']))
            if not booking:
                return result('REFUSE', reason='Booking not found')
            codes=[]
            for _ in range(int(p['quantity'])):
                code=secrets.token_urlsafe(32)
                db.add(Ticket(booking_id=booking.id,code=code,status='active'))
                codes.append(code)
            db.flush()
            return result('AGREE', issued=len(codes))
        if action == 'cancel':
            booking_id=int(p['booking_id'])
            changed=db.execute(update(Ticket).where(Ticket.booking_id==booking_id,
                Ticket.status!='cancelled').values(status='cancelled'))
            return result('AGREE', cancelled=changed.rowcount)
        if action == 'checkin':
            code=str(p.get('code','')).strip()
            ticket=db.scalar(select(Ticket).where(Ticket.code==code))
            if not ticket:
                return result('REFUSE', reason='Invalid ticket code')
            booking=db.get(Booking,ticket.booking_id)
            event=lock_row(db, Event, booking.event_id)
            db.refresh(booking)
            db.refresh(ticket)
            scan_count=load_state(db, 'TicketAgent', f'rejected_{ticket.id}', {'count':0})
            def refuse(reason):
                scan_count['count']+=1
                save_state(db, 'TicketAgent', f'rejected_{ticket.id}', scan_count)
                return result('REFUSE', reason=reason)
            if int(p.get('event_id',0)) != event.id:
                return refuse('Wrong event')
            if event.status == 'cancelled' or booking.status=='cancelled' or ticket.status=='cancelled':
                return refuse('Cancelled ticket or event')
            if ticket.status=='checked_in':
                return refuse('Already used: second scan rejected')
            instant=now_utc()
            if instant < event.start_at - timedelta(hours=2) or instant > event.end_at:
                return refuse('Outside check-in time (2 hours before start to event end)')
            changed=db.execute(update(Ticket).where(Ticket.id==ticket.id, Ticket.status=='active')
                               .values(status='checked_in',checked_in_at=instant))
            if changed.rowcount!=1:
                return refuse('Already used by another scan')
            return result('AGREE', reason='Admit: valid ticket', ticket_id=ticket.id, event_id=event.id)
        return result('FAILURE', reason='Unknown TicketAgent action')


class WaitlistAgent:
    """Goal: queue fairly, reserve released seats and expire unclaimed holds on timer."""
    def _offer(self, bus, category_id, cid):
        db=bus.db; offers=0
        category = db.get(TicketCategory, category_id)
        if category is None:
            return offers
        event = lock_row(db, Event, category.event_id)
        if event.status != 'published' or event.start_at <= now_utc():
            return offers
        for entry in db.scalars(select(WaitlistEntry).where(
                WaitlistEntry.category_id==category_id,WaitlistEntry.status=='waiting')
                .order_by(WaitlistEntry.created_at, WaitlistEntry.id)).all():
            updated=db.execute(update(TicketCategory).where(
                TicketCategory.id==category_id,TicketCategory.remaining>=entry.quantity)
                .values(remaining=TicketCategory.remaining-entry.quantity))
            if updated.rowcount!=1:
                break
            entry.status='offered'; entry.expires_at=now_utc()+timedelta(minutes=HOLD_MINUTES)
            bus.send('WaitlistAgent', 'ALL','INFORM',{
                'user_id':entry.user_id,
                'text':f'Waitlist offer #{entry.id}: {entry.quantity} seat(s) held for 5 minutes. Book the exact quantity now.'},cid)
            offers+=1
        return offers

    def handle(self,message,bus):
        db=bus.db; p=message['content']; action=p.get('action'); cid=message['conversation_id']
        if action=='join':
            user_id=int(p['user_id']); category_id=int(p['category_id']); qty=int(p.get('quantity',1))
            cat=db.get(TicketCategory,category_id)
            user = db.get(User, user_id)
            if not user or user.role != 'attendee' or not cat or qty<1 or qty>MAX_TICKETS:
                return result('REFUSE',reason='Invalid waitlist request')
            event = lock_row(db, Event, cat.event_id)
            db.refresh(cat)
            if event.status != 'published' or event.start_at <= now_utc():
                return result('REFUSE', reason='Event is not published or booking has closed')
            if qty > cat.quantity:
                return result('REFUSE', reason='Requested quantity exceeds the ticket category capacity')
            if db.scalar(select(WaitlistEntry).where(
                    WaitlistEntry.user_id==user_id,WaitlistEntry.category_id==category_id,
                    WaitlistEntry.status.in_(['waiting','offered'])).limit(1)):
                return result('REFUSE',reason='You are already in this waitlist')
            if cat.remaining >= qty and not has_waiting_entries(db, category_id):
                return result('REFUSE', reason='Tickets are currently available; please book directly')
            entry=WaitlistEntry(user_id=user_id, category_id=category_id,quantity=qty,status='waiting')
            db.add(entry); db.flush()
            self._offer(bus,category_id,cid)
            return result('AGREE',reason='Added to first-come-first-served waitlist',waitlist_id=entry.id)
        if action == 'leave':
            entry = db.get(WaitlistEntry, int(p['entry_id']))
            user_id = int(p['user_id'])
            if entry is None or entry.user_id != user_id:
                return result('REFUSE', reason='Waitlist entry not found')
            category = db.get(TicketCategory, entry.category_id)
            event = lock_row(db, Event, category.event_id)
            db.refresh(entry)
            if event.status != 'published' or entry.status not in ('waiting', 'offered'):
                return result('REFUSE', reason='This waitlist entry is no longer active')
            release_hold = entry.status == 'offered'
            entry.status = 'withdrawn'
            entry.expires_at = None
            if release_hold:
                db.execute(update(TicketCategory).where(TicketCategory.id == category.id)
                           .values(remaining=TicketCategory.remaining + entry.quantity))
            self._offer(bus, category.id, cid)
            return result('AGREE', reason='You left the waitlist. Your ticket hold was released.' if release_hold
                          else 'You left the waitlist.')
        if action=='seats_freed':
            n=self._offer(bus,int(p['category_id']),cid)
            return result('INFORM',reason='Released seats offered to first waiting customers',offers=n)
        if action == 'event_cancelled':
            event = lock_row(db, Event, int(p['event_id']))
            if event is None or event.status != 'cancelled':
                return result('REFUSE', reason='Event must already be cancelled')
            entries = db.scalars(select(WaitlistEntry).join(TicketCategory).where(
                TicketCategory.event_id == event.id,
                WaitlistEntry.status.in_(['waiting', 'offered']))).all()
            for entry in entries:
                if entry.status == 'offered':
                    db.execute(update(TicketCategory).where(TicketCategory.id == entry.category_id)
                               .values(remaining=TicketCategory.remaining + entry.quantity))
                entry.status = 'expired'
                bus.send('WaitlistAgent', 'ALL', 'INFORM', {
                    'user_id': entry.user_id, 'text': f'Event {event.title} cancelled. Your waitlist entry has closed.'}, cid)
            return result('INFORM', expired=len(entries))
        if action=='sweep':
            # Periodic proactive behaviour: no attendee or admin request is needed.
            instant = now_utc()
            expired_offers = select(WaitlistEntry).join(TicketCategory).where(
                WaitlistEntry.status == 'offered', WaitlistEntry.expires_at <= instant)
            event_ids = db.scalars(expired_offers.with_only_columns(TicketCategory.event_id)
                                   .distinct().order_by(TicketCategory.event_id)).all()
            expired_count = 0
            count = 0
            for event_id in event_ids:
                lock_row(db, Event, event_id)
                expired = db.scalars(expired_offers.where(TicketCategory.event_id == event_id)
                                     .execution_options(populate_existing=True)).all()
                for entry in expired:
                    entry.status = 'expired'
                    db.execute(update(TicketCategory).where(TicketCategory.id == entry.category_id)
                               .values(remaining=TicketCategory.remaining + entry.quantity))
                    bus.send('WaitlistAgent', 'ALL', 'INFORM', {
                        'user_id': entry.user_id, 'text': f'Waitlist offer #{entry.id} expired.'}, cid)
                expired_count += len(expired)
                for category_id in sorted({entry.category_id for entry in expired}):
                    count += self._offer(bus, category_id, cid)
            state=load_state(db,'WaitlistAgent','sweeper',{'runs':0})
            state['runs']=state.get('runs',0)+1
            state['last_run']=now_utc().isoformat()
            save_state(db,'WaitlistAgent','sweeper',state)
            return result('INFORM',expired=expired_count,reoffered=count,sweep_runs=state['runs'])
        return result('FAILURE',reason='Unknown WaitlistAgent action')


class NotificationAgent:
    """Reactive subscriber to broadcast INFORM messages from all agents."""
    def handle(self,message,bus):
        if message['performative']!='INFORM':
            return result('REFUSE',reason='Only INFORM messages accepted')
        p=message['content']
        user_id=p.get('user_id')
        if user_id and bus.db.get(User,int(user_id)):
            bus.db.add(Notification(user_id=int(user_id),message=str(p.get('text','Agent update'))))
            bus.db.flush()
            return result('INFORM',delivered=1)
        return result('INFORM',delivered=0,reason='No target user')
