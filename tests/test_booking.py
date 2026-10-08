from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest

from receptionist.booking import Calendar
from receptionist.config import load_business
from receptionist.tools import ToolSession, instructions

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def calendar(tmp_path, monkeypatch):
    business = load_business(ROOT/'examples/generic.json')
    business.capacity = 1
    c = Calendar(tmp_path/'test.sqlite3', business)
    monkeypatch.setattr(Calendar, 'now', staticmethod(lambda: datetime(2030,1,7,8,tzinfo=timezone.utc)))
    return c


def prepare(c, call='call-a', at='10:00', duration=60, **extra):
    return c.prepare(call, '+12025550123', day='2030-01-07', time=at,
                     service_id='consultation', duration=duration, name='Test Customer', **extra)


def test_proposal_needs_confirmation_and_same_session(calendar):
    p = prepare(calendar)
    assert calendar.availability('2030-01-07','10:00',60)['available']
    with pytest.raises(ValueError):
        calendar.confirm('other',p['confirmation_token'],True)
    with pytest.raises(ValueError):
        calendar.confirm('call-a',p['confirmation_token'],False)
    result=calendar.confirm('call-a',p['confirmation_token'],True)
    assert result['confirmed']
    assert calendar.confirm('call-a',p['confirmation_token'],True)['booking_id']==result['booking_id']
    assert calendar.availability('2030-01-07','10:00',60)['reason']=='already_booked'


def test_simultaneous_confirmations_cannot_double_book(calendar):
    tokens=[prepare(calendar,str(i))['confirmation_token'] for i in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results=list(pool.map(lambda pair:calendar.confirm(str(pair[0]),pair[1],True),enumerate(tokens)))
    assert sum(r['confirmed'] for r in results)==1


def test_adjacent_bookings_are_not_counted_twice(calendar):
    calendar.business.capacity=2
    for index,at in enumerate(['10:00','10:30']):
        p=prepare(calendar,str(index),at,duration=30)
        calendar.confirm(str(index),p['confirmation_token'],True)
    assert calendar.availability('2030-01-07','10:00',60)['available']


def test_start_only_does_not_promise_full_duration(calendar):
    assert calendar.availability('2030-01-07','16:45')['scope']=='start_only'
    assert calendar.availability('2030-01-07','16:45',60)['reason']=='outside_hours'


def test_capacity_override_and_cancel(calendar):
    p=prepare(calendar)
    booking=calendar.confirm('call-a',p['confirmation_token'],True)
    calendar.cancel(booking['booking_id'])
    assert calendar.availability('2030-01-07','10:00',60)['available']
    calendar.set_capacity('2030-01-07',0)
    assert calendar.availability('2030-01-07','10:00',60)['reason']=='insufficient_staff'


def test_callback_persistence_and_deduplication(calendar):
    a=calendar.callback('call-a','+12025550123','Please call me back')
    b=calendar.callback('call-a','+12025550123','Agent please')
    assert a==b
    with calendar.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM callbacks').fetchone()[0]==1
        assert db.execute('SELECT COUNT(*) FROM outbox WHERE sent=0').fetchone()[0]==1


def test_stream_tokens_one_time_and_bound_to_call(calendar):
    token=calendar.issue_stream('call-a','+12025550123')
    assert calendar.consume_stream(token,'call-b') is None
    assert calendar.consume_stream(token,'call-a')['phone']=='+12025550123'
    assert calendar.consume_stream(token,'call-a') is None


def test_closed_past_and_future_window(calendar):
    assert calendar.availability('2030-01-06','10:00')['reason']=='past'
    assert calendar.availability('2030-01-12','10:00')['reason']=='outside_hours'
    assert calendar.availability('2031-01-07','10:00')['reason']=='outside_booking_window'


def test_dst_ambiguity_rejected(calendar):
    with pytest.raises(ValueError,match='does not exist'):
        calendar.local_time('2030-03-10','02:30')
    with pytest.raises(ValueError,match='ambiguous'):
        calendar.local_time('2030-11-03','01:30')


def test_buffer_and_party_capacity(calendar):
    calendar.business.services[0].buffer_minutes=15
    p=prepare(calendar)
    calendar.confirm('call-a',p['confirmation_token'],True)
    assert not calendar.availability('2030-01-07','11:00',30)['available']
    assert calendar.availability('2030-01-07','11:15',30)['available']
    assert not calendar.availability('2030-01-07','13:00',30,party_size=2)['available']


def test_unsupported_service_duration_phone_and_units(calendar):
    with pytest.raises(ValueError): prepare(calendar,duration=75)
    with pytest.raises(ValueError): calendar.availability('2030-01-07','10:00',service_id='invented')
    with pytest.raises(ValueError): calendar.availability('2030-01-07','10:00',party_size=-1)
    with pytest.raises(ValueError): calendar.callback('x','anonymous','Agent')


def test_tool_result_retries_and_unknown_tool(calendar):
    session=ToolSession(calendar,'call-a','+12025550123')
    event={'call_id':'function-a','name':'prepare_booking','arguments':'{"day":"2030-01-07","time":"10:00","service_id":"consultation","duration":60,"name":"Demo"}'}
    assert session.event_result(event)==session.event_result(event)
    assert 'error' in session.execute('delete_everything',{})
    assert 'error' in session.execute('check_availability',{'day':'2030-01-07','time':'10:00','arbitrary':True})


@pytest.mark.parametrize('profile',['generic','salon','pet-grooming','repair','kspa','footprint'])
def test_examples_work_without_a_massage_assumption(tmp_path,profile,calendar):
    business=load_business(ROOT/f'examples/{profile}.json')
    c=Calendar(tmp_path/f'{profile}.db',business)
    service=business.services[0]
    duration=next(iter(service.prices))
    p=c.prepare('demo','+12025550123',day='2030-01-07',time='10:00',service_id=service.id,duration=duration,name='Demo')
    assert c.confirm('demo',p['confirmation_token'],True)['confirmed']
    assert business.name in instructions(c)


def test_invalid_config_is_rejected():
    business=load_business(ROOT/'examples/generic.json').model_dump()
    from receptionist.config import Business
    business['hours']={'0':[['17:00','09:00']]}
    with pytest.raises(ValueError): Business.model_validate(business)
