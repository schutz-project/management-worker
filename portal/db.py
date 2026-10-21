from contextvars import ContextVar
from time import perf_counter
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

observation: ContextVar[dict | None] = ContextVar('observation', default=None)

def build_engine(url):
    kw = {'pool_pre_ping': True, 'hide_parameters': True}
    if url.startswith('postgresql'):
        kw['connect_args'] = {'connect_timeout': 3, 'options': '-c statement_timeout=3000 -c lock_timeout=2000'}
    elif url.startswith('sqlite'):
        kw['connect_args'] = {'check_same_thread': False}
    engine = create_engine(url, **kw)
    if engine.dialect.name == 'sqlite':
        @event.listens_for(engine, 'connect')
        def foreign_keys(dbapi, _):
            dbapi.execute('PRAGMA foreign_keys=ON')
    @event.listens_for(engine, 'before_cursor_execute')
    def before(conn, cursor, statement, parameters, context, executemany):
        context.portal_started = perf_counter()
    @event.listens_for(engine, 'after_cursor_execute')
    def after(conn, cursor, statement, parameters, context, executemany):
        record(context)
    @event.listens_for(engine, 'handle_error')
    def error(ctx):
        if ctx.execution_context is not None:
            record(ctx.execution_context)
    def record(context):
        state = observation.get()
        start = getattr(context, 'portal_started', None)
        if state is not None and start is not None:
            state['db_duration_ms'] = (state['db_duration_ms'] or 0) + (perf_counter() - start) * 1000
    return engine

def session_factory(engine):
    return sessionmaker(engine, expire_on_commit=False)
