"""SQLAlchemy models: persistent state survives application restarts."""
from datetime import datetime, timezone
from sqlalchemy import ForeignKey, Integer, String, Float, Boolean, DateTime, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from database import Base


def now_utc():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(Base):
    __tablename__ = 'users'
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    email: Mapped[str] = mapped_column(String(250), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(20), default='attendee')


class Venue(Base):
    __tablename__ = 'venues'
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    location: Mapped[str] = mapped_column(String(250))
    capacity: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(Text, default='')
    image_url: Mapped[str] = mapped_column(Text, default='')
    hourly_rate: Mapped[float] = mapped_column(Float, default=0.0)
    hire_open: Mapped[bool] = mapped_column(Boolean, default=True)


class Event(Base):
    __tablename__ = 'events'
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(180))
    category: Mapped[str] = mapped_column(String(80))
    venue_id: Mapped[int] = mapped_column(ForeignKey('venues.id'))
    start_at: Mapped[datetime] = mapped_column(DateTime)
    end_at: Mapped[datetime] = mapped_column(DateTime)
    description: Mapped[str] = mapped_column(Text, default='')
    status: Mapped[str] = mapped_column(String(20), default='draft')
    venue: Mapped['Venue'] = relationship()
    ticket_categories: Mapped[list['TicketCategory']] = relationship(back_populates='event', cascade='all, delete-orphan')


class TicketCategory(Base):
    __tablename__ = 'ticket_categories'
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey('events.id'))
    name: Mapped[str] = mapped_column(String(100))
    price: Mapped[float] = mapped_column(Float)
    quantity: Mapped[int] = mapped_column(Integer)
    remaining: Mapped[int] = mapped_column(Integer)
    event: Mapped['Event'] = relationship(back_populates='ticket_categories')


class Booking(Base):
    __tablename__ = 'bookings'
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    event_id: Mapped[int] = mapped_column(ForeignKey('events.id'))
    category_id: Mapped[int] = mapped_column(ForeignKey('ticket_categories.id'))
    quantity: Mapped[int] = mapped_column(Integer)
    total: Mapped[float] = mapped_column(Float)
    payment_ref: Mapped[str] = mapped_column(String(80), unique=True)
    status: Mapped[str] = mapped_column(String(24), default='paid')
    refund_amount: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    user: Mapped['User'] = relationship()
    event: Mapped['Event'] = relationship()
    category: Mapped['TicketCategory'] = relationship()
    tickets: Mapped[list['Ticket']] = relationship(back_populates='booking')


class Ticket(Base):
    __tablename__ = 'tickets'
    id: Mapped[int] = mapped_column(primary_key=True)
    booking_id: Mapped[int] = mapped_column(ForeignKey('bookings.id'))
    code: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(24), default='active')
    checked_in_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    booking: Mapped['Booking'] = relationship(back_populates='tickets')


class HireRequest(Base):
    __tablename__ = 'hire_requests'
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    venue_id: Mapped[int] = mapped_column(ForeignKey('venues.id'))
    start_at: Mapped[datetime] = mapped_column(DateTime)
    end_at: Mapped[datetime] = mapped_column(DateTime)
    purpose: Mapped[str] = mapped_column(String(250))
    guest_count: Mapped[int] = mapped_column(Integer)
    price: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(25), default='pending')
    alternatives_json: Mapped[str] = mapped_column(Text, default='[]')
    user: Mapped['User'] = relationship()
    venue: Mapped['Venue'] = relationship()


class WaitlistEntry(Base):
    __tablename__ = 'waitlist_entries'
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    category_id: Mapped[int] = mapped_column(ForeignKey('ticket_categories.id'))
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(25), default='waiting')
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    category: Mapped['TicketCategory'] = relationship()
    user: Mapped['User'] = relationship()


class Notification(Base):
    __tablename__ = 'notifications'
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


class AgentMessage(Base):
    __tablename__ = 'agent_messages'
    id: Mapped[int] = mapped_column(primary_key=True)
    sender: Mapped[str] = mapped_column(String(80))
    receiver: Mapped[str] = mapped_column(String(80))
    performative: Mapped[str] = mapped_column(String(20))
    content_json: Mapped[str] = mapped_column(Text)
    conversation_id: Mapped[str] = mapped_column(String(80), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


class AgentState(Base):
    __tablename__ = 'agent_states'
    id: Mapped[int] = mapped_column(primary_key=True)
    agent: Mapped[str] = mapped_column(String(80))
    key: Mapped[str] = mapped_column(String(120))
    value_json: Mapped[str] = mapped_column(Text, default='{}')
    __table_args__ = (UniqueConstraint('agent', 'key'),)


class AdminActivity(Base):
    __tablename__ = 'admin_activity'
    id: Mapped[int] = mapped_column(primary_key=True)
    admin_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    action: Mapped[str] = mapped_column(String(80))
    details: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    admin: Mapped['User'] = relationship()
