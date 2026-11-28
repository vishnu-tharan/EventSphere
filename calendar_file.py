"""Create an iCalendar attachment from an event stored in UTC."""
from models import Event, now_utc


def escape_calendar_text(value: str) -> str:
    return (value.replace('\\', '\\\\').replace('\r\n', '\n').replace('\r', '\n')
            .replace('\n', '\\n').replace(',', '\\,').replace(';', '\\;'))


def event_calendar(event: Event) -> bytes:
    date_format = '%Y%m%dT%H%M%SZ'
    venue = f'{event.venue.name}, {event.venue.location}'
    lines = [
        'BEGIN:VCALENDAR',
        'VERSION:2.0',
        'PRODID:-//EventSphere//Campus Events//EN',
        'BEGIN:VEVENT',
        f'UID:event-{event.id}@eventsphere-ica02.onrender.com',
        f'DTSTAMP:{now_utc():{date_format}}',
        f'DTSTART:{event.start_at:{date_format}}',
        f'DTEND:{event.end_at:{date_format}}',
        f'SUMMARY:{escape_calendar_text(event.title)}',
        f'LOCATION:{escape_calendar_text(venue)}',
        f'DESCRIPTION:{escape_calendar_text(event.description)}',
        'END:VEVENT',
        'END:VCALENDAR',
    ]
    # RFC 5545 folds lines at 75 octets, without splitting a UTF-8 character.
    output = bytearray()
    for line in lines:
        width = 0
        for char in line:
            encoded = char.encode('utf-8')
            if width + len(encoded) > 75:
                output.extend(b'\r\n ')
                width = 1
            output.extend(encoded)
            width += len(encoded)
        output.extend(b'\r\n')
    return bytes(output)
