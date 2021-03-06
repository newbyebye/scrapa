from datetime import datetime

from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy import (Index, Column, Integer, String, Text, Boolean, DateTime,
                        LargeBinary)
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from .base import BaseStorage, GeneratorWrapper as GW
from ..utils import json_loads, json_dumps

Base = declarative_base()


class Task(Base):
    __tablename__ = 'scrapa_task'

    id = Column(Integer, primary_key=True)
    scraper_name = Column(String)
    task_id = Column(String)
    name = Column(String)
    args = Column(Text)
    kwargs = Column(Text)
    created = Column(DateTime)
    last_tried = Column(DateTime, nullable=True)
    tried = Column(Integer, default=0)
    done = Column(Boolean, default=False)
    failed = Column(Boolean, default=False)
    value = Column(Text, nullable=True)
    exception = Column(Text, nullable=True)

    def __repr__(self):
        return "<Task(scraper_name='%s', taskid='%s', name='%s')>" % (
                            self.scraper_name, self.task_id, self.name)

Index('scraper_name_task_id', Task.scraper_name, Task.task_id, unique=True)


class Result(Base):
    __tablename__ = 'scrapa_result'

    id = Column(Integer, primary_key=True)
    scraper_name = Column(String)
    result_id = Column(String)
    kind = Column(String)
    result = Column(String)

    def __repr__(self):
        return "<Result(scraper_name='%s', result_id='%s', kind='%s')>" % (
                            self.scraper_name, self.result_id, self.kind)

Index('scraper_name_result_id_kind', Result.scraper_name,
                                     Result.result_id, Result.kind, unique=True)


class Cache(Base):
    __tablename__ = 'scrapa_cache'

    id = Column(Integer, primary_key=True)
    cache_id = Column(String, index=True, unique=True)
    url = Column(String)
    created = Column(DateTime)
    content = Column(LargeBinary)

    def __repr__(self):
        return "<Cache(cache_id='%s', result_id='%s', kind='%s')>" % (
                            self.cache_id)


class DatabaseStorage(BaseStorage):
    def __init__(self, db_url='sqlite:///:memory:'):
        self.db_url = db_url

    async def create(self):
        self.engine = create_engine(self.db_url, echo=False)
        Base.metadata.create_all(self.engine)
        Session = sessionmaker(bind=self.engine)
        self.session = Session()

    async def store_task(self, scraper_name, coro, args, kwargs):
        task_id = self.get_task_id(coro, args, kwargs)
        task_obj = self.session.query(Task
                ).filter_by(scraper_name=scraper_name, task_id=task_id).first()
        if not task_obj:
            task = Task(
                scraper_name=scraper_name,
                task_id=task_id,
                name=coro.__name__,
                args=json_dumps(args),
                kwargs=json_dumps(kwargs),
                created=datetime.now(),
                tried=0,
                done=False,
                failed=False,
                value=None,
                exception=None
            )
            self.session.add(task)
            self.session.commit()
            return True
        return False

    async def clear_tasks(self, scraper_name):
        self.session.query(Task).filter_by(scraper_name=scraper_name).delete()

    async def get_task_count(self, scraper_name):
        return self.session.query(Task).filter_by(
                scraper_name=scraper_name).count()

    async def get_pending_task_count(self, scraper_name):
        return self.session.query(Task).filter_by(
                scraper_name=scraper_name, done=False).count()

    async def get_pending_tasks(self, scraper_name):
        result = self.session.query(Task).filter_by(scraper_name=scraper_name, done=False)
        return GW({
            'task_name': task.name,
            'args': json_loads(task.args),
            'kwargs': json_loads(task.kwargs),
            'meta': {'tried': task.tried}
        } for task in result)

    async def store_task_result(self, scraper_name, coro, args, kwargs, done, failed,
                          value, exception):
        task_id = self.get_task_id(coro, args, kwargs)
        (self.session.query(Task)
                    .filter_by(scraper_name=scraper_name, task_id=task_id)
                    .update({'done': done, 'failed': failed,
                            'last_tried': datetime.now(), 'value': json_dumps(value),
                            'exception': exception, 'tried': Task.tried + 1}))
        self.session.commit()

    async def has_result(self, scraper_name, result_id, kind):
        result_obj = self._get_result(scraper_name, result_id, kind)
        return bool(result_obj)

    async def get_result(self, scraper_name, result_id, kind):
        result_obj = self._get_result(scraper_name, result_id, kind)
        if result_obj is None:
            return None
        return json_loads(result_obj.result)

    def _get_result(self, scraper_name, result_id, kind):
        params = dict(scraper_name=scraper_name, kind=kind, result_id=result_id)
        result_obj = self.session.query(Result).filter_by(**params).first()
        return result_obj

    async def store_result(self, scraper_name, result_id, kind, result):
        params = dict(scraper_name=scraper_name, kind=kind, result_id=result_id)
        result_obj = self.session.query(Result).filter_by(**params).first()
        has_result = True
        if result_obj:
            result_value = json_loads(result_obj.result)
            if isinstance(result_value, dict):
                result_value.update(result)
            else:
                result_value = result
            self.session.query(Result).filter_by(id=result_obj.id).update({'result': json_dumps(result_value)})
            has_result = False
        else:
            params.update({'result': json_dumps(result)})
            self.session.add(Result(**params))
        self.session.commit()
        return has_result

    async def get_cached_content(self, cache_id):
        cached = self.session.query(Cache).filter_by(cache_id=cache_id).first()
        if cached:
            return cached.content
        return None

    async def set_cached_content(self, cache_id, url, content):
        self.session.add(Cache(cache_id=cache_id, url=url,
                               content=content,
                               created=datetime.now()))
        self.session.commit()

    async def clear_cache(self):
        self.session.query(Cache).delete()
