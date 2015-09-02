import argparse
import asyncio
from contextlib import contextmanager
import signal
from urllib.parse import urljoin, urlsplit, urlencode, parse_qsl, urlunsplit
import traceback

import aiohttp
from lxml import html

from .logger import make_logger
from .exceptions import HttpConnectionError, HttpError
from .utils import args_kwargs_iterator, add_func_to_iterator, get_cache_id
from .storage.dummy import DummyStorage

if not hasattr(asyncio, 'ensure_future'):
    asyncio.ensure_future = asyncio.async


class Scraper(object):
    PROGRESS_INTERVAL = 5
    CONNECTOR_LIMIT = 10
    HTTP_CONCURENCY_LIMIT = 10
    CONNECT_TIMEOUT = 30
    MAX_RETRIES = 3
    MAX_TIMEOUT_COUNT = 3
    TASK_RETRY_COUNT = 3
    QUEUE_SIZE = 0
    CONSUMER_COUNT = 10
    REUSE_SESSION = True
    REUSE_SESSION_COUNT = 50
    STORAGE_ENABLED = True
    DEFAULT_USER_AGENT = 'Scrapa'
    PROXY = None

    def __init__(self, name=None, storage=None, encoding='utf-8', **kwargs):
        self.name = name
        if self.name is None:
            self.name = self.__class__.__name__

        self.encoding = encoding
        self.session = None
        self.storage = None
        self.storage_obj = storage
        self.reuse_session = self.REUSE_SESSION
        self.logger = make_logger(self.name)
        self.queue = asyncio.Queue(self.QUEUE_SIZE)
        self.consumer_count = 0
        self.tasks_running = 0
        self.timeout_count = 0
        self.proxy = kwargs.get('proxy', self.PROXY)
        self.connector_limit = kwargs.get('connector_limit',
                                          self.CONNECTOR_LIMIT)
        self.http_concurrency_limit = kwargs.get('http_concurrency_limit',
                                                 self.HTTP_CONCURENCY_LIMIT)
        self.http_semaphore = asyncio.Semaphore(self.http_concurrency_limit)
        self._session_pool = [None for _ in range(self.http_concurrency_limit)]
        self._session_query_count = 0

    @asyncio.coroutine
    def request(self, method, url, **kwargs):
        url = self.get_full_url(url)
        session_arg = kwargs.pop('session', None)
        response = None
        for retry_num in range(self.MAX_RETRIES):
            with (yield from self.http_semaphore):
                with self.use_request_session(session_arg) as session:
                    full_url = self.get_full_url(url, params=kwargs.get('params', {}))
                    self.logger.info('%s %s DATA: %s',
                        method,
                        full_url,
                        kwargs.get('data', {})
                    )
                    try:
                        response = yield from asyncio.wait_for(
                            session.request(method, url, **kwargs),
                            self.CONNECT_TIMEOUT
                        )
                    except asyncio.TimeoutError as e:
                        error_msg = 'Request timed out'
                        self.timeout_count += 1
                        if self.timeout_count > self.MAX_TIMEOUT_COUNT:
                            self.reset_session(session)
                    except aiohttp.ClientError as e:
                        error_msg = 'Request connection error: {}'.format(e)
                    except aiohttp.ServerDisconnectedError as e:
                        error_msg = 'Server disconnected error: {}'.format(e)
                    else:
                        error_msg = None
                        self.timeout_count = 0
                        break
                if error_msg is not None:
                    self.logger.error(error_msg)
        if response is None:
            raise HttpConnectionError(error_msg)
        self.check_status(response, url)
        return response

    def check_status(self, response, url):
        http_error_msg = ''
        if 400 <= response.status < 500:
            http_error_msg = '%s Client Error for url: %s' % (response.status, url)

        elif 500 <= response.status < 600:
            http_error_msg = '%s Server Error for url: %s' % (response.status, url)

        if http_error_msg:
            response.close()
            raise HttpError(http_error_msg, response=self)

    def get_default_session_kwargs(self):
        return {
            'headers': self.get_default_session_headers()
        }

    def get_default_session_headers(self):
        return {'User-Agent': self.DEFAULT_USER_AGENT}

    def create_session(self, **kwargs):
        connector = self.get_connector()
        request_kwargs = self.get_default_session_kwargs()
        request_kwargs.update(kwargs)
        self.logger.info('Creating new session with %s and %s', connector, request_kwargs)
        return aiohttp.ClientSession(connector=connector, **request_kwargs)

    @contextmanager
    def get_session(self, **kwargs):
        try:
            session = self.create_session(**kwargs)
            yield session
        finally:
            session.close()

    def reset_session(self, session=None):
        if session is None:
            self.session.close()
            self.session = self.create_session()
        else:
            session.close()

    def get_session_from_pool(self):
        pool_size = len(self._session_pool)
        pool_index = self._session_query_count % pool_size
        session = self._session_pool[pool_index]

        if session is not None and getattr(session, '_use_count', 0) > self.REUSE_SESSION_COUNT:
            session.close()

        if session is None or session.closed:
            self._session_pool[pool_index] = self.create_session()
            session = self._session_pool[pool_index]
            session._use_count = 0
        return session

    @contextmanager
    def use_request_session(self, session=None):
        current_session = session or self.get_session_from_pool()
        try:
            yield current_session
        finally:
            self._session_query_count += 1
            if not hasattr(current_session, '_use_count'):
                current_session._use_count = 1
            else:
                current_session._use_count += 1

    def get_connector(self):
        if self.proxy is not None:
            conn = aiohttp.connector.ProxyConnector(
                proxy=self.proxy, conn_timeout=self.CONNECT_TIMEOUT,
                limit=self.connector_limit)
        else:
            conn = aiohttp.TCPConnector(
                verify_ssl=False,
                conn_timeout=self.CONNECT_TIMEOUT,
                limit=self.connector_limit
            )
        return conn

    @asyncio.coroutine
    def get(self, *args, **kwargs):
        response = yield from self.request('GET', *args, **kwargs)
        return response

    @asyncio.coroutine
    def get_text(self, url, *args, **kwargs):
        url = self.get_full_url(url)
        cache = kwargs.pop('cache', False)
        if cache:
            cache_id = get_cache_id(url, *args, **kwargs)
            cached_result = yield from self.get_cached_content(cache_id)
            if cached_result is not None:
                return cached_result

        encoding = kwargs.pop('encoding', self.encoding)

        response = yield from self.get(url, *args, **kwargs)
        try:
            text = yield from response.text(encoding=encoding)
            if cache:
                yield from self.set_cached_content(cache_id, response.url, text)
            return text
        finally:
            yield from response.release()

    @asyncio.coroutine
    def get_dom(self, *args, **kwargs):
        text = yield from self.get_text(*args, **kwargs)
        return html.fromstring(text)

    @asyncio.coroutine
    def consume_queue(self):
        """ Use get_nowait construct until Py 3.4.4 """
        try:
            self.consumer_count += 1
            while True:
                try:
                    coro, args, kwargs, meta = self.queue.get_nowait()
                    self.tasks_running += 1
                    try:
                        yield from self.run_task(coro, *args, **kwargs)
                    except Exception:
                        if self.storage_enabled(coro):
                            if meta is not None and meta.get('tried') < self.TASK_RETRY_COUNT:
                                yield from self.add_to_queue(coro, args, kwargs, meta)
                    finally:
                        self.tasks_running -= 1
                        if hasattr(self.queue, 'task_done'):
                            self.queue.task_done()
                except asyncio.QueueEmpty:
                    if self.finished():
                        return
                    yield from asyncio.sleep(0.5)
        finally:
            self.consumer_count -= 1

    @asyncio.coroutine
    def run_many(self, coro_arg, generator):
        generator = args_kwargs_iterator(generator)
        generator = add_func_to_iterator(coro_arg, generator)
        done, pending = yield from asyncio.wait(list(
            asyncio.ensure_future(self.run_one(coro, *args, **kwargs)) for coro, (args, kwargs) in generator
        ))
        assert len(pending) == 0
        return (d.result() for d in done)

    @asyncio.coroutine
    def run_one(self, coro, *args, **kwargs):
        result = yield from self.run_task(coro, *args, **kwargs)
        return result

    @asyncio.coroutine
    def run_task(self, coro, *args, **kwargs):
        failed = False
        done = False
        value = None
        exception = None
        try:
            result = (yield from coro(*args, **kwargs))
        except Exception as e:
            self.logger.exception(e)
            exception = traceback.format_exc(exception)
            failed = True
            raise e
        else:
            value = result
            done = True
            return result
        finally:
            if self.storage_enabled(coro):
                yield from self.store_task_result(
                    self.name,
                    coro, args, kwargs,
                    done, failed,
                    value, exception
                )

    @asyncio.coroutine
    def schedule_many(self, coro_arg, generator):
        generator = args_kwargs_iterator(generator)
        generator = add_func_to_iterator(coro_arg, generator)
        for coro, (args, kwargs) in generator:
            yield from self.schedule_one(coro, *args, **kwargs)

    @asyncio.coroutine
    def schedule_one(self, coro, *args, **kwargs):
        yield from self.prepare_schedule(coro, args, kwargs)
        yield from self.add_to_queue(coro, args, kwargs)

    @asyncio.coroutine
    def add_to_queue(self, coro, args, kwargs, meta=None):
        yield from self.queue.put((coro, args, kwargs, meta))

    @asyncio.coroutine
    def prepare_schedule(self, coro, args, kwargs):
        if self.storage_enabled(coro):
            yield from self.store_task(coro, args, kwargs)

    @asyncio.coroutine
    def get_storage(self):
        if self.storage is None:
            if self.storage_obj is None:
                self.storage_obj = DummyStorage()
            self.storage = self.storage_obj
            yield from self.storage.create()
        return self.storage

    def storage_enabled(self, coro):
        return getattr(coro, 'scrapa_store', False) and self.STORAGE_ENABLED

    @asyncio.coroutine
    def store_task(self, coro, args, kwargs):
        storage = yield from self.get_storage()
        yield from storage.store_task(self.name, coro, args, kwargs)

    @asyncio.coroutine
    def store_task_result(self, *args, **kwargs):
        storage = yield from self.get_storage()
        yield from storage.store_task_result(*args, **kwargs)

    @asyncio.coroutine
    def store_result(self, result_id, kind, result):
        storage = yield from self.get_storage()
        yield from storage.store_result(self.name, result_id, kind, result)

    @asyncio.coroutine
    def get_cached_content(self, cache_id):
        storage = yield from self.get_storage()
        result = yield from storage.get_cached_content(cache_id)
        return result

    @asyncio.coroutine
    def set_cached_content(self, cache_id, url, content):
        storage = yield from self.get_storage()
        yield from storage.set_cached_content(cache_id, url, content)

    def finished(self):
        qsize = self.queue.qsize()
        return qsize == 0 and self.tasks_running == 0

    @asyncio.coroutine
    def progress(self):
        while True:
            if not self.finished():
                self.logger.info('Tasks queued: %d, running: %d' % (
                                 self.queue.qsize(), self.tasks_running))
            if self.finished() and self.consumer_count == 0:
                break
            yield from asyncio.sleep(self.PROGRESS_INTERVAL)
        self.logger.info('All tasks finished, cleaning up...')
        yield from self.clean_up()

    @asyncio.coroutine
    def clean_up(self):
        for session in self._session_pool:
            if session is not None and not session.closed:
                session.close()

    def get_full_url(self, url, params=None):
        if not url.startswith(('http://', 'https://')):
            return urljoin(self.BASE_URL, url)
        if params is not None:
            url_parts = urlsplit(url)
            url_dict = vars(url_parts)
            qs_list = parse_qsl(url_dict['query'])
            qs_list += list(params.items())
            qs_string = urlencode(qs_list)
            url_dict['query'] = qs_string
            # url_dict is an OrderedDict, that's why this works
            url = urlunsplit(url_dict.values())
        return url

    def start(self):
        raise NotImplementedError

    def run(self, clear=False, clear_cache=False):
        loop = asyncio.get_event_loop()

        self.add_signal_handler(loop)

        consumers = [self.consume_queue() for _ in range(self.CONSUMER_COUNT)]
        try:
            loop.run_until_complete(asyncio.wait([
                self.schedule_one(self.run_start, clear=clear,
                                                  clear_cache=clear_cache),
                self.progress()
            ] + consumers))
        finally:
            if self.session is not None:
                self.session.close()
            loop.close()
            self.logger.info('Done.')

    def run_from_cli(self):
        parser = argparse.ArgumentParser(description='Scrapa arguments.')
        parser.add_argument('--clear', dest='clear', action='store_true',
                           default=False,
                           help='Clear all stored tasks for this scraper')
        parser.add_argument('--clear-cache', dest='clear_cache', action='store_true',
                           default=False,
                           help='Clear the http cache')
        args = parser.parse_args()
        self.run(**vars(args))

    @asyncio.coroutine
    def run_start(self, clear=False, clear_cache=False):
        storage = yield from self.get_storage()
        task_count = yield from storage.get_task_count(self.name)

        if clear_cache:
            yield from storage.clear_cache()

        if clear or task_count == 0:
            if task_count > 0:
                self.logger.info('Deleting %s tasks...', task_count)
                yield from storage.clear_tasks(self.name)
            self.logger.info('Starting scraper from scratch')
            yield from self.start()
        else:
            pending_task_count = yield from storage.get_pending_task_count(self.name)
            if pending_task_count == 0:
                self.logger.info('All %d tasks are complete.', task_count)
                return
            self.logger.info('Resuming scraper with %d/%d pending tasks',
                             pending_task_count, task_count)
            tasks = yield from storage.get_pending_tasks(self.name, self)
            for task_dict in tasks:
                yield from self.add_to_queue(
                    task_dict['coro'],
                    task_dict['args'],
                    task_dict['kwargs'],
                    meta=task_dict['meta']
                )

    def interrupt(self):
        loop = asyncio.get_event_loop()
        self.remove_signal_handler(loop)
        try:
            print('Tasks are still running, do you want to abort? [y, n]')
            yes_no = input()
            if yes_no.lower() == 'n':
                self.add_signal_handler(loop)
                return
        except KeyboardInterrupt:
            pass
        self.stop()

    def stop(self):
        loop = asyncio.get_event_loop()
        loop.stop()

    def add_signal_handler(self, loop):
        for signame in ('SIGINT',):
            loop.add_signal_handler(getattr(signal, signame), self.interrupt)

    def remove_signal_handler(self, loop):
        for signame in ('SIGINT',):
            loop.remove_signal_handler(getattr(signal, signame))
