from contextvars import ContextVar
import time
from sqlalchemy import event

request_metrics=ContextVar('request_metrics',default=None)

def install(engine):
    @event.listens_for(engine,'before_cursor_execute')
    def before(conn,cursor,statement,parameters,context,executemany):
        context._core_started=time.perf_counter()
    @event.listens_for(engine,'after_cursor_execute')
    def after(conn,cursor,statement,parameters,context,executemany):
        m=request_metrics.get()
        if m is not None:
            m['sql_count']+=1;m['sql_ms']+=(time.perf_counter()-context._core_started)*1000
